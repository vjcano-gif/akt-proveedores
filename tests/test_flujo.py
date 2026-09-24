"""Pruebas end-to-end de integridad del flujo operativo."""
import datetime as dt
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))

from core.db import init_db, session_scope  # noqa: E402
from core.document_ai import (analizar_documento, completar_con_catalogo,
                              estructurar, inferir_origen, lineas_desde_catalogo,
                              lineas_bin_desde_texto, resumir_ubicaciones_bin,
                              extraer_bin_columnas_pdf, _texto_rapid_ordenado,
                              _extraer_bin_columnas_resultado)  # noqa: E402
from core.models import (Articulo, Averia, Bom, Inventario, MovimientoInventario,
                         Novedad, OrdenCompra, ProgramaProduccion, Proveedor,
                         Recibo, Ubicacion)  # noqa: E402
import core.services as sv  # noqa: E402
import core.document_ai as dai  # noqa: E402
from core.services import campos_faltantes_proveedor, proveedor_listo_para_activar  # noqa: E402
from core.auth import alcance_proveedor, puede  # noqa: E402
from core.ui import catalogo_proveedores  # noqa: E402
from app_pages.recibo import (_consolidar_extracciones,
                               _enriquecer_extraccion,
                               _guardar_soportes_consolidados)  # noqa: E402

OK, FAIL = [], []


def check(nombre, cond, extra=""):
    (OK if cond else FAIL).append(nombre)
    print(("  ✓ " if cond else "  ✗ ") + nombre + (f" [{extra}]" if extra else ""))


def esperar_error(nombre, fn, fragmento=""):
    try:
        fn()
        check(nombre, False, "no lanzó error")
    except sv.ReglaNegocio as e:
        ok = fragmento.lower() in str(e).lower() if fragmento else True
        check(nombre, ok, str(e)[:120])


print("\n=== 00. ACTIVACIÓN DE PROVEEDORES ===")
faltan = campos_faltantes_proveedor(
    codigo="VDR-X", nombre="Proveedor X", nit="",
    ubicacion_origen="UB-ORIGEN", ubicacion_destino="UB-PROV-01",
    tolerancia_averia_pct=1.0)
check("Proveedor sin NIT no está listo para activar", "NIT" in faltan)
check("Proveedor completo sí está listo para activar",
      proveedor_listo_para_activar(
          codigo="VDR-X", nombre="Proveedor X", nit="900123456",
          ubicacion_origen="UB-ORIGEN", ubicacion_destino="UB-PROV-01",
          tolerancia_averia_pct=1.0))
check("Proveedor sin HASTA no está listo para activar",
      not proveedor_listo_para_activar(
          codigo="VDR-X", nombre="Proveedor X", nit="900123456",
          ubicacion_origen="UB-ORIGEN", ubicacion_destino="",
          tolerancia_averia_pct=1.0))

print("\n=== 0A. PERMISOS INVENTARIOS ===")
inv_user = {"rol": "INVENTARIOS"}
for permiso in (
    "recibo_registrar", "recibo_sellar", "recibo_adjuntar_bin", "recibo_match_oc",
    "novedades_registrar", "novedades_ajustar", "conteos_programar",
    "conteos_ejecutar", "averias_registrar", "averias_ajustar",
    "produccion_programar", "produccion_ejecutar", "despacho_registrar",
    "maestros", "maestro_articulos", "maestro_bom", "maestro_ubicaciones",
    "maestro_proveedores", "maestro_oc", "maestro_usuarios", "ddmrp",
    "panel_control", "permiso_futuro_que_no_existe_aun",
):
    check(f"Inventarios permite {permiso}", puede(inv_user, permiso))

check("Proveedor no hereda comodín",
      not puede({"rol": "PROVEEDOR"}, "maestro_usuarios"))

print("\n=== PREPARACIÓN ===")
init_db()
with session_scope() as s:
    p = Proveedor(codigo="VDR0013714", nombre="Transformador Principal",
                  tolerancia_averia_pct=1.0,
                  ubicacion_origen="UB-ORIGEN",
                  ubicacion_destino="UB-PROV-01", activo=True)
    origen = Proveedor(codigo="VDRORIGEN", nombre="Proveedor Origen", activo=True)
    alt = Proveedor(codigo="VDRALT", nombre="Transformador Alterno", activo=True)
    inactivo = Proveedor(codigo="VDRINACT", nombre="Proveedor Inactivo", activo=False)
    s.add_all([p, origen, alt, inactivo]); s.flush()
    PID, ORIGEN_ID, ALT_ID = p.id, origen.id, alt.id

    for cod, pid, cerrada, restringida in (
        ("UB-ORIGEN", PID, False, False),
        ("UB-PROV-01", PID, False, False),
        ("UB-PROC-01", PID, False, False),
        ("UB-CERRADA", PID, True, False),
        ("UB-RESTRINGIDA", PID, False, True),
    ):
        s.add(Ubicacion(codigo=cod, proveedor_id=pid, activo=True,
                        cerrada=cerrada, restringida=restringida))

    for cod, desc, tipo in (
        ("TR-100", "Carenaje Pintado", "TRANSFORMADO"),
        ("CP-A", "Carenaje Crudo", "CRUDO"),
        ("CP-B", "Calca", "CRUDO"),
        ("CP-C", "Soporte", "CRUDO"),
        ("CP-D", "Pieza sobrante", "CRUDO"),
    ):
        s.add(Articulo(codigo=cod, descripcion=desc, tipo=tipo))

    s.add_all([
        Bom(articulo_transformado="TR-100", componente="CP-A", cantidad=2,
            secuencia=10, proveedor_codigo="VDR0013714", activo=True),
        Bom(articulo_transformado="TR-100", componente="CP-B", cantidad=1,
            secuencia=20, proveedor_codigo="VDR0013714", activo=True),
        Bom(articulo_transformado="TR-100", componente="CP-A", cantidad=99,
            secuencia=10, proveedor_codigo="VDRALT", activo=True),
    ])

    for numero, art, qty in (
        ("OC-A-100", "CP-A", 100),
        ("OC-B-60", "CP-B", 60),
        ("OC-C-10", "CP-C", 10),
        ("OC-B-10", "CP-B", 10),
        ("OC-D-10", "CP-D", 10),
    ):
        s.add(OrdenCompra(numero=numero, proveedor_id=PID, articulo=art,
                          cantidad=qty, estado="ABIERTA", fecha=dt.date.today()))
    s.flush()
    OCS = {o.numero: o.id for o in s.query(OrdenCompra).all()}

print("\n=== 0A0. PERSISTENCIA HISTÓRICA DE DOCUMENTOS DE RECIBO ===")
contenido_hist = b"DOCUMENTO-ORIGINAL-HISTORICO"
with session_scope() as s:
    arch_hist = sv.guardar_archivo(
        s, "registro_historico.jpg", contenido_hist,
        "image/jpeg", "historia@akt.com")
    rec_hist = sv.crear_recibo(
        s,
        proveedor_id=PID,
        origen="REGISTRO",
        referencia="REG-HIST-001",
        usuario="historia@akt.com",
        archivo_id=arch_hist.id,
        lineas=[sv.LineaRecibo(
            "CP-C", "Soporte", cantidad_documento=1,
            cantidad_fisica=1)],
    )
    HIST_RID = rec_hist.id
    HIST_TRZ = rec_hist.documento.trz

# Se abre una sesión nueva para demostrar que no depende de session_state ni
# de memoria del proceso: cabecera, vínculo y bytes deben persistir.
with session_scope() as s:
    rec_hist_db = s.get(Recibo, HIST_RID)
    check("Recibo histórico persiste en una nueva sesión",
          rec_hist_db is not None and rec_hist_db.documento is not None)
    check("TRZ histórico persiste",
          rec_hist_db.documento.trz == HIST_TRZ,
          rec_hist_db.documento.trz if rec_hist_db else "")
    check("Soporte original queda vinculado al Documento",
          rec_hist_db.documento.archivo is not None
          and rec_hist_db.documento.archivo.nombre == "registro_historico.jpg")
    check("Bytes del soporte histórico pueden recuperarse",
          sv.leer_archivo(rec_hist_db.documento.archivo) == contenido_hist)
    check("Soporte histórico conserva SHA-256",
          bool(rec_hist_db.documento.archivo.sha256)
          and len(rec_hist_db.documento.archivo.sha256) == 64)

print("\n=== 0AA. SELECTOR DE PROVEEDORES ===")
df_prov = catalogo_proveedores()
codigos_activos = set(df_prov["codigo"].tolist())
check("Selector incluye proveedor activo", "VDR0013714" in codigos_activos)
check("Selector excluye proveedor inactivo", "VDRINACT" not in codigos_activos)
check("Rol PROVEEDOR queda acotado a su proveedor",
      alcance_proveedor({"rol": "PROVEEDOR", "proveedor_id": PID}) == PID)

print("\n=== 0AB. PERSISTENCIA ESTADO PROVEEDOR ===")
with session_scope() as s:
    p = s.get(Proveedor, PID)
    p.activo = False
with session_scope() as s:
    check("Desactivación persiste tras nuevo session_scope",
          s.get(Proveedor, PID).activo is False)
df_inact = catalogo_proveedores()
check("Proveedor desactivado desaparece del catálogo activo",
      "VDR0013714" not in set(df_inact["codigo"].tolist()))
with session_scope() as s:
    p = s.get(Proveedor, PID)
    p.activo = True
with session_scope() as s:
    check("Reactivación persiste tras nuevo session_scope",
          s.get(Proveedor, PID).activo is True)

print("\n=== 0AC. ELIMINACIÓN SEGURA DE PROVEEDORES ===")
with session_scope() as s:
    p_del = Proveedor(
        codigo="VDRDEL", nombre="Proveedor para eliminar",
        nit="900999001", ubicacion_origen="UB-DEL-O",
        ubicacion_destino="UB-DEL-D", tolerancia_averia_pct=1.0,
        activo=False)
    s.add(p_del); s.flush()
    PID_DEL = p_del.id
    u_del = Ubicacion(
        codigo="UB-DEL-D", proveedor_id=PID_DEL, activo=True,
        cerrada=False, restringida=False)
    s.add(u_del); s.flush()
    UID_DEL = u_del.id

with session_scope() as s:
    deps = sv.dependencias_proveedor(s, PID_DEL)
    check("Proveedor nuevo sin historia puede eliminarse", len(deps) == 0, str(deps))
    res_del = sv.eliminar_proveedor_seguro(
        s, PID_DEL, usuario="inventarios@akt.com")
    check("Eliminar proveedor libera una ubicación",
          res_del["ubicaciones_liberadas"] == 1)

with session_scope() as s:
    check("Proveedor eliminado ya no existe", s.get(Proveedor, PID_DEL) is None)
    check("Ubicación del proveedor eliminado queda libre",
          s.get(Ubicacion, UID_DEL).proveedor_id is None)

with session_scope() as s:
    p_dep = Proveedor(
        codigo="VDRDELDEP", nombre="Proveedor con dependencia",
        nit="900999002", ubicacion_origen="UB-ORIGEN",
        ubicacion_destino="UB-PROV-01", tolerancia_averia_pct=1.0,
        activo=False)
    s.add(p_dep); s.flush()
    PID_DEP = p_dep.id
    s.add(OrdenCompra(
        numero="OC-DEL-BLOCK", proveedor_id=PID_DEP,
        articulo="CP-A", cantidad=1, estado="ABIERTA",
        fecha=dt.date.today()))

