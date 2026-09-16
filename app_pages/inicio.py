"""Tablero de inicio: estado del flujo y cola de trabajo."""
import datetime as dt

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import func

from core import ui
from core.auth import alcance_proveedor
from core.db import session_scope
from core.models import (Averia, ConteoEjecutado, ConteoProgramado, Despacho,
                         Documento, Inventario, Novedad, ProgramaProduccion, Recibo)
from core.services import (antiguedad_documentos, cola_inventarios,
                           cumplimiento_conteos, cumplimiento_mps)


def render(user):
    ui.encabezado("Tablero de control",
                  "Recibo · Almacenamiento · Producción · Despacho")
    pid = alcance_proveedor(user)

    with session_scope() as s:
        def f(q, col):
            return q.filter(col == pid) if pid else q

        rec_novedad = f(s.query(func.count(Recibo.id)).filter(
            Recibo.estado == "NOVEDAD"), Recibo.proveedor_id).scalar() or 0
        rec_abiertos = f(s.query(func.count(Recibo.id)).filter(
            Recibo.estado.in_(("BORRADOR", "SELLADO", "PENDIENTE_MATCH"))),
            Recibo.proveedor_id).scalar() or 0
        crudo = f(s.query(func.coalesce(func.sum(Inventario.cantidad), 0.0)).filter(
            Inventario.estado == "CRUDO", Inventario.condicion == "DISPONIBLE"),
            Inventario.proveedor_id).scalar() or 0
        proc = f(s.query(func.coalesce(func.sum(Inventario.cantidad), 0.0)).filter(
            Inventario.estado == "PROCESADO", Inventario.condicion == "DISPONIBLE"),
            Inventario.proveedor_id).scalar() or 0
        restr = f(s.query(func.coalesce(func.sum(Inventario.cantidad), 0.0)).filter(
            Inventario.condicion == "RESTRINGIDO"), Inventario.proveedor_id).scalar() or 0
        mps_ab = f(s.query(func.count(ProgramaProduccion.id)).filter(
            ProgramaProduccion.estado.in_(("PROGRAMADO", "EN_PROCESO"))),
            ProgramaProduccion.proveedor_id).scalar() or 0
        desp = f(s.query(func.count(Despacho.id)).filter(
            Despacho.estado == "DESPACHADO"), Despacho.proveedor_id).scalar() or 0

        cola = cola_inventarios(s, pid)
        cmp_conteos = cumplimiento_conteos(s, pid)
        cmp_mps = cumplimiento_mps(s, pid)
        docs = antiguedad_documentos(s, pid, limite=1000)

        pend_conteos = f(s.query(func.count(ConteoProgramado.id)).filter(
            ConteoProgramado.estado == "PROGRAMADO"),
            ConteoProgramado.proveedor_id).scalar() or 0

        filas_cola = []
        for n in cola["novedades"]:
            filas_cola.append({"Tipo": f"Novedad {n.tipo}", "TRZ": n.documento.trz if n.documento else "",
                               "Artículo": n.articulo, "Cantidad": n.cantidad,
                               "Motivo": n.motivo or "", "Estado": n.estado,
                               "Fecha": n.creado_en, "id": n.id})
        for a in cola["averias"]:
            filas_cola.append({"Tipo": f"Avería {a.momento}", "TRZ": a.documento.trz if a.documento else "",
                               "Artículo": a.articulo, "Cantidad": a.cantidad,
                               "Motivo": a.motivo or "", "Estado": a.estado,
                               "Fecha": a.fecha, "id": a.id})
        for c in cola["conteos"]:
            filas_cola.append({"Tipo": "Conteo con discrepancia",
                               "TRZ": c.documento.trz if c.documento else "",
                               "Artículo": c.articulo, "Cantidad": c.diferencia,
                               "Motivo": "DISCREPANCIA", "Estado": c.estado,
                               "Fecha": c.fecha, "id": c.id})

    c = st.columns(4)
    ui.kpi(c[0], "Inventario crudo", f"{crudo:,.0f}", "disponible", "#2563eb")
    ui.kpi(c[1], "Inventario procesado", f"{proc:,.0f}", "listo para despacho", "#16a34a")
    ui.kpi(c[2], "Restringido", f"{restr:,.0f}", "averías / novedades", "#e02424")
    ui.kpi(c[3], "Cola de trabajo", f"{len(filas_cola):,.0f}",
           "pendientes de Inventarios", "#f59e0b" if filas_cola else "#6b7280")

    c = st.columns(4)
    ui.kpi(c[0], "Recibos en novedad", f"{rec_novedad:,.0f}",
           f"{rec_abiertos} abiertos", "#e02424" if rec_novedad else "#16a34a")
    ui.kpi(c[1], "Cumplimiento conteos", f"{cmp_conteos['cumplimiento_pct']:.0f}%",
           f"{cmp_conteos['ejecutados']}/{cmp_conteos['programados']} · "
           f"{pend_conteos} pendientes",
           "#16a34a" if cmp_conteos["cumplimiento_pct"] >= 90 else "#f59e0b")
    ui.kpi(c[2], "Cumplimiento MPS", f"{cmp_mps['cumplimiento_pct']:.0f}%",
           f"{cmp_mps['ejecutado']:,.0f} de {cmp_mps['programado']:,.0f} und",
           "#16a34a" if cmp_mps["cumplimiento_pct"] >= 90 else "#f59e0b")
    ui.kpi(c[3], "Despachos", f"{desp:,.0f}", f"{mps_ab} programas abiertos", "#0f172a")

    st.divider()
    izq, der = st.columns([1.35, 1])

    with izq:
        st.subheader("Cola de trabajo de Inventarios")
        if filas_cola:
            df = pd.DataFrame(filas_cola).drop(columns=["id"]).sort_values(
                "Fecha", ascending=False)
            st.dataframe(df, use_container_width=True, hide_index=True,
                         column_config={"Fecha": st.column_config.DatetimeColumn(
                             "Fecha", format="DD/MM/YYYY HH:mm")})
            st.caption("Resuélvelas en **Novedades**, **Averías** o **Almacenamiento**.")
        else:
            st.success("No hay pendientes en la cola de trabajo.", icon="✅")

    with der:
        st.subheader("Antigüedad de documentos")
        if docs:
            dd = pd.DataFrame(docs)
            orden = ["0-7", "8-30", "31-60", "61-90", "90+"]
            colores = ["#16a34a", "#22c55e", "#f59e0b", "#fb923c", "#e02424"]
            g = dd.groupby("rango").size().reindex(orden, fill_value=0).reset_index()
            g.columns = ["Antigüedad (días)", "Documentos"]
            fig = px.bar(g, x="Antigüedad (días)", y="Documentos", text_auto=True,
                         color="Antigüedad (días)",
                         category_orders={"Antigüedad (días)": orden},
                         color_discrete_sequence=colores)
            fig.update_layout(showlegend=False, height=240,
                              margin=dict(l=0, r=0, t=10, b=0),
                              xaxis_title=None)
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(
                dd[["trz", "tipo", "referencia", "antiguedad_dias"]]
                .head(8).rename(columns={"trz": "Trazabilidad", "tipo": "Tipo",
                                         "referencia": "Referencia",
                                         "antiguedad_dias": "Días"}),
                use_container_width=True, hide_index=True)
        else:
            st.info("Aún no hay documentos registrados.")
