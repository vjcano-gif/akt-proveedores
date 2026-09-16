"""Despacho: plan de ensamble, registro y descargue del inventario procesado."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import Despacho, Inventario
from core.services import (ReglaNegocio, confirmar_despacho, crear_despacho,
                           guardar_archivo, saldo_articulo)


def render(user):
    ui.encabezado("Despacho",
                  "Plan de ensamble · descarga el artículo del inventario procesado")
    t1, t2 = st.tabs(["📋 Despachos", "➕ Registrar despacho"])
    with t1:
        _panel(user)
    with t2:
        _registrar(user)


def _panel(user):
    pid = alcance_proveedor(user)
    with session_scope() as s:
        q = s.query(Despacho)
        if pid:
            q = q.filter(Despacho.proveedor_id == pid)
        desps = q.order_by(Despacho.creado_en.desc()).limit(300).all()
        filas = [{"Trazabilidad": d.documento.trz if d.documento else "",
                  "Lote": d.lote or "", "Plan de ensamble": d.plan_ensamble or "",
                  "Estado": d.estado, "Líneas": len(d.lineas),
                  "Unidades": sum(float(l.cantidad or 0) for l in d.lineas),
                  "Proveedor": d.proveedor.nombre if d.proveedor else "",
                  "Creado": d.creado_en, "Despachado": d.despachado_en,
                  "_id": d.id} for d in desps]
    if not filas:
        st.info("No hay despachos registrados.")
        return
    df = pd.DataFrame(filas)
    c = st.columns(3)
    ui.kpi(c[0], "Despachos", len(df))
    ui.kpi(c[1], "Despachados", int((df.Estado == "DESPACHADO").sum()), "", "#16a34a")
    ui.kpi(c[2], "Unidades despachadas",
           f"{df[df.Estado == 'DESPACHADO'].Unidades.sum():,.0f}", "", "#16a34a")
    st.dataframe(df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
                 column_config={
                     "Creado": st.column_config.DatetimeColumn("Creado", format="DD/MM/YYYY HH:mm"),
                     "Despachado": st.column_config.DatetimeColumn("Despachado", format="DD/MM/YYYY HH:mm")})

    borradores = df[df.Estado == "BORRADOR"]
    if not borradores.empty and puede(user, "despacho_registrar"):
        st.markdown("##### Confirmar despacho")
        sel = st.selectbox("Despacho en borrador", borradores.Trazabilidad.tolist())
        did = int(borradores[borradores.Trazabilidad == sel]["_id"].iloc[0])
        with session_scope() as s:
            d = s.get(Despacho, did)
            st.dataframe(pd.DataFrame([{
                "Artículo": l.articulo, "Descripción": l.descripcion,
                "Cantidad": l.cantidad, "Ubicación": l.ubicacion,
                "Disponible (procesado)": saldo_articulo(
                    s, d.proveedor_id, l.articulo, "PROCESADO", "DISPONIBLE")}
                for l in d.lineas]), use_container_width=True, hide_index=True)
        if st.button("Confirmar y descargar del inventario", type="primary"):
            try:
                with session_scope() as s:
                    confirmar_despacho(s, despacho_id=did, usuario=user["email"])
                ui.ok("Lote despachado y descontado del inventario procesado.")
                st.rerun()
            except ReglaNegocio as e:
                ui.err(str(e))


def _registrar(user):
    if not puede(user, "despacho_registrar"):
        st.warning("Su rol no registra despachos.")
        return
    pid = ui.selector_proveedor(user, key="dsp_prov")
    if not pid:
        return

    c1, c2, c3 = st.columns(3)
    lote = c1.text_input("Lote", placeholder="LOTE-ENS-001")
    plan = c2.text_input("Plan de ensamble", placeholder="Plan Ensamble CR4 Sem-38")
    origen = c3.selectbox("Ubicación origen", [""] + ui.catalogo_ubicaciones(pid),
                          key="dsp_ub")
    soporte = st.file_uploader("Soporte del despacho (PDF o foto)",
                               type=["pdf", "png", "jpg", "jpeg"], key="dsp_arch")

    with session_scope() as s:
        disp = s.query(Inventario).filter(
            Inventario.proveedor_id == pid, Inventario.estado == "PROCESADO",
            Inventario.condicion == "DISPONIBLE", Inventario.cantidad > 0).all()
        opciones = sorted({i.articulo for i in disp})
        df_disp = pd.DataFrame([{"Artículo": i.articulo, "Ubicación": i.ubicacion,
                                 "Disponible": i.cantidad} for i in disp])
    if df_disp.empty:
        st.warning("No hay inventario procesado disponible para despachar. "
                   "Ejecute primero una transformación en Producción.")
        return
    st.markdown("###### Inventario procesado disponible")
    st.dataframe(df_disp, use_container_width=True, hide_index=True, height=180)

    modo = st.radio("Captura", ["Manual", "Cargue masivo"], horizontal=True, key="m_dsp")
    lineas_df = None
    if modo == "Cargue masivo":
        ui.boton_plantilla("despacho_lineas", key="dsp")
        arch = st.file_uploader("Archivo de líneas", type=["csv", "xlsx", "xls"],
                                key="up_dsp")
        lineas_df = ui.leer_archivo_tabular(arch)
        if lineas_df is not None:
            st.dataframe(lineas_df.head(20), use_container_width=True, hide_index=True)
    else:
        base = pd.DataFrame([{"articulo": opciones[0] if opciones else "",
                              "descripcion": "", "cantidad": 0.0, "ubicacion": origen}])
        lineas_df = st.data_editor(
            base, num_rows="dynamic", use_container_width=True, key="ed_dsp",
            column_config={
                "articulo": st.column_config.SelectboxColumn("Artículo", options=opciones,
                                                             required=True),
                "descripcion": st.column_config.TextColumn("Descripción"),
                "cantidad": st.column_config.NumberColumn("Cantidad", min_value=0.0),
                "ubicacion": st.column_config.SelectboxColumn(
                    "Ubicación", options=[""] + ui.catalogo_ubicaciones(pid))})

    if st.button("Crear despacho", type="primary", use_container_width=True):
        if lineas_df is None or lineas_df.empty:
            ui.err("Capture al menos una línea.")
            return
        lineas = []
        for _, r in lineas_df.iterrows():
            cod = str(r.get("articulo") or "").strip()
            try:
                cant = float(str(r.get("cantidad") or 0).replace(",", ""))
            except ValueError:
                cant = 0.0
            if cod and cant > 0:
                lineas.append({"articulo": cod, "descripcion": str(r.get("descripcion") or ""),
                               "cantidad": cant, "ubicacion": str(r.get("ubicacion") or origen)})
        if not lineas:
            ui.err("Ninguna línea válida.")
            return
        try:
            with session_scope() as s:
                aid = None
                if soporte is not None:
                    aid = guardar_archivo(s, soporte.name, soporte.getvalue(),
                                          soporte.type, user["email"]).id
                d = crear_despacho(s, proveedor_id=pid, lineas=lineas,
                                   lote=lote or None, plan_ensamble=plan or None,
                                   usuario=user["email"], ubicacion_origen=origen,
                                   archivo_id=aid)
                trz, did = d.documento.trz, d.id
            ui.ok(f"Despacho **{trz}** creado en borrador. Confírmelo en la pestaña "
                  f"«Despachos» para descargar el inventario.")
        except ReglaNegocio as e:
            ui.err(str(e))
