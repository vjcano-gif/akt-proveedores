"""Panel de control de inventario: posición, rotación, cobertura, kardex y trazabilidad."""
import datetime as dt

import pandas as pd
import plotly.express as px
import streamlit as st

from core import ui
from core.auth import alcance_proveedor
from core.db import session_scope
from core.models import Documento, Inventario, MovimientoInventario
from core.services import (SEMAFORO_COLOR, antiguedad_documentos,
                           indicadores_inventario)

ORDEN_SEM = ["CRÍTICO", "ALERTA", "ÓPTIMO", "EXCESO", "SIN MOVIMIENTO"]


def render(user):
    ui.encabezado("Panel de control de inventario",
                  "Posición · rotación · cobertura · kardex · trazabilidad")
    t1, t2, t3 = st.tabs(["📊 Posición y rotación", "📒 Kardex", "🔎 Trazabilidad"])
    with t1:
        _panel(user)
    with t2:
        _kardex(user)
    with t3:
        _traza(user)


def _panel(user):
    pid = alcance_proveedor(user)
    c1, c2 = st.columns([1, 3])
    dias = c1.selectbox("Ventana de consumo", [30, 60, 90, 180, 365], index=2)

    with session_scope() as s:
        ind = indicadores_inventario(s, pid, dias)
    if not ind:
        st.info("Aún no hay inventario. Registre un recibo para comenzar.")
        return

    df = pd.DataFrame(ind)
    df["disponible"] = df.crudo_disp + df.proc_disp
    df["restringido"] = df.crudo_restr + df.proc_restr

    c = st.columns(5)
    ui.kpi(c[0], "Crudo disponible", f"{df.crudo_disp.sum():,.0f}", "materia prima", "#2563eb")
    ui.kpi(c[1], "Procesado disponible", f"{df.proc_disp.sum():,.0f}",
           "listo para despacho", "#16a34a")
    ui.kpi(c[2], "Restringido", f"{df.restringido.sum():,.0f}",
           "averías / novedades", "#e02424")
    criticos = int((df.semaforo == "CRÍTICO").sum())
    ui.kpi(c[3], "Artículos críticos", criticos, "cobertura < 7 días",
           "#e02424" if criticos else "#16a34a")
    ui.kpi(c[4], "Sin movimiento", int((df.semaforo == "SIN MOVIMIENTO").sum()),
           "con saldo y sin salidas", "#6b7280")

    st.divider()
    izq, der = st.columns([1, 1])
    with izq:
        st.markdown("###### Posición del inventario por estado")
        pos = pd.DataFrame({
            "Categoría": ["Crudo disponible", "Crudo restringido",
                          "Procesado disponible", "Procesado restringido"],
            "Unidades": [df.crudo_disp.sum(), df.crudo_restr.sum(),
                         df.proc_disp.sum(), df.proc_restr.sum()]})
        fig = px.bar(pos, x="Unidades", y="Categoría", orientation="h",
                     color="Categoría", text_auto=".0f",
                     color_discrete_sequence=["#2563eb", "#93c5fd", "#16a34a", "#86efac"])
        fig.update_layout(showlegend=False, height=270, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
    with der:
        st.markdown("###### Semáforo de cobertura")
        g = df.groupby("semaforo").size().reindex(ORDEN_SEM, fill_value=0).reset_index()
        g.columns = ["Semáforo", "Artículos"]
        fig = px.bar(g, x="Semáforo", y="Artículos", color="Semáforo", text_auto=True,
                     color_discrete_map=SEMAFORO_COLOR)
        fig.update_layout(showlegend=False, height=270, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("###### Rotación vs. cobertura")
    top = df.nlargest(40, "total")
    fig = px.scatter(top, x="cobertura_dias", y="rotacion_anual", size="total",
                     color="semaforo", hover_name="articulo",
                     color_discrete_map=SEMAFORO_COLOR,
                     labels={"cobertura_dias": "Cobertura (días)",
                             "rotacion_anual": "Rotación (veces/año)",
                             "semaforo": "Semáforo"})
    fig.update_layout(height=330, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("###### Detalle por artículo")
    sem = st.multiselect("Filtrar por semáforo", ORDEN_SEM, key="inv_sem")
    v = df[df.semaforo.isin(sem)] if sem else df
    vista = v.rename(columns={
        "articulo": "Artículo", "crudo_disp": "Crudo disp.", "crudo_restr": "Crudo restr.",
        "proc_disp": "Proc. disp.", "proc_restr": "Proc. restr.", "total": "Total",
        "consumo_dia": "Consumo/día", "cobertura_dias": "Cobertura (días)",
        "rotacion_anual": "Rotación", "semaforo": "Semáforo"}).drop(
        columns=["proveedor_id", "disponible", "restringido"], errors="ignore")

    def pintar(row):
        c = SEMAFORO_COLOR.get(row["Semáforo"], "#6b7280") + "22"
        return [f"background-color: {c}"] * len(row)

    st.dataframe(vista.style.apply(pintar, axis=1), use_container_width=True,
                 hide_index=True, height=400)
    st.download_button("Exportar a Excel", ui.exportar_excel({"inventario": vista}),
                       "panel_inventario.xlsx", key="exp_inv")


def _kardex(user):
    pid = alcance_proveedor(user)
    c1, c2, c3 = st.columns(3)
    art = c1.text_input("Artículo (opcional)", key="kdx_art")
    tipos = c2.multiselect("Tipo de movimiento", [
        "ENTRADA", "SALIDA", "TRANSFORMACION_CONSUMO", "TRANSFORMACION_PRODUCCION",
        "AJUSTE", "DESPACHO", "CONTEO", "RECLASIFICACION"], key="kdx_tipo")
    dias = c3.number_input("Últimos N días", 1, 3650, 90, key="kdx_dias")

    with session_scope() as s:
        q = s.query(MovimientoInventario).filter(
            MovimientoInventario.fecha >= dt.datetime.utcnow() - dt.timedelta(days=int(dias)))
        if pid:
            q = q.filter(MovimientoInventario.proveedor_id == pid)
        if art.strip():
            q = q.filter(MovimientoInventario.articulo == art.strip())
        if tipos:
            q = q.filter(MovimientoInventario.tipo.in_(tipos))
        movs = q.order_by(MovimientoInventario.fecha.desc()).limit(2000).all()
        filas = [{"Fecha": m.fecha, "Tipo": m.tipo, "Artículo": m.articulo,
                  "Ubicación": m.ubicacion or "", "Estado": m.estado,
                  "Condición": m.condicion, "Cantidad": m.cantidad,
                  "Saldo": m.saldo_resultante, "Documento": m.referencia or "",
                  "Usuario": m.usuario or ""} for m in movs]
    if not filas:
        st.info("Sin movimientos con ese filtro.")
        return
    df = pd.DataFrame(filas)
    st.caption(f"{len(df)} movimientos")
    st.dataframe(df, use_container_width=True, hide_index=True, height=480,
                 column_config={"Fecha": st.column_config.DatetimeColumn(
                     "Fecha", format="DD/MM/YYYY HH:mm")})
    st.download_button("Exportar a Excel", ui.exportar_excel({"kardex": df}),
                       "kardex.xlsx", key="exp_kdx")


def _traza(user):
    pid = alcance_proveedor(user)
    c1, c2, c3 = st.columns([1.7, 1.0, 1.0])
    busq = c1.text_input(
        "Buscar por ID de trazabilidad o referencia",
        placeholder="TRZ-BIN-2026-000001 · BIN2686958",
    )
    periodo = c2.selectbox(
        "Periodo",
        ["Todo histórico", "Últimos 30 días", "Últimos 90 días",
         "Último año", "Últimos 3 años"],
        index=0,
        key="trz_periodo",
    )
    soporte_filtro = c3.selectbox(
        "Soporte",
        ["Todos", "Con soporte", "Sin soporte"],
        index=0,
        key="trz_soporte",
    )
    dias_periodo = {
        "Últimos 30 días": 30,
        "Últimos 90 días": 90,
        "Último año": 365,
        "Últimos 3 años": 1095,
    }.get(periodo)

    with session_scope() as s:
        q = s.query(Documento)
        if pid:
            q = q.filter(Documento.proveedor_id == pid)
        if dias_periodo:
            q = q.filter(
                Documento.creado_en >= (
                    dt.datetime.utcnow() - dt.timedelta(days=int(dias_periodo))
                )
            )
        if busq.strip():
            like = f"%{busq.strip()}%"
            q = q.filter(
                (Documento.trz.ilike(like))
                | (Documento.referencia.ilike(like))
            )
        if soporte_filtro == "Con soporte":
            q = q.filter(Documento.archivo_id.is_not(None))
        elif soporte_filtro == "Sin soporte":
            q = q.filter(Documento.archivo_id.is_(None))

        # Trazabilidad histórica: no se recorta a los últimos 500 documentos.
        docs = q.order_by(Documento.creado_en.desc()).all()
        filas = [{
            "Trazabilidad": d.trz,
            "Tipo": d.tipo,
            "Referencia": d.referencia or "",
            "Proveedor": d.proveedor.nombre if d.proveedor else "",
            "Fecha documento": d.fecha_documento,
            "Antigüedad (días)": d.antiguedad_dias,
            "Creado por": d.creado_por or "",
            "Creado": d.creado_en,
            "Soporte guardado": "Sí" if d.archivo else "No",
            "Archivo original": d.archivo.nombre if d.archivo else "",
        } for d in docs]

        # Los KPI se mantienen acotados; la lista principal sí es histórica.
        ant = antiguedad_documentos(s, pid, limite=1000)

    if not filas:
        st.info("No hay documentos.")
        return

    dd = pd.DataFrame(ant)
    c = st.columns(5)
    for i, r in enumerate(["0-7", "8-30", "31-60", "61-90", "90+"]):
        n = int((dd.rango == r).sum()) if not dd.empty else 0
        color = ["#16a34a", "#22c55e", "#f59e0b", "#fb923c", "#e02424"][i]
        ui.kpi(c[i], f"{r} días", n, "documentos", color)

    st.divider()
    st.caption(
        f"**{len(filas):,}** documento(s) encontrados. "
        "Los soportes originales vinculados al TRZ se conservan para auditoría."
    )
    df = pd.DataFrame(filas)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=420,
        column_config={
            "Creado": st.column_config.DatetimeColumn(
                "Creado", format="DD/MM/YYYY HH:mm"),
            "Fecha documento": st.column_config.DateColumn(
                "Fecha documento", format="DD/MM/YYYY"),
        },
    )
    st.download_button(
        "Exportar a Excel",
        ui.exportar_excel({"documentos": df}),
        "trazabilidad.xlsx",
        key="exp_trz",
    )

    sel = st.selectbox(
        "Consultar / descargar soporte del documento",
        ["—"] + df.Trazabilidad.tolist(),
    )
    if sel != "—":
        with session_scope() as s:
            d = s.query(Documento).filter(Documento.trz == sel).first()
            if d and d.archivo:
                a = d.archivo
                st.caption(
                    f"**{a.nombre}** · {a.mime or 'tipo desconocido'} · "
                    f"{int(a.tamano or 0):,} bytes · "
                    f"SHA-256: {(a.sha256 or '')[:16]}…"
                )
                contenido = leer_archivo(a)
                if contenido:
                    st.download_button(
                        f"Descargar soporte original: {a.nombre}",
                        contenido,
                        file_name=a.nombre,
                        mime=a.mime or "application/octet-stream",
                        key="dl_trz",
                    )
                else:
                    st.error(
                        "El documento conserva la referencia del soporte, "
                        "pero no fue posible recuperar el archivo."
                    )
            else:
                st.caption("Ese documento no tiene soporte adjunto.")

