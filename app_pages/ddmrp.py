"""DDMRP (fase 2): parametrización de topes, zonas y alertas por colores."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import DdmrpParametro
from core.services import (calcular_zonas_ddmrp, consumo_promedio_dia, estado_ddmrp,
                           saldo_articulo)

COLOR_DDMRP = {"ROJO OSCURO": "#991b1b", "ROJO": "#e02424", "AMARILLO": "#f59e0b",
               "VERDE": "#16a34a", "AZUL (exceso)": "#2563eb"}


def render(user):
    ui.encabezado("DDMRP — Fase 2",
                  "Parametrización de topes, zonas y alertas por colores")
    st.info("Módulo de fase 2 del proyecto: ya calcula zonas y semáforo sobre el "
            "inventario real de la app. La planificación automática de órdenes "
            "queda para la siguiente etapa.", icon="ℹ️")
    t1, t2 = st.tabs(["🚦 Buffer y alertas", "⚙️ Parametrización"])
    with t1:
        _tablero(user)
    with t2:
        _parametrizar(user)


def _calcular(s, pid):
    filas = []
    q = s.query(DdmrpParametro).filter(DdmrpParametro.activo.is_(True))
    if pid:
        q = q.filter(DdmrpParametro.proveedor_id == pid)
    for p in q.all():
        cpd = p.consumo_promedio_dia or consumo_promedio_dia(s, p.proveedor_id, p.articulo)
        z = calcular_zonas_ddmrp(cpd, p.lead_time_dias or 7, p.factor_variabilidad or 0.5,
                                 p.factor_lead_time or 0.5, p.moq or 0)
        disp = saldo_articulo(s, p.proveedor_id, p.articulo, "CRUDO", "DISPONIBLE")
        est, pct = estado_ddmrp(disp, z)
        filas.append({"Artículo": p.articulo, "Disponible": disp,
                      "Consumo/día": round(cpd, 2), "Lead time": p.lead_time_dias,
                      "Zona roja": z["zona_roja"], "Zona amarilla": z["zona_amarilla"],
                      "Zona verde": z["zona_verde"], "Tope mín.": z["tope_min"],
                      "Punto de reposición": z["tope_reposicion"],
                      "Tope máx.": z["tope_max"], "% buffer": round(pct, 1),
                      "Alerta": est,
                      "Sugerido": max(0.0, round(z["tope_max"] - disp, 1))})
    return filas


def _tablero(user):
    pid = alcance_proveedor(user)
    with session_scope() as s:
        filas = _calcular(s, pid)
    if not filas:
        st.info("Aún no hay artículos parametrizados. Use la pestaña «Parametrización».")
        return
    df = pd.DataFrame(filas)
    c = st.columns(4)
    for i, (et, col) in enumerate([("ROJO OSCURO", "#991b1b"), ("ROJO", "#e02424"),
                                   ("AMARILLO", "#f59e0b"), ("VERDE", "#16a34a")]):
        ui.kpi(c[i], et, int((df.Alerta == et).sum()), "artículos", col)

    orden = df.sort_values("% buffer").head(25)
    fig = go.Figure()
    for col, nombre in (("Zona roja", "#e02424"), ("Zona amarilla", "#f59e0b"),
                        ("Zona verde", "#16a34a")):
        fig.add_bar(y=orden["Artículo"], x=orden[col], name=col, orientation="h",
                    marker_color=nombre)
    fig.add_scatter(y=orden["Artículo"], x=orden["Disponible"], mode="markers",
                    name="Disponible", marker=dict(color="#0f172a", size=10,
                                                   symbol="diamond"))
    fig.update_layout(barmode="stack", height=max(340, 22 * len(orden)),
                      margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", y=1.08))
    st.plotly_chart(fig, use_container_width=True)

    def pintar(row):
        c = COLOR_DDMRP.get(row["Alerta"], "#6b7280") + "22"
        return [f"background-color: {c}"] * len(row)

    st.dataframe(df.style.apply(pintar, axis=1), use_container_width=True,
                 hide_index=True)
    st.download_button("Exportar a Excel", ui.exportar_excel({"ddmrp": df}),
                       "ddmrp.xlsx", key="exp_ddm")


def _parametrizar(user):
    if not puede(user, "ddmrp"):
        st.warning("Su rol no parametriza DDMRP.")
        return
    pid = ui.selector_proveedor(user, key="ddm_prov")
    if not pid:
        return

    ui.boton_plantilla("ddmrp", key="ddm")
    arch = st.file_uploader("Cargue masivo de parámetros", type=["csv", "xlsx", "xls"],
                            key="up_ddm")
    df = ui.leer_archivo_tabular(arch)
    if df is not None and st.button("Cargar parámetros", type="primary"):
        n = 0
        with session_scope() as s:
            for _, r in df.iterrows():
                art = str(r.get("articulo") or "").strip()
                if not art:
                    continue
                p = s.query(DdmrpParametro).filter_by(proveedor_id=pid, articulo=art).first()
                if not p:
                    p = DdmrpParametro(proveedor_id=pid, articulo=art)
                    s.add(p)

                def num(k, d):
                    try:
                        return float(r.get(k) or d)
                    except (TypeError, ValueError):
                        return d
                p.consumo_promedio_dia = num("consumo_promedio_dia", 0)
                p.lead_time_dias = int(num("lead_time_dias", 7))
                p.factor_variabilidad = num("factor_variabilidad", 0.5)
                p.factor_lead_time = num("factor_lead_time", 0.5)
                p.moq = num("moq", 0)
                p.activo = True
                n += 1
        ui.ok(f"{n} parámetros cargados.")
        st.rerun()

    st.divider()
    st.markdown("##### Parametrizar un artículo")
    c1, c2, c3 = st.columns(3)
    art = c1.text_input("Artículo", key="ddm_art")
    lt = c2.number_input("Lead time (días)", 1, 365, 15)
    moq = c3.number_input("MOQ", 0.0, 1e9, 0.0, step=10.0)
    c1, c2, c3 = st.columns(3)
    cpd = c1.number_input("Consumo promedio/día (0 = calculado)", 0.0, 1e9, 0.0)
    fv = c2.slider("Factor de variabilidad", 0.0, 1.0, 0.5, 0.1)
    flt = c3.slider("Factor de lead time", 0.0, 1.0, 0.5, 0.1)

    if art.strip():
        with session_scope() as s:
            real = consumo_promedio_dia(s, pid, art.strip())
            disp = saldo_articulo(s, pid, art.strip(), "CRUDO", "DISPONIBLE")
        z = calcular_zonas_ddmrp(cpd or real, lt, fv, flt, moq)
        est, pct = estado_ddmrp(disp, z)
        c = st.columns(5)
        ui.kpi(c[0], "Zona roja", f"{z['zona_roja']:,.0f}", "", "#e02424")
        ui.kpi(c[1], "Zona amarilla", f"{z['zona_amarilla']:,.0f}", "", "#f59e0b")
        ui.kpi(c[2], "Zona verde", f"{z['zona_verde']:,.0f}", "", "#16a34a")
        ui.kpi(c[3], "Tope máximo", f"{z['tope_max']:,.0f}", "", "#2563eb")
        ui.kpi(c[4], "Alerta", est, f"disponible {disp:,.0f} ({pct:.0f}%)",
               COLOR_DDMRP.get(est, "#6b7280"))
        st.caption(f"Consumo calculado por la app: {real:,.2f} und/día")

    if st.button("Guardar parámetro", type="primary"):
        if not art.strip():
            ui.err("Indique el artículo.")
            return
        with session_scope() as s:
            p = s.query(DdmrpParametro).filter_by(proveedor_id=pid,
                                                  articulo=art.strip()).first()
            if not p:
                p = DdmrpParametro(proveedor_id=pid, articulo=art.strip())
                s.add(p)
            p.consumo_promedio_dia = cpd
            p.lead_time_dias = int(lt)
            p.factor_variabilidad = fv
            p.factor_lead_time = flt
            p.moq = moq
            p.activo = True
        ui.ok("Parámetro guardado.")
