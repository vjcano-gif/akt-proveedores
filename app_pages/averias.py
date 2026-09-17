"""Averías: reporte con evidencia de destrucción y panel de tolerancia por proveedor."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import Averia, Proveedor
from core.services import (MOTIVOS_AVERIA, ReglaNegocio, ajustar_averia,
                           guardar_archivo, leer_archivo, registrar_averia,
                           tolerancia_averias)

MOMENTOS = ["RECIBO", "ALMACENAMIENTO", "PRODUCCION"]
AYUDA_MOTIVO = {
    "ORIGEN": "La pieza llegó averiada desde el proveedor origen.",
    "MANIPULACION": "Se averió manipulando el material en el proveedor.",
    "PUESTA_A_PUNTO": "Se perdió en la puesta a punto del proceso de transformación.",
    "AKT": "La avería es responsabilidad de AKT MOTOS.",
}


def render(user):
    ui.encabezado("Reporte de averías",
                  "Artículo, cantidad y evidencia de la destrucción · motivo y momento")
    t1, t2, t3 = st.tabs(["📋 Panel", "➕ Reportar avería",
                          "⚖️ Tolerancia por acuerdo comercial"])
    with t1:
        _panel(user)
    with t2:
        _reportar(user)
    with t3:
        _tolerancia(user)


def _panel(user):
    pid = alcance_proveedor(user)
    with session_scope() as s:
        q = s.query(Averia)
        if pid:
            q = q.filter(Averia.proveedor_id == pid)
        avs = q.order_by(Averia.fecha.desc()).limit(500).all()
        filas = [{"Trazabilidad": a.documento.trz if a.documento else "",
                  "Artículo": a.articulo, "Cantidad": a.cantidad,
                  "Momento": a.momento, "Motivo": a.motivo,
                  "Inventario": a.estado_inventario, "Estado": a.estado,
                  "Ajuste": a.documento_ajuste or "",
                  "Proveedor": a.proveedor.nombre if a.proveedor else "",
                  "Evidencia": "Sí" if a.evidencia_id else "No",
                  "Fecha": a.fecha, "_id": a.id} for a in avs]
    if not filas:
        st.info("No hay averías registradas.")
        return
    df = pd.DataFrame(filas)
    c = st.columns(4)
    ui.kpi(c[0], "Averías", len(df), "", "#e02424")
    ui.kpi(c[1], "Unidades", f"{df.Cantidad.sum():,.0f}", "", "#e02424")
    ui.kpi(c[2], "Pendientes de ajuste",
           int((df.Estado != "AJUSTADA").sum()), "", "#f59e0b")
    top = df.groupby("Motivo").Cantidad.sum().sort_values(ascending=False)
    ui.kpi(c[3], "Motivo principal", top.index[0] if len(top) else "—",
           f"{top.iloc[0]:,.0f} und" if len(top) else "")

    st.divider()
    izq, der = st.columns([1.4, 1])
    with izq:
        st.dataframe(df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
                     column_config={"Fecha": st.column_config.DatetimeColumn(
                         "Fecha", format="DD/MM/YYYY HH:mm")})
    with der:
        st.markdown("###### Unidades por motivo")
        st.bar_chart(top, color="#e02424", height=200)
        st.markdown("###### Unidades por momento")
        st.bar_chart(df.groupby("Momento").Cantidad.sum(), color="#f59e0b", height=200)

    st.download_button("Exportar a Excel",
                       ui.exportar_excel({"averias": df.drop(columns=["_id"])}),
                       "averias.xlsx", key="exp_av")

    if puede(user, "averias_ajustar"):
        pend = df[df.Estado != "AJUSTADA"]
        if not pend.empty:
            st.markdown("##### Montar ajuste de avería")
            sel = st.selectbox("Avería", pend.Trazabilidad.tolist())
            aid = int(pend[pend.Trazabilidad == sel]["_id"].iloc[0])
            with session_scope() as s:
                a = s.get(Averia, aid)
                if a.evidencia:
                    st.download_button(f"Ver evidencia: {a.evidencia.nombre}",
                                       leer_archivo(a.evidencia),
                                       file_name=a.evidencia.nombre, key=f"eva_{aid}")
            c1, c2 = st.columns(2)
            doc = c1.selectbox("Documento de ajuste", ["TD90", "TD96"], key="av_doc")
            num = c2.text_input("No. del ajuste", key="av_num")
            if st.button("Aplicar ajuste (dar de baja el producto averiado)",
                         type="primary"):
                try:
                    with session_scope() as s:
                        ajustar_averia(
                            s, averia_id=aid, documento_ajuste=doc,
                            numero_ajuste=num, usuario=user["email"], actor=user)
                    ui.ok("Avería ajustada: el producto averiado salió del inventario.")
                    st.rerun()
                except ReglaNegocio as e:
                    ui.err(str(e))


def _reportar(user):
    if not puede(user, "averias_registrar"):
        st.warning("Su rol no reporta averías.")
        return
    pid = ui.selector_proveedor(user, key="av_prov")
    if not pid:
        return

    c1, c2, c3 = st.columns(3)
    art = c1.text_input("Artículo", key="av_art")
    cant = c2.number_input("Cantidad averiada", min_value=0.0, step=1.0)
    momento = c3.selectbox("Momento de la avería", MOMENTOS, index=2)

    c1, c2 = st.columns(2)
    motivo = c1.selectbox("Motivo", list(MOTIVOS_AVERIA))
    estado_inv = c2.selectbox("Estado del inventario", ["CRUDO", "PROCESADO"])
    st.caption(AYUDA_MOTIVO[motivo])

    evid = st.file_uploader("Evidencia fotográfica de la destrucción (obligatoria)",
                            type=["png", "jpg", "jpeg", "pdf"], key="av_ev")
    obs = st.text_area("Observaciones", height=68, key="av_obs")

    if st.button("Reportar avería", type="primary", use_container_width=True):
        try:
            with session_scope() as s:
                eid = None
                if evid is not None:
                    eid = guardar_archivo(s, evid.name, evid.getvalue(), evid.type,
                                          user["email"]).id
                a = registrar_averia(s, proveedor_id=pid, articulo=art, cantidad=cant,
                                     motivo=motivo, momento=momento, evidencia_id=eid,
                                     usuario=user["email"], estado_inventario=estado_inv,
                                     observaciones=obs or None, actor=user)
                trz = a.documento.trz
            ui.ok(f"Avería **{trz}** registrada. El producto pasó a RESTRINGIDO y "
                  f"entró a la cola de Inventarios.")
        except ReglaNegocio as e:
            ui.err(str(e))


def _tolerancia(user):
    pid = alcance_proveedor(user)
    c1, c2 = st.columns(2)
    desde = c1.date_input("Desde", dt.date.today() - dt.timedelta(days=180), key="tol_d")
    hasta = c2.date_input("Hasta", dt.date.today(), key="tol_h")
    with session_scope() as s:
        filas = tolerancia_averias(s, pid, dt.datetime.combine(desde, dt.time.min),
                                   dt.datetime.combine(hasta, dt.time.max))
    if not filas:
        st.info("No hay movimiento en el periodo.")
        return
    df = pd.DataFrame(filas)
    exceden = int(df.excede.sum())
    c = st.columns(3)
    ui.kpi(c[0], "Proveedores evaluados", len(df))
    ui.kpi(c[1], "Exceden tolerancia", exceden, "acuerdo comercial",
           "#e02424" if exceden else "#16a34a")
    ui.kpi(c[2], "% avería global",
           f"{100 * df.averiado.sum() / df.recibido.sum():.2f}%"
           if df.recibido.sum() else "0%")

    vista = df.rename(columns={
        "proveedor": "Proveedor", "codigo": "Código", "recibido": "Recibido",
        "averiado": "Averiado", "pct_averia": "% avería",
        "tolerancia_pct": "% tolerancia", "exceso_pp": "Exceso (p.p.)"})
    vista["Estado"] = vista["excede"].map({True: "EXCEDE", False: "DENTRO"})
    vista = vista.drop(columns=["excede", "proveedor_id"])

    def color(row):
        c = "#fee2e2" if row.Estado == "EXCEDE" else "#dcfce7"
        return [f"background-color: {c}"] * len(row)

    st.dataframe(vista.style.apply(color, axis=1), use_container_width=True,
                 hide_index=True)

    if puede(user, "maestro_proveedores"):
        st.markdown("##### Ajustar el margen de tolerancia pactado")
        with session_scope() as s:
            provs = s.query(Proveedor).filter(Proveedor.activo.is_(True)).order_by(
                Proveedor.nombre).all()
            op = {f"{p.nombre} ({p.codigo}) — actual {p.tolerancia_averia_pct:.2f}%": p.id
                  for p in provs}
        c1, c2 = st.columns([2, 1])
        sel = c1.selectbox("Proveedor", list(op), key="tol_prov")
        nueva = c2.number_input("Nueva tolerancia (%)", 0.0, 100.0, 1.0, 0.1)
        if st.button("Guardar tolerancia"):
            with session_scope() as s:
                p = s.get(Proveedor, op[sel])
                p.tolerancia_averia_pct = float(nueva)
            ui.limpiar_cache()
            ui.ok("Tolerancia actualizada.")
            st.rerun()
