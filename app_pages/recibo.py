"""Recibo de mercancía: BIN a BIN, Factura (proveedor origen), Registro y match con OC."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import Documento, OrdenCompra, Proveedor, Recibo, ReciboLinea, Ubicacion
from core.services import (LineaRecibo, ReglaNegocio, adjuntar_bin_y_match,
                           confirmar_recibo_simple, crear_recibo, guardar_archivo,
                           ordenes_compra_abiertas, registrar_novedad, sellar_recibo,
                           validar_bin_a_bin)

ORIGENES = {
    "BIN_A_BIN": "BIN a BIN — materia prima cruda desde AKT MOTOS",
    "FACTURA": "Factura — producto desde otro proveedor ORIGEN",
    "REGISTRO": "Registro manual (PDF o foto)",
}


def render(user):
    ui.encabezado("Recibo de mercancía",
                  "Todo lo recibido entra como «Inventario crudo» con su ID de trazabilidad")
    t1, t2, t3 = st.tabs(["📋 Documentos", "➕ Registrar recibo",
                          "🔗 Adjuntar BIN a BIN y match con OC"])
    with t1:
        _consulta(user)
    with t2:
        _registrar(user)
    with t3:
        _match(user)


# ------------------------------------------------------------------ consulta
def _consulta(user):
    pid = alcance_proveedor(user)
    c1, c2, c3 = st.columns(3)
    estado = c1.multiselect("Estado", ["BORRADOR", "SELLADO", "NOVEDAD", "CERRADA"],
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
                "Origen": r.origen, "Referencia": r.documento.referencia if r.documento else "",
                "Proveedor": r.proveedor.nombre if r.proveedor else "",
                "Estado": r.estado, "Líneas": len(r.lineas), "Cantidad": cant,
                "Reproceso": "Sí" if r.es_reproceso else "",
                "OC": r.orden_compra.numero if r.orden_compra else "",
                "Antigüedad (días)": r.documento.antiguedad_dias if r.documento else 0,
                "Fecha": r.creado_en, "_id": r.id})

    if not filas:
        st.info("No hay recibos con ese filtro.")
        return

    df = pd.DataFrame(filas)
    st.dataframe(df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
                 column_config={"Fecha": st.column_config.DatetimeColumn(
                     "Fecha", format="DD/MM/YYYY HH:mm")})
    st.download_button("Exportar a Excel",
                       ui.exportar_excel({"recibos": df.drop(columns=["_id"])}),
                       "recibos.xlsx", key="exp_rec")

    sel = st.selectbox("Ver detalle del documento",
                       ["—"] + [f["Trazabilidad"] for f in filas])
    if sel == "—":
        return

    rid = next(f["_id"] for f in filas if f["Trazabilidad"] == sel)
    with session_scope() as s:
        r = s.get(Recibo, rid)
        st.markdown(f"**{r.documento.trz}** · {ORIGENES.get(r.origen, r.origen)} · "
                    + ui.pill(r.estado), unsafe_allow_html=True)
        cols = st.columns(4)
        cols[0].metric("Proveedor", r.proveedor.codigo if r.proveedor else "—")
        cols[1].metric("Referencia", r.documento.referencia or "—")
        cols[2].metric("Fecha documento", str(r.documento.fecha_documento))
        cols[3].metric("Antigüedad", f"{r.documento.antiguedad_dias} días")
        st.dataframe(pd.DataFrame([{
            "Artículo": l.articulo, "Descripción": l.descripcion,
            "Cant. documento": l.cantidad_documento, "Cant. física": l.cantidad_fisica,
            "Diferencia": l.diferencia, "Desde": l.ubicacion_desde,
            "Hasta": l.ubicacion_hasta, "Lote": l.lote, "Condición": l.condicion,
        } for l in r.lineas]), use_container_width=True, hide_index=True)

        if r.documento.archivo_id:
            a = r.documento.archivo
            st.download_button(f"Descargar soporte: {a.nombre}", a.contenido or b"",
                               file_name=a.nombre, key=f"dl_{rid}")

        if r.origen == "FACTURA" and r.estado == "BORRADOR" and \
                puede(user, "recibo_sellar") and (not pid or r.proveedor_id == pid):
            st.info("Certifique («selle») que recibió el producto del proveedor ORIGEN. "
                    "AKT MOTOS hará luego el ingreso con la factura y el BIN a BIN.")
            if st.button("Sellar / certificar recepción", type="primary", key=f"sl_{rid}"):
                try:
                    sellar_recibo(s, rid, user["email"])
                    ui.ok("Recibo sellado.")
                    st.rerun()
                except ReglaNegocio as e:
                    ui.err(str(e))

        if r.origen in ("BIN_A_BIN", "REGISTRO") and r.estado == "BORRADOR":
            if st.button("Confirmar ingreso al inventario", type="primary", key=f"cf_{rid}"):
                try:
                    confirmar_recibo_simple(s, rid, user["email"])
                    ui.ok("Inventario crudo actualizado.")
                    st.rerun()
                except ReglaNegocio as e:
                    ui.err(str(e))


# ----------------------------------------------------------------- registrar
def _registrar(user):
    if not puede(user, "recibo_registrar"):
        st.warning("Su rol no registra recibos.")
        return

    pid = ui.selector_proveedor(user, key="rec_prov")
    if not pid:
        return

    origen = st.radio("Origen del recibo", list(ORIGENES),
                      format_func=lambda k: ORIGENES[k], horizontal=False)

    c1, c2, c3 = st.columns(3)
    referencia = c1.text_input("Referencia / No. documento",
                               placeholder="BIN2686958 · FV-3-8619")
    fecha_doc = c2.date_input("Fecha del documento", dt.date.today())
    ubicaciones = [""] + ui.catalogo_ubicaciones(pid)
    ubic_dest = c3.selectbox("Ubicación destino (WMS, opcional)", ubicaciones)

    c1, c2 = st.columns([1, 2])
    reproceso = c1.checkbox("Es **reproceso** (garantía / calidad)",
                            help="Se debe tipificar en el momento del ingreso.")
    soporte = c2.file_uploader("Soporte (PDF o foto)",
                               type=["pdf", "png", "jpg", "jpeg"])

    st.markdown("##### Líneas del recibo")
    modo = st.radio("Captura", ["Manual", "Cargue masivo"], horizontal=True,
                    key="modo_rec")

    lineas_df = None
    if modo == "Cargue masivo":
        ui.boton_plantilla("recibo_lineas", key="rec")
        arch = st.file_uploader("Archivo de líneas", type=["csv", "xlsx", "xls"],
                                key="up_rec")
        lineas_df = ui.leer_archivo_tabular(arch)
        if lineas_df is not None:
            st.dataframe(lineas_df.head(20), use_container_width=True, hide_index=True)
            st.caption(f"{len(lineas_df)} líneas leídas.")
    else:
        base = pd.DataFrame([{"articulo": "", "descripcion": "", "cantidad_documento": 0.0,
                              "cantidad_fisica": 0.0, "lote": "", "serial": "",
                              "ubicacion_desde": "", "ubicacion_hasta": ubic_dest}])
        lineas_df = st.data_editor(
            base, num_rows="dynamic", use_container_width=True, key="ed_rec",
            column_config={
                "articulo": st.column_config.TextColumn("Artículo", required=True),
                "descripcion": st.column_config.TextColumn("Descripción"),
                "cantidad_documento": st.column_config.NumberColumn("Cant. documento", min_value=0.0),
                "cantidad_fisica": st.column_config.NumberColumn("Cant. física recibida", min_value=0.0),
                "ubicacion_desde": st.column_config.SelectboxColumn("Desde", options=ubicaciones),
                "ubicacion_hasta": st.column_config.SelectboxColumn("Hasta", options=ubicaciones),
            })

    if st.button("Crear recibo", type="primary", use_container_width=True):
        if lineas_df is None or lineas_df.empty:
            ui.err("Debe capturar al menos una línea.")
            return
        lineas = []
        for _, r in lineas_df.iterrows():
            cod = str(r.get("articulo") or "").strip()
            if not cod:
                continue
            def num(v):
                try:
                    return float(str(v).replace(",", "") or 0)
                except (TypeError, ValueError):
                    return 0.0
            lineas.append(LineaRecibo(
                articulo=cod, descripcion=str(r.get("descripcion") or ""),
                cantidad_documento=num(r.get("cantidad_documento")),
                cantidad_fisica=num(r.get("cantidad_fisica")),
                lote=str(r.get("lote") or ""), serial=str(r.get("serial") or ""),
                ubicacion_desde=str(r.get("ubicacion_desde") or ""),
                ubicacion_hasta=str(r.get("ubicacion_hasta") or ubic_dest or "")))
        if not lineas:
            ui.err("Ninguna línea tiene artículo.")
            return
        try:
            with session_scope() as s:
                arch_id = None
                if soporte is not None:
                    arch_id = guardar_archivo(s, soporte.name, soporte.getvalue(),
                                              soporte.type, user["email"]).id
                r = crear_recibo(s, proveedor_id=pid, origen=origen, lineas=lineas,
                                 referencia=referencia or None, usuario=user["email"],
                                 es_reproceso=reproceso, ubicacion_destino=ubic_dest,
                                 archivo_id=arch_id, fecha_documento=fecha_doc)
                trz = r.documento.trz
                alertas = validar_bin_a_bin(s, r, pid) if origen == "BIN_A_BIN" else []
                if origen in ("BIN_A_BIN", "REGISTRO"):
                    confirmar_recibo_simple(s, r.id, user["email"])
                    msg = "Recibo creado e ingresado al inventario crudo."
                else:
                    msg = ("Recibo creado. Séllelo para certificar la recepción; "
                           "luego AKT adjunta el BIN a BIN y hace el match con la OC.")
            ui.ok(f"{msg}  Trazabilidad: **{trz}**")
            for a in alertas:
                st.warning(a, icon="⚠️")
        except ReglaNegocio as e:
            ui.err(str(e))


# --------------------------------------------------------------- match con OC
def _match(user):
    if not puede(user, "recibo_match_oc"):
        st.info("Solo el equipo de Recibo de AKT MOTOS adjunta el BIN a BIN y hace "
                "el match con la orden de compra.")
        return

    with session_scope() as s:
        pendientes = s.query(Recibo).filter(
            Recibo.estado.in_(("SELLADO", "BORRADOR", "PENDIENTE_MATCH")),
            Recibo.origen == "FACTURA").order_by(Recibo.creado_en.desc()).limit(200).all()
        opciones = {f"{r.documento.trz} · {r.proveedor.nombre} · {r.estado}": r.id
                    for r in pendientes if r.documento}

    if not opciones:
        st.info("No hay recibos de factura pendientes de match.")
        return

    sel = st.selectbox("Recibo pendiente", list(opciones))
    rid = opciones[sel]

    with session_scope() as s:
        r = s.get(Recibo, rid)
        st.dataframe(pd.DataFrame([{
            "Artículo": l.articulo, "Descripción": l.descripcion,
            "Cant. documento": l.cantidad_documento,
            "Cant. física": l.cantidad_fisica} for l in r.lineas]),
            use_container_width=True, hide_index=True)
        arts = {l.articulo for l in r.lineas}
        ocs = [o for o in ordenes_compra_abiertas(s, r.proveedor_id)
               if o.articulo in arts]
        if not ocs:
            st.warning("No hay órdenes de compra ABIERTAS de este proveedor para los "
                       "artículos del recibo. Créelas en Maestros → Órdenes de compra.")
            return
        op = {f"{o.numero} · {o.articulo} · pendiente {o.pendiente:,.0f}": o.id for o in ocs}
        sel_oc = st.selectbox("Orden de compra ABIERTA", list(op))
        ref_bin = st.text_input("No. del BIN a BIN", placeholder="BIN2686958")
        arch = st.file_uploader("Adjuntar BIN a BIN (PDF o foto)",
                                type=["pdf", "png", "jpg", "jpeg"], key="up_bin")

    if st.button("Validar cantidad y cerrar", type="primary", use_container_width=True):
        try:
            with session_scope() as s:
                aid = None
                if arch is not None:
                    aid = guardar_archivo(s, arch.name, arch.getvalue(), arch.type,
                                          user["email"]).id
                res = adjuntar_bin_y_match(s, recibo_id=rid, orden_compra_id=op[sel_oc],
                                           usuario=user["email"], archivo_id=aid,
                                           referencia_bin=ref_bin or None)
            if res["estado"] == "CERRADA":
                ui.ok(f"Cantidades coinciden ({res['recibido']:,.0f}). Documento **CERRADA**.")
            else:
                st.error(
                    f"Discrepancia: esperado {res['esperado']:,.0f}, recibido "
                    f"{res['recibido']:,.0f} (diferencia {res['diferencia']:+,.0f}). "
                    f"El documento queda en **NOVEDAD** y se abrió la novedad "
                    f"automáticamente.", icon="⚠️")
        except ReglaNegocio as e:
            ui.err(str(e))
