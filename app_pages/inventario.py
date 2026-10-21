"""Panel de control de inventario: posición, rotación, cobertura, kardex y trazabilidad."""
import datetime as dt

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import func

from core import ui
from core.auth import alcance_proveedor
from core.db import session_scope
from core.models import Articulo, Documento, Inventario, MovimientoInventario
from core.services import (SEMAFORO_COLOR, antiguedad_documentos,
                           indicadores_inventario)

ORDEN_SEM = ["CRÍTICO", "ALERTA", "ÓPTIMO", "EXCESO", "SIN MOVIMIENTO"]


def render(user):
    ui.encabezado(
        "Posición del inventario",
        "Saldo actual · detalle por artículo · evolución histórica · antigüedad · trazabilidad")
    t1, t2, t3, t4 = st.tabs([
        "📊 Posición del inventario",
        "🚫 Restringido",
        "📒 Kardex",
        "🔎 Trazabilidad",
    ])
    with t1:
        _panel(user)
    with t2:
        _restringido(user)
    with t3:
        _kardex(user)
    with t4:
        _traza(user)


def _serie_historica_articulo(pid, articulo):
    """Reconstruye la posición diaria del artículo desde el kardex."""
    with session_scope() as s:
        movs = s.query(MovimientoInventario).filter(
            MovimientoInventario.proveedor_id == pid,
            MovimientoInventario.articulo == articulo,
        ).order_by(MovimientoInventario.fecha.asc()).all()

    if not movs:
        return pd.DataFrame()

    rows = [{
        "Fecha": m.fecha,
        "Condición": m.condicion or "DISPONIBLE",
        "Cantidad": float(m.cantidad or 0),
    } for m in movs]
    d = pd.DataFrame(rows)
    d["Día"] = pd.to_datetime(d["Fecha"]).dt.date
    diario = d.groupby(["Día", "Condición"], as_index=False)["Cantidad"].sum()
    piv = diario.pivot(index="Día", columns="Condición", values="Cantidad").fillna(0)
    for col in ("DISPONIBLE", "RESTRINGIDO"):
        if col not in piv.columns:
            piv[col] = 0.0
    piv = piv.sort_index()
    piv[["DISPONIBLE", "RESTRINGIDO"]] = piv[
        ["DISPONIBLE", "RESTRINGIDO"]
    ].cumsum()
    piv["TOTAL"] = piv["DISPONIBLE"] + piv["RESTRINGIDO"]
    return piv.reset_index()


def _antiguedad_inventario(pid):
    """Antigüedad operativa: días desde el último movimiento de cada artículo."""
    with session_scope() as s:
        saldos = s.query(Inventario).filter(
            Inventario.proveedor_id == pid,
            Inventario.cantidad > 0,
        ).all()
        if not saldos:
            return pd.DataFrame()

        codigos = sorted({x.articulo for x in saldos})
        desc = {
            a.codigo: (a.descripcion or "")
            for a in s.query(Articulo).filter(Articulo.codigo.in_(codigos)).all()
        }
        ultimos = dict(
            s.query(
                MovimientoInventario.articulo,
                func.max(MovimientoInventario.fecha),
            ).filter(
                MovimientoInventario.proveedor_id == pid,
                MovimientoInventario.articulo.in_(codigos),
            ).group_by(MovimientoInventario.articulo).all()
        )

    hoy = dt.datetime.utcnow()
    agrupado = {}
    for x in saldos:
        a = agrupado.setdefault(x.articulo, {
            "Artículo": x.articulo,
            "Descripción": desc.get(x.articulo, ""),
            "Disponible": 0.0,
            "Restringido": 0.0,
            "Total": 0.0,
            "Último movimiento": ultimos.get(x.articulo),
        })
        qty = float(x.cantidad or 0)
        a["Total"] += qty
        if x.condicion == "RESTRINGIDO":
            a["Restringido"] += qty
        else:
            a["Disponible"] += qty

    out = []
    for a in agrupado.values():
        ult = a["Último movimiento"]
        dias = (hoy - ult).days if ult else None
        if dias is None:
            rango = "Sin histórico"
        elif dias <= 30:
            rango = "0-30"
        elif dias <= 60:
            rango = "31-60"
        elif dias <= 90:
            rango = "61-90"
        elif dias <= 180:
            rango = "91-180"
        else:
            rango = "180+"
        a["Días sin movimiento"] = dias
        a["Rango"] = rango
        out.append(a)
    return pd.DataFrame(out)


