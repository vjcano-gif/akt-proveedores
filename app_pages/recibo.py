"""Recibo de mercancía: documento -> revisión -> match por línea con OC -> inventario."""
import datetime as dt
import hashlib
import re
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.document_ai import analizar_documento, completar_con_catalogo
from core.models import Articulo, OrdenCompra, Proveedor, Recibo
from core.services import (
    LineaRecibo, ReglaNegocio, confirmar_recibo_simple, crear_recibo,
    guardar_archivo, leer_archivo, match_recibo_lineas,
    ordenes_compra_abiertas, sellar_recibo, sugerir_oc_por_linea,
    validar_bin_a_bin,
)

ORIGENES = {
    "BIN_A_BIN": "BIN a BIN — materia prima cruda desde AKT MOTOS",
    "FACTURA": "Factura — producto desde otro proveedor ORIGEN",
    "REGISTRO": "Registro manual (sin OC)",
}


def render(user):
    ui.encabezado(
        "Recibo de mercancía",
        "Documento → revisión → match por línea contra OC → inventario disponible/restringido")
    t1, t2, t3 = st.tabs([
        "📋 Documentos", "➕ Registrar / leer documento", "🔗 Match por línea con OC"
    ])
    with t1:
        _consulta(user)
    with t2:
        _registrar(user)
    with t3:
        _match(user)


def _consulta(user):
    pid = alcance_proveedor(user)
    c1, c2, c3 = st.columns(3)
    estado = c1.multiselect(
        "Estado", ["BORRADOR", "PENDIENTE_MATCH", "NOVEDAD", "CERRADA"], default=[])
    origen = c2.multiselect("Origen", list(ORIGENES), default=[])
    dias = c3.number_input("Últimos N días", 1, 3650, 365)

    with session_scope() as s:
        q = s.query(Recibo).filter(
            Recibo.creado_en >= dt.datetime.utcnow() - dt.timedelta(days=int(dias)))
        if pid:
            q = q.filter(Recibo.proveedor_id == pid)
        if estado:
            q = q.filter(Recibo.estado.in_(estado))
        if origen:
            q = q.filter(Recibo.origen.in_(origen))
        recibos = q.order_by(Recibo.creado_en.desc()).limit(500).all()

        filas = []
        for r in recibos:
            filas.append({
                "Trazabilidad": r.documento.trz if r.documento else "",
                "Origen": r.origen,
                "Referencia": r.documento.referencia if r.documento else "",
                "Transformador": r.proveedor.nombre if r.proveedor else "",
                "Proveedor origen": r.proveedor_origen.nombre if r.proveedor_origen else "",
                "Estado": r.estado,
                "Líneas": len(r.lineas),
                "Cantidad física": sum(float(l.cantidad_fisica or 0) for l in r.lineas),
                "Reproceso": "Sí" if r.es_reproceso else "",
                "Fecha": r.creado_en,
                "_id": r.id,
            })

    if not filas:
        st.info("No hay recibos con ese filtro.")
        return

    df = pd.DataFrame(filas)
    st.dataframe(
        df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
        column_config={"Fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm")})
    st.download_button(
        "Exportar a Excel", ui.exportar_excel({"recibos": df.drop(columns=["_id"])}),
        "recibos.xlsx", key="exp_rec")

    sel = st.selectbox("Ver detalle", ["—"] + [x["Trazabilidad"] for x in filas])
    if sel == "—":
        return
    rid = next(x["_id"] for x in filas if x["Trazabilidad"] == sel)

    with session_scope() as s:
        r = s.get(Recibo, rid)
        st.markdown(
            f"**{r.documento.trz}** · {ORIGENES.get(r.origen, r.origen)} · "
            + ui.pill(r.estado), unsafe_allow_html=True)
        c = st.columns(5)
        c[0].metric("Transformador", r.proveedor.codigo if r.proveedor else "—")
        c[1].metric("Proveedor origen", r.proveedor_origen.codigo if r.proveedor_origen else "—")
        c[2].metric("Referencia", r.documento.referencia or "—")
        c[3].metric("Fecha documento", str(r.documento.fecha_documento))
        c[4].metric("Antigüedad", f"{r.documento.antiguedad_dias} días")

        detalle = []
        for l in r.lineas:
            detalle.append({
                "Artículo": l.articulo, "Descripción": l.descripcion,
                "Cant. documento": l.cantidad_documento, "Cant. física": l.cantidad_fisica,
                "OC": l.orden_compra.numero if l.orden_compra else "",
                "Aceptado": l.cantidad_match or 0,
                "Match": l.estado_match or "",
                "Lote": l.lote,
            })
        st.dataframe(pd.DataFrame(detalle), use_container_width=True, hide_index=True)

        if r.documento.archivo:
            a = r.documento.archivo
            contenido = leer_archivo(a)
            if contenido:
                st.download_button(
                    f"Descargar soporte: {a.nombre}", contenido,
                    file_name=a.nombre, key=f"dl_{rid}")

        if (r.origen == "FACTURA" and r.estado == "BORRADOR"
                and puede(user, "recibo_sellar")
                and (not pid or r.proveedor_id == pid)):
            st.info(
                "El proveedor de transformación debe certificar la recepción. "
                "Después el documento pasa a PENDIENTE_MATCH; aún no afecta inventario.")
            if st.button("Sellar / certificar recepción", type="primary", key=f"sl_{rid}"):
                try:
                    sellar_recibo(s, rid, user["email"])
                    ui.ok("Recepción certificada. Quedó PENDIENTE_MATCH.")
                    st.rerun()
                except ReglaNegocio as e:
                    ui.err(str(e))


def _nit_normalizado(valor):
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