with session_scope() as s:
    deps = sv.dependencias_proveedor(s, PID_DEP)
    check("OC bloquea eliminación física",
          any(d["tabla"] == "ordenes_compra" for d in deps), str(deps))
    esperar_error(
        "Proveedor con trazabilidad no se elimina",
        lambda: sv.eliminar_proveedor_seguro(
            s, PID_DEP, usuario="inventarios@akt.com"),
        "no se puede eliminar")

print("\n=== 0AD. INGRESO MANUAL DESDE / HASTA ===")
with session_scope() as s:
    p_manual = Proveedor(
        codigo="VDRMANUAL", nombre="Proveedor ubicación manual",
        nit="900999003", tolerancia_averia_pct=1.0, activo=False)
    s.add(p_manual); s.flush()
    PID_MANUAL = p_manual.id
    u_desde_manual = sv.asegurar_ubicacion_ingresada(
        s, "desde-nuevo-001", rol="ORIGEN")
    u_hasta_manual = sv.asegurar_ubicacion_ingresada(
        s, "hasta-nuevo-001", rol="DESTINO", proveedor_id=PID_MANUAL)
    p_manual.ubicacion_origen = u_desde_manual.codigo
    p_manual.ubicacion_destino = u_hasta_manual.codigo
    p_manual.activo = True

with session_scope() as s:
    p_manual = s.get(Proveedor, PID_MANUAL)
    check("DESDE escrito manualmente se crea en Ubicaciones",
          s.query(Ubicacion).filter_by(codigo="DESDE-NUEVO-001").first() is not None)
    u_hasta_manual = s.query(Ubicacion).filter_by(codigo="HASTA-NUEVO-001").first()
    check("HASTA escrito manualmente se crea en Ubicaciones",
          u_hasta_manual is not None)
    check("HASTA nuevo queda asignado al proveedor",
          u_hasta_manual is not None and u_hasta_manual.proveedor_id == PID_MANUAL)
    check("Proveedor conserva DESDE/HASTA digitados",
          p_manual.ubicacion_origen == "DESDE-NUEVO-001"
          and p_manual.ubicacion_destino == "HASTA-NUEVO-001")

print("\n=== 0B. REGLAS DESDE / HASTA DEL BIN ===")
with session_scope() as s:
    esperar_error(
        "HASTA distinto al maestro bloquea el BIN",
        lambda: sv.crear_recibo(
            s, proveedor_id=PID, origen="BIN_A_BIN", usuario="test",
            ubicacion_destino="UB-PROV-01",
            lineas=[sv.LineaRecibo(
                "CP-A", "Carenaje", 1, 1,
                ubicacion_desde="UB-ORIGEN",
                ubicacion_hasta="UB-PROC-01")]),
        "HASTA")

with session_scope() as s:
    r_alerta = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN",
        referencia="BIN-DESDE-ALERTA", usuario="test",
        ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo(
            "CP-A", "Carenaje", 1, 1,
            ubicacion_desde="UB-PROC-01",
            ubicacion_hasta="UB-PROV-01")])
    alertas = sv.validar_bin_a_bin(s, r_alerta, PID)
    check("DESDE distinto genera alerta pero no bloquea",
          bool(alertas) and "DESDE" in alertas[0], str(alertas))
    check("HASTA correcto permite crear el BIN",
          r_alerta.estado == "PENDIENTE_MATCH")

print("\n=== 0B2. DETECCIÓN DEL PROVEEDOR DESTINO DEL BIN ===")
with session_scope() as s:
    p_doc = Proveedor(
        codigo="VDRDOCALT", nombre="Proveedor destino documento",
        nit="900999777", ubicacion_origen="UB-ORIGEN",
        ubicacion_destino="UB-DOC-ALT", tolerancia_averia_pct=1.0,
        activo=True)
    s.add(p_doc); s.flush()
    PID_DOC_ALT = p_doc.id
    s.add(Ubicacion(
        codigo="UB-DOC-ALT", proveedor_id=PID_DOC_ALT,
        activo=True, cerrada=False, restringida=False))

    p_doc_real = Proveedor(
        codigo="VDRDOCREAL", nombre="Proveedor destino BIN real",
        nit="900999778", ubicacion_origen="WSERE-PSER",
        ubicacion_destino="MOTOS-WSERE-WSER-VIPI-NTAR",
        tolerancia_averia_pct=1.0, activo=True)
    s.add(p_doc_real); s.flush()
    PID_DOC_REAL = p_doc_real.id
    s.add(Ubicacion(
        codigo="MOTOS-WSERE-WSER-VIPI-NTAR", proveedor_id=PID_DOC_REAL,
        activo=True, cerrada=False, restringida=False))

    p_pint = Proveedor(
        codigo="VDRPINT", nombre="Proveedor PINT prueba",
        nit="900999779",
        ubicacion_origen="WSERE PINT 1 1 1",
        ubicacion_destino="WSERE WINT 1 1 1",
        tolerancia_averia_pct=1.0, activo=True)
    s.add(p_pint); s.flush()
    PID_PINT = p_pint.id
    s.add(Ubicacion(
        codigo="WSERE PINT 1 1 1", proveedor_id=None,
        rol="ORIGEN", activo=True, cerrada=False, restringida=False))
    s.add(Ubicacion(
        codigo="WSERE WINT 1 1 1", proveedor_id=PID_PINT,
        rol="DESTINO", activo=True, cerrada=False, restringida=False))

extr_destino = {
    "ocr_ok": True,
    "confianza_texto": 0.99,
    "texto": "MOVIMIENTO BIN A BIN\nProveedor Código Descripción Cantidad Serial Lote Desde Hasta",
    "lineas": [],
    "bin_desde_valores": ["UB-ORIGEN"],
    "bin_hasta_valores": ["UB-DOC-ALT"],
    "orden_compra": {"valor": None},
    "nit": {"valor": None},
}
extr_destino = _enriquecer_extraccion(extr_destino, PID)
check("BIN identifica proveedor destino por HASTA",
      extr_destino.get("proveedor_destino_detectado_id") == PID_DOC_ALT,
      str(extr_destino))
check("BIN alerta cuando el proveedor seleccionado no es el destino",
      extr_destino.get("proveedor_destino_coincide") is False,
      str(extr_destino))

# Fallback específico para fotos/OCR: aunque el parser espacial no haya podido
# extraer HASTA, la ubicación destino maestra aparece repetida en el texto.
extr_destino_ocr = {
    "ocr_ok": True,
    "confianza_texto": 0.98,
    "texto": (
        "MOVIMIENTO BIN A BIN\n"
        "Proveedor Código Descripción Cantidad Serial Lote Desde Hasta\n"
        "SANYANG IN-0017700149603447 Cubierta manubrio 60 NONE NONE "
        "UB-ORIGEN UB-DOC-ALT\n"
        "SANYANG IN-0017700149603980 Cubta Frontal 60 NONE NONE "
        "UB-ORIGEN UB-DOC-ALT"
    ),
    "lineas": [],
    "bin_desde_valores": [],
    "bin_hasta_valores": [],
    "orden_compra": {"valor": None},
    "nit": {"valor": None},
}
extr_destino_ocr = _enriquecer_extraccion(extr_destino_ocr, PID)
check("Foto/OCR identifica proveedor por HASTA aun sin parser espacial",
      extr_destino_ocr.get("proveedor_destino_detectado_id") == PID_DOC_ALT,
      str(extr_destino_ocr))
check("Foto/OCR promueve HASTA detectado desde texto",
      extr_destino_ocr.get("bin_hasta_canon") == "UB-DOC-ALT",
      str(extr_destino_ocr))
check("Foto/OCR bloquea proveedor seleccionado incorrecto",
      extr_destino_ocr.get("proveedor_destino_coincide") is False,
      str(extr_destino_ocr))

# Caso equivalente al pantallazo real: el maestro incluye prefijo MOTOS-,
# mientras el BIN fotografiado imprime HASTA sin ese prefijo y con "TE" al final.
extr_bin_foto_real = {
    "ocr_ok": True,
    "confianza_texto": 0.98,
    "texto": (
        "MOVIMIENTO BIN A BIN\n"
        "Id de Bin: BIN2686958\n"
        "Proveedor Código Descripción Cantidad Serial Lote Desde Hasta\n"
        "CHONGQING-012 7700149386142 Carenaje Farola 200DS+ Mp 179 "
        "NONE NONE WSERE PSER 1 1 1 WSERE WSER VIPI NTAR TE\n"
        "SANYANG IN-0017700149603447 Cubierta manubrio JetEvo Mp 60 "
        "NONE NONE WSERE PSER 1 1 1 WSERE WSER VIPI NTAR TE"
    ),
    "lineas": [],
    "bin_desde_valores": [],
    "bin_hasta_valores": [],
    "orden_compra": {"valor": None},
    "nit": {"valor": None},
}
extr_bin_foto_real = _enriquecer_extraccion(extr_bin_foto_real, PID)
check("Foto BIN estándar identifica proveedor aunque maestro tenga prefijo MOTOS",
      extr_bin_foto_real.get("proveedor_destino_detectado_id") == PID_DOC_REAL,
      str(extr_bin_foto_real))
check("Foto BIN estándar detecta que no corresponde al proveedor seleccionado",
      extr_bin_foto_real.get("proveedor_destino_coincide") is False,
      str(extr_bin_foto_real))
check("Foto BIN estándar recupera HASTA maestro para validar",
      extr_bin_foto_real.get("bin_hasta_canon") == "MOTOS-WSERE-WSER-VIPI-NTAR",
      str(extr_bin_foto_real))

# Caso del BIN2720232 mostrado en producción: la geometría de la foto pierde
# DESDE, pero OCR sí contiene "WSERE PINT 1 1 1" en cada fila.
extr_bin_pint = {
    "ocr_ok": True,
    "confianza_texto": 0.98,
    "texto": (
        "MOVIMIENTO BIN A BIN\n"
        "Id de Bin: BIN2720232\n"
        "Proveedor Código Descripción Cantidad Serial Lote Desde Hasta\n"
        "CHONGQING-0127700149213509 Cbta Lat Izq Tras 300Rally Mp 98 "
        "NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1\n"
        "CHONGQING-0127700149213622 Cbta Tanq Der Decor Rally Mp 98 "
        "NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1"
    ),
    "lineas": [],
    "bin_desde_valores": [],
    "bin_hasta_valores": [],
    "orden_compra": {"valor": None},
    "nit": {"valor": None},
}
extr_bin_pint = _enriquecer_extraccion(extr_bin_pint, PID_PINT)
check("Foto BIN2720232 recupera DESDE desde OCR",
      extr_bin_pint.get("bin_desde_canon") == "WSERE PINT 1 1 1",
      str(extr_bin_pint))
check("Foto BIN2720232 marca DESDE detectado por texto",
      extr_bin_pint.get("bin_desde_detectado_por_texto") is True,
      str(extr_bin_pint))
check("Foto BIN2720232 conserva HASTA del proveedor",
      extr_bin_pint.get("bin_hasta_canon") == "WSERE WINT 1 1 1",
      str(extr_bin_pint))
check("Foto BIN2720232 corresponde al proveedor seleccionado",
      extr_bin_pint.get("proveedor_destino_coincide") is True,
      str(extr_bin_pint))

