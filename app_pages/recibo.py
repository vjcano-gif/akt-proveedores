"""Recibo de mercancía: documento -> reconocimiento -> match por línea contra OC -> inventario."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import document_ai, ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import OrdenCompra, Proveedor, Recibo
from core.services import (
    LineaRecibo, ReglaNegocio, adjuntar_bin_y_match, confirmar_recibo_simple,
    crear_recibo, guardar_archivo, leer_archivo, ordenes_compra_abiertas,
    sellar_recibo, validar_bin_a_bin,
)

ORIGENES = {
    "BIN_A_BIN": "BIN a BIN — materia prima cruda desde AKT MOTOS",
    "FACTURA": "Factura — producto desde otro proveedor ORIGEN",
    "REGISTRO": "Registro manual (PDF o foto)",
}


def render(user):
    ui.encabezado(
        "Recibo de mercancía",
        "Documento → reconocimiento → validación → match por línea contra OC → inventario")
    t1, t2, t3 = st.tabs([
        "📋 Documentos", "➕ Registrar recibo", "🔗 Match por línea con OC"])
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
        "Estado",
        ["BORRADOR", "SELLADO", "PENDIENTE_MATCH", "NOVEDAD", "CERRADA"],
        default=[])
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
            cant = sum(float(l.cantidad_fisica or 0) for l in r.lineas)
            filas.append({
                "Trazabilidad": r.documento.trz if r.documento else "",
                "Origen": r.origen,
                "Referencia": r.documento.referencia if r.documento else "",
                "Proveedor transformación": r.proveedor.nombre if r.proveedor else "",
                "Proveedor origen": r.proveedor_origen.nombre if r.proveedor_origen else "",
                "Estado": r.estado,
                "Líneas": len(r.lineas),
                "Cantidad física": cant,
                "Reproceso": "Sí" if r.es_reproceso else "",
                "Extracción": (r.documento.estado_extraccion or "") if r.documento else "",
                "Confianza": (
                    round(float(r.documento.confianza_extraccion or 0) * 100, 1)
                    if r.documento and r.documento.confianza_extraccion is not None else None),
                "Antigüedad (días)": r.documento.antiguedad_dias if r.documento else 0,
                "Fecha": r.creado_en,
                "_id": r.id,
            })

    if not filas:
        st.info("No hay recibos con ese filtro.")
        return

    df = pd.DataFrame(filas)
    st.dataframe(
        df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
        column_config={
            "Fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm"),
            "Confianza": st.column_config.NumberColumn("Confianza %", format="%.1f"),
        })
    st.download_button(
        "Exportar a Excel",
        ui.exportar_excel({"recibos": df.drop(columns=["_id"])}),
        "recibos.xlsx", key="exp_rec")

    sel = st.selectbox("Ver detalle del documento", ["—"] + [f["Trazabilidad"] for f in filas])
    if sel == "—":
        return
    rid = next(f["_id"] for f in filas if f["Trazabilidad"] == sel)

    with session_scope() as s:
        r = s.get(Recibo, rid)
        st.markdown(
            f"**{r.documento.trz}** · {ORIGENES.get(r.origen, r.origen)} · "
            + ui.pill(r.estado), unsafe_allow_html=True)
        cols = st.columns(6)
        cols[0].metric("Transformador", r.proveedor.codigo if r.proveedor else "—")
        cols[1].metric("Origen", r.proveedor_origen.codigo if r.proveedor_origen else "—")
        cols[2].metric("Referencia", r.documento.referencia or "—")
        cols[3].metric("BIN", r.referencia_bin or "—")
        cols[4].metric("Fecha documento", str(r.documento.fecha_documento))
        cols[5].metric("Antigüedad", f"{r.documento.antiguedad_dias} días")

        st.dataframe(pd.DataFrame([{
            "Artículo": l.articulo,
            "Descripción": l.descripcion,
            "Cant. documento": l.cantidad_documento,
            "Cant. física": l.cantidad_fisica,
            "OC": l.orden_compra.numero if l.orden_compra else "",
            "Match": l.cantidad_match,
            "Restringido": l.cantidad_restringida,
            "Estado match": l.estado_match,
            "Desde": l.ubicacion_desde,
            "Hasta": l.ubicacion_hasta,
            "Lote": l.lote,
        } for l in r.lineas]), use_container_width=True, hide_index=True)

        adjuntos = list(r.documento.adjuntos or [])
        if not adjuntos and r.documento.archivo:
            adjuntos = [type("_A", (), {"tipo": "SOPORTE", "archivo": r.documento.archivo})()]
        for i, da in enumerate(adjuntos):
            if not da.archivo:
                continue
            try:
                data = leer_archivo(da.archivo)
                st.download_button(
                    f"Descargar {da.tipo}: {da.archivo.nombre}", data,
                    file_name=da.archivo.nombre, key=f"dl_{rid}_{i}")
            except Exception as exc:
                st.warning(f"No fue posible descargar {da.archivo.nombre}: {exc}")

        if r.origen == "FACTURA" and r.estado == "BORRADOR" and \
                puede(user, "recibo_sellar") and (not pid or r.proveedor_id == pid):
            st.info(
                "El proveedor certifica que recibió físicamente el material del proveedor ORIGEN. "
                "Después Recibo AKT hará el match por línea contra la OC.")
            if st.button("Sellar / certificar recepción", type="primary", key=f"sl_{rid}"):
                try:
                    sellar_recibo(s, rid, user["email"], actor=user)
                    ui.ok("Recibo sellado.")
                    st.rerun()
                except ReglaNegocio as exc:
                    ui.err(str(exc))

        if r.origen == "REGISTRO" and r.estado == "BORRADOR":
            if st.button("Confirmar ingreso al inventario", type="primary", key=f"cf_{rid}"):
                try:
                    confirmar_recibo_simple(s, rid, user["email"], actor=user)
                    ui.ok("Inventario crudo actualizado.")
                    st.rerun()
                except ReglaNegocio as exc:
                    ui.err(str(exc))


def _parse_date(value):
    if not value:
        return dt.date.today()
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return dt.date.today()


def _registrar(user):
    if not puede(user, "recibo_registrar"):
        st.warning("Su rol no registra recibos.")
        return

    pid = ui.selector_proveedor(user, key="rec_prov")
    if not pid:
        return

    origen = st.radio(
        "Origen del recibo", list(ORIGENES),
        format_func=lambda k: ORIGENES[k], horizontal=False, key="rec_origen")

    soporte = st.file_uploader(
        "Documento de entrada (PDF o foto)",
        type=["pdf", "png", "jpg", "jpeg"], key="rec_soporte")
    c_ai1, c_ai2 = st.columns([1, 2])
    if soporte is not None and c_ai1.button("Analizar documento", use_container_width=True):
        with st.spinner("Reconociendo campos y líneas..."):
            ext = document_ai.analizar_documento(
                soporte.name, soporte.getvalue(), soporte.type)
        st.session_state["rec_doc_ai_result"] = ext
        if ext.get("referencia"):
            st.session_state["rec_referencia"] = str(ext["referencia"])

    ext = st.session_state.get("rec_doc_ai_result")
    if ext:
        conf = float(ext.get("confianza_global") or 0) * 100
        fuente = ext.get("fuente", "")
        c_ai2.info(
            f"Reconocimiento: **{fuente}** · confianza **{conf:.0f}%** · "
            + ("requiere revisión humana" if ext.get("requiere_revision", True)
               else "alta confianza"))
        detectado = ext.get("tipo_documento")
        if detectado and detectado != "DESCONOCIDO" and detectado != origen:
            st.warning(
                f"El documento parece ser **{detectado}**, pero seleccionó **{origen}**. "
                "Revise antes de guardar.")
        for obs in ext.get("observaciones") or []:
            st.caption(str(obs))

    c1, c2, c3 = st.columns(3)
    referencia = c1.text_input(
        "Referencia / No. documento",
        placeholder="BIN2686958 · FV-3-8619", key="rec_referencia")
    fecha_doc = c2.date_input(
        "Fecha del documento", _parse_date((ext or {}).get("fecha")), key="rec_fecha")
    ubicaciones = [""] + ui.catalogo_ubicaciones(pid)
    ubic_dest = c3.selectbox(
        "Ubicación destino", ubicaciones, key="rec_ubic_dest")

    proveedor_origen_id = None
    if origen == "FACTURA":
        with session_scope() as s:
            provs = s.query(Proveedor).filter(
                Proveedor.activo.is_(True), Proveedor.id != pid).order_by(Proveedor.nombre).all()
            opciones_prov = {"— no identificado —": None}
            opciones_prov.update({f"{p.nombre} ({p.codigo})": p.id for p in provs})
        psel = st.selectbox("Proveedor ORIGEN", list(opciones_prov), key="rec_prov_origen")
        proveedor_origen_id = opciones_prov[psel]

        nit_ai = ((ext or {}).get("proveedor_origen") or {}).get("nit")
        nom_ai = ((ext or {}).get("proveedor_origen") or {}).get("nombre")
        if nit_ai or nom_ai:
            st.caption(f"Detectado en documento: {nom_ai or '—'} · NIT {nit_ai or '—'}")

    c1, c2 = st.columns([1, 2])
    reproceso = c1.checkbox(
        "Es **reproceso** (garantía / calidad)",
        help="Se tipifica desde el ingreso.")
    c2.caption(
        "Los datos reconocidos son una propuesta. El usuario debe confirmar artículo, "
        "cantidad física, OC y ubicaciones antes de procesar.")

    st.markdown("##### Líneas del recibo")
    modo = st.radio(
        "Captura", ["Documento reconocido", "Manual", "Cargue masivo"],
        horizontal=True, key="modo_rec")

    lineas_df = None
    if modo == "Cargue masivo":
        ui.boton_plantilla("recibo_lineas", key="rec")
        arch = st.file_uploader(
            "Archivo de líneas", type=["csv", "xlsx", "xls"], key="up_rec")
        lineas_df = ui.leer_archivo_tabular(arch)
        if lineas_df is not None:
            st.dataframe(lineas_df.head(20), use_container_width=True, hide_index=True)
    else:
        ai_lines = (ext or {}).get("lineas") or []
        rows = []
        if modo == "Documento reconocido" and ai_lines:
            for x in ai_lines:
                qdoc = float(x.get("cantidad_documento") or 0)
                qfis = x.get("cantidad_fisica")
                rows.append({
                    "articulo": str(x.get("articulo") or ""),
                    "descripcion": str(x.get("descripcion") or ""),
                    "cantidad_documento": qdoc,
                    "cantidad_fisica": float(qfis) if qfis not in (None, "") else qdoc,
                    "lote": "",
                    "serial": "",
                    "ubicacion_desde": str(x.get("ubicacion_desde") or ""),
                    "ubicacion_hasta": str(x.get("ubicacion_hasta") or ubic_dest),
                    "confianza": float(x.get("confianza") or 0),
                })
        if not rows:
            rows = [{
                "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
                "cantidad_fisica": 0.0, "lote": "", "serial": "",
                "ubicacion_desde": "", "ubicacion_hasta": ubic_dest, "confianza": None,
            }]
        lineas_df = st.data_editor(
            pd.DataFrame(rows), num_rows="dynamic", use_container_width=True, key="ed_rec",
            column_config={
                "articulo": st.column_config.TextColumn("Artículo", required=True),
                "descripcion": st.column_config.TextColumn("Descripción"),
                "cantidad_documento": st.column_config.NumberColumn(
                    "Cant. documento", min_value=0.0),
                "cantidad_fisica": st.column_config.NumberColumn(
                    "Cant. física confirmada", min_value=0.0),
                "ubicacion_desde": st.column_config.TextColumn("Desde"),
                "ubicacion_hasta": st.column_config.SelectboxColumn(
                    "Hasta", options=ubicaciones),
                "confianza": st.column_config.NumberColumn(
                    "Confianza IA", min_value=0.0, max_value=1.0, format="%.2f"),
            })

    if st.button("Crear recibo", type="primary", use_container_width=True):
        if lineas_df is None or lineas_df.empty:
            ui.err("Debe capturar al menos una línea.")
            return

        def num(v, default=0.0):
            try:
                if pd.isna(v):
                    return default
                return float(str(v).replace(",", "") or default)
            except (TypeError, ValueError):
                return default

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
                ubicacion_desde=str(row.get("ubicacion_desde") or ""),
                ubicacion_hasta=str(row.get("ubicacion_hasta") or ubic_dest or "")))

        if not lineas:
            ui.err("Ninguna línea tiene artículo.")
            return
        try:
            with session_scope() as s:
                arch_id = None
                if soporte is not None:
                    arch_id = guardar_archivo(
                        s, soporte.name, soporte.getvalue(), soporte.type, user["email"]).id
                r = crear_recibo(
                    s, proveedor_id=pid, proveedor_origen_id=proveedor_origen_id,
                    origen=origen, lineas=lineas, referencia=referencia or None,
                    usuario=user["email"], es_reproceso=reproceso,
                    ubicacion_destino=ubic_dest, archivo_id=arch_id,
                    fecha_documento=fecha_doc, extraccion=ext, actor=user)
                trz = r.documento.trz
                alertas = validar_bin_a_bin(s, r, pid) if origen == "BIN_A_BIN" else []
                if origen == "REGISTRO":
                    confirmar_recibo_simple(s, r.id, user["email"], actor=user)
                    msg = "Registro creado e ingresado al inventario crudo."
                elif origen == "FACTURA":
                    msg = (
                        "Factura registrada. El proveedor debe sellarla y luego Recibo AKT "
                        "hará el match de cada línea contra su OC.")
                else:
                    msg = (
                        "BIN registrado en PENDIENTE_MATCH. No afecta inventario hasta validar "
                        "cada línea contra una OC abierta.")
            ui.ok(f"{msg} Trazabilidad: **{trz}**")
            for alerta in alertas:
                st.warning(alerta, icon="⚠️")
            st.session_state.pop("rec_doc_ai_result", None)
        except ReglaNegocio as exc:
            ui.err(str(exc))


def _match(user):
    if not puede(user, "recibo_match_oc"):
        st.info("Solo Recibo AKT hace el match por línea contra órdenes de compra.")
        return

    with session_scope() as s:
        pendientes = s.query(Recibo).filter(
            ((Recibo.origen == "BIN_A_BIN") &
             (Recibo.estado.in_(("PENDIENTE_MATCH", "BORRADOR"))))
            |
            ((Recibo.origen == "FACTURA") & (Recibo.estado == "SELLADO"))
        ).order_by(Recibo.creado_en.desc()).limit(200).all()
        opciones = {
            f"{r.documento.trz} · {r.proveedor.nombre} · {r.origen} · {r.estado}": r.id
            for r in pendientes if r.documento
        }

    if not opciones:
        st.info("No hay recibos pendientes de match.")
        return

    sel = st.selectbox("Recibo pendiente", list(opciones), key="match_recibo")
    rid = opciones[sel]
    mappings = {}
    todo_listo = True

    with session_scope() as s:
        r = s.get(Recibo, rid)
        st.markdown(f"**{r.documento.trz}** · proveedor {r.proveedor.nombre}")
        st.dataframe(pd.DataFrame([{
            "ID línea": l.id,
            "Artículo": l.articulo,
            "Descripción": l.descripcion,
            "Cant. documento": l.cantidad_documento,
            "Cant. física": l.cantidad_fisica,
            "Desde": l.ubicacion_desde,
            "Hasta": l.ubicacion_hasta,
        } for l in r.lineas if not l.procesada]), use_container_width=True, hide_index=True)

        st.markdown("##### Asignación de OC por línea")
        for ln in [x for x in r.lineas if not x.procesada]:
            ocs = ordenes_compra_abiertas(s, r.proveedor_id, ln.articulo)
            if not ocs:
                st.error(
                    f"**{ln.articulo}**: no hay OC ABIERTA. Créela/cárguela en "
                    "Maestros → Órdenes de compra.")
                todo_listo = False
                continue
            labels = {
                f"{o.numero} · pendiente {o.pendiente:,.0f} · {o.fecha}": o.id for o in ocs
            }
            elegido = st.selectbox(
                f"{ln.articulo} · físico {float(ln.cantidad_fisica or 0):,.0f}",
                list(labels), key=f"oc_linea_{rid}_{ln.id}")
            mappings[ln.id] = labels[elegido]

    c1, c2 = st.columns(2)
    ref_bin = c1.text_input(
        "No. BIN a BIN / referencia adicional", placeholder="BIN2686958",
        key=f"match_bin_{rid}")
    arch = c2.file_uploader(
        "Adjuntar BIN/soporte adicional", type=["pdf", "png", "jpg", "jpeg"],
        key=f"up_bin_{rid}")

    if st.button(
            "Validar todas las líneas y procesar", type="primary",
            use_container_width=True, disabled=not todo_listo):
        try:
            with session_scope() as s:
                aid = None
                if arch is not None:
                    aid = guardar_archivo(
                        s, arch.name, arch.getvalue(), arch.type, user["email"]).id
                res = adjuntar_bin_y_match(
                    s, recibo_id=rid, usuario=user["email"], lineas_oc=mappings,
                    archivo_id=aid, referencia_bin=ref_bin or None, actor=user)
            st.dataframe(pd.DataFrame(res["lineas"]), use_container_width=True, hide_index=True)
            if res["estado"] == "CERRADA":
                ui.ok("Todas las líneas coinciden. Recibo cerrado e inventario actualizado.")
            else:
                st.warning(
                    "El inventario se actualizó con la cantidad física. Los faltantes no se "
                    "descuentan de nuevo; los sobrantes quedaron RESTRINGIDOS hasta decisión "
                    "de Inventarios.", icon="⚠️")
        except ReglaNegocio as exc:
            ui.err(str(exc))