def _panel(user):
    c1, c2 = st.columns([2.2, 1.0])
    with c1:
        pid = ui.selector_proveedor(
            user,
            label="Proveedor",
            key="inv_panel_proveedor_v2",
            allow_blank=True,
        )
    with c2:
        dias = st.selectbox(
            "Ventana de consumo", [30, 60, 90, 180, 365], index=2,
            key="inv_panel_dias")
    if not pid:
        st.caption("Seleccione un proveedor para consultar su posición de inventario.")
        return

    with session_scope() as s:
        ind = indicadores_inventario(s, pid, dias)
    if not ind:
        st.info("Aún no hay inventario. Registre un recibo para comenzar.")
        return

    df = pd.DataFrame(ind)
    df["disponible"] = df.crudo_disp + df.proc_disp
    df["restringido"] = df.crudo_restr + df.proc_restr

    resumen_tab, articulo_tab, antig_tab = st.tabs([
        "Resumen",
        "Detalle por artículo",
        "Antigüedad",
    ])

    with resumen_tab:
        c = st.columns(5)
        ui.kpi(c[0], "Inventario total", f"{df.total.sum():,.0f}", "unidades")
        ui.kpi(c[1], "Disponible", f"{df.disponible.sum():,.0f}",
               "crudo + procesado", "#16a34a")
        ui.kpi(c[2], "Restringido", f"{df.restringido.sum():,.0f}",
               "requiere gestión", "#e02424")
        criticos = int((df.semaforo == "CRÍTICO").sum())
        ui.kpi(c[3], "Críticos", criticos, "cobertura < 7 días",
               "#e02424" if criticos else "#16a34a")
        ui.kpi(c[4], "Sin movimiento",
               int((df.semaforo == "SIN MOVIMIENTO").sum()),
               "saldo sin consumo", "#6b7280")

        st.divider()
        izq, der = st.columns([1.15, 1.0])
        with izq:
            st.markdown("###### Composición del inventario")
            pos = pd.DataFrame({
                "Categoría": [
                    "Crudo disponible", "Crudo restringido",
                    "Procesado disponible", "Procesado restringido"],
                "Unidades": [
                    df.crudo_disp.sum(), df.crudo_restr.sum(),
                    df.proc_disp.sum(), df.proc_restr.sum()]})
            fig = px.bar(
                pos, x="Unidades", y="Categoría", orientation="h",
                text_auto=".0f",
                color="Categoría",
                color_discrete_sequence=[
                    "#2563eb", "#93c5fd", "#16a34a", "#86efac"])
            fig.update_layout(
                showlegend=False, height=280,
                margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)
        with der:
            st.markdown("###### Estado de cobertura")
            g = df.groupby("semaforo").size().reindex(
                ORDEN_SEM, fill_value=0).reset_index()
            g.columns = ["Semáforo", "Artículos"]
            fig = px.bar(
                g, x="Semáforo", y="Artículos", color="Semáforo",
                text_auto=True, color_discrete_map=SEMAFORO_COLOR)
            fig.update_layout(
                showlegend=False, height=280,
                margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("###### Posición actual por artículo")
        f1, f2, f3 = st.columns([1.2, 1.2, 2.0])
        condicion_f = f1.selectbox(
            "Condición", ["TODOS", "DISPONIBLE", "RESTRINGIDO"],
            key="inv_condicion")
        estado_f = f2.selectbox(
            "Estado", ["TODOS", "CRUDO", "PROCESADO"],
            key="inv_estado")
        buscar_f = f3.text_input(
            "Buscar artículo", placeholder="Código o descripción",
            key="inv_buscar")

        with session_scope() as s:
            qsaldo = s.query(Inventario).filter(
                Inventario.proveedor_id == pid,
                Inventario.cantidad != 0,
            )
            if condicion_f != "TODOS":
                qsaldo = qsaldo.filter(Inventario.condicion == condicion_f)
            if estado_f != "TODOS":
                qsaldo = qsaldo.filter(Inventario.estado == estado_f)
            saldos = qsaldo.order_by(
                Inventario.articulo, Inventario.ubicacion).all()
            codigos = sorted({x.articulo for x in saldos})
            desc_map = {
                a.codigo: (a.descripcion or "")
                for a in s.query(Articulo).filter(
                    Articulo.codigo.in_(codigos)).all()
            } if codigos else {}

        detalle_saldo = pd.DataFrame([{
            "Artículo": x.articulo,
            "Descripción": desc_map.get(x.articulo, ""),
            "Ubicación": x.ubicacion or "",
            "Estado": x.estado,
            "Condición": x.condicion,
            "Cantidad": float(x.cantidad or 0),
            "Actualizado": x.actualizado_en,
        } for x in saldos])
        if buscar_f.strip() and not detalle_saldo.empty:
            needle = buscar_f.strip().lower()
            detalle_saldo = detalle_saldo[
                detalle_saldo["Artículo"].astype(str).str.lower().str.contains(
                    needle, regex=False)
                | detalle_saldo["Descripción"].astype(str).str.lower().str.contains(
                    needle, regex=False)
            ]
        if detalle_saldo.empty:
            st.caption("No hay saldos con esos filtros.")
        else:
            st.dataframe(
                detalle_saldo,
                use_container_width=True,
                hide_index=True,
                height=360,
                column_config={
                    "Cantidad": st.column_config.NumberColumn(
                        "Cantidad", format="%.2f"),
                    "Actualizado": st.column_config.DatetimeColumn(
                        "Actualizado", format="DD/MM/YYYY HH:mm"),
                },
            )

    with articulo_tab:
        articulos = sorted(df.articulo.astype(str).unique().tolist())
        art = st.selectbox(
            "Artículo",
            ["— Seleccione un artículo —"] + articulos,
            index=0,
            key="inv_det_articulo",
        )
        if art == "— Seleccione un artículo —":
            st.info(
                "Seleccione un artículo para ver saldo, ubicación, condición, "
                "cobertura y evolución histórica.")
        else:
            fila = df[df.articulo.astype(str) == art].iloc[0]
            k = st.columns(5)
            ui.kpi(k[0], "Total", f"{float(fila.total):,.0f}", "unidades")
            ui.kpi(k[1], "Disponible", f"{float(fila.disponible):,.0f}", "")
            ui.kpi(k[2], "Restringido", f"{float(fila.restringido):,.0f}", "")
            cob = fila.cobertura_dias
            ui.kpi(k[3], "Cobertura",
                   "∞" if pd.isna(cob) or cob == float("inf") else f"{cob:,.1f} días",
                   str(fila.semaforo))
            ui.kpi(k[4], "Rotación", f"{float(fila.rotacion_anual):,.2f}",
                   "veces/año")

            with session_scope() as s:
                saldos_art = s.query(Inventario).filter(
                    Inventario.proveedor_id == pid,
                    Inventario.articulo == art,
                    Inventario.cantidad != 0,
                ).order_by(
                    Inventario.condicion,
                    Inventario.estado,
                    Inventario.ubicacion,
                ).all()
                art_obj = s.query(Articulo).filter(
                    Articulo.codigo == art).first()
            if art_obj and art_obj.descripcion:
                st.caption(f"**{art}** · {art_obj.descripcion}")

            dfa = pd.DataFrame([{
                "Ubicación": x.ubicacion or "",
                "Estado": x.estado,
                "Condición": x.condicion,
                "Cantidad": float(x.cantidad or 0),
                "Actualizado": x.actualizado_en,
            } for x in saldos_art])
            cdet1, cdet2 = st.columns([1.0, 1.6])
            with cdet1:
                st.markdown("###### Dónde está")
                st.dataframe(
                    dfa, use_container_width=True, hide_index=True,
                    height=300,
                    column_config={
                        "Actualizado": st.column_config.DatetimeColumn(
                            "Actualizado", format="DD/MM/YYYY HH:mm")})
            with cdet2:
                st.markdown("###### Evolución histórica")
                serie = _serie_historica_articulo(pid, art)
                if serie.empty:
                    st.caption("Sin movimientos históricos para este artículo.")
                else:
                    fig = px.line(
                        serie,
                        x="Día",
                        y=["DISPONIBLE", "RESTRINGIDO", "TOTAL"],
                        labels={"value": "Unidades", "variable": "Condición"},
                    )
                    fig.update_layout(
                        height=300, margin=dict(l=0, r=0, t=10, b=0),
                        legend_title_text="")
                    st.plotly_chart(fig, use_container_width=True)

            st.markdown("###### Movimientos recientes del artículo")
            with session_scope() as s:
                movs = s.query(MovimientoInventario).filter(
                    MovimientoInventario.proveedor_id == pid,
                    MovimientoInventario.articulo == art,
                ).order_by(
                    MovimientoInventario.fecha.desc()).limit(100).all()
            mov_df = pd.DataFrame([{
                "Fecha": m.fecha,
                "Tipo": m.tipo,
                "Ubicación": m.ubicacion or "",
                "Estado": m.estado,
                "Condición": m.condicion,
                "Cantidad": float(m.cantidad or 0),
                "Saldo": float(m.saldo_resultante or 0),
                "Documento": m.referencia or "",
            } for m in movs])
            if mov_df.empty:
                st.caption("Sin movimientos.")
            else:
                st.dataframe(
                    mov_df, use_container_width=True, hide_index=True,
                    height=320,
                    column_config={"Fecha": st.column_config.DatetimeColumn(
                        "Fecha", format="DD/MM/YYYY HH:mm")})

    with antig_tab:
        ant = _antiguedad_inventario(pid)
        st.caption(
            "La antigüedad se muestra como **días desde el último movimiento** "
            "del artículo. Es una medida operativa de inmovilidad; no es FIFO por lote.")
        if ant.empty:
            st.info("No hay saldo para calcular antigüedad.")
        else:
            orden = ["0-30", "31-60", "61-90", "91-180", "180+", "Sin histórico"]
            agg = ant.groupby("Rango", as_index=False)["Total"].sum()
            agg["Rango"] = pd.Categorical(
                agg["Rango"], categories=orden, ordered=True)
            agg = agg.sort_values("Rango")
            g1, g2 = st.columns([1.0, 1.5])
            with g1:
                fig = px.bar(
                    agg, x="Rango", y="Total", text_auto=".0f",
                    labels={"Total": "Unidades"})
                fig.update_layout(
                    height=300, margin=dict(l=0, r=0, t=10, b=0))
                st.plotly_chart(fig, use_container_width=True)
            with g2:
                inmovil = ant.sort_values(
                    ["Días sin movimiento", "Total"],
                    ascending=[False, False],
                    na_position="last",
                )
                st.dataframe(
                    inmovil,
                    use_container_width=True,
                    hide_index=True,
                    height=300,
                    column_config={
                        "Último movimiento": st.column_config.DatetimeColumn(
                            "Último movimiento", format="DD/MM/YYYY HH:mm"),
                    },
                )

            sem = st.multiselect(
                "Filtrar semáforo de cobertura",
                ORDEN_SEM,
                key="inv_sem",
            )
            detalle = df[df.semaforo.isin(sem)] if sem else df
            vista = detalle.rename(columns={
                "articulo": "Artículo",
                "crudo_disp": "Crudo disp.",
                "crudo_restr": "Crudo restr.",
                "proc_disp": "Proc. disp.",
                "proc_restr": "Proc. restr.",
                "total": "Total",
                "consumo_dia": "Consumo/día",
                "cobertura_dias": "Cobertura (días)",
                "rotacion_anual": "Rotación",
                "semaforo": "Semáforo",
            }).drop(
                columns=["proveedor_id", "disponible", "restringido"],
                errors="ignore",
            )
            st.markdown("###### Cobertura y rotación por artículo")
            st.dataframe(
                vista,
                use_container_width=True,
                hide_index=True,
                height=360,
            )

    st.download_button(
        "Exportar posición de inventario a Excel",
        ui.exportar_excel({"inventario": df}),
        "posicion_inventario.xlsx",
        key="exp_inv",
    )

def _restringido(user):
    pid = ui.selector_proveedor(
        user, label="Proveedor", key="inv_restr_proveedor")
    if not pid:
        return

    with session_scope() as s:
        rows = s.query(Inventario).filter(
            Inventario.proveedor_id == pid,
            Inventario.condicion == "RESTRINGIDO",
            Inventario.cantidad > 0,
        ).order_by(
            Inventario.estado,
            Inventario.articulo,
            Inventario.ubicacion,
        ).all()
        codigos = sorted({x.articulo for x in rows})
        desc_map = {
            a.codigo: (a.descripcion or "")
            for a in s.query(Articulo).filter(Articulo.codigo.in_(codigos)).all()
        } if codigos else {}

    total = sum(float(x.cantidad or 0) for x in rows)
    crudo = sum(float(x.cantidad or 0) for x in rows if x.estado == "CRUDO")
    procesado = sum(
        float(x.cantidad or 0) for x in rows if x.estado == "PROCESADO")

    k1, k2, k3 = st.columns(3)
    ui.kpi(k1, "Restringido total", f"{total:,.0f}", "unidades", "#e02424")
    ui.kpi(k2, "Crudo restringido", f"{crudo:,.0f}", "materia prima", "#f59e0b")
    ui.kpi(k3, "Procesado restringido", f"{procesado:,.0f}", "producto", "#9333ea")

    st.caption(
        "RESTRINGIDO no está disponible para consumo/producción o despacho "
        "hasta que la novedad sea resuelta o el inventario sea reclasificado."
    )

    if not rows:
        st.success("Este proveedor no tiene inventario restringido.")
        return

    df = pd.DataFrame([{
        "Artículo": x.articulo,
        "Descripción": desc_map.get(x.articulo, ""),
        "Ubicación": x.ubicacion or "",
        "Estado": x.estado,
        "Cantidad restringida": float(x.cantidad or 0),
        "Actualizado": x.actualizado_en,
    } for x in rows])
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        height=440,
        column_config={
            "Cantidad restringida": st.column_config.NumberColumn(
                "Cantidad restringida", format="%.2f"),
            "Actualizado": st.column_config.DatetimeColumn(
                "Actualizado", format="DD/MM/YYYY HH:mm"),
        },
    )
    st.download_button(
        "Exportar restringido a Excel",
        ui.exportar_excel({"restringido": df}),
        "inventario_restringido.xlsx",
        key="exp_inv_restringido",
    )


def _kardex(user):
    pid = ui.selector_proveedor(
        user, label="Proveedor", key="inv_kdx_proveedor")
    if not pid:
        return
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
    pid = ui.selector_proveedor(
        user, label="Proveedor", key="inv_trz_proveedor")
    if not pid:
        return
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