print("\n=== 0C. CANTIDAD FÍSICA CERO ES VÁLIDA ===")
with session_scope() as s:
    r0 = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-CERO",
        usuario="test", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo(
            "CP-C", "Soporte", cantidad_documento=10, cantidad_fisica=0,
            ubicacion_desde="UB-ORIGEN",
            ubicacion_hasta="UB-PROV-01")])
    check("Cantidad física cero no se reemplaza por documento",
          float(r0.lineas[0].cantidad_fisica) == 0.0)

print("\n=== 1. BIN -> MATCH -> INVENTARIO ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-100",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-A", "Carenaje Crudo", 100, 100,
                               ubicacion_desde="UB-ORIGEN",
                               ubicacion_hasta="UB-PROV-01")])
    RB1 = r.id
    check("BIN nace PENDIENTE_MATCH", r.estado == "PENDIENTE_MATCH")
    n_ing = sv.ingresar_inventario_bin_satisfactorio(
        s, RB1, "recibo@akt.com")
    check("Recibo satisfactorio ingresa 1 línea al inventario", n_ing == 1)
    check("Recibo satisfactorio carga 100 CRUDO/DISPONIBLE antes del match",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 100)
    n_repite = sv.ingresar_inventario_bin_satisfactorio(
        s, RB1, "recibo@akt.com")
    check("Ingreso satisfactorio es idempotente", n_repite == 0)
    check("Reintento no duplica inventario",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 100)
    alertas_bin = sv.validar_bin_a_bin(s, r, PID)
    check("DESDE correcto no genera alerta documental", not alertas_bin)

with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=RB1, orden_compra_id=OCS["OC-A-100"],
        usuario="recibo@akt.com")
    check("Match exacto cierra recibo", res["estado"] == "CERRADA")
    check("Match exacto NO duplica las 100 ya ingresadas",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 100)

print("\n=== 1B. BIN SATISFACTORIO CON SOBRANTE -> RESTRINGIDO ===")
with session_scope() as s:
    r_pre = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-PRE-SOB",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo(
            "CP-D", "Pieza sobrante", 13, 13,
            ubicacion_desde="UB-ORIGEN",
            ubicacion_hasta="UB-PROV-01")])
    RB_PRE_SOB = r_pre.id
    LIN_PRE_SOB = r_pre.lineas[0].id
    sv.ingresar_inventario_bin_satisfactorio(
        s, RB_PRE_SOB, "recibo@akt.com")
    check("Preingreso satisfactorio pone 13 disponibles",
          sv.saldo_articulo(s, PID, "CP-D", "CRUDO", "DISPONIBLE") == 13)

with session_scope() as s:
    res_pre = sv.match_recibo_lineas(
        s, recibo_id=RB_PRE_SOB,
        asignaciones={LIN_PRE_SOB: OCS["OC-D-10"]},
        usuario="recibo@akt.com")
    check("Match de preingreso detecta sobrante 3",
          res_pre["lineas"][0]["sobrante"] == 3)
    check("Preingreso + match deja 10 disponibles sin duplicar",
          sv.saldo_articulo(s, PID, "CP-D", "CRUDO", "DISPONIBLE") == 10)
    check("Preingreso + match reclasifica 3 a RESTRINGIDO",
          sv.saldo_articulo(s, PID, "CP-D", "CRUDO", "RESTRINGIDO") == 3)

print("\n=== 2. FACTURA MULTILÍNEA / MÚLTIPLES OC ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, proveedor_origen_id=ORIGEN_ID,
        factura_origen="FV-9001", origen="FACTURA", referencia="FV-9001",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[
            sv.LineaRecibo("CP-B", "Calca", 60, 55, ubicacion_hasta="UB-PROV-01"),
            sv.LineaRecibo("CP-C", "Soporte", 10, 10, ubicacion_hasta="UB-PROV-01"),
        ])
    RB2 = r.id
    check("Proveedor origen queda separado del transformador",
          r.proveedor_origen_id == ORIGEN_ID and r.proveedor_id == PID)
    sv.sellar_recibo(s, RB2, "proveedor@akt.com")
    check("Factura sellada pasa a PENDIENTE_MATCH",
          s.get(Recibo, RB2).estado == "PENDIENTE_MATCH")
    lineas = {l.articulo: l.id for l in r.lineas}

with session_scope() as s:
    res = sv.match_recibo_lineas(
        s, recibo_id=RB2,
        asignaciones={
            lineas["CP-B"]: OCS["OC-B-60"],
            lineas["CP-C"]: OCS["OC-C-10"],
        },
        usuario="recibo@akt.com")
    check("Un recibo soporta varias OC", len({x["oc"] for x in res["lineas"]}) == 2)
    check("Faltante deja recibo en NOVEDAD", res["estado"] == "NOVEDAD")
    nov_b = s.query(Novedad).filter_by(recibo_id=RB2, articulo="CP-B").one()
    NOV_FALT = nov_b.id
    check("Faltante automático = 5", nov_b.tipo == "FALTANTE" and nov_b.cantidad == 5)
    check("Solo ingresa lo físico recibido (55)",
          sv.saldo_articulo(s, PID, "CP-B") == 55)
    check("Línea exacta CP-C ingresa 10",
          sv.saldo_articulo(s, PID, "CP-C") == 10)

with session_scope() as s:
    sv.ajustar_novedad(s, novedad_id=NOV_FALT, documento_ajuste="TD90",
                       numero_ajuste="AJ-FALT-1", usuario="inventarios@akt.com")
    check("Cerrar faltante NO descuenta inventario otra vez",
          sv.saldo_articulo(s, PID, "CP-B") == 55)

print("\n=== 3. SOBRANTE SEGREGADO ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-SOB",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-B", "Calca", 10, 13,
                               ubicacion_desde="UB-ORIGEN",
                               ubicacion_hasta="UB-PROV-01")])
    RB3, LIN3 = r.id, r.lineas[0].id

with session_scope() as s:
    res = sv.match_recibo_lineas(
        s, recibo_id=RB3, asignaciones={LIN3: OCS["OC-B-10"]},
        usuario="recibo@akt.com")
    nov = s.query(Novedad).filter_by(recibo_id=RB3, tipo="SOBRANTE").one()
    NOV_SOB = nov.id
    check("Sobrante de 3 genera novedad", nov.cantidad == 3)
    check("Solo 10 pasan a DISPONIBLE", sv.saldo_articulo(s, PID, "CP-B") == 65)
    check("3 quedan RESTRINGIDAS",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "RESTRINGIDO") == 3)

with session_scope() as s:
    sv.ajustar_novedad(s, novedad_id=NOV_SOB, documento_ajuste="TD96",
                       numero_ajuste="AJ-SOB-1", usuario="inventarios@akt.com")
    check("Aprobación del sobrante lo libera a DISPONIBLE",
          sv.saldo_articulo(s, PID, "CP-B") == 68)
    check("Ya no queda sobrante restringido",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "RESTRINGIDO") == 0)

print("\n=== 4. REGISTRO MANUAL ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="REGISTRO", usuario="proveedor@akt.com",
        ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-B", "Calca", 30, 30,
                               ubicacion_hasta="UB-PROV-01")])
    sv.confirmar_recibo_simple(s, r.id, "proveedor@akt.com")
    check("Registro manual suma 30 (68 -> 98)",
          sv.saldo_articulo(s, PID, "CP-B") == 98)

print("\n=== 5. UBICACIONES ===")
with session_scope() as s:
    esperar_error(
        "Ubicación cerrada rechaza movimientos",
        lambda: sv.mover_inventario(
            s, proveedor_id=PID, articulo="CP-A", cantidad=1, tipo="ENTRADA",
            ubicacion="UB-CERRADA"),
        "cerrada")
    esperar_error(
        "Ubicación restringida no acepta DISPONIBLE",
        lambda: sv.mover_inventario(
            s, proveedor_id=PID, articulo="CP-A", cantidad=1, tipo="ENTRADA",
            ubicacion="UB-RESTRINGIDA", condicion="DISPONIBLE"),
        "restringida")

print("\n=== 6. CONTEOS Y AVERÍAS ===")
with session_scope() as s:
    cp = sv.programar_conteo(
        s, proveedor_id=PID, articulo="CP-A", ubicacion="UB-PROV-01",
        prioridad=1, usuario="inventarios@akt.com")
    ce = sv.ejecutar_conteo(
        s, proveedor_id=PID, articulo="CP-A", cantidad_fisica=98,
        ubicacion="UB-PROV-01", programado_id=cp.id, usuario="proveedor@akt.com")
    CE_ID = ce.id
    check("Conteo detecta diferencia -2", ce.diferencia == -2)

with session_scope() as s:
    sv.ajustar_conteo(s, conteo_id=CE_ID, documento_ajuste="TD96",
                      numero_ajuste="AJ-CNT", usuario="inventarios@akt.com")
    check("Conteo ajusta saldo A a 98", sv.saldo_articulo(s, PID, "CP-A") == 98)

with session_scope() as s:
    ev = sv.guardar_archivo(s, "destruccion.jpg", b"FOTO-EVIDENCIA",
                            "image/jpeg", "proveedor@akt.com")
    check("Archivo guarda SHA-256", bool(ev.sha256) and len(ev.sha256) == 64)
    av = sv.registrar_averia(
        s, proveedor_id=PID, articulo="CP-A", cantidad=3,
        motivo="MANIPULACION", momento="ALMACENAMIENTO",
        evidencia_id=ev.id, usuario="proveedor@akt.com")
    check("Avería pasa 3 a RESTRINGIDO",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "RESTRINGIDO") == 3)
    check("Disponible A queda 95", sv.saldo_articulo(s, PID, "CP-A") == 95)
    AV_ID = av.id

print("\n=== 7. BOM POR PROVEEDOR / PRODUCCIÓN ===")
with session_scope() as s:
    bom = sv.explosion_bom(s, "TR-100", PID)
    check("BOM del proveedor no mezcla BOM alterno",
          len(bom) == 2 and {b.cantidad for b in bom} == {1, 2})
    maximo, _ = sv.maximo_producible(s, PID, "TR-100")
    check("Máximo producible = 47", maximo == 47, f"max={maximo}")
    esperar_error(
        "No permite programar 48 > 47",
        lambda: sv.programar_mps(
            s, proveedor_id=PID, articulo="TR-100", cantidad=48,
            usuario="planeacion@akt.com"),
        "máximo")

with session_scope() as s:
    m = sv.programar_mps(
        s, proveedor_id=PID, articulo="TR-100", cantidad=40,
        usuario="planeacion@akt.com", ubicacion_destino="UB-PROC-01")
    MPS_ID = m.id
    check("MPS creado", m.estado == "PROGRAMADO")

with session_scope() as s:
    maximo2, _ = sv.maximo_producible(s, PID, "TR-100")
    check("MPS reserva componentes; libre baja a 7", maximo2 == 7, f"max={maximo2}")

