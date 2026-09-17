"""
Reglas de negocio de la App de Proveedores de Transformación (AKT MOTOS).

Flujo: RECIBO -> ALMACENAMIENTO -> PRODUCCIÓN -> DESPACHO
Estados de inventario: CRUDO -> (transformación) -> PROCESADO
Condición:             DISPONIBLE | RESTRINGIDO
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import dataclass

from sqlalchemy import func, select, text

from core.models import (
    Archivo, Articulo, Averia, Bom, ConsumoProduccion, ConteoEjecutado,
    ConteoProgramado, Despacho, DespachoLinea, Documento, FotoInventarioERP,
    Inventario, MovimientoInventario, Novedad, OrdenCompra, OrdenProduccion,
    ProgramaProduccion, Proveedor, Recibo, ReciboLinea, Auditoria, Ubicacion,
    DocumentoArchivo,
)

TOL = 1e-6


class ReglaNegocio(Exception):
    """Error de validación de negocio (se muestra al usuario)."""


def _actor_rol(actor):
    return (actor or {}).get("rol") if isinstance(actor, dict) else None


def _exigir_rol(actor, *roles):
    """Defensa en profundidad; actor=None conserva compatibilidad de pruebas."""
    if actor is not None and _actor_rol(actor) not in roles:
        raise ReglaNegocio("Su rol no está autorizado para ejecutar esta operación.")


def _exigir_proveedor(actor, proveedor_id):
    if actor is not None and _actor_rol(actor) == "PROVEEDOR":
        if int((actor or {}).get("proveedor_id") or 0) != int(proveedor_id or 0):
            raise ReglaNegocio("No puede operar inventario de otro proveedor.")


def _validar_ubicacion_movimiento(s, proveedor_id, ubicacion, condicion, cantidad):
    codigo = (ubicacion or "").strip().upper()
    if not codigo:
        return
    u = s.query(Ubicacion).filter(Ubicacion.codigo == codigo).first()
    if not u:
        raise ReglaNegocio(f"La ubicación {codigo} no existe en el maestro.")
    if not u.activo or u.cerrada:
        raise ReglaNegocio(f"La ubicación {codigo} está inactiva o cerrada.")
    if u.proveedor_id and int(u.proveedor_id) != int(proveedor_id):
        raise ReglaNegocio(f"La ubicación {codigo} pertenece a otro proveedor.")
    if float(cantidad or 0) > 0 and condicion == "DISPONIBLE" and (u.restringida or u.inspeccion):
        raise ReglaNegocio(
            f"La ubicación {codigo} es restringida/de inspección y no admite producto DISPONIBLE.")


# =========================================================================
# TRAZABILIDAD
# =========================================================================

PREFIJOS = {
    "BIN_A_BIN": "BIN", "FACTURA": "FAC", "REGISTRO": "REG",
    "NOVEDAD": "NOV", "AVERIA": "AVR", "CONTEO": "CNT",
    "MPS": "MPS", "PRODUCCION": "OPR", "DESPACHO": "DSP", "AJUSTE": "AJU",
}


def nuevo_trz(s, tipo: str) -> str:
    """Consecutivo atómico por tipo/año, seguro ante usuarios concurrentes."""
    anio = dt.date.today().year
    pre = PREFIJOS.get(tipo, "DOC")
    clave = f"{pre}:{anio}"
    sql = text("""
        INSERT INTO consecutivos_documento (clave, valor)
        VALUES (:clave, 1)
        ON CONFLICT(clave) DO UPDATE
        SET valor = consecutivos_documento.valor + 1
        RETURNING valor
    """)
    for _ in range(100000):
        n = int(s.execute(sql, {"clave": clave}).scalar_one())
        cand = f"TRZ-{pre}-{anio}-{n:06d}"
        if not s.query(Documento.id).filter(Documento.trz == cand).first():
            return cand
    raise ReglaNegocio("No fue posible generar un consecutivo de trazabilidad único.")

def crear_documento(s, tipo, proveedor_id=None, referencia=None, archivo_id=None,
                    creado_por=None, fecha_documento=None, observaciones=None,
                    extraccion=None) -> Documento:
    extraccion_json = None
    confianza = None
    estado_extraccion = None
    if extraccion:
        extraccion_json = json.dumps(extraccion, ensure_ascii=False, default=str)
        confianza = float(extraccion.get("confianza_global") or 0)
        estado_extraccion = "REVISION" if extraccion.get("requiere_revision", True) else "EXTRAIDO"
    doc = Documento(
        trz=nuevo_trz(s, tipo), tipo=tipo, proveedor_id=proveedor_id,
        referencia=referencia, archivo_id=archivo_id, creado_por=creado_por,
        fecha_documento=fecha_documento or dt.date.today(),
        observaciones=observaciones, extraccion_json=extraccion_json,
        confianza_extraccion=confianza, estado_extraccion=estado_extraccion,
    )
    s.add(doc)
    s.flush()
    if archivo_id:
        s.add(DocumentoArchivo(documento_id=doc.id, archivo_id=archivo_id, tipo="SOPORTE"))
        s.flush()
    return doc


def guardar_archivo(s, nombre, contenido: bytes, mime=None, usuario=None) -> Archivo:
    """Guarda evidencia en Storage si está configurado; si no, usa fallback binario."""
    contenido = contenido or b""
    digest = hashlib.sha256(contenido).hexdigest()
    existente = s.query(Archivo).filter(Archivo.sha256 == digest).first()
    if existente:
        return existente

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(nombre or "archivo"))[:180]
    path = f"{dt.date.today():%Y/%m}/{digest[:20]}-{safe}"
    storage_path = None
    db_content = contenido
    try:
        from core import storage
        if storage.configured():
            storage_path = storage.upload_bytes(path, contenido, mime)
            db_content = None
    except Exception:
        storage_path = None
        db_content = contenido

    a = Archivo(nombre=nombre, contenido=db_content, mime=mime,
                tamano=len(contenido), storage_path=storage_path, sha256=digest,
                subido_por=usuario)
    s.add(a)
    s.flush()
    return a


def leer_archivo(archivo: Archivo) -> bytes:
    if archivo is None:
        return b""
    if archivo.contenido is not None:
        return bytes(archivo.contenido)
    if archivo.storage_path:
        from core import storage
        return storage.download_bytes(archivo.storage_path)
    return b""

def auditar(s, usuario, rol, accion, entidad, entidad_id, detalle=""):
    s.add(Auditoria(usuario=usuario, rol=rol, accion=accion, entidad=entidad,
                    entidad_id=str(entidad_id), detalle=detalle))


# =========================================================================
# INVENTARIO (núcleo)
# =========================================================================

def _clave(proveedor_id, articulo, ubicacion, estado, condicion):
    return (proveedor_id, str(articulo).strip(), (ubicacion or "").strip().upper(),
            estado, condicion)


def obtener_saldo(s, proveedor_id, articulo, ubicacion="", estado="CRUDO",
                  condicion="DISPONIBLE") -> float:
    p, a, u, e, c = _clave(proveedor_id, articulo, ubicacion, estado, condicion)
    _validar_ubicacion_movimiento(s, p, u, c, cantidad)
    row = s.query(Inventario).filter_by(
        proveedor_id=p, articulo=a, ubicacion=u, estado=e, condicion=c).first()
    return float(row.cantidad) if row else 0.0


def saldo_articulo(s, proveedor_id, articulo, estado="CRUDO", condicion="DISPONIBLE") -> float:
    """Saldo consolidado del artículo (todas las ubicaciones)."""
    q = s.query(func.coalesce(func.sum(Inventario.cantidad), 0.0)).filter(
        Inventario.articulo == str(articulo).strip(),
        Inventario.estado == estado,
        Inventario.condicion == condicion,
    )
    if proveedor_id:
        q = q.filter(Inventario.proveedor_id == proveedor_id)
    return float(q.scalar() or 0.0)


def mover_inventario(s, *, proveedor_id, articulo, cantidad, tipo,
                     ubicacion="", estado="CRUDO", condicion="DISPONIBLE",
                     documento_id=None, referencia=None, usuario=None,
                     permitir_negativo=False) -> float:
    """
    Aplica un movimiento (+entra / -sale) y deja rastro en el kardex.
    Devuelve el saldo resultante.
    """
    cantidad = float(cantidad or 0)
    if abs(cantidad) < TOL:
        return obtener_saldo(s, proveedor_id, articulo, ubicacion, estado, condicion)

    p, a, u, e, c = _clave(proveedor_id, articulo, ubicacion, estado, condicion)
    row = s.query(Inventario).filter_by(
        proveedor_id=p, articulo=a, ubicacion=u, estado=e, condicion=c).first()
    if row is None:
        row = Inventario(proveedor_id=p, articulo=a, ubicacion=u, estado=e,
                         condicion=c, cantidad=0.0)
        s.add(row)
        s.flush()

    nuevo = float(row.cantidad or 0) + cantidad
    if nuevo < -TOL and not permitir_negativo:
        raise ReglaNegocio(
            f"Saldo insuficiente de {a} ({e}/{c}"
            + (f" en {u}" if u else "")
            + f"): disponible {row.cantidad:,.2f}, requerido {abs(cantidad):,.2f}."
        )
    row.cantidad = round(nuevo, 6)
    row.actualizado_en = dt.datetime.utcnow()

    s.add(MovimientoInventario(
        tipo=tipo, proveedor_id=p, articulo=a, ubicacion=u, estado=e,
        condicion=c, cantidad=cantidad, saldo_resultante=row.cantidad,
        documento_id=documento_id, referencia=referencia, usuario=usuario))
    s.flush()
    return row.cantidad


def filas_saldo(s, proveedor_id, articulo, estado, condicion, ubicacion_preferida=""):
    """Filas de inventario con saldo positivo, ordenadas por ubicación preferida."""
    filas = s.query(Inventario).filter(
        Inventario.proveedor_id == proveedor_id,
        Inventario.articulo == str(articulo).strip(),
        Inventario.estado == estado,
        Inventario.condicion == condicion,
        Inventario.cantidad > 0).all()
    pref = (ubicacion_preferida or "").upper()
    filas.sort(key=lambda r: (0 if r.ubicacion == pref else 1, -float(r.cantidad or 0)))
    return filas


def descontar_distribuido(s, *, proveedor_id, articulo, cantidad, tipo,
                          estado="CRUDO", condicion="DISPONIBLE", ubicacion=None,
                          documento_id=None, referencia=None, usuario=None,
                          permitir_negativo=False) -> list[tuple[str, float]]:
    """
    Descuenta `cantidad` repartiéndola entre las ubicaciones con saldo
    (WMS opcional). Devuelve [(ubicacion, cantidad_tomada), ...].
    """
    cantidad = abs(float(cantidad or 0))
    if cantidad <= TOL:
        return []
    if ubicacion:  # ubicación explícita: se descuenta solo de allí
        mover_inventario(s, proveedor_id=proveedor_id, articulo=articulo,
                         cantidad=-cantidad, tipo=tipo, ubicacion=ubicacion,
                         estado=estado, condicion=condicion, documento_id=documento_id,
                         referencia=referencia, usuario=usuario,
                         permitir_negativo=permitir_negativo)
        return [((ubicacion or "").upper(), cantidad)]

    disponible = saldo_articulo(s, proveedor_id, articulo, estado, condicion)
    if disponible + TOL < cantidad and not permitir_negativo:
        raise ReglaNegocio(
            f"Saldo insuficiente de {articulo} ({estado}/{condicion}): "
            f"disponible {disponible:,.2f}, requerido {cantidad:,.2f}.")

    tomado, restante = [], cantidad
    for f in filas_saldo(s, proveedor_id, articulo, estado, condicion):
        if restante <= TOL:
            break
        toma = min(float(f.cantidad or 0), restante)
        mover_inventario(s, proveedor_id=proveedor_id, articulo=articulo,
                         cantidad=-toma, tipo=tipo, ubicacion=f.ubicacion,
                         estado=estado, condicion=condicion, documento_id=documento_id,
                         referencia=referencia, usuario=usuario)
        tomado.append((f.ubicacion, toma))
        restante -= toma
    if restante > TOL:
        if not permitir_negativo:
            raise ReglaNegocio(
                f"Saldo insuficiente de {articulo} ({estado}/{condicion}): "
                f"faltan {restante:,.2f}.")
        mover_inventario(s, proveedor_id=proveedor_id, articulo=articulo,
                         cantidad=-restante, tipo=tipo, ubicacion="",
                         estado=estado, condicion=condicion, documento_id=documento_id,
                         referencia=referencia, usuario=usuario, permitir_negativo=True)
        tomado.append(("", restante))
    return tomado


def reclasificar(s, *, proveedor_id, articulo, cantidad, ubicacion=None,
                 estado="CRUDO", desde="RESTRINGIDO", hacia="DISPONIBLE",
                 documento_id=None, referencia=None, usuario=None):
    """Cambia la condición del producto (DISPONIBLE <-> RESTRINGIDO) sin mover ubicación."""
    tomado = descontar_distribuido(
        s, proveedor_id=proveedor_id, articulo=articulo, cantidad=cantidad,
        tipo="RECLASIFICACION", estado=estado, condicion=desde, ubicacion=ubicacion,
        documento_id=documento_id, referencia=referencia, usuario=usuario)
    for ubic, qty in tomado:
        mover_inventario(s, proveedor_id=proveedor_id, articulo=articulo,
                         cantidad=qty, tipo="RECLASIFICACION", ubicacion=ubic,
                         estado=estado, condicion=hacia, documento_id=documento_id,
                         referencia=referencia, usuario=usuario)


# =========================================================================
# RECIBO
# =========================================================================

@dataclass
class LineaRecibo:
    articulo: str
    descripcion: str = ""
    cantidad_documento: float = 0.0
    cantidad_fisica: float | None = None
    lote: str = ""
    serial: str = ""
    ubicacion_desde: str = ""
    ubicacion_hasta: str = ""


def crear_recibo(s, *, proveedor_id, origen, lineas: list[LineaRecibo],
                 referencia=None, usuario=None, es_reproceso=False,
                 ubicacion_destino="", archivo_id=None, fecha_documento=None,
                 observaciones=None, proveedor_origen_id=None, extraccion=None,
                 actor=None) -> Recibo:
    """Crea el recibo. BIN/FACTURA no impactan inventario hasta completar match por línea."""
    if origen not in ("BIN_A_BIN", "FACTURA", "REGISTRO"):
        raise ReglaNegocio(f"Origen de recibo no válido: {origen}")
    if not lineas:
        raise ReglaNegocio("El recibo debe tener al menos una línea.")
    _exigir_rol(actor, "PROVEEDOR", "RECIBO_AKT", "INVENTARIOS")
    _exigir_proveedor(actor, proveedor_id)

    prov = s.get(Proveedor, proveedor_id)
    if not prov or not prov.activo:
        raise ReglaNegocio("Proveedor inexistente o inactivo.")
    if proveedor_origen_id:
        po = s.get(Proveedor, proveedor_origen_id)
        if not po or not po.activo:
            raise ReglaNegocio("Proveedor origen inexistente o inactivo.")

    if referencia:
        fecha_ref = fecha_documento or dt.date.today()
        duplicado = s.query(Documento).filter(
            Documento.proveedor_id == proveedor_id,
            Documento.tipo == origen,
            Documento.referencia == str(referencia).strip(),
            Documento.fecha_documento == fecha_ref,
        ).first()
        if duplicado:
            raise ReglaNegocio(
                f"Documento duplicado: {origen} {referencia} ya existe como {duplicado.trz}.")

    doc = crear_documento(
        s, origen, proveedor_id=proveedor_id, referencia=referencia,
        archivo_id=archivo_id, creado_por=usuario, fecha_documento=fecha_documento,
        observaciones=observaciones, extraccion=extraccion)
    estado_inicial = "PENDIENTE_MATCH" if origen == "BIN_A_BIN" else "BORRADOR"
    r = Recibo(
        documento_id=doc.id, proveedor_id=proveedor_id,
        proveedor_origen_id=proveedor_origen_id, origen=origen,
        estado=estado_inicial, es_reproceso=bool(es_reproceso),
        ubicacion_destino=(ubicacion_destino or "").upper(), creado_por=usuario)
    s.add(r)
    s.flush()

    creadas = 0
    for ln in lineas:
        cod = str(ln.articulo).strip()
        if not cod:
            continue
        qdoc = float(ln.cantidad_documento or 0)
        qfis = qdoc if ln.cantidad_fisica is None else float(ln.cantidad_fisica or 0)
        s.add(ReciboLinea(
            recibo_id=r.id, articulo=cod, descripcion=ln.descripcion or "",
            cantidad_documento=qdoc, cantidad_fisica=qfis,
            lote=ln.lote or "", serial=ln.serial or "",
            ubicacion_desde=(ln.ubicacion_desde or "").upper(),
            ubicacion_hasta=(ln.ubicacion_hasta or ubicacion_destino or "").upper(),
            estado_match="PENDIENTE"))
        creadas += 1
    if not creadas:
        raise ReglaNegocio("El recibo no contiene líneas válidas.")
    s.flush()
    auditar(s, usuario, _actor_rol(actor), "CREAR_RECIBO", "recibos", r.id, f"{origen} {doc.trz}")
    return r

def validar_bin_a_bin(s, recibo: Recibo, proveedor_id_logueado: int) -> list[str]:
    """
    Hoja "Bin a Bin": las columnas Desde/Hasta deben corresponder al proveedor
    logueado. Se valida contra las ubicaciones asignadas al proveedor.
    """
    from core.models import Ubicacion
    alertas = []
    ubic_prov = {u.codigo for u in s.query(Ubicacion).filter(
        Ubicacion.proveedor_id == proveedor_id_logueado).all()}
    if not ubic_prov:
        return ["El proveedor no tiene ubicaciones asignadas: no se pudo validar Desde/Hasta."]
    for ln in recibo.lineas:
        if ln.ubicacion_desde and ln.ubicacion_desde not in ubic_prov and \
           ln.ubicacion_hasta and ln.ubicacion_hasta not in ubic_prov:
            alertas.append(
                f"Línea {ln.articulo}: ni 'Desde' ({ln.ubicacion_desde}) ni "
                f"'Hasta' ({ln.ubicacion_hasta}) corresponden al proveedor logueado.")
    return alertas


def sellar_recibo(s, recibo_id: int, usuario: str, actor=None) -> Recibo:
    """
    El proveedor de transformación 'sella' / certifica que recibió el producto
    que llegó desde otro proveedor ORIGEN. Queda a la espera de la factura y
    del BIN a BIN que adjunta el equipo de Recibo de AKT.
    """
    r = s.get(Recibo, recibo_id)
    if not r:
        raise ReglaNegocio("Recibo inexistente.")
    _exigir_rol(actor, "PROVEEDOR")
    _exigir_proveedor(actor, r.proveedor_id)
    if r.origen != "FACTURA":
        raise ReglaNegocio("Solo se sellan los recibos con origen FACTURA (proveedor origen).")
    if r.estado not in ("BORRADOR",):
        raise ReglaNegocio(f"El recibo ya está en estado {r.estado}.")
    r.estado = "SELLADO"
    r.sellado_por = usuario
    r.sellado_en = dt.datetime.utcnow()
    s.flush()
    auditar(s, usuario, None, "SELLAR_RECIBO", "recibos", r.id, "")
    return r


def ordenes_compra_abiertas(s, proveedor_id, articulo=None):
    q = s.query(OrdenCompra).filter(OrdenCompra.proveedor_id == proveedor_id,
                                    OrdenCompra.estado == "ABIERTA")
    if articulo:
        q = q.filter(OrdenCompra.articulo == str(articulo).strip())
    return q.order_by(OrdenCompra.fecha).all()


def adjuntar_bin_y_match(s, *, recibo_id, usuario, lineas_oc=None,
                         orden_compra_id=None, archivo_id=None,
                         referencia_bin=None, actor=None) -> dict:
    """Cruza cada línea con su OC y luego impacta inventario.

    lineas_oc: dict {recibo_linea_id: orden_compra_id}.
    orden_compra_id se conserva solo para recibos legados de una línea.
    """
    _exigir_rol(actor, "RECIBO_AKT", "INVENTARIOS")
    r = s.get(Recibo, recibo_id)
    if not r:
        raise ReglaNegocio("Recibo inexistente.")
    if r.estado == "CERRADA":
        raise ReglaNegocio("El recibo ya está cerrado.")
    if r.origen == "FACTURA" and r.estado != "SELLADO":
        raise ReglaNegocio("La factura debe estar SELLADA por el proveedor antes del match.")
    if r.origen not in ("FACTURA", "BIN_A_BIN"):
        raise ReglaNegocio("Este tipo de recibo no requiere match contra OC.")

    if archivo_id:
        s.add(DocumentoArchivo(documento_id=r.documento_id, archivo_id=archivo_id,
                               tipo="BIN_A_BIN"))
        if not r.documento.archivo_id:
            r.documento.archivo_id = archivo_id
    if referencia_bin:
        r.referencia_bin = referencia_bin

    pendientes = [ln for ln in r.lineas if not ln.procesada]
    if not pendientes:
        raise ReglaNegocio("El recibo no tiene líneas pendientes por procesar.")

    mapping = {}
    for k, v in (lineas_oc or {}).items():
        try:
            mapping[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    if orden_compra_id is not None:
        if len(pendientes) != 1 and not mapping:
            raise ReglaNegocio(
                "El recibo tiene varias líneas: debe seleccionar una OC para cada línea.")
        if len(pendientes) == 1:
            mapping[pendientes[0].id] = int(orden_compra_id)

    resultados, novedad_ids, ocs_usadas = [], [], set()
    for ln in pendientes:
        oc_id = mapping.get(ln.id) or ln.orden_compra_id
        if not oc_id:
            raise ReglaNegocio(f"Falta asignar OC a la línea {ln.articulo}.")
        oc = s.get(OrdenCompra, int(oc_id))
        if not oc:
            raise ReglaNegocio(f"OC inexistente para la línea {ln.articulo}.")
        if oc.estado != "ABIERTA":
            raise ReglaNegocio(f"La orden de compra {oc.numero} no está ABIERTA.")
        if oc.proveedor_id != r.proveedor_id:
            raise ReglaNegocio(f"La OC {oc.numero} pertenece a otro proveedor.")
        if str(oc.articulo).strip() != str(ln.articulo).strip():
            raise ReglaNegocio(
                f"La OC {oc.numero} corresponde a {oc.articulo}, no a {ln.articulo}.")

        esperado = max(0.0, float(oc.pendiente or 0))
        if esperado <= TOL:
            raise ReglaNegocio(f"La OC {oc.numero} ya no tiene saldo pendiente.")
        recibido = max(0.0, float(ln.cantidad_fisica or 0))
        aplicado = min(recibido, esperado)
        diferencia = round(recibido - esperado, 6)

        oc.cantidad_recibida = float(oc.cantidad_recibida or 0) + aplicado
        if oc.pendiente <= TOL:
            oc.estado = "CERRADA"

        ln.orden_compra_id = oc.id
        ln.cantidad_match = aplicado
        ln.cantidad_restringida = max(0.0, diferencia)
        if abs(diferencia) <= TOL:
            ln.estado_match = "COINCIDE"
        elif diferencia < 0:
            ln.estado_match = "FALTANTE"
            nov = registrar_novedad(
                s, recibo_id=r.id, proveedor_id=r.proveedor_id,
                recibo_linea_id=ln.id, articulo=ln.articulo, tipo="FALTANTE",
                cantidad=abs(diferencia), motivo="ORIGEN", usuario=usuario,
                origen_novedad="RECEPCION", actor=None,
                observaciones=(
                    f"Faltante contra OC {oc.numero}: esperado {esperado:,.2f}, "
                    f"recibido {recibido:,.2f}."))
            novedad_ids.append(nov.id)
        else:
            ln.estado_match = "SOBRANTE"
            nov = registrar_novedad(
                s, recibo_id=r.id, proveedor_id=r.proveedor_id,
                recibo_linea_id=ln.id, articulo=ln.articulo, tipo="SOBRANTE",
                cantidad=diferencia, motivo="ORIGEN", usuario=usuario,
                origen_novedad="RECEPCION", actor=None,
                observaciones=(
                    f"Sobrante contra OC {oc.numero}: esperado {esperado:,.2f}, "
                    f"recibido {recibido:,.2f}."))
            novedad_ids.append(nov.id)

        ocs_usadas.add(oc.id)
        resultados.append({
            "linea_id": ln.id, "articulo": ln.articulo, "oc_id": oc.id,
            "oc": oc.numero, "esperado": esperado, "recibido": recibido,
            "aplicado_oc": aplicado, "diferencia": diferencia,
            "estado": ln.estado_match,
        })

    r.orden_compra_id = next(iter(ocs_usadas)) if len(ocs_usadas) == 1 else None
    ingresar_inventario_recibo(s, r, usuario=usuario)
    if novedad_ids:
        r.estado = "NOVEDAD"
    else:
        r.estado = "CERRADA"
        r.cerrado_en = dt.datetime.utcnow()
    s.flush()
    auditar(s, usuario, _actor_rol(actor), "MATCH_OC", "recibos", r.id,
            f"{len(resultados)} linea(s) -> {r.estado}")

    out = {"estado": r.estado, "lineas": resultados, "novedad_ids": novedad_ids}
    if len(resultados) == 1:
        out.update({k: resultados[0][k] for k in ("esperado", "recibido", "diferencia")})
        if novedad_ids:
            out["novedad_id"] = novedad_ids[0]
    return out


def ingresar_inventario_recibo(s, recibo: Recibo, usuario=None) -> int:
    """Ingresa cantidades físicas validadas. El sobrante queda RESTRINGIDO."""
    n = 0
    for ln in recibo.lineas:
        if ln.procesada:
            continue
        if recibo.origen in ("BIN_A_BIN", "FACTURA") and ln.estado_match == "PENDIENTE":
            raise ReglaNegocio(
                f"La línea {ln.articulo} no puede ingresar a inventario sin match contra OC.")
        fisico = max(0.0, float(ln.cantidad_fisica or 0))
        restringido = (max(0.0, float(ln.cantidad_restringida or 0))
                       if ln.estado_match == "SOBRANTE" else 0.0)
        disponible = max(0.0, fisico - restringido)
        ubic = ln.ubicacion_hasta or recibo.ubicacion_destino or ""
        if disponible > TOL:
            mover_inventario(
                s, proveedor_id=recibo.proveedor_id, articulo=ln.articulo,
                cantidad=disponible, tipo="ENTRADA", ubicacion=ubic,
                estado="CRUDO", condicion="DISPONIBLE",
                documento_id=recibo.documento_id,
                referencia=recibo.documento.trz if recibo.documento else None,
                usuario=usuario)
        if restringido > TOL:
            mover_inventario(
                s, proveedor_id=recibo.proveedor_id, articulo=ln.articulo,
                cantidad=restringido, tipo="ENTRADA_SOBRANTE", ubicacion=ubic,
                estado="CRUDO", condicion="RESTRINGIDO",
                documento_id=recibo.documento_id,
                referencia=recibo.documento.trz if recibo.documento else None,
                usuario=usuario)
        ln.procesada = True
        n += 1
    s.flush()
    return n


def confirmar_recibo_simple(s, recibo_id: int, usuario=None, actor=None) -> Recibo:
    """Cierra únicamente REGISTRO manual. BIN/FACTURA siempre requieren match con OC."""
    r = s.get(Recibo, recibo_id)
    if not r:
        raise ReglaNegocio("Recibo inexistente.")
    _exigir_proveedor(actor, r.proveedor_id)
    if r.origen != "REGISTRO":
        raise ReglaNegocio("BIN a BIN y FACTURA deben pasar por el match contra OC.")
    if r.estado == "CERRADA":
        raise ReglaNegocio("El recibo ya está cerrado.")
    ingresar_inventario_recibo(s, r, usuario=usuario)
    r.estado = "CERRADA"
    r.cerrado_en = dt.datetime.utcnow()
    s.flush()
    return r

# =========================================================================
# NOVEDADES
# =========================================================================

def registrar_novedad(s, *, recibo_id=None, proveedor_id=None, articulo, tipo,
                      cantidad, motivo="ORIGEN", usuario=None, evidencia_id=None,
                      observaciones=None, recibo_linea_id=None,
                      origen_novedad="MANUAL", actor=None) -> Novedad:
    """Registra una diferencia distinguiendo recepción vs hallazgo físico."""
    if tipo not in ("FALTANTE", "SOBRANTE", "AVERIA"):
        raise ReglaNegocio(f"Tipo de novedad no válido: {tipo}")
    _exigir_rol(actor, "PLANEACION")
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        raise ReglaNegocio("La cantidad de la novedad debe ser mayor que cero.")

    r = s.get(Recibo, recibo_id) if recibo_id else None
    if r and proveedor_id is None:
        proveedor_id = r.proveedor_id
    if not proveedor_id:
        raise ReglaNegocio("Falta el proveedor de la novedad.")
    _exigir_proveedor(actor, proveedor_id)

    if tipo == "AVERIA" and not evidencia_id:
        raise ReglaNegocio(
            "Una novedad de AVERÍA exige adjuntar evidencia fotográfica de la destrucción.")

    origen_novedad = (origen_novedad or "MANUAL").upper()
    doc = crear_documento(s, "NOVEDAD", proveedor_id=proveedor_id,
                          referencia=f"{tipo}:{articulo}", creado_por=usuario,
                          observaciones=observaciones)
    condicion = "RESTRINGIDO" if tipo in ("AVERIA", "SOBRANTE") else "DISPONIBLE"
    estado = "EN_COLA_INVENTARIOS" if tipo == "AVERIA" else "ABIERTA"

    nov = Novedad(
        documento_id=doc.id, recibo_id=recibo_id, recibo_linea_id=recibo_linea_id,
        proveedor_id=proveedor_id, articulo=str(articulo).strip(), tipo=tipo,
        cantidad=cantidad, motivo=motivo, origen_novedad=origen_novedad,
        condicion_resultante=condicion, evidencia_id=evidencia_id, estado=estado,
        creado_por=usuario, observaciones=observaciones)
    s.add(nov)
    s.flush()

    if origen_novedad == "MANUAL":
        if tipo == "SOBRANTE":
            mover_inventario(
                s, proveedor_id=proveedor_id, articulo=articulo, cantidad=cantidad,
                tipo="NOVEDAD_SOBRANTE", estado="CRUDO", condicion="RESTRINGIDO",
                documento_id=doc.id, referencia=doc.trz, usuario=usuario)
        elif tipo == "AVERIA":
            reclasificar(
                s, proveedor_id=proveedor_id, articulo=articulo, cantidad=cantidad,
                estado="CRUDO", desde="DISPONIBLE", hacia="RESTRINGIDO",
                documento_id=doc.id, referencia=doc.trz, usuario=usuario)

    if r and r.estado not in ("CERRADA",):
        r.estado = "NOVEDAD"
    auditar(s, usuario, _actor_rol(actor), "NOVEDAD", "novedades", nov.id,
            f"{tipo} {articulo} {cantidad} [{origen_novedad}]")
    return nov


def ajustar_novedad(s, *, novedad_id, documento_ajuste, numero_ajuste,
                    usuario, aplicar_inventario=True, accion_sobrante="LIBERAR",
                    actor=None) -> Novedad:
    """Aplica TD90/TD96 sin duplicar el efecto de diferencias de recepción."""
    _exigir_rol(actor, "INVENTARIOS")
    if documento_ajuste not in ("TD90", "TD96"):
        raise ReglaNegocio("El documento de ajuste debe ser TD90 o TD96.")
    if not str(numero_ajuste or "").strip():
        raise ReglaNegocio("Debe indicar el número del documento de ajuste.")
    nov = s.get(Novedad, novedad_id)
    if not nov:
        raise ReglaNegocio("Novedad inexistente.")
    if nov.estado == "CERRADA":
        raise ReglaNegocio("La novedad ya está cerrada.")

    doc = crear_documento(
        s, "AJUSTE", proveedor_id=nov.proveedor_id,
        referencia=f"{documento_ajuste} {numero_ajuste}", creado_por=usuario)

    if aplicar_inventario:
        if nov.tipo == "AVERIA":
            descontar_distribuido(
                s, proveedor_id=nov.proveedor_id, articulo=nov.articulo,
                cantidad=nov.cantidad, tipo="AJUSTE_AVERIA", estado="CRUDO",
                condicion="RESTRINGIDO", documento_id=doc.id,
                referencia=doc.referencia, usuario=usuario)
            nov.condicion_resultante = "DESTRUIDO"
        elif nov.tipo == "FALTANTE":
            if (nov.origen_novedad or "MANUAL").upper() != "RECEPCION":
                descontar_distribuido(
                    s, proveedor_id=nov.proveedor_id, articulo=nov.articulo,
                    cantidad=nov.cantidad, tipo="AJUSTE_FALTANTE", estado="CRUDO",
                    condicion="DISPONIBLE", documento_id=doc.id,
                    referencia=doc.referencia, usuario=usuario)
            nov.condicion_resultante = "SIN_INGRESO"
        elif nov.tipo == "SOBRANTE":
            accion = (accion_sobrante or "LIBERAR").upper()
            if accion == "LIBERAR":
                reclasificar(
                    s, proveedor_id=nov.proveedor_id, articulo=nov.articulo,
                    cantidad=nov.cantidad, estado="CRUDO",
                    desde="RESTRINGIDO", hacia="DISPONIBLE",
                    documento_id=doc.id, referencia=doc.referencia, usuario=usuario)
                nov.condicion_resultante = "DISPONIBLE"
            elif accion == "DEVOLVER":
                descontar_distribuido(
                    s, proveedor_id=nov.proveedor_id, articulo=nov.articulo,
                    cantidad=nov.cantidad, tipo="DEVOLUCION_SOBRANTE", estado="CRUDO",
                    condicion="RESTRINGIDO", documento_id=doc.id,
                    referencia=doc.referencia, usuario=usuario)
                nov.condicion_resultante = "DEVUELTO"
            else:
                raise ReglaNegocio("Acción de sobrante no válida: use LIBERAR o DEVOLVER.")

    nov.documento_ajuste = documento_ajuste
    nov.numero_ajuste = numero_ajuste
    nov.estado = "CERRADA"
    nov.ajustada_por = usuario
    nov.ajustada_en = dt.datetime.utcnow()
    s.flush()

    if nov.recibo_id:
        pend = s.query(Novedad.id).filter(
            Novedad.recibo_id == nov.recibo_id, Novedad.estado != "CERRADA").first()
        if not pend:
            r = s.get(Recibo, nov.recibo_id)
            if r and r.estado == "NOVEDAD":
                r.estado = "CERRADA"
                r.cerrado_en = dt.datetime.utcnow()
    s.flush()
    auditar(s, usuario, _actor_rol(actor), "AJUSTAR_NOVEDAD", "novedades", nov.id,
            f"{documento_ajuste} {numero_ajuste}")
    return nov

def cola_inventarios(s, proveedor_id=None):
    """Cola de trabajo del equipo de Inventarios."""
    qn = s.query(Novedad).filter(Novedad.estado.in_(("ABIERTA", "EN_COLA_INVENTARIOS")))
    qa = s.query(Averia).filter(Averia.estado.in_(("REGISTRADA", "EN_COLA_INVENTARIOS")))
    qc = s.query(ConteoEjecutado).filter(ConteoEjecutado.genera_cola.is_(True),
                                         ConteoEjecutado.estado != "AJUSTADO")
    if proveedor_id:
        qn = qn.filter(Novedad.proveedor_id == proveedor_id)
        qa = qa.filter(Averia.proveedor_id == proveedor_id)
        qc = qc.filter(ConteoEjecutado.proveedor_id == proveedor_id)
    return {"novedades": qn.all(), "averias": qa.all(), "conteos": qc.all()}


# =========================================================================
# CONTEOS CÍCLICOS
# =========================================================================

def programar_conteo(s, *, proveedor_id, articulo, ubicacion="", fecha_programada=None,
                     prioridad=3, usuario=None, actor=None) -> ConteoProgramado:
    _exigir_rol(actor, "INVENTARIOS")
    doc = crear_documento(s, "CONTEO", proveedor_id=proveedor_id,
                          referencia=f"PROG:{articulo}", creado_por=usuario)
    c = ConteoProgramado(documento_id=doc.id, proveedor_id=proveedor_id,
                         articulo=str(articulo).strip(),
                         ubicacion=(ubicacion or "").upper(),
                         fecha_programada=fecha_programada or dt.date.today(),
                         prioridad=int(prioridad), creado_por=usuario)
    s.add(c)
    s.flush()
    return c


def ejecutar_conteo(s, *, proveedor_id, articulo, cantidad_fisica, ubicacion="",
                    programado_id=None, usuario=None, observaciones=None,
                    estado_inv="CRUDO", actor=None) -> ConteoEjecutado:
    """
    Registra el conteo. Si hay discrepancia genera cola de trabajo para que
    el equipo de Inventarios haga el análisis mayor.
    """
    _exigir_proveedor(actor, proveedor_id)
    if actor is not None:
        _exigir_rol(actor, "PROVEEDOR", "INVENTARIOS")
    sistema = (obtener_saldo(s, proveedor_id, articulo, ubicacion, estado_inv, "DISPONIBLE")
               if ubicacion else saldo_articulo(s, proveedor_id, articulo, estado_inv, "DISPONIBLE"))
    fisica = float(cantidad_fisica or 0)
    dif = round(fisica - sistema, 6)

    doc = crear_documento(s, "CONTEO", proveedor_id=proveedor_id,
                          referencia=f"EJEC:{articulo}", creado_por=usuario,
                          observaciones=observaciones)
    ce = ConteoEjecutado(
        documento_id=doc.id, programado_id=programado_id, proveedor_id=proveedor_id,
        articulo=str(articulo).strip(), ubicacion=(ubicacion or "").upper(),
        cantidad_sistema=sistema, cantidad_fisica=fisica, diferencia=dif,
        es_aleatorio=programado_id is None, genera_cola=abs(dif) > TOL,
        estado="EN_COLA_INVENTARIOS" if abs(dif) > TOL else "REGISTRADO",
        ejecutado_por=usuario, observaciones=observaciones)
    s.add(ce)
    if programado_id:
        p = s.get(ConteoProgramado, programado_id)
        if p:
            p.estado = "EJECUTADO"
    s.flush()
    return ce


def ajustar_conteo(s, *, conteo_id, documento_ajuste, numero_ajuste, usuario,
                   actor=None) -> ConteoEjecutado:
    _exigir_rol(actor, "INVENTARIOS")
    ce = s.get(ConteoEjecutado, conteo_id)
    if not ce:
        raise ReglaNegocio("Conteo inexistente.")
    if ce.estado == "AJUSTADO":
        raise ReglaNegocio("El conteo ya fue ajustado.")
    if documento_ajuste not in ("TD90", "TD96"):
        raise ReglaNegocio("El documento de ajuste debe ser TD90 o TD96.")
    doc = crear_documento(s, "AJUSTE", proveedor_id=ce.proveedor_id,
                          referencia=f"{documento_ajuste} {numero_ajuste}", creado_por=usuario)
    if abs(ce.diferencia or 0) > TOL:
        mover_inventario(s, proveedor_id=ce.proveedor_id, articulo=ce.articulo,
                         cantidad=ce.diferencia, tipo="CONTEO",
                         ubicacion=ce.ubicacion, estado="CRUDO",
                         condicion="DISPONIBLE", documento_id=doc.id,
                         referencia=doc.referencia, usuario=usuario,
                         permitir_negativo=True)
    ce.estado = "AJUSTADO"
    s.flush()
    return ce


def cumplimiento_conteos(s, proveedor_id=None, desde=None, hasta=None) -> dict:
    """Programado versus ejecutado."""
    q = s.query(ConteoProgramado)
    if proveedor_id:
        q = q.filter(ConteoProgramado.proveedor_id == proveedor_id)
    if desde:
        q = q.filter(ConteoProgramado.fecha_programada >= desde)
    if hasta:
        q = q.filter(ConteoProgramado.fecha_programada <= hasta)
    progs = q.all()
    total = len(progs)
    ejec = sum(1 for p in progs if p.estado == "EJECUTADO")
    venc = sum(1 for p in progs if p.estado == "PROGRAMADO"
               and p.fecha_programada and p.fecha_programada < dt.date.today())
    return {"programados": total, "ejecutados": ejec, "vencidos": venc,
            "cumplimiento_pct": (100.0 * ejec / total) if total else 0.0}


def sugerir_conteos_por_foto_erp(s, corte=None, proveedor_id=None, limite=50):
    """
    Compara la foto del inventario del ERP contra el saldo de la app y devuelve
    los artículos con mayor discrepancia, en orden de prioridad para agendar conteo.
    """
    q = s.query(FotoInventarioERP)
    if corte:
        q = q.filter(FotoInventarioERP.corte == corte)
    if proveedor_id:
        q = q.filter(FotoInventarioERP.proveedor_id == proveedor_id)
    out = []
    for f in q.all():
        app_qty = (obtener_saldo(s, f.proveedor_id, f.articulo, f.ubicacion or "",
                                 "CRUDO", "DISPONIBLE") if f.ubicacion
                   else saldo_articulo(s, f.proveedor_id, f.articulo, "CRUDO", "DISPONIBLE"))
        dif = round(float(f.cantidad_erp or 0) - app_qty, 6)
        out.append({"proveedor_id": f.proveedor_id, "articulo": f.articulo,
                    "ubicacion": f.ubicacion or "", "erp": float(f.cantidad_erp or 0),
                    "app": app_qty, "diferencia": dif, "abs": abs(dif)})
    out.sort(key=lambda x: x["abs"], reverse=True)
    for i, row in enumerate(out):
        row["prioridad"] = 1 if i < len(out) * 0.2 else (2 if i < len(out) * 0.5 else 3)
    return out[:limite]


# =========================================================================
# AVERÍAS
# =========================================================================

MOTIVOS_AVERIA = ("ORIGEN", "MANIPULACION", "PUESTA_A_PUNTO", "AKT")


def registrar_averia(s, *, proveedor_id, articulo, cantidad, motivo,
                     momento="PRODUCCION", evidencia_id=None, usuario=None,
                     estado_inventario="CRUDO", observaciones=None,
                     descontar=True, actor=None) -> Averia:
    """Requiere evidencia fotográfica de la destrucción."""
    _exigir_proveedor(actor, proveedor_id)
    if actor is not None:
        _exigir_rol(actor, "PROVEEDOR", "INVENTARIOS")
    if motivo not in MOTIVOS_AVERIA:
        raise ReglaNegocio(f"Motivo de avería no válido. Use: {', '.join(MOTIVOS_AVERIA)}")
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        raise ReglaNegocio("La cantidad de la avería debe ser mayor que cero.")
    if not evidencia_id:
        raise ReglaNegocio("Debe adjuntar la evidencia fotográfica de la destrucción.")

    doc = crear_documento(s, "AVERIA", proveedor_id=proveedor_id,
                          referencia=f"{motivo}:{articulo}", creado_por=usuario,
                          observaciones=observaciones)
    av = Averia(documento_id=doc.id, proveedor_id=proveedor_id,
                articulo=str(articulo).strip(), cantidad=cantidad, momento=momento,
                motivo=motivo, evidencia_id=evidencia_id,
                estado_inventario=estado_inventario, estado="EN_COLA_INVENTARIOS",
                registrado_por=usuario, observaciones=observaciones)
    s.add(av)
    s.flush()

    if descontar:
        # El producto averiado se aparta: pasa a RESTRINGIDO
        reclasificar(s, proveedor_id=proveedor_id, articulo=articulo,
                     cantidad=cantidad, estado=estado_inventario,
                     desde="DISPONIBLE", hacia="RESTRINGIDO",
                     documento_id=doc.id, referencia=doc.trz, usuario=usuario)
    auditar(s, usuario, None, "AVERIA", "averias", av.id, f"{motivo} {articulo} {cantidad}")
    return av


def ajustar_averia(s, *, averia_id, documento_ajuste, numero_ajuste,
                   usuario, actor=None) -> Averia:
    """Da de baja una avería previamente reclasificada a RESTRINGIDO."""
    _exigir_rol(actor, "INVENTARIOS")
    if documento_ajuste not in ("TD90", "TD96"):
        raise ReglaNegocio("El documento de ajuste debe ser TD90 o TD96.")
    if not str(numero_ajuste or "").strip():
        raise ReglaNegocio("Debe indicar el número del ajuste.")
    av = s.get(Averia, averia_id)
    if not av:
        raise ReglaNegocio("Avería inexistente.")
    if av.estado == "AJUSTADA":
        raise ReglaNegocio("La avería ya fue ajustada.")
    doc = crear_documento(
        s, "AJUSTE", proveedor_id=av.proveedor_id,
        referencia=f"{documento_ajuste} {numero_ajuste}", creado_por=usuario)
    descontar_distribuido(
        s, proveedor_id=av.proveedor_id, articulo=av.articulo,
        cantidad=av.cantidad, tipo="AJUSTE_AVERIA",
        estado=av.estado_inventario, condicion="RESTRINGIDO",
        documento_id=doc.id, referencia=doc.referencia, usuario=usuario)
    av.estado = "AJUSTADA"
    av.documento_ajuste = documento_ajuste
    s.flush()
    auditar(s, usuario, _actor_rol(actor), "AJUSTAR_AVERIA", "averias", av.id,
            f"{documento_ajuste} {numero_ajuste}")
    return av


def tolerancia_averias(s, proveedor_id=None, desde=None, hasta=None):
    """
    Compara las averías del periodo contra el margen de tolerancia pactado
    en el acuerdo comercial de cada proveedor.
    """
    provs = s.query(Proveedor).filter(Proveedor.activo.is_(True))
    if proveedor_id:
        provs = provs.filter(Proveedor.id == proveedor_id)
    filas = []
    for p in provs.all():
        qa = s.query(func.coalesce(func.sum(Averia.cantidad), 0.0)).filter(
            Averia.proveedor_id == p.id)
        qe = s.query(func.coalesce(func.sum(MovimientoInventario.cantidad), 0.0)).filter(
            MovimientoInventario.proveedor_id == p.id,
            MovimientoInventario.tipo == "ENTRADA")
        if desde:
            qa = qa.filter(Averia.fecha >= desde)
            qe = qe.filter(MovimientoInventario.fecha >= desde)
        if hasta:
            qa = qa.filter(Averia.fecha <= hasta)
            qe = qe.filter(MovimientoInventario.fecha <= hasta)
        averiado = float(qa.scalar() or 0)
        recibido = float(qe.scalar() or 0)
        if averiado == 0 and recibido == 0:
            continue
        pct = (100.0 * averiado / recibido) if recibido else 0.0
        tol = float(p.tolerancia_averia_pct or 0)
        filas.append({"proveedor_id": p.id, "proveedor": p.nombre, "codigo": p.codigo,
                      "recibido": recibido, "averiado": averiado,
                      "pct_averia": round(pct, 3), "tolerancia_pct": tol,
                      "excede": pct > tol,
                      "exceso_pp": round(pct - tol, 3)})
    filas.sort(key=lambda x: x["pct_averia"], reverse=True)
    return filas


# =========================================================================
# PRODUCCIÓN (MPS) Y SUBCONTRATACIÓN
# =========================================================================

def explosion_bom(s, articulo_transformado: str, proveedor_id=None) -> list[Bom]:
    """BOM específico del proveedor; usa BOM global solo si no existe uno específico."""
    base = s.query(Bom).filter(
        Bom.articulo_transformado == str(articulo_transformado).strip(),
        Bom.activo.is_(True))
    if not proveedor_id:
        return base.order_by(Bom.secuencia).all()
    p = s.get(Proveedor, proveedor_id)
    if not p:
        return []
    especifico = base.filter(Bom.proveedor_codigo == p.codigo).order_by(Bom.secuencia).all()
    if especifico:
        return especifico
    return base.filter(
        (Bom.proveedor_codigo.is_(None)) | (Bom.proveedor_codigo == "")
    ).order_by(Bom.secuencia).all()

def maximo_producible(s, proveedor_id, articulo_transformado) -> tuple[float, list[dict]]:
    """
    Cuánto se puede transformar con el inventario CRUDO DISPONIBLE actual,
    descontando lo ya comprometido en MPS abiertos.
    """
    lineas = explosion_bom(s, articulo_transformado, proveedor_id)
    if not lineas:
        return 0.0, []
    comprometido = _componentes_comprometidos(s, proveedor_id, excluir_mps=None)
    detalle, maximos = [], []
    for b in lineas:
        req = float(b.cantidad or 1)
        disp = saldo_articulo(s, proveedor_id, b.componente, "CRUDO", "DISPONIBLE")
        libre = max(0.0, disp - comprometido.get(b.componente, 0.0))
        # Unidades discretas: no se puede transformar media unidad
        posible = math.floor(libre / req) if req > 0 else 0.0
        maximos.append(posible)
        detalle.append({"componente": b.componente, "descripcion": b.desc_componente,
                        "req_unitario": req, "disponible": disp,
                        "comprometido": comprometido.get(b.componente, 0.0),
                        "libre": libre, "max_por_componente": posible})
    return (min(maximos) if maximos else 0.0), detalle


def _componentes_comprometidos(s, proveedor_id, excluir_mps=None) -> dict:
    """Componentes reservados por programas de producción aún no ejecutados."""
    q = s.query(ProgramaProduccion).filter(
        ProgramaProduccion.proveedor_id == proveedor_id,
        ProgramaProduccion.estado.in_(("PROGRAMADO", "EN_PROCESO")))
    if excluir_mps:
        q = q.filter(ProgramaProduccion.id != excluir_mps)
    comp = {}
    for m in q.all():
        pend = float(m.pendiente or 0)
        if pend <= 0:
            continue
        for b in explosion_bom(s, m.articulo, proveedor_id):
            comp[b.componente] = comp.get(b.componente, 0.0) + pend * float(b.cantidad or 1)
    return comp


def programar_mps(s, *, proveedor_id, articulo, cantidad, fecha_programada=None,
                  usuario=None, ubicacion_destino="", observaciones=None,
                  validar_existencias=True, actor=None) -> ProgramaProduccion:
    """
    Planeación sugiere qué transformar. Se valida existencia de componentes y
    se controla que no se programe de más.
    """
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        raise ReglaNegocio("La cantidad a programar debe ser mayor que cero.")
    if not explosion_bom(s, articulo, proveedor_id):
        raise ReglaNegocio(
            f"El artículo {articulo} no tiene BOM cargado: no se puede programar su transformación.")

    if validar_existencias:
        maximo, detalle = maximo_producible(s, proveedor_id, articulo)
        if cantidad > maximo + TOL:
            faltan = [d for d in detalle if d["max_por_componente"] < cantidad - TOL]
            msg = "; ".join(
                f"{d['componente']} (libre {d['libre']:,.0f}, "
                f"requiere {d['req_unitario'] * cantidad:,.0f})" for d in faltan[:5])
            raise ReglaNegocio(
                f"No se puede programar {cantidad:,.0f} de {articulo}: el máximo "
                f"producible con el inventario libre es {maximo:,.0f}. Falta: {msg}")

    doc = crear_documento(s, "MPS", proveedor_id=proveedor_id,
                          referencia=f"MPS:{articulo}", creado_por=usuario,
                          observaciones=observaciones)
    m = ProgramaProduccion(
        documento_id=doc.id, proveedor_id=proveedor_id,
        articulo=str(articulo).strip(), cantidad_programada=cantidad,
        fecha_programada=fecha_programada or dt.date.today(),
        ubicacion_destino=(ubicacion_destino or "").upper(),
        creado_por=usuario, observaciones=observaciones)
    s.add(m)
    s.flush()
    auditar(s, usuario, None, "PROGRAMAR_MPS", "mps", m.id, f"{articulo} x {cantidad}")
    return m


def ejecutar_produccion(s, *, mps_id, cantidad, usuario=None,
                        ubicacion_origen="", ubicacion_destino=None,
                        actor=None) -> OrdenProduccion:
    """
    Ejecuta la transformación: consume los componentes (CRUDO) según el BOM y
    produce el artículo transformado (PROCESADO). Actualiza el inventario.
    """
    m = s.get(ProgramaProduccion, mps_id)
    if not m:
        raise ReglaNegocio("Programa de producción inexistente.")
    _exigir_rol(actor, "PROVEEDOR")
    _exigir_proveedor(actor, m.proveedor_id)
    if m.estado in ("EJECUTADO", "CERRADO", "CANCELADO"):
        raise ReglaNegocio(f"El programa ya está {m.estado}.")
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        raise ReglaNegocio("La cantidad a producir debe ser mayor que cero.")
    if cantidad > m.pendiente + TOL:
        raise ReglaNegocio(
            f"No se puede ejecutar {cantidad:,.0f}: el pendiente del programa es "
            f"{m.pendiente:,.0f} (programado {m.cantidad_programada:,.0f}, "
            f"ejecutado {m.cantidad_ejecutada:,.0f}).")

    lineas = explosion_bom(s, m.articulo, m.proveedor_id)
    if not lineas:
        raise ReglaNegocio(f"El artículo {m.articulo} no tiene BOM activo.")

    # Validación previa: alcanza el inventario para todos los componentes
    faltantes = []
    for b in lineas:
        req = cantidad * float(b.cantidad or 1)
        disp = saldo_articulo(s, m.proveedor_id, b.componente, "CRUDO", "DISPONIBLE")
        if disp + TOL < req:
            faltantes.append(f"{b.componente}: requiere {req:,.0f}, disponible {disp:,.0f}")
    if faltantes:
        raise ReglaNegocio("Inventario insuficiente para la transformación -> "
                           + "; ".join(faltantes[:5]))

    doc = crear_documento(s, "PRODUCCION", proveedor_id=m.proveedor_id,
                          referencia=f"OP:{m.articulo}", creado_por=usuario)
    destino = (ubicacion_destino if ubicacion_destino is not None
               else (m.ubicacion_destino or ""))
    op = OrdenProduccion(documento_id=doc.id, mps_id=m.id, proveedor_id=m.proveedor_id,
                         articulo=m.articulo, cantidad=cantidad,
                         ubicacion_destino=(destino or "").upper(),
                         ejecutado_por=usuario)
    s.add(op)
    s.flush()

    for b in lineas:
        req = cantidad * float(b.cantidad or 1)
        _consumir_componente(s, m.proveedor_id, b.componente, req, doc.id, doc.trz,
                             usuario, ubicacion_origen)
        s.add(ConsumoProduccion(orden_id=op.id, componente=b.componente,
                                cantidad=req, ubicacion=(ubicacion_origen or "").upper()))

    mover_inventario(s, proveedor_id=m.proveedor_id, articulo=m.articulo,
                     cantidad=cantidad, tipo="TRANSFORMACION_PRODUCCION",
                     ubicacion=destino, estado="PROCESADO", condicion="DISPONIBLE",
                     documento_id=doc.id, referencia=doc.trz, usuario=usuario)

    m.cantidad_ejecutada = float(m.cantidad_ejecutada or 0) + cantidad
    m.estado = "EJECUTADO" if m.pendiente <= TOL else "EN_PROCESO"
    s.flush()
    auditar(s, usuario, None, "EJECUTAR_PRODUCCION", "ordenes_produccion", op.id,
            f"{m.articulo} x {cantidad}")
    return op


def _consumir_componente(s, proveedor_id, componente, requerido, doc_id, ref,
                         usuario, ubicacion_preferida=""):
    """Descuenta el componente, tomando primero la ubicación indicada (WMS opcional)."""
    restante = requerido
    for f in filas_saldo(s, proveedor_id, componente, "CRUDO", "DISPONIBLE",
                         ubicacion_preferida):
        if restante <= TOL:
            break
        toma = min(float(f.cantidad or 0), restante)
        mover_inventario(s, proveedor_id=proveedor_id, articulo=componente,
                         cantidad=-toma, tipo="TRANSFORMACION_CONSUMO",
                         ubicacion=f.ubicacion, estado="CRUDO", condicion="DISPONIBLE",
                         documento_id=doc_id, referencia=ref, usuario=usuario)
        restante -= toma
    if restante > TOL:
        raise ReglaNegocio(
            f"Saldo insuficiente del componente {componente}: faltan {restante:,.2f}.")


def cumplimiento_mps(s, proveedor_id=None, desde=None, hasta=None):
    q = s.query(ProgramaProduccion)
    if proveedor_id:
        q = q.filter(ProgramaProduccion.proveedor_id == proveedor_id)
    if desde:
        q = q.filter(ProgramaProduccion.fecha_programada >= desde)
    if hasta:
        q = q.filter(ProgramaProduccion.fecha_programada <= hasta)
    progs = q.all()
    prog = sum(float(m.cantidad_programada or 0) for m in progs)
    ejec = sum(float(m.cantidad_ejecutada or 0) for m in progs)
    return {"programado": prog, "ejecutado": ejec, "ordenes": len(progs),
            "cumplimiento_pct": (100.0 * ejec / prog) if prog else 0.0}


# =========================================================================
# DESPACHO
# =========================================================================

def crear_despacho(s, *, proveedor_id, lineas, lote=None, plan_ensamble=None,
                   usuario=None, ubicacion_origen="", archivo_id=None,
                   observaciones=None, actor=None) -> Despacho:
    _exigir_rol(actor, "PROVEEDOR")
    _exigir_proveedor(actor, proveedor_id)
    if not lineas:
        raise ReglaNegocio("El despacho debe tener al menos una línea.")
    doc = crear_documento(s, "DESPACHO", proveedor_id=proveedor_id,
                          referencia=lote or plan_ensamble, archivo_id=archivo_id,
                          creado_por=usuario, observaciones=observaciones)
    d = Despacho(documento_id=doc.id, proveedor_id=proveedor_id, lote=lote or doc.trz,
                 plan_ensamble=plan_ensamble, ubicacion_origen=(ubicacion_origen or "").upper(),
                 creado_por=usuario)
    s.add(d)
    s.flush()
    for ln in lineas:
        cod = str(ln.get("articulo", "")).strip()
        cant = float(ln.get("cantidad") or 0)
        if not cod or cant <= 0:
            continue
        s.add(DespachoLinea(despacho_id=d.id, articulo=cod,
                            descripcion=ln.get("descripcion", ""), cantidad=cant,
                            ubicacion=(ln.get("ubicacion") or ubicacion_origen or "").upper()))
    s.flush()
    if not d.lineas:
        raise ReglaNegocio("Ninguna línea del despacho es válida.")
    return d


def confirmar_despacho(s, *, despacho_id, usuario=None,
                       estado_inventario="PROCESADO", actor=None) -> Despacho:
    """Descarga el artículo del inventario y marca el lote como despachado."""
    d = s.get(Despacho, despacho_id)
    if not d:
        raise ReglaNegocio("Despacho inexistente.")
    _exigir_rol(actor, "PROVEEDOR")
    _exigir_proveedor(actor, d.proveedor_id)
    if d.estado == "DESPACHADO":
        raise ReglaNegocio("El despacho ya fue confirmado.")

    for ln in d.lineas:
        disp = saldo_articulo(s, d.proveedor_id, ln.articulo, estado_inventario, "DISPONIBLE")
        if disp + TOL < float(ln.cantidad or 0):
            raise ReglaNegocio(
                f"Saldo insuficiente para despachar {ln.articulo}: "
                f"disponible {disp:,.0f} ({estado_inventario}), requerido {ln.cantidad:,.0f}.")

    for ln in d.lineas:
        restante = float(ln.cantidad or 0)
        pref = ln.ubicacion or d.ubicacion_origen or ""
        for f in filas_saldo(s, d.proveedor_id, ln.articulo, estado_inventario,
                             "DISPONIBLE", pref):
            if restante <= TOL:
                break
            toma = min(float(f.cantidad or 0), restante)
            mover_inventario(s, proveedor_id=d.proveedor_id, articulo=ln.articulo,
                             cantidad=-toma, tipo="DESPACHO", ubicacion=f.ubicacion,
                             estado=estado_inventario, condicion="DISPONIBLE",
                             documento_id=d.documento_id, referencia=d.lote,
                             usuario=usuario)
            restante -= toma
    d.estado = "DESPACHADO"
    d.despachado_en = dt.datetime.utcnow()
    s.flush()
    auditar(s, usuario, None, "DESPACHAR", "despachos", d.id, d.lote or "")
    return d


# =========================================================================
# PANEL DE CONTROL DE INVENTARIO
# =========================================================================

def posicion_inventario(s, proveedor_id=None):
    q = s.query(Inventario).filter(Inventario.cantidad != 0)
    if proveedor_id:
        q = q.filter(Inventario.proveedor_id == proveedor_id)
    return q.all()


def consumo_promedio_dia(s, proveedor_id, articulo, dias=90) -> float:
    """Salidas promedio por día (consumo + despacho) de los últimos N días."""
    desde = dt.datetime.utcnow() - dt.timedelta(days=dias)
    q = s.query(func.coalesce(func.sum(MovimientoInventario.cantidad), 0.0)).filter(
        MovimientoInventario.articulo == str(articulo).strip(),
        MovimientoInventario.fecha >= desde,
        MovimientoInventario.tipo.in_(("TRANSFORMACION_CONSUMO", "DESPACHO", "SALIDA")))
    if proveedor_id:
        q = q.filter(MovimientoInventario.proveedor_id == proveedor_id)
    salidas = abs(float(q.scalar() or 0.0))
    return salidas / dias if dias else 0.0


def indicadores_inventario(s, proveedor_id=None, dias=90):
    """Rotación, cobertura (días) y posición por artículo."""
    filas = {}
    for inv in posicion_inventario(s, proveedor_id):
        k = (inv.proveedor_id, inv.articulo)
        d = filas.setdefault(k, {
            "proveedor_id": inv.proveedor_id, "articulo": inv.articulo,
            "crudo_disp": 0.0, "crudo_restr": 0.0,
            "proc_disp": 0.0, "proc_restr": 0.0, "total": 0.0})
        key = ("crudo" if inv.estado == "CRUDO" else "proc") + \
              ("_disp" if inv.condicion == "DISPONIBLE" else "_restr")
        d[key] += float(inv.cantidad or 0)
        d["total"] += float(inv.cantidad or 0)

    out = []
    for (pid, art), d in filas.items():
        cpd = consumo_promedio_dia(s, pid, art, dias)
        cobertura = (d["total"] / cpd) if cpd > 0 else (999.0 if d["total"] > 0 else 0.0)
        promedio = d["total"] if d["total"] else 0.0
        rotacion = ((cpd * 365) / promedio) if promedio > 0 else 0.0
        if cpd <= 0 and d["total"] > 0:
            semaforo = "SIN MOVIMIENTO"
        elif cobertura < 7:
            semaforo = "CRÍTICO"
        elif cobertura < 21:
            semaforo = "ALERTA"
        elif cobertura <= 90:
            semaforo = "ÓPTIMO"
        else:
            semaforo = "EXCESO"
        d.update({"consumo_dia": round(cpd, 4),
                  "cobertura_dias": round(min(cobertura, 999), 1),
                  "rotacion_anual": round(rotacion, 2),
                  "semaforo": semaforo})
        out.append(d)
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


SEMAFORO_COLOR = {
    "CRÍTICO": "#e02424", "ALERTA": "#f59e0b", "ÓPTIMO": "#16a34a",
    "EXCESO": "#2563eb", "SIN MOVIMIENTO": "#6b7280",
}


def antiguedad_documentos(s, proveedor_id=None, limite=500):
    q = s.query(Documento)
    if proveedor_id:
        q = q.filter(Documento.proveedor_id == proveedor_id)
    docs = q.order_by(Documento.creado_en.desc()).limit(limite).all()
    ahora = dt.datetime.utcnow()
    out = []
    for d in docs:
        dias = (ahora - (d.creado_en or ahora)).days
        rango = ("0-7" if dias <= 7 else "8-30" if dias <= 30
                 else "31-60" if dias <= 60 else "61-90" if dias <= 90 else "90+")
        out.append({"trz": d.trz, "tipo": d.tipo, "referencia": d.referencia or "",
                    "proveedor_id": d.proveedor_id, "fecha": d.creado_en,
                    "antiguedad_dias": dias, "rango": rango})
    return out


# =========================================================================
# DDMRP (FASE 2)
# =========================================================================

def calcular_zonas_ddmrp(consumo_dia, lead_time, factor_var=0.5, factor_lt=0.5, moq=0.0):
    roja_base = consumo_dia * lead_time * factor_lt
    roja = roja_base + roja_base * factor_var
    amarilla = consumo_dia * lead_time
    verde = max(consumo_dia * lead_time * factor_lt, moq)
    return {"zona_roja": round(roja, 2), "zona_amarilla": round(amarilla, 2),
            "zona_verde": round(verde, 2),
            "tope_min": round(roja, 2),
            "tope_reposicion": round(roja + amarilla, 2),
            "tope_max": round(roja + amarilla + verde, 2)}


def estado_ddmrp(disponible, zonas):
    tope_max = zonas["tope_max"] or 1
    pct = 100.0 * disponible / tope_max
    if disponible <= zonas["zona_roja"] * 0.5:
        return "ROJO OSCURO", pct
    if disponible <= zonas["zona_roja"]:
        return "ROJO", pct
    if disponible <= zonas["tope_reposicion"]:
        return "AMARILLO", pct
    if disponible <= tope_max:
        return "VERDE", pct
    return "AZUL (exceso)", pct