def _fecha_ocr(extr):
    raw = str(((extr or {}).get("fecha") or {}).get("valor") or "").strip()
    hoy_colombia = dt.datetime.now(ZoneInfo("America/Bogota")).date()
    if not raw:
        return hoy_colombia
    try:
        if len(raw) >= 10 and raw[:4].isdigit() and raw[4] in "-/":
            ts = pd.to_datetime(raw, yearfirst=True, errors="raise")
        else:
            ts = pd.to_datetime(raw, dayfirst=True, errors="raise")
        return ts.date()
    except Exception:
        try:
            ts = pd.to_datetime(raw, errors="raise")
            return ts.date()
        except Exception:
            return hoy_colombia


def _normalizar_codigo_ubicacion(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _canonizar_ubicacion_ocr(valor, codigos):
    """Mapea OCR ruidoso al código WMS más probable, solo con alta confianza."""
    raw = str(valor or "").strip().upper()
    if not raw:
        return "", 0.0
    nraw = _normalizar_codigo_ubicacion(raw)
    if not nraw:
        return raw, 0.0

    candidatos = []
    for codigo in codigos:
        nc = _normalizar_codigo_ubicacion(codigo)
        if not nc:
            continue
        score = SequenceMatcher(None, nraw, nc).ratio()
        # Premia contenido completo con ruido adicional al final/inicio.
        if nc in nraw or nraw in nc:
            score = max(score, min(len(nraw), len(nc)) / max(len(nraw), len(nc)))
        candidatos.append((score, codigo))
    if not candidatos:
        return raw, 0.0
    candidatos.sort(reverse=True)
    best_score, best = candidatos[0]
    second = candidatos[1][0] if len(candidatos) > 1 else 0.0

    # Conservador: evita transformar una lectura ambigua en un código falso.
    if best_score >= 0.82 and (best_score - second >= 0.04 or best_score >= 0.93):
        return best, best_score
    return raw, best_score


def _enriquecer_extraccion(extr, proveedor_id):
    """Cruza OCR con maestros y OC para autocompletar líneas con datos reales."""
    if not extr or not extr.get("ocr_ok"):
        return extr

    with session_scope() as s:
        articulos = s.query(Articulo).filter(Articulo.activo.is_(True)).all()
        catalogo = {a.codigo: (a.descripcion or "") for a in articulos}
        completar_con_catalogo(extr, catalogo)

        from core.models import Ubicacion
        codigos_ubi = [
            u.codigo for u in s.query(Ubicacion).filter(
                Ubicacion.activo.is_(True),
                Ubicacion.cerrada.is_(False)).all()
        ]

        # DESDE/HASTA son del BIN completo, no de cada artículo.
        # Canoniza los valores detectados una sola vez a nivel documental.
        desde_vals = list(extr.get("bin_desde_valores") or [])
        hasta_vals = list(extr.get("bin_hasta_valores") or [])

        desde_canon = []
        for raw in desde_vals:
            canon, score = _canonizar_ubicacion_ocr(raw, codigos_ubi)
            desde_canon.append({
                "raw": raw, "valor": canon, "score": round(float(score), 3)
            })
        hasta_canon = []
        for raw in hasta_vals:
            canon, score = _canonizar_ubicacion_ocr(raw, codigos_ubi)
            hasta_canon.append({
                "raw": raw, "valor": canon, "score": round(float(score), 3)
            })

        desde_unicos = []
        for x in desde_canon:
            if x["valor"] and x["valor"] not in desde_unicos:
                desde_unicos.append(x["valor"])
        hasta_unicos = []
        for x in hasta_canon:
            if x["valor"] and x["valor"] not in hasta_unicos:
                hasta_unicos.append(x["valor"])

        extr["bin_desde_canon"] = desde_unicos[0] if len(desde_unicos) == 1 else ""
        extr["bin_hasta_canon"] = hasta_unicos[0] if len(hasta_unicos) == 1 else ""
        extr["bin_desde_canon_valores"] = desde_unicos
        extr["bin_hasta_canon_valores"] = hasta_unicos
        extr["bin_desde_detalle"] = desde_canon
        extr["bin_hasta_detalle"] = hasta_canon

        # Determina para qué proveedor está dirigido el BIN usando HASTA.
        # En el proceso AKT, la ubicación HASTA es la ubicación destino
        # configurada en el maestro del proveedor transformador. No se usa la
        # columna "Proveedor" del BIN para esto, porque esa columna identifica
        # proveedores/orígenes de los materiales y puede contener varios valores.
        seleccionado = s.get(Proveedor, proveedor_id)
        extr["proveedor_seleccionado_id"] = proveedor_id
        extr["proveedor_seleccionado_codigo"] = (
            seleccionado.codigo if seleccionado else "")
        extr["proveedor_seleccionado_nombre"] = (
            seleccionado.nombre if seleccionado else "")

        destino_bin = _norm_ubi(extr.get("bin_hasta_canon") or "")
        candidatos_destino = {}
        proveedores_activos = s.query(Proveedor).filter(
            Proveedor.activo.is_(True)).all()

        if destino_bin:
            # 1) Relación directa Ubicación -> Proveedor.
            ubic_destino = s.query(Ubicacion).filter(
                Ubicacion.codigo == destino_bin,
                Ubicacion.activo.is_(True),
                Ubicacion.cerrada.is_(False),
            ).first()
            if ubic_destino and ubic_destino.proveedor_id:
                p_dest = s.get(Proveedor, ubic_destino.proveedor_id)
                if p_dest and p_dest.activo:
                    candidatos_destino[p_dest.id] = p_dest

            # 2) Respaldo: ubicación_destino guardada directamente en Proveedor.
            for p_dest in proveedores_activos:
                if _norm_ubi(p_dest.ubicacion_destino) == destino_bin:
                    candidatos_destino[p_dest.id] = p_dest

        # 3) Fallback para FOTO/OCR: aunque el parser espacial no haya logrado
        # separar todas las filas (por ejemplo proveedor+código pegados), busca
        # la ubicación HASTA maestra directamente dentro del texto OCR completo.
        # Se compactan espacios/guiones para tolerar pequeñas variaciones del OCR.
        texto_compacto = _normalizar_codigo_ubicacion(extr.get("texto") or "")
        coincidencias_texto = {}
        if texto_compacto:
            for p_dest in proveedores_activos:
                destino_maestro = _norm_ubi(p_dest.ubicacion_destino)
                nd = _normalizar_codigo_ubicacion(destino_maestro)
                if len(nd) >= 6 and nd in texto_compacto:
                    coincidencias_texto[p_dest.id] = (
                        p_dest, texto_compacto.count(nd), destino_maestro)

        if len(coincidencias_texto) == 1:
            p_dest, repeticiones, destino_maestro = next(
                iter(coincidencias_texto.values()))
            candidatos_destino[p_dest.id] = p_dest
            # Si HASTA no salió de la geometría pero aparece inequívocamente en
            # el OCR, lo promovemos a HASTA documental para toda la validación.
            if not destino_bin:
                destino_bin = destino_maestro
                extr["bin_hasta_canon"] = destino_maestro
                extr["bin_hasta_canon_valores"] = [destino_maestro]
                extr["bin_hasta_detectado_por_texto"] = True
                extr["bin_hasta_repeticiones_texto"] = int(repeticiones)

        # 4) Fallback fuzzy orientado a la estructura real de una fila BIN:
        # después de "NONE NONE" vienen DESDE y al final HASTA. Se compara la
        # parte posterior de cada fila contra las ubicaciones destino maestras
        # y se exige alta cobertura + posición hacia el final de la fila.
        if not destino_bin and not candidatos_destino:
            filas_texto = [
                " ".join(x.split()) for x in str(extr.get("texto") or "").splitlines()
                if str(x).upper().count("NONE") >= 2
            ]
            puntajes = {}
            for p_dest in proveedores_activos:
                destino_maestro = _norm_ubi(p_dest.ubicacion_destino)
                nd = _normalizar_codigo_ubicacion(destino_maestro)
                if len(nd) < 6:
                    continue
                # En el maestro algunas ubicaciones llevan el prefijo de UN
                # "MOTOS-" mientras el reporte BIN imprime solo la ubicación.
                variantes_nd = [nd]
                if nd.startswith("MOTOS") and len(nd) > 10:
                    variantes_nd.append(nd[5:])

                mejor = 0.0
                for linea in filas_texto:
                    partes_none = re.split(r"\bNONE\b", linea, maxsplit=2, flags=re.I)
                    resto = partes_none[-1] if len(partes_none) >= 3 else linea
                    nr = _normalizar_codigo_ubicacion(resto)
                    if not nr:
                        continue

                    for nd_var in variantes_nd:
                        pos = nr.rfind(nd_var)
                        if pos >= 0:
                            frac_inicio = pos / max(1, len(nr))
                            # HASTA debe estar en la mitad posterior del remanente.
                            if frac_inicio >= 0.28:
                                mejor = max(mejor, 1.0 + 0.05 * frac_inicio)
                            continue

                        m = SequenceMatcher(None, nd_var, nr).find_longest_match()
                        cobertura = m.size / max(1, len(nd_var))
                        frac_inicio = m.b / max(1, len(nr))
                        if cobertura >= 0.78 and frac_inicio >= 0.28:
                            mejor = max(mejor, cobertura + 0.05 * frac_inicio)

                if mejor > 0:
                    puntajes[p_dest.id] = (mejor, p_dest, destino_maestro)

            if puntajes:
                ranking = sorted(
                    puntajes.values(), key=lambda z: z[0], reverse=True)
                best_score, best_p, best_destino = ranking[0]
                second_score = ranking[1][0] if len(ranking) > 1 else 0.0
                if best_score >= 0.84 and (
                        best_score - second_score >= 0.035 or best_score >= 1.02):
                    candidatos_destino[best_p.id] = best_p
                    destino_bin = best_destino
                    extr["bin_hasta_canon"] = best_destino
                    extr["bin_hasta_canon_valores"] = [best_destino]
                    extr["bin_hasta_detectado_por_texto"] = True
                    extr["bin_hasta_score_texto"] = round(float(best_score), 3)

        detectado = (
            next(iter(candidatos_destino.values()))
            if len(candidatos_destino) == 1 else None
        )
        extr["proveedor_destino_detectado_id"] = (
            detectado.id if detectado else None)
        extr["proveedor_destino_detectado_codigo"] = (
            detectado.codigo if detectado else "")
        extr["proveedor_destino_detectado_nombre"] = (
            detectado.nombre if detectado else "")
        extr["proveedor_destino_detectado_hasta"] = destino_bin

        # None = no fue posible determinarlo con suficiente certeza.
        # False = el documento pertenece inequívocamente a otro proveedor.
        coincide = None
        if detectado:
            coincide = int(detectado.id) == int(proveedor_id)
        elif destino_bin and seleccionado:
            hasta_sel = _norm_ubi(seleccionado.ubicacion_destino)
            # Solo afirmamos que NO corresponde cuando HASTA fue reconocido
            # como una ubicación maestra real.
            destino_conocido = s.query(Ubicacion.id).filter(
                Ubicacion.codigo == destino_bin,
                Ubicacion.activo.is_(True),
                Ubicacion.cerrada.is_(False),
            ).first() is not None
            if destino_conocido and hasta_sel:
                coincide = destino_bin == hasta_sel
        extr["proveedor_destino_coincide"] = coincide

        # Si el documento trae OC, úsela como respaldo para líneas que el OCR
        # no pudo leer completamente. Nunca sobreescribe una cantidad OCR > 0.
        oc_num = str(((extr.get("orden_compra") or {}).get("valor") or "")).strip()
        if oc_num:
            ocs = s.query(OrdenCompra).filter(
                OrdenCompra.proveedor_id == proveedor_id,
                OrdenCompra.numero == oc_num,
                OrdenCompra.estado == "ABIERTA",
            ).order_by(OrdenCompra.id).all()

            existentes = {
                str(x.get("articulo") or "").strip().upper(): x
                for x in (extr.get("lineas") or [])
                if str(x.get("articulo") or "").strip()
            }
            for oc in ocs:
                key = oc.articulo.upper()
                if key not in existentes:
                    ln = {
                        "articulo": oc.articulo,
                        "descripcion": catalogo.get(oc.articulo, ""),
                        "cantidad_documento": max(0.0, float(oc.pendiente or 0)),
                        "cantidad_fisica": 0.0,
                        "confianza": 0.75,
                        "fuente": "OC_ABIERTA",
                    }
                    extr.setdefault("lineas", []).append(ln)
                    existentes[key] = ln
                elif float(existentes[key].get("cantidad_documento") or 0) <= 0:
                    existentes[key]["cantidad_documento"] = max(
                        0.0, float(oc.pendiente or 0))
                    existentes[key]["cantidad_fisica"] = 0.0
                    existentes[key]["fuente"] = "OC_ABIERTA"

        # Sugiere proveedor origen por NIT, solo si existe en el maestro.
        nit = _nit_normalizado(((extr.get("nit") or {}).get("valor")))
        prov_origen_id = None
        if nit:
            for p in s.query(Proveedor).filter(
                    Proveedor.activo.is_(True), Proveedor.id != proveedor_id).all():
                if _nit_normalizado(p.nit) == nit:
                    prov_origen_id = p.id
                    break
        extr["proveedor_origen_sugerido_id"] = prov_origen_id

    extr["requiere_revision"] = (
        float(extr.get("confianza_texto") or 0) < 0.85
        or not extr.get("lineas")
        or any(float(x.get("cantidad_documento") or 0) <= 0
               for x in extr.get("lineas", []))
    )
    return extr


def _extraer_documento(soporte, proveedor_id):
    """Procesa automáticamente un archivo nuevo; no requiere botón intermedio."""
    if soporte is None:
        return None, "manual"

    contenido = soporte.getvalue()
    digest = hashlib.sha256(contenido).hexdigest()[:16]
    # Versiona el resultado de extracción para no reutilizar en session_state
    # una lectura hecha por un parser anterior después de un redeploy.
    extractor_version = "bin-destino-v11"
    clave = f"extract_{extractor_version}_{soporte.name}_{digest}_{proveedor_id}"

    if clave not in st.session_state:
        with st.spinner("Procesando documento automáticamente..."):
            try:
                extr = analizar_documento(soporte.name, contenido, soporte.type)
                st.session_state[clave] = _enriquecer_extraccion(extr, proveedor_id)
            except Exception as e:
                st.session_state[clave] = {
                    "ocr_ok": False,
                    "texto": "",
                    "lineas": [],
                    "metodo": "ERROR",
                    "confianza_texto": 0.0,
                    "requiere_revision": True,
                    "diagnostico": f"{type(e).__name__}: {e}",
                }

    return st.session_state.get(clave), digest


def _filas_sin_cantidad_documento(df):
    if df is None or df.empty:
        return []
    faltan = []
    for _, row in df.iterrows():
        articulo = str(row.get("articulo") or "").strip()
        if not articulo:
            continue
        try:
            qty = float(row.get("cantidad_documento") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            faltan.append(articulo)
    return faltan


def _diferencias_recepcion(df):
    if df is None or df.empty:
        return []
    difs = []
    for _, row in df.iterrows():
        articulo = str(row.get("articulo") or "").strip()
        if not articulo:
            continue
        try:
            qdoc = float(row.get("cantidad_documento") or 0)
            qfis = float(row.get("cantidad_fisica") or 0)
        except (TypeError, ValueError):
            continue
        if abs(qfis - qdoc) > 1e-9:
            difs.append({
                "Artículo": articulo,
                "Documento": qdoc,
                "Recibido": qfis,
                "Diferencia": qfis - qdoc,
            })
    return difs


def _norm_ubi(v):
    return " ".join(str(v or "").strip().upper().split())


def _ubicaciones_proveedor(proveedor_id):
    """Valida las dos ubicaciones maestras usadas por el BIN."""
    with session_scope() as s:
        p = s.get(Proveedor, proveedor_id)
        if not p:
            return None, None, "Proveedor inexistente."

        desde = _norm_ubi(p.ubicacion_origen)
        hasta = _norm_ubi(p.ubicacion_destino)
        if not desde or not hasta:
            return desde or None, hasta or None, (
                "El proveedor debe tener configuradas las ubicaciones DESDE y HASTA "
                "en Maestros → Proveedores.")

        from core.models import Ubicacion
        u_desde = s.query(Ubicacion).filter(Ubicacion.codigo == desde).first()
        u_hasta = s.query(Ubicacion).filter(Ubicacion.codigo == hasta).first()

        if not u_desde or not u_desde.activo or u_desde.cerrada:
            return desde, hasta, f"La ubicación DESDE {desde} no está disponible."
        if not u_hasta or not u_hasta.activo or u_hasta.cerrada:
            return desde, hasta, f"La ubicación HASTA {hasta} no está disponible."
        if u_hasta.proveedor_id and u_hasta.proveedor_id != proveedor_id:
            return desde, hasta, (
                f"La ubicación HASTA {hasta} está asignada a otro proveedor.")

        return desde, hasta, None

def _registrar(user):
    if not puede(user, "recibo_registrar"):
        st.warning("Su rol no registra recibos.")
        return

    pid = ui.selector_proveedor(user, key="rec_prov")
    if not pid:
        return

    ubicacion_desde_maestro, ubicacion_hasta_maestro, error_ubicacion = (
        _ubicaciones_proveedor(pid))
    if error_ubicacion:
        st.error(error_ubicacion)
        st.info(
            "Antes de recibir un BIN, configure DESDE y HASTA en "
            "Maestros → Proveedores.")
        return

    soporte = st.file_uploader(
        "Documento de entrada (PDF o foto)",
        type=["pdf", "png", "jpg", "jpeg"],
        key="rec_soporte",
        help="Al cargar el archivo se procesa automáticamente y se completan los campos detectados.")

    extr, digest = _extraer_documento(soporte, pid)
    suffix = digest or "manual"

    origenes = list(ORIGENES)
    origen_sugerido = (extr or {}).get("origen_sugerido")
    origen_confianza = float((extr or {}).get("origen_confianza") or 0.0)
    origen_evidencia = str((extr or {}).get("origen_evidencia") or "").strip()

    # Alerta temprana: para BIN A BIN, HASTA identifica al proveedor
    # transformador al que está dirigido el documento.
    proveedor_doc_mismatch = bool(
        soporte is not None
        and origen_sugerido == "BIN_A_BIN"
        and origen_confianza >= 0.90
        and (extr or {}).get("proveedor_destino_coincide") is False
    )

    if soporte is not None and extr and origen_sugerido in ORIGENES:
        pct_tipo = int(round(origen_confianza * 100))
        st.info(
            f"Tipo de documento detectado automáticamente: "
            f"**{ORIGENES[origen_sugerido]}**"
            + (f" ({pct_tipo}% de confianza)." if pct_tipo else ".")
            + (f" Evidencia: {origen_evidencia}." if origen_evidencia else "")
        )

    if proveedor_doc_mismatch:
        sel_nombre = str((extr or {}).get("proveedor_seleccionado_nombre") or "Proveedor seleccionado")
        sel_codigo = str((extr or {}).get("proveedor_seleccionado_codigo") or "")
        det_nombre = str((extr or {}).get("proveedor_destino_detectado_nombre") or "")
        det_codigo = str((extr or {}).get("proveedor_destino_detectado_codigo") or "")
        hasta_bin_alerta = str((extr or {}).get("proveedor_destino_detectado_hasta") or "")
        destino_txt = (
            f" El HASTA del documento corresponde a **{det_nombre} ({det_codigo})**."
            if det_nombre else
            f" El HASTA detectado es **{hasta_bin_alerta}**."
        )
        st.error(
            f"Este BIN A BIN **no corresponde al proveedor seleccionado "
            f"{sel_nombre}{f' ({sel_codigo})' if sel_codigo else ''}**."
            + destino_txt
            + " Cambie el proveedor antes de continuar con la recepción."
        )

    indice_origen = origenes.index(origen_sugerido) if origen_sugerido in origenes else 0
    origen = st.radio(
        "Origen del recibo", origenes,
        index=indice_origen,
        format_func=lambda k: ORIGENES[k],
        horizontal=False,
        key=f"rec_origen_{suffix}",
        help="Si el documento puede clasificarse con seguridad, el sistema deja seleccionado automáticamente BIN a BIN o Factura.")

    if extr:
        cf = int(100 * float(extr.get("confianza_texto") or 0))
        metodo = extr.get("metodo", "")
        diagnostico = str(extr.get("diagnostico") or "").strip()

        if not extr.get("ocr_ok"):
            st.error(
                "No fue posible extraer texto del documento. "
                "Puede continuar con captura manual o revisar el diagnóstico.")
            if diagnostico:
                with st.expander("Diagnóstico OCR"):
                    st.code(diagnostico)
        elif extr.get("requiere_revision"):
            st.warning(
                f"Documento procesado automáticamente por {metodo} con confianza "
                f"aproximada {cf}%. Revise los campos marcados antes de crear el recibo.")
        else:
            st.success(
                f"Documento procesado automáticamente por {metodo}. "
                f"Confianza aproximada: {cf}%.")

        if diagnostico:
            st.caption(f"Diagnóstico OCR: {diagnostico}")

        with st.expander("Texto detectado", expanded=False):
            texto_ocr = extr.get("texto", "")
            st.text(texto_ocr[:12000] if texto_ocr else "Sin texto detectado.")

        oc_detectada = str(((extr.get("orden_compra") or {}).get("valor") or "")).strip()
        if oc_detectada:
            st.caption(f"OC detectada: **{oc_detectada}**")

    # Proveedor origen: si el NIT coincide con el maestro, queda preseleccionado.
    prov_origen_id = None
    if origen == "FACTURA":
        with session_scope() as s:
            provs = s.query(Proveedor).filter(
                Proveedor.activo.is_(True), Proveedor.id != pid
            ).order_by(Proveedor.nombre).all()
            ops = {"— No identificado —": None}
            ops.update({f"{p.nombre} ({p.codigo})": p.id for p in provs})

        sugerido_id = (extr or {}).get("proveedor_origen_sugerido_id")
        labels = list(ops)
        sugerido_label = next(
            (label for label, value in ops.items() if value == sugerido_id),
            labels[0])
        sel_po = st.selectbox(
            "Proveedor ORIGEN", labels,
            index=labels.index(sugerido_label),
            key=f"rec_prov_origen_{suffix}")
        prov_origen_id = ops[sel_po]

    ref_sugerida = str(((extr or {}).get("referencia") or {}).get("valor") or "")
    fecha_sugerida = _fecha_ocr(extr)

    desde_doc_vals = list((extr or {}).get("bin_desde_canon_valores") or [])
    hasta_doc_vals = list((extr or {}).get("bin_hasta_canon_valores") or [])
    desde_doc = _norm_ubi((extr or {}).get("bin_desde_canon") or "")
    hasta_doc = _norm_ubi((extr or {}).get("bin_hasta_canon") or "")

    # DESDE/HASTA se validan una sola vez por BIN.
    alerta_desde_doc = None
    error_hasta_doc = None
    if origen == "BIN_A_BIN" and soporte is not None:
        if len(desde_doc_vals) > 1:
            alerta_desde_doc = (
                "El BIN presenta más de un valor DESDE detectado: "
                + ", ".join(desde_doc_vals[:4]))
        elif not desde_doc:
            alerta_desde_doc = "No fue posible leer DESDE en el BIN."
        elif desde_doc != ubicacion_desde_maestro:
            alerta_desde_doc = (
                f"DESDE del BIN: {desde_doc}. Maestro del proveedor: "
                f"{ubicacion_desde_maestro}.")

        if len(hasta_doc_vals) > 1:
            error_hasta_doc = (
                "El BIN presenta más de un valor HASTA detectado: "
                + ", ".join(hasta_doc_vals[:4]))
        elif not hasta_doc:
            error_hasta_doc = "No fue posible leer HASTA en el BIN."
        elif hasta_doc != ubicacion_hasta_maestro:
            error_hasta_doc = (
                f"HASTA del BIN: {hasta_doc}. Maestro del proveedor: "
                f"{ubicacion_hasta_maestro}.")

    c1, c2, c3, c4 = st.columns(4)
    referencia = c1.text_input(
        "Referencia / No. documento",
        value=ref_sugerida,
        placeholder="BIN2686958 · FV-3-8619",
        key=f"rec_ref_{suffix}")
    fecha_doc = c2.date_input(
        "Fecha del documento",
        value=fecha_sugerida,
        key=f"rec_fecha_{suffix}")
    c3.text_input(
        "DESDE BIN",
        value=desde_doc or "No leído",
        disabled=True,
        help=f"Esperado según maestro: {ubicacion_desde_maestro}",
        key=f"rec_desde_doc_{suffix}")
    ubic_dest = c4.text_input(
        "HASTA BIN",
        value=hasta_doc or "No leído",
        disabled=True,
        help=f"Esperado según maestro: {ubicacion_hasta_maestro}",
        key=f"rec_hasta_doc_{suffix}")

    if origen == "BIN_A_BIN":
        st.caption(
            f"Maestro proveedor → DESDE: **{ubicacion_desde_maestro}** · "
            f"HASTA: **{ubicacion_hasta_maestro}**")
        if alerta_desde_doc:
            st.warning(
                "DESDE no coincide o no pudo validarse. "
                "Es una alerta no bloqueante. " + alerta_desde_doc)
        if error_hasta_doc:
            st.error(
                "HASTA no coincide con el maestro del proveedor. "
                "El recibo queda bloqueado. " + error_hasta_doc)

    c1, c2 = st.columns([1, 2])
    reproceso = c1.checkbox(
        "Es reproceso (garantía/calidad)",
        value=bool((extr or {}).get("es_reproceso_sugerido", False)),
        key=f"rec_repro_{suffix}")
    obs = c2.text_input("Observación general", key=f"rec_obs_{suffix}")

    st.markdown("##### Líneas del recibo")
    modos = ["Documento leído", "Manual", "Cargue masivo"]
    modo_default = 0 if soporte is not None else 1
    modo = st.radio(
        "Captura", modos,
        index=modo_default,
        horizontal=True,
        key=f"modo_rec_{suffix}")

    lineas_df = None
    recepcion_estado = None
    bloqueo_hasta = (
        bool(error_hasta_doc) if origen == "BIN_A_BIN" else False
    ) or proveedor_doc_mismatch
    if modo == "Cargue masivo":
        ui.boton_plantilla("recibo_lineas", key=f"rec_{suffix}")
        arch = st.file_uploader(
            "Archivo de líneas", type=["csv", "xlsx", "xls"],
            key=f"up_rec_{suffix}")
        lineas_df = ui.leer_archivo_tabular(arch)

    elif modo == "Documento leído":
        lineas = list((extr or {}).get("lineas") or [])
        if not lineas:
            st.info(
                "El OCR leyó el documento, pero no pudo identificar todas las líneas. "
                "Agregue o corrija las referencias en la tabla antes de confirmar la recepción.")

        base = pd.DataFrame(lineas or [{
            "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
            "cantidad_fisica": 0.0, "lote": "", "serial": "",
        }])

        for col in ("lote", "serial"):
            if col not in base.columns:
                base[col] = ""

        base = base.drop(columns=[
            "confianza", "fuente",
            "ubicacion_desde", "ubicacion_hasta",
            "ubicacion_desde_ocr_raw", "ubicacion_desde_ocr_score",
            "ubicacion_hasta_ocr_raw", "ubicacion_hasta_ocr_score",
        ], errors="ignore")

        estado_key = f"rec_estado_{suffix}"
        df_key = f"rec_df_{suffix}"
        version_key = f"rec_df_version_{suffix}"

        if estado_key not in st.session_state:
            st.session_state[estado_key] = "PENDIENTE"
        if version_key not in st.session_state:
            st.session_state[version_key] = 0
        if df_key not in st.session_state:
            st.session_state[df_key] = base.copy()

        stored = st.session_state[df_key].copy()
        st.session_state[df_key] = stored

        recepcion_estado = st.session_state[estado_key]
        if recepcion_estado == "COMPLETO":
            st.success(
                "Recepción marcada como satisfactoria y completa. "
                "Cantidad física = cantidad documento en todas las líneas.")
        elif recepcion_estado == "DISCREPANCIA":
            st.warning(
                "Modo discrepancias activo. Modifique únicamente la cantidad física "
                "de las referencias que llegaron diferentes.")
        else:
            st.info(
                "Primero revise las referencias y cantidades del documento. "
                "Luego marque recepción completa o recepción con discrepancias.")

        disabled_cols = []
        if recepcion_estado == "PENDIENTE":
            disabled_cols = ["cantidad_fisica"]
        elif recepcion_estado == "COMPLETO":
            disabled_cols = [
                "articulo", "descripcion", "cantidad_documento", "cantidad_fisica"
            ]
        elif recepcion_estado == "DISCREPANCIA":
            disabled_cols = [
                "articulo", "descripcion", "cantidad_documento"
            ]

        editor_key = (
            f"ed_rec_ai_{suffix}_{st.session_state[version_key]}"
        )
        lineas_df = st.data_editor(
            st.session_state[df_key],
            num_rows="dynamic" if recepcion_estado == "PENDIENTE" else "fixed",
            use_container_width=True,
            key=editor_key,
            disabled=disabled_cols,
            column_config={
                "articulo": st.column_config.TextColumn("Artículo", required=True),
                "descripcion": st.column_config.TextColumn("Descripción"),
                "cantidad_documento": st.column_config.NumberColumn(
                    "Cantidad documento", min_value=0.0, step=1.0),
                "cantidad_fisica": st.column_config.NumberColumn(
                    "Cantidad física real", min_value=0.0, step=1.0),
            })
        st.session_state[df_key] = lineas_df.copy()

        faltan_qty = _filas_sin_cantidad_documento(lineas_df)
        if faltan_qty:
            st.error(
                "Falta cantidad documental para: "
                + ", ".join(faltan_qty[:12])
                + ("…" if len(faltan_qty) > 12 else "")
                + ". Corrija esas cantidades antes de marcar recepción completa.")

        b1, b2, b3 = st.columns([1.3, 1.1, 0.8])
        if b1.button(
            "✓ Marcar recibo satisfactorio y completo",
            type="primary",
            use_container_width=True,
            disabled=bool(faltan_qty) or lineas_df.empty or bloqueo_hasta,
            key=f"btn_completo_{suffix}",
        ):
            nuevo = lineas_df.copy()
            nuevo["cantidad_fisica"] = pd.to_numeric(
                nuevo["cantidad_documento"], errors="coerce").fillna(0.0)
            st.session_state[df_key] = nuevo
            st.session_state[estado_key] = "COMPLETO"
            st.session_state[version_key] += 1
            st.rerun()

        if b2.button(
            "⚠ Registrar discrepancias",
            use_container_width=True,
            disabled=lineas_df.empty or bloqueo_hasta,
            key=f"btn_disc_{suffix}",
        ):
            nuevo = lineas_df.copy()
            # Parte de una recepción completa para que el usuario solo cambie
            # las referencias realmente diferentes.
            nuevo["cantidad_fisica"] = pd.to_numeric(
                nuevo["cantidad_documento"], errors="coerce").fillna(0.0)
            st.session_state[df_key] = nuevo
            st.session_state[estado_key] = "DISCREPANCIA"
            st.session_state[version_key] += 1
            st.rerun()

        if b3.button(
            "Revisar datos",
            use_container_width=True,
            disabled=recepcion_estado == "PENDIENTE",
            key=f"btn_revisar_{suffix}",
        ):
            st.session_state[estado_key] = "PENDIENTE"
            st.session_state[version_key] += 1
            st.rerun()

        # Recupera el estado después de cualquier edición.
        recepcion_estado = st.session_state[estado_key]
        difs = _diferencias_recepcion(lineas_df)
        if recepcion_estado == "DISCREPANCIA":
            if difs:
                st.warning(f"{len(difs)} referencia(s) presentan diferencia física.")
                st.dataframe(
                    pd.DataFrame(difs),
                    use_container_width=True,
                    hide_index=True)
            else:
                st.caption(
                    "Aún no hay diferencias. Cambie la cantidad física real "
                    "de la referencia que corresponda.")

        detectadas = [
            x for x in lineas if str(x.get("articulo") or "").strip()
        ]
        if detectadas:
            st.caption(
                f"{len(detectadas)} línea(s) detectada(s) automáticamente. "
                "Puede agregar referencias faltantes antes de confirmar la recepción.")

    else:
        base = pd.DataFrame([{
            "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
            "cantidad_fisica": 0.0, "lote": "", "serial": "",
        }])
        lineas_df = st.data_editor(
            base, num_rows="dynamic", use_container_width=True,
            key=f"ed_rec_manual_{suffix}")

    if lineas_df is not None and not lineas_df.empty:
        st.caption(
            "Los datos autocompletados son una propuesta: el usuario confirma antes "
            "de que el documento afecte el flujo.")

    crear_label = "Crear recibo"
    if modo == "Documento leído" and recepcion_estado == "COMPLETO":
        crear_label = "Crear recibo satisfactorio y completo"
    elif modo == "Documento leído" and recepcion_estado == "DISCREPANCIA":
        crear_label = "Crear recibo con discrepancias"

    if st.button(crear_label, type="primary", use_container_width=True):
        if proveedor_doc_mismatch:
            ui.err(
                "No se puede crear el recibo: el BIN A BIN detectado pertenece "
                "a otro proveedor según su ubicación HASTA. Cambie el proveedor "
                "seleccionado y vuelva a validar el documento.")
            return
        if origen == "BIN_A_BIN" and bloqueo_hasta:
            ui.err(
                "No se puede crear el recibo: HASTA debe coincidir con la "
                "ubicación HASTA asignada al proveedor.")
            return
        if lineas_df is None or lineas_df.empty:
            ui.err("Debe capturar al menos una línea.")
            return
        if modo == "Documento leído" and recepcion_estado == "PENDIENTE":
            ui.err(
                "Defina primero el resultado de la recepción: "
                "satisfactoria/completa o con discrepancias.")
            return
        faltan_qty = _filas_sin_cantidad_documento(lineas_df)
        if modo == "Documento leído" and faltan_qty:
            ui.err(
                "Hay referencias sin cantidad documental: "
                + ", ".join(faltan_qty[:12]))
            return

        def num(v):
            try:
                return float(str(v).replace(",", "") or 0)
            except (TypeError, ValueError):
                return 0.0

        if modo == "Documento leído" and recepcion_estado == "COMPLETO":
            lineas_df = lineas_df.copy()
            lineas_df["cantidad_fisica"] = pd.to_numeric(
                lineas_df["cantidad_documento"], errors="coerce").fillna(0.0)

        lineas = []
        for _, row in lineas_df.iterrows():
            cod = str(row.get("articulo") or "").strip()
            if not cod:
                continue
            lineas.append(LineaRecibo(
                articulo=cod,
                descripcion=str(row.get("descripcion") or ""),
                cantidad_documento=num(row.get("cantidad_documento")),
                cantidad_fisica=num(row.get("cantidad_fisica")),
                lote=str(row.get("lote") or ""),
                serial=str(row.get("serial") or ""),
                ubicacion_desde=(desde_doc or ubicacion_desde_maestro)
                    if origen == "BIN_A_BIN" else "",
                ubicacion_hasta=(hasta_doc or ubicacion_hasta_maestro)
                    if origen == "BIN_A_BIN" else ubicacion_hasta_maestro))
        if not lineas:
            ui.err("Ninguna línea tiene artículo.")
            return

        try:
            with session_scope() as s:
                arch_id = None
                if soporte is not None:
                    arch_id = guardar_archivo(
                        s, soporte.name, soporte.getvalue(), soporte.type,
                        user["email"]).id
                r = crear_recibo(
                    s, proveedor_id=pid, origen=origen, lineas=lineas,
                    referencia=referencia or None, usuario=user["email"],
                    es_reproceso=reproceso,
                    ubicacion_destino=(hasta_doc if origen == "BIN_A_BIN"
                                       else ubicacion_hasta_maestro),
                    archivo_id=arch_id, fecha_documento=fecha_doc,
                    observaciones=obs or None, proveedor_origen_id=prov_origen_id,
                    factura_origen=referencia if origen == "FACTURA" else None)
                trz, rid = r.documento.trz, r.id

                alertas = validar_bin_a_bin(s, r, pid) if origen == "BIN_A_BIN" else []

                if origen == "REGISTRO":
                    confirmar_recibo_simple(s, rid, user["email"])
                    msg = "Registro confirmado e ingresado al inventario."
                elif origen == "BIN_A_BIN":
                    msg = "BIN creado PENDIENTE_MATCH. No afecta inventario hasta validar las OC."
                else:
                    msg = "Factura creada en BORRADOR. El proveedor debe sellarla antes del match."

            ui.ok(f"{msg} Trazabilidad: **{trz}**")
            for alerta in alertas:
                st.warning(alerta)
        except ReglaNegocio as e:
            ui.err(str(e))


def _match(user):
    if not puede(user, "recibo_match_oc"):
        st.info("Solo el equipo de Recibo AKT hace el match contra órdenes de compra.")
        return

    with session_scope() as s:
        pendientes = s.query(Recibo).filter(
            Recibo.estado == "PENDIENTE_MATCH",
            Recibo.origen.in_(("BIN_A_BIN", "FACTURA"))
        ).order_by(Recibo.creado_en.desc()).limit(300).all()
        opciones = {
            f"{r.documento.trz} · {r.proveedor.nombre} · {r.origen}": r.id
            for r in pendientes if r.documento
        }

    if not opciones:
        st.info("No hay recibos pendientes de match.")
        return

    sel = st.selectbox("Recibo pendiente", list(opciones), key="match_rec")
    rid = opciones[sel]

    with session_scope() as s:
        r = s.get(Recibo, rid)
        sugerencias = sugerir_oc_por_linea(s, rid)
        st.caption(
            "Cada línea se valida contra su propia OC. La aplicación sugiere la OC abierta "
            "más antigua del mismo artículo; usted debe confirmarla.")
        if r.proveedor_origen:
            st.caption(f"Proveedor origen: **{r.proveedor_origen.nombre}**")

        asignaciones = {}
        bloqueado = False
        for sg in sugerencias:
            st.markdown(f"**{sg['articulo']}** · físico: {sg['cantidad_fisica']:,.2f}")
            alts = sg["alternativas"]
            if not alts:
                st.error("No existe una OC ABIERTA para este artículo.")
                bloqueado = True
                continue
            etiquetas = {
                f"{num} · pendiente {pend:,.2f}": oid for oid, num, pend in alts
            }
            sugerida_label = next(
                (k for k, v in etiquetas.items() if v == sg["oc_sugerida_id"]),
                list(etiquetas)[0])
            labels = list(etiquetas)
            idx = labels.index(sugerida_label)
            elegido = st.selectbox(
                "OC asignada", labels, index=idx, key=f"oc_linea_{sg['linea_id']}")
            asignaciones[int(sg["linea_id"])] = int(etiquetas[elegido])
            st.divider()

    ref_bin = st.text_input(
        "No. BIN / referencia logística (opcional)", key=f"ref_bin_{rid}")
    arch = st.file_uploader(
        "Soporte adicional del match (PDF o foto, opcional)",
        type=["pdf", "png", "jpg", "jpeg"], key=f"up_bin_{rid}")

    if st.button(
        "Validar líneas, actualizar OC e ingresar inventario",
        type="primary", use_container_width=True, disabled=bloqueado):
        try:
            with session_scope() as s:
                aid = None
                if arch is not None:
                    aid = guardar_archivo(
                        s, arch.name, arch.getvalue(), arch.type, user["email"]).id
                res = match_recibo_lineas(
                    s, recibo_id=rid, asignaciones=asignaciones,
                    usuario=user["email"], archivo_id=aid,
                    referencia_bin=ref_bin or None)

            df = pd.DataFrame(res["lineas"])
            st.dataframe(df, use_container_width=True, hide_index=True)
            if res["estado"] == "CERRADA":
                ui.ok("Todas las líneas hicieron match exacto. Recibo CERRADO.")
            else:
                st.warning(
                    f"Recibo en **{res['estado']}**. Las diferencias quedaron como "
                    "novedades; los sobrantes están RESTRINGIDOS y los faltantes no "
                    "descuentan inventario por segunda vez.")
        except ReglaNegocio as e:
            ui.err(str(e))