with session_scope() as s:
    sv.ejecutar_produccion(
        s, mps_id=MPS_ID, cantidad=25, usuario="proveedor@akt.com",
        ubicacion_origen="UB-PROV-01", ubicacion_destino="UB-PROC-01")
    check("Producción consume 50 A (95 -> 45)", sv.saldo_articulo(s, PID, "CP-A") == 45)
    check("Producción consume 25 B (98 -> 73)", sv.saldo_articulo(s, PID, "CP-B") == 73)
    check("Produce 25 TR-100", sv.saldo_articulo(
        s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 25)

with session_scope() as s:
    ev = sv.guardar_archivo(s, "dest2.jpg", b"FOTO2", "image/jpeg", "prov")
    sv.registrar_averia(
        s, proveedor_id=PID, articulo="TR-100", cantidad=2,
        motivo="PUESTA_A_PUNTO", momento="PRODUCCION",
        estado_inventario="PROCESADO", evidencia_id=ev.id,
        usuario="proveedor@akt.com")
    check("Avería procesada deja 23 disponibles",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 23)

print("\n=== 8. DESPACHO ===")
with session_scope() as s:
    d = sv.crear_despacho(
        s, proveedor_id=PID, lote="LOTE-1", plan_ensamble="PLAN-1",
        usuario="proveedor@akt.com", ubicacion_origen="UB-PROC-01",
        lineas=[{"articulo": "TR-100", "cantidad": 20, "ubicacion": "UB-PROC-01"}])
    DSP_ID = d.id

with session_scope() as s:
    sv.confirmar_despacho(s, despacho_id=DSP_ID, usuario="proveedor@akt.com")
    check("Despacho deja 3 procesadas",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 3)

print("\n=== 9. TRAZABILIDAD / CONCURRENCIA LÓGICA ===")
with session_scope() as s:
    docs = [sv.crear_documento(s, "REGISTRO", proveedor_id=PID, creado_por="test")
            for _ in range(20)]
    trz = [d.trz for d in docs]
    check("TRZ no se repite en lote", len(set(trz)) == len(trz))

print("\n=== 10. EXTRACCIÓN DOCUMENTAL ===")
sample = """
FACTURA FV-3-8619
NIT 900123456-7
ORDEN DE COMPRA OC-2026-0001
FECHA 17/09/2026
CP-A Carenaje Crudo 100
CP-B Calca 55
"""
ext = estructurar(sample, 0.99)
check("Extractor identifica referencia", ext["referencia"]["valor"] is not None)
check("Extractor identifica OC", ext["orden_compra"]["valor"] == "OC-2026-0001")
check("Extractor propone líneas", len(ext["lineas"]) >= 2)
check("Extractor infiere FACTURA", inferir_origen(sample) == "FACTURA")
check("Extractor infiere estándar MOVIMIENTO BIN A BIN",
      inferir_origen(
          "MOVIMIENTO BIN A BIN\nId de Bin: BIN2686958\n"
          "Proveedor Código Descripción Cantidad Serial Lote Desde Hasta"
      ) == "BIN_A_BIN")
check("Extractor infiere BIN por estructura tabular aunque falte el título",
      inferir_origen(
          "Id de Bin: BIN2686958\n"
          "Proveedor Código Descripción Cantidad Serial Lote Desde Hasta"
      ) == "BIN_A_BIN")

sample_fechas_bin = """
MOVIMIENTO BIN A BIN
Id de Bin: BIN2654425
Fecha: 18/09/2026
Fecha Transacción: 2026-08-14-07.11.30
Proveedor Código Descripción Cantidad Serial Lote Desde Hasta
CHONGQING-012 7700149453691 Base Silla 200DS+ Mp 80 NONE NONE WSERE PUMO 1 1 1 WSERE WUMO 1 1 1
"""
ext_fechas_bin = estructurar(sample_fechas_bin, 0.99)
check("BIN prioriza Fecha Transacción sobre Fecha de encabezado",
      ext_fechas_bin["fecha"]["valor"] == "2026-08-14",
      str(ext_fechas_bin["fecha"]))
check("BIN conserva Fecha Transacción como campo auditable",
      ext_fechas_bin["fecha_transaccion"]["valor"] == "2026-08-14",
      str(ext_fechas_bin["fecha_transaccion"]))
check("BIN conserva fecha de encabezado solo como origen secundario",
      ext_fechas_bin["fecha_documento_origen"]["valor"] == "18/09/2026",
      str(ext_fechas_bin["fecha_documento_origen"]))

sample_catalogo = """
FACTURA FV-777
FECHA 17/09/2026
UBICACION DESTINO UB-PROV-01
GARANTIA
CP-A Carenaje Crudo CANTIDAD 12
"""
ext_cat = estructurar(sample_catalogo, 0.97)
ext_cat["confianza_texto"] = 0.97
ext_cat = completar_con_catalogo(
    ext_cat, {"CP-A": "Carenaje Crudo", "CP-B": "Calca"})
check("Maestro confirma artículo OCR",
      any(x["articulo"] == "CP-A" for x in ext_cat["lineas"]))
check("Maestro conserva/proponen cantidad 12",
      any(x["articulo"] == "CP-A" and float(x["cantidad_documento"]) == 12
          for x in ext_cat["lineas"]))
check("Extractor detecta ubicación destino",
      ext_cat["ubicacion_destino"]["valor"] == "UB-PROV-01")
check("Extractor detecta reproceso/garantía",
      ext_cat["es_reproceso_sugerido"] is True)

# Simula RapidOCR detectando celdas separadas en la misma fila.
fake_rapid = SimpleNamespace(
    txts=[
        "7700149386142", "Carenaje Farola 200DS+ Mp", "12",
        "7700149386173", "Cubierta Tras 200DS+ Mp", "8",
        "7700149385725", "Cubta Der Tanq Gas 200DS+ Mp", "5",
    ],
    scores=[0.99] * 9,
    boxes=[
        [[10, 10], [210, 10], [210, 40], [10, 40]],
        [[260, 10], [760, 10], [760, 40], [260, 40]],
        [[900, 10], [960, 10], [960, 40], [900, 40]],
        [[10, 70], [210, 70], [210, 100], [10, 100]],
        [[260, 70], [760, 70], [760, 100], [260, 100]],
        [[900, 70], [960, 70], [960, 100], [900, 100]],
        [[10, 130], [210, 130], [210, 160], [10, 160]],
        [[260, 130], [760, 130], [760, 160], [260, 160]],
        [[900, 130], [960, 130], [960, 160], [900, 160]],
    ],
)
texto_tabla, _ = _texto_rapid_ordenado(fake_rapid)
check("RapidOCR recompone tres filas de tabla",
      len(texto_tabla.splitlines()) == 3, texto_tabla)

catalogo_tabla = {
    "7700149386142": "Carenaje Farola 200DS+ Mp",
    "7700149386173": "Cubierta Tras 200DS+ Mp",
    "7700149385725": "Cubta Der Tanq Gas 200DS+ Mp",
}
lineas_tabla = lineas_desde_catalogo(texto_tabla, catalogo_tabla, 0.99)
cantidades = {x["articulo"]: x["cantidad_documento"] for x in lineas_tabla}
check("Tabla recupera todas las referencias", len(cantidades) == 3, str(cantidades))
check("Tabla asocia cantidad 12 y no confunde 200DS",
      cantidades.get("7700149386142") == 12.0, str(cantidades))
check("Tabla asocia cantidades 8 y 5",
      cantidades.get("7700149386173") == 8.0
      and cantidades.get("7700149385725") == 5.0, str(cantidades))
check("OCR no presume cantidad física",
      all(float(x["cantidad_fisica"]) == 0 for x in lineas_tabla))

# Regresión de producción: BIN fotografiado. El texto estructurado correcto
# debe ganar sobre una geometría RapidOCR que inventó un código y cambió
# cantidades. Solo se aceptan códigos EXACTOS presentes en el maestro.
texto_inter = """
MOVIMIENTO BIN A BIN
Id de Bin: BIN2720232
Proveedor Código Descripción Cantidad Serial Lote Desde Hasta
CHONGQING-012 7700149213509 Cbta Lat Izq Tras 300Rally Mp 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149213622 Cbta Tanq Der Decor Rally Mp 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149213639 Cbta Tanq Izq Decor Rally Mp 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149212113 Cbta Tanque Izq 300Rally Mp 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149649926 Cubta Fron Sup Der SR1-A MP 120 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149649933 Cubta Int Cent Inf SR1-A MP 150 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149648813 Cubta Lat inf Der SR1-A MP 100 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149212359 Guardabarro Del 300Rally Mp 72 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149211895 Quilla Izq 300Ral MP 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149212120 Tap Lat Der 300Rally Mp 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
CHONGQING-012 7700149212144 Tap Lat Izq 300Rally Mp 98 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
GUANGDONG-024 7700149266475 Tanque 125CH Mp 100 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
GUANGDONG-024 7700149266468 Tapa Lat Sup Der CH125 Mp 150 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
GUANGDONG-024 7700149266451 Tapa Lat Sup Izq CH125 Mp 150 NONE NONE WSERE PINT 1 1 1 WSERE WINT 1 1 1
"""
catalogo_inter = {
    "7700149213509": "Cbta Lat Izq Tras 300Rally Mp",
    "7700149213622": "Cbta Tanq Der Decor Rally Mp",
    "7700149213639": "Cbta Tanq Izq Decor Rally Mp",
    "7700149212113": "Cbta Tanque Izq 300Rally Mp",
    "7700149649926": "Cubta Fron Sup Der SR1-A MP",
    "7700149649933": "Cubta Int Cent Inf SR1-A MP",
    "7700149648813": "Cubta Lat inf Der SR1-A MP",
    "7700149212359": "Guardabarro Del 300Rally Mp",
    "7700149211895": "Quilla Izq 300Ral MP",
    "7700149212120": "Tap Lat Der 300Rally Mp",
    "7700149212144": "Tap Lat Izq 300Rally Mp",
    "7700149266475": "Tanque 125CH Mp",
    "7700149266468": "Tapa Lat Sup Der CH125 Mp",
    "7700149266451": "Tapa Lat Sup Izq CH125 Mp",
    # Existe en el maestro, pero NO está en el documento: simula una
    # alucinación espacial que casualmente coincide con un código válido.
    "7700999999999": "Articulo ajeno al BIN",
}
esperado_inter = {
    "7700149213509": 98.0,
    "7700149213622": 98.0,
    "7700149213639": 98.0,
    "7700149212113": 98.0,
    "7700149649926": 120.0,
    "7700149649933": 150.0,
    "7700149648813": 100.0,
    "7700149212359": 72.0,
    "7700149211895": 98.0,
    "7700149212120": 98.0,
    "7700149212144": 98.0,
    "7700149266475": 100.0,
    "7700149266468": 150.0,
    "7700149266451": 150.0,
}
resultado_inter = estructurar(texto_inter, 0.91)
resultado_inter["confianza_texto"] = 0.91
resultado_inter["metodo"] = "TESSERACT/BIN_ESTRUCTURADO"
resultado_inter["entrada_imagen"] = True
resultado_inter["bin_filas_espaciales"] = [
    {
        "articulo": "7700149213509",
        "descripcion_ocr": "Cbta Lat Izq Tras 300Rally Mp",
        "cantidad_documento": 38.0,  # lectura geométrica errada
        "fuente": "BIN_ESPACIAL",
    },
    {
        "articulo": "7700999999999",
        "descripcion_ocr": "Articulo ajeno al BIN",
        "cantidad_documento": 999.0,  # código inventado/ajeno
        "fuente": "BIN_ESPACIAL",
    },
]
completar_con_catalogo(resultado_inter, catalogo_inter)
obtenido_inter = {
    x["articulo"]: float(x["cantidad_documento"])
    for x in resultado_inter["lineas"]
}
check("BIN foto no inventa códigos fuera del texto validado",
      "7700999999999" not in obtenido_inter, str(obtenido_inter))
check("BIN foto conserva exactamente los 14 códigos del documento",
      set(obtenido_inter) == set(esperado_inter), str(obtenido_inter))
check("BIN foto prioriza cantidades texto/NONE-NONE sobre geometría errada",
      obtenido_inter == esperado_inter, str(obtenido_inter))

print("\n=== 10A1. MISTRAL DOCUMENT AI (MOCK) ===")
_mistral_original = dai._mistral_document_ai
try:
    dai._mistral_document_ai = lambda nombre, data, mime=None: {
        "ok": True,
        "texto": (
            "MOVIMIENTO BIN A BIN\n"
            "Proveedor | Código | Descripción | Cantidad | Serial | Lote | Desde | Hasta"
        ),
        "tipo_documento": "BIN_A_BIN",
        "referencia": "BIN-IA-001",
        "fecha": "18/09/2026",
        "modelo": "mistral-ocr-4-1",
        "usage_info": {"pages_processed": 1},
        "filas": [
            {
                "proveedor": "CHONGQING-012",
                "codigo": "7700149213509",
                "descripcion": "Cbta Lat Izq Tras 300Rally Mp",
                "cantidad": 98,
                "serial": "NONE", "lote": "NONE",
                "desde": "WSERE PINT 1 1 1",
                "hasta": "WSERE WINT 1 1 1",
            },
            {
                "proveedor": "CHONGQING-012",
                "codigo": "7700149649926",
                "descripcion": "Cubta Fron Sup Der SR1-A MP",
                "cantidad": 120,
                "serial": "NONE", "lote": "NONE",
                "desde": "WSERE PINT 1 1 1",
                "hasta": "WSERE WINT 1 1 1",
            },
            # El servicio puede devolver algo que no existe en el maestro.
            # Debe descartarse localmente y nunca mostrarse al usuario.
            {
                "proveedor": "X",
                "codigo": "9999999999999",
                "descripcion": "Inventado",
                "cantidad": 777,
                "serial": "NONE", "lote": "NONE",
                "desde": "X", "hasta": "Y",
            },
        ],
    }

    analisis_ia = analizar_documento(
        "foto_bin.jpg", b"BYTES-DE-PRUEBA", "image/jpeg")
    catalogo_ia = {
        "7700149213509": "Cbta Lat Izq Tras 300Rally Mp",
        "7700149649926": "Cubta Fron Sup Der SR1-A MP",
    }
    completar_con_catalogo(analisis_ia, catalogo_ia)
    cantidades_ia = {
        x["articulo"]: float(x["cantidad_documento"])
        for x in analisis_ia["lineas"]
    }
    check("Mistral AI clasifica BIN automáticamente",
          analisis_ia.get("origen_sugerido") == "BIN_A_BIN",
          str(analisis_ia.get("origen_sugerido")))
    check("Mistral AI conserva cantidades 98/120",
          cantidades_ia == {
              "7700149213509": 98.0,
              "7700149649926": 120.0,
          }, str(cantidades_ia))
    check("Mistral AI descarta código que no existe en maestro",
          "9999999999999" not in cantidades_ia, str(cantidades_ia))
    check("Mistral AI conserva DESDE/HASTA",
          all(
              x.get("ubicacion_desde") == "WSERE PINT 1 1 1"
              and x.get("ubicacion_hasta") == "WSERE WINT 1 1 1"
              for x in analisis_ia["lineas"]
          ), str(analisis_ia["lineas"]))
finally:
    dai._mistral_document_ai = _mistral_original

print("\n=== 10A0A. DOCUMENTO MULTIFOTO / MULTIARCHIVO ===")
multi_1 = {
    "_archivo_nombre": "pagina_1.jpg",
    "texto": "MOVIMIENTO BIN A BIN pagina 1",
    "origen_sugerido": "BIN_A_BIN",
    "origen_confianza": 0.99,
    "referencia": {"valor": "BIN-MULTI-001", "confianza": 0.99},
    "fecha": {"valor": "2026-09-17", "confianza": 0.99},
    "bin_desde_canon": "WSERE PSER 1 1 1",
    "bin_hasta_canon": "WSERE WSER VIPI NTAR TE",
    "proveedor_destino_coincide": True,
    "ocr_ok": True,
    "confianza_texto": 0.96,
    "metodo": "OPENAI_GPT5_NANO",
    "entrada_ia": True,
    "ia_fuente": "OPENAI_GPT5_NANO",
    "ia_modelo": "gpt-5-nano-2025-08-07",
    "tiempos_procesamiento": {"total_s": 2.0},
    "lineas": [
        {
            "articulo": "7700149603447",
            "descripcion": "Cubierta manubrio JetEvo Mp",
            "cantidad_documento": 60,
            "cantidad_fisica": 0,
            "serial": "NONE", "lote": "NONE",
            "ubicacion_desde": "WSERE PSER 1 1 1",
            "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
        },
        {
            "articulo": "7700149603980",
            "descripcion": "Cubta Frontal Der JetEvo Mp",
            "cantidad_documento": 240,
            "cantidad_fisica": 0,
            "serial": "NONE", "lote": "NONE",
            "ubicacion_desde": "WSERE PSER 1 1 1",
            "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
        },
    ],
}
multi_2 = {
    "_archivo_nombre": "pagina_2.jpg",
    "texto": "MOVIMIENTO BIN A BIN pagina 2",
    "origen_sugerido": "BIN_A_BIN",
    "origen_confianza": 0.98,
    "referencia": {"valor": "BIN-MULTI-001", "confianza": 0.98},
    "fecha": {"valor": "2026-09-17", "confianza": 0.98},
    "bin_desde_canon": "WSERE PSER 1 1 1",
    "bin_hasta_canon": "WSERE WSER VIPI NTAR TE",
    "proveedor_destino_coincide": True,
    "ocr_ok": True,
    "confianza_texto": 0.94,
    "metodo": "OPENAI_GPT5_NANO",
    "entrada_ia": True,
    "ia_fuente": "OPENAI_GPT5_NANO",
    "ia_modelo": "gpt-5-nano-2025-08-07",
    "tiempos_procesamiento": {"total_s": 3.0},
    "lineas": [
        {
            "articulo": "7700149603447",
            "descripcion": "Cubierta manubrio JetEvo Mp",
            "cantidad_documento": 40,
            "cantidad_fisica": 0,
            "serial": "NONE", "lote": "NONE",
            "ubicacion_desde": "WSERE PSER 1 1 1",
            "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
        },
        {
            "articulo": "7700149604611",
            "descripcion": "Cubta Frontal Izq JetEvo Mp",
            "cantidad_documento": 180,
            "cantidad_fisica": 0,
            "serial": "NONE", "lote": "NONE",
            "ubicacion_desde": "WSERE PSER 1 1 1",
            "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
        },
    ],
}
multi = _consolidar_extracciones([multi_1, multi_2])
multi_qty = {
    x["articulo"]: float(x["cantidad_documento"])
    for x in multi["lineas"]
}
check("Multiarchivo consolida páginas como un solo documento",
      multi["soportes_count"] == 2)
check("Multiarchivo suma artículo repetido entre páginas",
      multi_qty["7700149603447"] == 100.0, str(multi_qty))
check("Multiarchivo conserva artículos exclusivos de cada página",
      multi_qty["7700149603980"] == 240.0
      and multi_qty["7700149604611"] == 180.0, str(multi_qty))
check("Multiarchivo conserva referencia única por consenso",
      multi["referencia"]["valor"] == "BIN-MULTI-001",
      str(multi["referencia"]))
check("Multiarchivo suma tiempo total de procesamiento",
      multi["tiempos_procesamiento"]["total_s"] == 5.0,
      str(multi["tiempos_procesamiento"]))
check("Multiarchivo sin conflicto no genera alerta",
      not multi["advertencias_consolidacion"],
      str(multi["advertencias_consolidacion"]))

multi_conf = _consolidar_extracciones([
    multi_1,
    dict(multi_2, referencia={"valor": "BIN-OTRO", "confianza": 0.90}),
])
check("Multiarchivo detecta referencia contradictoria",
      any("referencia" in x for x in multi_conf["advertencias_consolidacion"]),
      str(multi_conf["advertencias_consolidacion"]))

check("Multiarchivo conflictivo bloquea consolidación",
      multi_conf["conflicto_consolidacion_bloqueante"] is True,
      str(multi_conf.get("conflicto_consolidacion_bloqueante")))

# Los originales deben quedar juntos en la trazabilidad, no solo los datos OCR.
soporte_a = SimpleNamespace(
    name="pagina_1.jpg", type="image/jpeg",
    getvalue=lambda: b"FOTO-PAGINA-1")
soporte_b = SimpleNamespace(
    name="pagina_2.jpg", type="image/jpeg",
    getvalue=lambda: b"FOTO-PAGINA-2")
with session_scope() as s:
    arch_multi = _guardar_soportes_consolidados(
        s, [soporte_a, soporte_b], "qa@akt.com", "abc123")
    zip_bytes = sv.leer_archivo(arch_multi)
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        nombres_zip = set(zf.namelist())
        contenido_manifest = zf.read("MANIFIESTO.txt").decode("utf-8")
    check("Multiarchivo guarda las dos fotos originales en ZIP",
          any("pagina_1.jpg" in x for x in nombres_zip)
          and any("pagina_2.jpg" in x for x in nombres_zip),
          str(sorted(nombres_zip)))
    check("Multiarchivo conserva manifiesto SHA-256",
          "sha256=" in contenido_manifest
          and "pagina_1.jpg" in contenido_manifest
          and "pagina_2.jpg" in contenido_manifest)

print("\n=== 10A0B. FOTO REAL BIN2717603 ===")
texto_bin2717603 = """
MOVIMIENTO BIN A BIN
Id de Bin: BIN2717603 Fecha: 18/09/2026
Fecha Transacción: 2026-09-17-12.33.26
Proveedor Cédigo Descripcién Gantidad Serial Lote Desde Hasta
SANYANG IN-0017700149603447 Cubierta manubrio JetEvo Mp 60 NONE NONE WSERE PSER 1 1 1 WSERE WSER VIPI NTAR TE
SANVANG IN-007700149603980 Cubta Frontal Der JetEvo Mp 240 NONE NONE WSERE PSER 1 1 1 WSERE WSER VIPI NIAR TE
SANYANG IN-0077700149604611 Cubta Frontal Izq JetEvo Mp 240 NONE NONE, WSERE PSER 111 WSERE WSER VIPI NTAR TE
SANYANG IN-0017700149604543 Cubta Frontal Jetvo Mp 180 NONE NONE WSERE PSER 1 1.1 WSERE WSER VIPI NTAR TE
SANYANG IN-007700149422758 Cubta Frontal RX Mp 230 NONE NONE WSERE PSER 1 11 WSERE WSER VIPI NTAR TE
SANYANG IN-001700149604673 Cubta Lat Piso Der JetBvo Mp 72 NONE NONE WSERE PSER 1 11 WSERE NSER VIPI NTAR TE
SANYANG IN-001/700149604161 Cubta Lat Piso Izq JetEvo Mp 156 NONE NONE WSERE PSER 1 1 1 WSERE HSER VIPI NTAR TE
SANYANG IN-0077700149604819 Cubta Tras Der Jetevo Mp uaz NONE | NONE WSBRE PSER-1 1 1 WSERE WSER VIPI NTAR T2
SANYANG IN-000700149604710. Cubta Tras Der int Jetzvo Mp 90 NONE NONE WSERE PSER 111 NSERE WSER VIPY NTAR TE
SANYANG IN-0077700149604802 Cubta Tras Izq Jetevo Mp. 180 SNONE SNONES WSERE PSER 1 1 1 WSERE WSER VIPI NTAR TE
"""
catalogo_bin2717603 = {
    "7700149603447": "Cubierta manubrio JetEvo Mp",
    "7700149603980": "Cubta Frontal Der JetEvo Mp",
    "7700149604611": "Cubta Frontal Izq JetEvo Mp",
    "7700149604543": "Cubta Frontal JetEvo Mp",
    "7700149422758": "Cubta Frontal RX Mp",
    "7700149604673": "Cubta Lat Piso Der JetEvo Mp",
    "7700149604161": "Cubta Lat Piso Izq JetEvo Mp",
    "7700149604819": "Cubta Tras Der JetEvo Mp",
    "7700149604710": "Cubta Tras Der int JetEvo Mp",
    "7700149604802": "Cubta Tras Izq JetEvo Mp",
}
esperado_bin2717603 = {
    "7700149603447": 60.0,
    "7700149603980": 240.0,
    "7700149604611": 240.0,
    "7700149604543": 180.0,
    "7700149422758": 230.0,
    "7700149604673": 72.0,
    "7700149604161": 156.0,
    "7700149604819": 142.0,
    "7700149604710": 90.0,
    "7700149604802": 180.0,
}
resultado_bin2717603 = estructurar(texto_bin2717603, 0.95)
resultado_bin2717603["confianza_texto"] = 0.95
resultado_bin2717603["metodo"] = "TESSERACT/BIN_ESTRUCTURADO"
resultado_bin2717603["entrada_imagen"] = True
resultado_bin2717603["origen_sugerido"] = "BIN_A_BIN"
resultado_bin2717603["bin_filas_espaciales"] = [{
    # Caso real de foto: Proveedor + Código pueden quedar pegados.
    "articulo": "SANYANGIN-0017700149604819",
    "descripcion_ocr": "Cubta Tras Der JetEvo Mp",
    "cantidad_documento": 142.0,
    "cantidad_fisica": 0.0,
    "serial": "NONE",
    "lote": "NONE",
    "ubicacion_desde": "WSERE PSER 1 1 1",
    "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
    "fuente": "BIN_ESPACIAL",
}]
completar_con_catalogo(resultado_bin2717603, catalogo_bin2717603)
cantidades_bin2717603 = {
    x["articulo"]: float(x["cantidad_documento"])
    for x in resultado_bin2717603["lineas"]
}
check("BIN2717603 conserva las 10 referencias",
      set(cantidades_bin2717603) == set(esperado_bin2717603),
      str(cantidades_bin2717603))
recuperadas_maestro = {
    x["articulo"]: float(x["cantidad_documento"])
    for x in dai.lineas_bin_desde_texto_maestro(
        texto_bin2717603, catalogo_bin2717603, 0.95)
}
check("BIN2717603 recupera códigos con un 7 omitido por OCR",
      all(c in recuperadas_maestro for c in (
          "7700149604673", "7700149604161", "7700149604710")),
      str(recuperadas_maestro))
check("BIN2717603 rescata 142 aunque OCR textual lea uaz",
      cantidades_bin2717603 == esperado_bin2717603,
      str(cantidades_bin2717603))
check("BIN2717603 separa código aunque proveedor venga pegado",
      cantidades_bin2717603.get("7700149604819") == 142.0,
      str(cantidades_bin2717603))
check("BIN2717603 usa Fecha Transacción 17/09/2026",
      resultado_bin2717603["fecha"]["valor"] == "2026-09-17",
      str(resultado_bin2717603["fecha"]))

print("\n=== 10A0C. FOTO REAL ROTADA / CANTIDAD DEFORMADA ===")
try:
    import cv2
    import numpy as np
    import pytesseract

    # Simula una foto tomada verticalmente de un BIN cuya tabla real es
    # horizontal. La selección debe rotarla antes del parser geométrico.
    img_portrait = np.full((900, 600, 3), 255, dtype=np.uint8)
    ok_png, buf_png = cv2.imencode(".png", img_portrait)
    foto_rotada_bytes = buf_png.tobytes() if ok_png else b""

    ocr_original = dai._ocr_tesseract
    try:
        texto_bin_orientado = (
            "MOVIMIENTO BIN A BIN\n"
            "Proveedor Código Descripción Cantidad Serial Lote Desde Hasta\n"
            "SANYANG 7700149603447 Pieza A 60 NONE NONE WSERE PSER WSERE WSER\n"
            "SANYANG 7700149603980 Pieza B 240 NONE NONE WSERE PSER WSERE WSER"
        )

        def _ocr_por_orientacion(im):
            h, w = im.shape[:2]
            if w > h:
                return texto_bin_orientado, 0.99
            return "texto lateral ilegible", 0.10

        dai._ocr_tesseract = _ocr_por_orientacion
        dai._orientar_bin_bytes.cache_clear()
        _, grados_test, texto_test, _ = dai._orientar_bin_bytes(
            foto_rotada_bytes)
        check("Foto BIN lateral se rota automáticamente",
              abs(grados_test) == 90, str(grados_test))
        check("Foto rotada recupera estructura BIN",
              dai._score_bin_texto(texto_test)[1] == 2,
              texto_test)
    finally:
        dai._ocr_tesseract = ocr_original
        dai._orientar_bin_bytes.cache_clear()

    # Caso observado en foto real: una cantidad como 142 puede ser leída por
    # OCR general como letras (p.ej. 'uaz'). Al estar geométricamente dentro de
    # la columna Cantidad debe releerse con whitelist numérica.
    fake_qty = SimpleNamespace(
        txts=["Cantidad", "Serial", "Lote", "uaz", "NONE", "NONE"],
        scores=[0.99] * 6,
        boxes=[
            [[100, 20], [180, 20], [180, 45], [100, 45]],
            [[220, 20], [280, 20], [280, 45], [220, 45]],
            [[320, 20], [370, 20], [370, 45], [320, 45]],
            [[120, 80], [175, 80], [175, 108], [120, 108]],
            [[225, 80], [280, 80], [280, 108], [225, 108]],
            [[325, 80], [380, 80], [380, 108], [325, 108]],
        ],
    )
    qty_img = np.full((160, 440, 3), 255, dtype=np.uint8)
    tess_img_original = pytesseract.image_to_string
    try:
        pytesseract.image_to_string = lambda *args, **kwargs: "142"
        refinadas = dai._refinar_cantidades_bin(
            qty_img, fake_qty, list(fake_qty.txts))
        check("Cantidad deformada uaz se relee como 142",
              refinadas[3] == "142", str(refinadas))
    finally:
        pytesseract.image_to_string = tess_img_original
except Exception as e:
    check("Prueba foto rotada/cantidad deformada disponible", False, str(e))

print("\n=== 10A1B. OPENAI GPT-5 NANO VISION (MOCK) ===")
_openai_original = dai._openai_document_ai
try:
    dai._openai_document_ai = lambda nombre, data, mime=None: {
        "ok": True,
        "texto": json.dumps({
            "tipo_documento": "BIN_A_BIN",
            "referencia": "BIN-NANO-001",
            "fecha": "18/09/2026",
            "filas": []
        }),
        "tipo_documento": "BIN_A_BIN",
        "referencia": "BIN-NANO-001",
        "fecha": "18/09/2026",
        "modelo": "gpt-5-nano-2025-08-07",
        "fuente": "OPENAI_GPT5_NANO",
        "usage_info": {"input_tokens": 1200, "output_tokens": 250},
        "filas": [
            {
                "proveedor": "CHONGQING-012",
                "codigo": "7700149213509",
                "descripcion": "Cbta Lat Izq Tras 300Rally Mp",
                "cantidad": 98,
                "serial": "NONE", "lote": "NONE",
                "desde": "WSERE PINT 1 1 1",
                "hasta": "WSERE WINT 1 1 1",
            },
            {
                "proveedor": "CHONGQING-012",
                "codigo": "7700149649926",
                "descripcion": "Cubta Fron Sup Der SR1-A MP",
                "cantidad": 120,
                "serial": "NONE", "lote": "NONE",
                "desde": "WSERE PINT 1 1 1",
                "hasta": "WSERE WINT 1 1 1",
            },
            {
                "proveedor": "X",
                "codigo": "9999999999999",
                "descripcion": "No está en maestro",
                "cantidad": 777,
                "serial": "NONE", "lote": "NONE",
                "desde": "X", "hasta": "Y",
            },
        ],
    }

    analisis_nano = analizar_documento(
        "foto_bin_nano.jpg", b"BYTES-DE-PRUEBA", "image/jpeg")
    catalogo_nano = {
        "7700149213509": "Cbta Lat Izq Tras 300Rally Mp",
        "7700149649926": "Cubta Fron Sup Der SR1-A MP",
    }
    completar_con_catalogo(analisis_nano, catalogo_nano)
    cantidades_nano = {
        x["articulo"]: float(x["cantidad_documento"])
        for x in analisis_nano["lineas"]
    }
    check("GPT-5 nano queda como fuente IA principal",
          analisis_nano.get("ia_fuente") == "OPENAI_GPT5_NANO",
          str(analisis_nano.get("ia_fuente")))
    check("GPT-5 nano clasifica BIN automáticamente",
          analisis_nano.get("origen_sugerido") == "BIN_A_BIN",
          str(analisis_nano.get("origen_sugerido")))
    check("GPT-5 nano conserva cantidades 98/120",
          cantidades_nano == {
              "7700149213509": 98.0,
              "7700149649926": 120.0,
          }, str(cantidades_nano))
    check("GPT-5 nano descarta código fuera del maestro",
          "9999999999999" not in cantidades_nano,
          str(cantidades_nano))
finally:
    dai._openai_document_ai = _openai_original

# Tabla BIN completa: valida geometría de Código/Cantidad/DESDE/HASTA.
def _box(cx, cy, w=100, h=24):
    return [
        [cx-w/2, cy-h/2], [cx+w/2, cy-h/2],
        [cx+w/2, cy+h/2], [cx-w/2, cy+h/2],
    ]

headers_txt = [
    "Proveedor", "Código", "Descripción", "Cantidad",
    "Serial", "Lote", "Desde", "Hasta",
]
xs = [70, 210, 420, 650, 760, 850, 1010, 1260]
row_txt = [
    "CHONGQING-012", "7700149386142", "Carenaje Farola 200DS+ Mp",
    "179", "NONE", "NONE", "WSERE-PSER",
    "MOTOS-WSERE-WSER-VIPI-NTAR",
]
fake_bin = SimpleNamespace(
    txts=headers_txt + row_txt,
    scores=[0.99] * 16,
    boxes=[_box(x, 30) for x in xs] + [_box(x, 80) for x in xs],
)
filas_bin_geom = _extraer_bin_columnas_resultado(fake_bin)
check("Parser geométrico BIN obtiene una fila", len(filas_bin_geom) == 1,
      str(filas_bin_geom))
if filas_bin_geom:
    fg = filas_bin_geom[0]
    check("Parser geométrico conserva cantidad 179",
          fg["cantidad_documento"] == 179.0, str(fg))
    check("Parser geométrico conserva DESDE",
          fg["ubicacion_desde"] == "WSERE-PSER", str(fg))
    check("Parser geométrico conserva HASTA",
          fg["ubicacion_hasta"] == "MOTOS-WSERE-WSER-VIPI-NTAR", str(fg))

    # Foto BIN: cantidades pueden quedar desplazadas a la izquierda del encabezado
    # por perspectiva. Debe prevalecer el patrón semántico CANTIDAD + NONE + NONE,
    # incluso cuando la geometría pura enviaría la cifra a Descripción.
    headers_skew = [
        "Proveedor", "Código", "Descripción", "Cantidad", "Serial", "Lote", "Desde", "Hasta",
    ]
    xs_skew = [70, 210, 420, 650, 760, 850, 1010, 1260]
    rows_skew = [
        ("CHONGQING-012", "7700149213509", "Cbta Lat Izq Tras 300Rally Mp", "98"),
        ("CHONGQING-012", "7700149649926", "Cubta Fron Sup Der SR1-A MP", "120"),
        ("CHONGQING-012", "7700149649933", "Cubta Int Cent Inf SR1-A MP", "150"),
        ("CHONGQING-012", "7700149648813", "Cubta Lat inf Der SR1-A MP", "100"),
        ("CHONGQING-012", "7700149212359", "Guardabarro Del 300Rally Mp", "72"),
    ]
    txt_skew = list(headers_skew)
    box_skew = [_box(x, 30) for x in xs_skew]
    for n, (prov, cod, desc, qty) in enumerate(rows_skew):
        y = 80 + n * 50
        # Cantidad en x=620: visualmente pertenece a Cantidad, pero queda a la
        # izquierda del x0 del encabezado 650 por la perspectiva de la foto.
        vals = [prov, cod, desc, qty, "NONE", "NONE", "WSERE PINT 1 1 1", "WSERE WINT 1 1 1"]
        xs_row = [70, 210, 420, 620, 760, 850, 1010, 1260]
        txt_skew.extend(vals)
        box_skew.extend([_box(x, y) for x in xs_row])
    
    fake_bin_skew = SimpleNamespace(
        txts=txt_skew,
        boxes=box_skew,
        scores=[0.99] * len(txt_skew),
    )
    filas_skew = _extraer_bin_columnas_resultado(fake_bin_skew)
    qty_skew = {x["articulo"]: x["cantidad_documento"] for x in filas_skew}
    esperado_skew = {
        "7700149213509": 98.0,
        "7700149649926": 120.0,
        "7700149649933": 150.0,
        "7700149648813": 100.0,
        "7700149212359": 72.0,
    }
    check("Foto BIN desplazada conserva cantidades por patrón NONE/NONE",
          qty_skew == esperado_skew, str(qty_skew))


    resumen_bin = resumir_ubicaciones_bin([
        fg,
        {**fg, "articulo": "7700149386173"},
        {**fg, "articulo": "7700149385725"},
    ])
    check("BIN resume DESDE una vez por documento",
          resumen_bin["bin_ubicacion_desde"] == "WSERE-PSER", str(resumen_bin))
    check("BIN resume HASTA una vez por documento",
          resumen_bin["bin_ubicacion_hasta"] == "MOTOS-WSERE-WSER-VIPI-NTAR",
          str(resumen_bin))
    check("BIN con ubicaciones iguales es consistente",
          resumen_bin["bin_desde_consistente"]
          and resumen_bin["bin_hasta_consistente"])

    inconsistente = resumir_ubicaciones_bin([
        fg,
        {**fg, "articulo": "OTRO", "ubicacion_hasta": "OTRO-HASTA"},
    ])
    check("BIN detecta HASTA inconsistente a nivel documento",
          not inconsistente["bin_hasta_consistente"], str(inconsistente))

# PDF/ERP puede traer dos capas de texto superpuestas. Debe deduplicar
# cada palabra sin borrar repeticiones legítimas como "1 1 1".
def _rect(x0, x1, cy, h=14):
    return [
        [x0, cy-h/2], [x1, cy-h/2],
        [x1, cy+h/2], [x0, cy+h/2],
    ]

dup_txts = []
dup_boxes = []
header_defs = [
    ("Proveedor", 15, 85), ("Código", 120, 175), ("Descripción", 260, 345),
    ("Cantidad", 540, 600), ("Serial", 635, 680), ("Lote", 775, 810),
    ("Desde", 900, 945), ("Hasta", 1120, 1160),
]
for txt_h, x0_h, x1_h in header_defs:
    dup_txts.append(txt_h)
    dup_boxes.append(_rect(x0_h, x1_h, 30))

row_defs = [
    ("CHONGQING-012", 15, 105),
    ("7700149453691", 120, 210),
    ("Base", 260, 285), ("Silla", 290, 315), ("200DS+", 320, 360), ("Mp", 365, 380),
    ("80", 545, 558),
    ("NONE", 640, 675),
    ("NONE", 780, 815),
    ("WSERE", 920, 955), ("PUMO", 965, 995),
    ("1", 1005, 1010), ("1", 1018, 1023), ("1", 1031, 1036),
    ("WSERE", 1125, 1160), ("WUMO", 1170, 1200),
    ("1", 1210, 1215), ("1", 1223, 1228), ("1", 1236, 1241),
]
for txt_v, x0_v, x1_v in row_defs:
    # Capa visible.
    dup_txts.append(txt_v)
    dup_boxes.append(_rect(x0_v, x1_v, 80))
    # Capa duplicada casi en la misma coordenada.
    dup_txts.append(txt_v)
    dup_boxes.append(_rect(x0_v + 0.4, x1_v + 0.4, 80.2))

fake_pdf_dup = SimpleNamespace(
    txts=dup_txts,
    boxes=dup_boxes,
    scores=[1.0] * len(dup_txts),
)
filas_dup = _extraer_bin_columnas_resultado(fake_pdf_dup)
check("Parser elimina capas PDF duplicadas", len(filas_dup) == 1, str(filas_dup))
if filas_dup:
    fd = filas_dup[0]
    check("PDF duplicado conserva cantidad 80",
          fd["cantidad_documento"] == 80.0, str(fd))
    check("PDF duplicado deja DESDE exacto sin repetir",
          fd["ubicacion_desde"] == "WSERE PUMO 1 1 1", str(fd))
    check("PDF duplicado deja HASTA exacto sin repetir",
          fd["ubicacion_hasta"] == "WSERE WUMO 1 1 1", str(fd))

sample_bin_real = """
Proveedor Código Descripción Cantidad Serial Lote Desde Hasta
CHONGQING-012 7700149386142 Carenaje Farola 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149386173 Cubierta Tras 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149385725 Cubta Der Tanq Gas 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149385718 Cubta Izq Tanq Gas 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149386081 Guardabarro Del Frontal Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
SANYANG IN-001 7700149603447 Cubierta manubrio JetEvo Mp 60 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
SANYANG IN-001 7700149603980 Cubta Frontal Der JetEvo Mp 60 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
"""
catalogo_bin = {
    "7700149386142": "Carenaje Farola 200DS+ Mp",
    "7700149386173": "Cubierta Tras 200DS+ Mp",
    "7700149385725": "Cubta Der Tanq Gas 200DS+ Mp",
    "7700149385718": "Cubta Izq Tanq Gas 200DS+ Mp",
    "7700149386081": "Guardabarro Del Frontal Mp",
    "7700149603447": "Cubierta manubrio JetEvo Mp",
    "7700149603980": "Cubta Frontal Der JetEvo Mp",
}
bin_lines = lineas_bin_desde_texto(sample_bin_real, catalogo_bin, 0.99)
bin_qty = {x["articulo"]: x["cantidad_documento"] for x in bin_lines}
check("BIN real recupera todas las referencias de muestra",
      len(bin_qty) == len(catalogo_bin), str(bin_qty))
check("BIN real conserva 179 en grupo CHONGQING",
      all(bin_qty.get(k) == 179.0 for k in list(catalogo_bin)[:5]), str(bin_qty))
check("BIN real conserva 60 en grupo SANYANG",
      bin_qty.get("7700149603447") == 60.0
      and bin_qty.get("7700149603980") == 60.0, str(bin_qty))

# Una lectura heurística errónea (1) debe ser corregida por el parser BIN (179).
resultado_malo = {
    "texto": sample_bin_real,
    "confianza_texto": 0.99,
    "lineas": [{
        "articulo": "7700149386173",
        "descripcion": "Cubierta Tras 200DS+ Mp",
        "cantidad_documento": 1.0,
        "cantidad_fisica": 0.0,
        "fuente": "OCR_HEURISTICO",
    }],
}
corregido = completar_con_catalogo(resultado_malo, catalogo_bin)
q_corregida = next(
    x["cantidad_documento"] for x in corregido["lineas"]
    if x["articulo"] == "7700149386173")
check("Parser BIN corrige cantidad heurística 1 -> 179", q_corregida == 179.0)

resultado_espacial = {
    "texto": sample_bin_real,
    "confianza_texto": 0.99,
    "lineas": [],
    "bin_filas_espaciales": [{
        "articulo": "7700149386142",
        "descripcion_ocr": "Carenaje Farola 200DS+ Mp",
        "cantidad_documento": 179.0,
        "cantidad_fisica": 0.0,
        "serial": "NONE",
        "lote": "NONE",
        "ubicacion_desde": "WSERE PSER 1 1 1",
        "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
        "fuente": "BIN_ESPACIAL",
    }],
}
resultado_espacial = completar_con_catalogo(resultado_espacial, catalogo_bin)
esp = next(x for x in resultado_espacial["lineas"]
           if x["articulo"] == "7700149386142")
check("BIN espacial conserva DESDE",
      esp["ubicacion_desde"] == "WSERE PSER 1 1 1", str(esp))
check("BIN espacial conserva HASTA",
      esp["ubicacion_hasta"] == "WSERE WSER VIPI NTAR TE", str(esp))
check("BIN espacial conserva cantidad",
      esp["cantidad_documento"] == 179.0, str(esp))

print("\n=== 10A2. PDF BIN NATIVO CON COLUMNAS ===")
try:
    import fitz

    pdf_doc = fitz.open()
    page = pdf_doc.new_page(width=1400, height=600)

    # Encabezado general.
    page.insert_text((40, 45), "MOVIMIENTO BIN A BIN", fontsize=13)
    page.insert_text((40, 75), "Id de Bin: BIN2654425", fontsize=10)
    page.insert_text((740, 75), "Fecha: 18/09/2026", fontsize=10)

    # Cabeceras en posiciones equivalentes al BIN real.
    headers_pdf = [
        ("Proveedor", 20), ("Código", 120), ("Descripción", 260),
        ("Cantidad", 540), ("Serial", 635), ("Lote", 775),
        ("Desde", 925), ("Hasta", 1120),
    ]
    for txt_h, x_h in headers_pdf:
        page.insert_text((x_h, 140), txt_h, fontsize=9)

    rows_pdf = [
        ("CHONGQING-012", "7700149453691", "Base Silla 200DS+ Mp", "80",
         "NONE", "NONE", "WSERE PUMO 1 1 1", "WSERE WUMO 1 1 1"),
        ("SANYANG IN-001", "7700149422819", "Base Sillin RX Mp", "156",
         "NONE", "NONE", "WSERE PUMO 1 1 1", "WSERE WUMO 1 1 1"),
        ("SANYANG IN-001", "7705946161657", "Base silla tras 125SC-R PRO Mp", "288",
         "NONE", "NONE", "WSERE PUMO 1 1 1", "WSERE WUMO 1 1 1"),
        ("SANYANG IN-001", "7705946161923", "Base sillin Del SC-R PRO Mp", "288",
         "NONE", "NONE", "WSERE PUMO 1 1 1", "WSERE WUMO 1 1 1"),
    ]
    # Cantidad se coloca ligeramente a la izquierda del encabezado para
    # reproducir el alineado real del ERP. Cada fila se imprime dos veces con
    # un pequeño desplazamiento para simular las capas duplicadas del PDF real.
    xs_pdf = [20, 120, 260, 526, 635, 775, 925, 1120]
    # Reproduce el fallo real: desde la segunda fila, Cantidad y ubicaciones
    # tienen baselines distintos al Código/Descripción.
    offsets_por_fila = [
        [0, 0, 0, 0,    0, 0, 0,    0],
        [0, 0, 0, 7.5,  0, 0, 4.2,  4.2],
        [0, 0, 0, 12.5, 0, 0, -3.8, -3.8],
        [0, 0, 0, -12.5,0, 0, 5.5,  5.5],
    ]
    y_pdf = 175
    for idx_row, row in enumerate(rows_pdf):
        for idx_col, (txt_cell, x_cell) in enumerate(zip(row, xs_pdf)):
            y_cell = y_pdf + offsets_por_fila[idx_row][idx_col]
            page.insert_text((x_cell, y_cell), txt_cell, fontsize=8)
            page.insert_text((x_cell + 1.8, y_cell + 0.9), txt_cell, fontsize=8)
        y_pdf += 28

    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()

    filas_pdf = extraer_bin_columnas_pdf(pdf_bytes)
    check("PDF BIN obtiene las 4 filas reales", len(filas_pdf) == 4, str(filas_pdf))
    qty_pdf = {x["articulo"]: x["cantidad_documento"] for x in filas_pdf}
    check("PDF BIN conserva cantidades reales 80/156/288/288",
          qty_pdf.get("7700149453691") == 80.0
          and qty_pdf.get("7700149422819") == 156.0
          and qty_pdf.get("7705946161657") == 288.0
          and qty_pdf.get("7705946161923") == 288.0,
          str(qty_pdf))
    check("PDF BIN reconoce DESDE",
          all(x["ubicacion_desde"] == "WSERE PUMO 1 1 1" for x in filas_pdf),
          str(filas_pdf))
    check("PDF BIN reconoce HASTA",
          all(x["ubicacion_hasta"] == "WSERE WUMO 1 1 1" for x in filas_pdf),
          str(filas_pdf))

    analisis_pdf = analizar_documento(
        "BIN2654425.pdf", pdf_bytes, "application/pdf")
    check("Analizador PDF usa geometría BIN",
          len(analisis_pdf.get("bin_filas_espaciales") or []) == 4,
          str(analisis_pdf.get("bin_filas_espaciales")))
    check("Analizador PDF resume DESDE documental",
          analisis_pdf.get("bin_ubicacion_desde") == "WSERE PUMO 1 1 1",
          str(analisis_pdf.get("bin_desde_valores")))
    check("Analizador PDF resume HASTA documental",
          analisis_pdf.get("bin_ubicacion_hasta") == "WSERE WUMO 1 1 1",
          str(analisis_pdf.get("bin_hasta_valores")))
except Exception as e:
    check("PDF BIN nativo ejecuta sin excepción", False, f"{type(e).__name__}: {e}")

print("\n=== 10A3. PDF BIN REAL: PROVEEDOR Y CÓDIGO PEGADOS SIN ESPACIO ===")
# Reproduce EXACTAMENTE la falla observada en producción con el documento
# real BIN2654425_UMO.pdf: el ERP origen no deja espacio entre el código del
# proveedor y el código de artículo cuando ambos caen en la misma celda
# ("SANYANG IN-0017700149422819" llega como una sola palabra de PyMuPDF).
# La prueba sintética anterior (10A2) inserta proveedor y código como dos
# llamadas independientes en columnas separadas y por eso NUNCA ejercitó
# este camino de código: pasaba en pruebas y seguía fallando con el PDF
# real. Aquí se reconstruye el documento con las coordenadas de encabezado
# reales (obtenidas de get_text('words'/'rawdict') sobre el PDF real) y se
# concatena proveedor+código en una sola inserción de texto, tal como
# aparece en el reporte real. También incluye la fila de pie de página
# "Cantidad Total 812.00", que en el documento real cae dentro del rango X
# de la columna Código y generaba una fila fantasma con cantidad 0.
try:
    import fitz

    pdf_doc = fitz.open()
    page = pdf_doc.new_page(width=800, height=600)

    page.insert_text((383, 90), "MOVIMIENTO BIN A BIN", fontsize=10)
    page.insert_text((24, 118), "Id de Bin: BIN2654425", fontsize=8)

    # Coordenadas X reales de cada encabezado, tomadas del PDF real.
    headers_reales = [
        ("Proveedor", 11.52), ("Código", 76.32), ("Descripción", 154.08),
        ("Cantidad", 313.92), ("Serial", 365.76), ("Lote", 452.16),
        ("Desde", 538.56), ("Hasta", 650.88),
    ]
    for txt_h, x_h in headers_reales:
        page.insert_text((x_h, 163), txt_h, fontsize=8)

    def _fila_real(y, proveedor, codigo, desc, cant, pegado):
        """pegado=True reproduce el defecto: proveedor+código en una sola
        inserción de texto, sin espacio, como en el documento real."""
        if pegado:
            page.insert_text((11.52, y), proveedor + codigo,
                             fontsize=8, fontname="courier")
        else:
            page.insert_text((11.52, y), proveedor, fontsize=8, fontname="courier")
            page.insert_text((76.32, y), codigo, fontsize=8, fontname="courier")
        page.insert_text((154.08, y), desc, fontsize=8, fontname="courier")
        page.insert_text((313.92, y), cant, fontsize=8, fontname="courier")
        page.insert_text((365.76, y), "NONE", fontsize=8, fontname="courier")
        page.insert_text((452.16, y), "NONE", fontsize=8, fontname="courier")
        page.insert_text((538.56, y), "WSERE PUMO 1 1 1", fontsize=8, fontname="courier")
        page.insert_text((650.88, y), "WSERE WUMO 1 1 1", fontsize=8, fontname="courier")

    _fila_real(181, "CHONGQING-012 ", "7700149453691",
               "Base Silla 200DS+ Mp", "80", pegado=False)
    _fila_real(190, "SANYANG IN-001", "7700149422819",
               "Base Sillin RX Mp", "156", pegado=True)
    _fila_real(199, "SANYANG IN-001", "7705946161657",
               "Base silla tras 125SC-R PRO Mp", "288", pegado=True)
    _fila_real(208, "SANYANG IN-001", "7705946161923",
               "Base sillin Del SC-R PRO Mp", "288", pegado=True)

    # Pie de página real: cae dentro del rango X de la columna Código.
    page.insert_text((11.52, 532), "Cantidad Total", fontsize=8)
    page.insert_text((97.92, 532), "812.00", fontsize=8)
    page.insert_text((400.32, 559), "End of Report", fontsize=8)

    pdf_bytes_real = pdf_doc.tobytes()
    pdf_doc.close()

    esperado_real = {
        "7700149453691": 80.0,
        "7700149422819": 156.0,
        "7705946161657": 288.0,
        "7705946161923": 288.0,
    }

    filas_real = extraer_bin_columnas_pdf(pdf_bytes_real)
    check("BIN real (pegado): exactamente 4 filas, sin pie de página fantasma",
          len(filas_real) == 4, str(filas_real))
    obtenido_real = {x["articulo"]: x["cantidad_documento"] for x in filas_real}
    check("BIN real (pegado): código no queda contaminado con el proveedor",
          set(obtenido_real.keys()) == set(esperado_real.keys()), str(obtenido_real))
    check("BIN real (pegado): cantidades 80/156/288/288 correctas",
          obtenido_real == esperado_real, str(obtenido_real))
    check("BIN real (pegado): los dos 288 son dos filas distintas",
          sum(1 for x in filas_real if x["cantidad_documento"] == 288.0) == 2,
          str(filas_real))

    catalogo_real = {
        "7700149453691": "Base Silla 200DS+ Mp",
        "7700149422819": "Base Sillin RX Mp",
        "7705946161657": "Base silla tras 125SC-R PRO Mp",
        "7705946161923": "Base sillin Del SC-R PRO Mp",
    }
    analisis_real = analizar_documento(
        "BIN2654425_UMO.pdf", pdf_bytes_real, "application/pdf")
    completar_con_catalogo(analisis_real, catalogo_real)
    lineas_finales = {
        x["articulo"]: x["cantidad_documento"]
        for x in analisis_real.get("lineas", [])
    }
    check("Pipeline completo (analizar_documento + completar_con_catalogo) "
          "produce 80/156/288/288, no 0",
          lineas_finales == esperado_real, str(lineas_finales))
except Exception as e:
    check("BIN real (proveedor+código pegados) ejecuta sin excepción",
          False, f"{type(e).__name__}: {e}")

print("\n=== 10B. OCR REAL SOBRE IMAGEN ===")
try:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1500, 620), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    except Exception:
        font = ImageFont.load_default()

    y = 35
    for linea in (
        "FACTURA FV-3-8619",
        "NIT 900123456-7",
        "ORDEN DE COMPRA OC-2026-0001",
        "FECHA 17/09/2026",
        "CP-A Carenaje Crudo 100",
        "CP-B Calca 55",
    ):
        draw.text((50, y), linea, fill="black", font=font)
        y += 88

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    ocr = analizar_documento("factura_prueba.png", bio.getvalue(), "image/png")
    texto_ocr = (ocr.get("texto") or "").upper()
    check("OCR real devuelve texto", bool(ocr.get("ocr_ok")), ocr.get("diagnostico", ""))
    check("OCR real reconoce FACTURA", "FACTURA" in texto_ocr, texto_ocr[:120])
    check("OCR real reconoce artículo CP-A",
          ("CP-A" in texto_ocr) or ("CP A" in texto_ocr), texto_ocr[:200])
except Exception as e:
    check("OCR real ejecuta sin excepción", False, f"{type(e).__name__}: {e}")

print("\n=== 11. KARDEX ===")
with session_scope() as s:
    malas = 0
    from sqlalchemy import func as F
    for inv in s.query(Inventario).all():
        suma = s.query(F.coalesce(F.sum(MovimientoInventario.cantidad), 0.0)).filter(
            MovimientoInventario.proveedor_id == inv.proveedor_id,
            MovimientoInventario.articulo == inv.articulo,
            MovimientoInventario.ubicacion == inv.ubicacion,
            MovimientoInventario.estado == inv.estado,
            MovimientoInventario.condicion == inv.condicion).scalar()
        if abs(float(suma or 0) - float(inv.cantidad or 0)) > 1e-6:
            malas += 1
    check("Kardex cuadra con saldos", malas == 0, f"{malas} descuadres")

print("\n" + "=" * 70)
print(f"RESULTADO: {len(OK)} OK, {len(FAIL)} fallidas")
if FAIL:
    for x in FAIL:
        print("  FALLÓ:", x)
    sys.exit(1)
print("Flujo integral validado.")
