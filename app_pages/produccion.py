"""Producción: MPS (programa maestro) y subcontratación (transformación por BOM)."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import Bom, OrdenProduccion, ProgramaProduccion
from core.services import (ReglaNegocio, cumplimiento_mps, ejecutar_produccion,
                           explosion_bom, maximo_producible, programar_mps,
                           saldo_articulo)


def render(user):
    ui.encabezado("Producción y subcontratación",
                  "MPS con validación de existencias · la transformación consume el BOM")
    t1, t2, t3, t4 = st.tabs(["📊 Programa (MPS)", "🗓️ Programar",
                              "⚙️ Ejecutar transformación", "🧩 Explosión de BOM"])
    with t1:
        _panel(user)
    with t2:
        _programar(user)
    with t3:
        _ejecutar(user)
    with t4:
        _bom(user)


def _panel(user):
    pid = alcance_proveedor(user)
    c1, c2 = st.columns(2)
    desde = c1.date_input("Desde", dt.date.today() - dt.timedelta(days=60), key="mp_d")
    hasta = c2.date_input("Hasta", dt.date.today() + dt.timedelta(days=60), key="mp_h")

    with session_scope() as s:
        cmp_ = cumplimiento_mps(s, pid, desde, hasta)
        q = s.query(ProgramaProduccion).filter(
            ProgramaProduccion.fecha_programada >= desde,
            ProgramaProduccion.fecha_programada <= hasta)
        if pid:
            q = q.filter(ProgramaProduccion.proveedor_id == pid)
        mps = q.order_by(ProgramaProduccion.fecha_programada.desc()).limit(500).all()
        filas = [{"Trazabilidad": m.documento.trz if m.documento else "",
                  "Artículo": m.articulo, "Programado": m.cantidad_programada,
                  "Ejecutado": m.cantidad_ejecutada, "Pendiente": m.pendiente,
                  "Cumplimiento %": round(m.cumplimiento, 1), "Estado": m.estado,
                  "Fecha": m.fecha_programada, "Destino": m.ubicacion_destino or "",
                  "Proveedor": m.proveedor.nombre if m.proveedor else "",
                  "_id": m.id} for m in mps]
        ops = s.query(OrdenProduccion).filter(
            *( [OrdenProduccion.proveedor_id == pid] if pid else [])
        ).order_by(OrdenProduccion.fecha.desc()).limit(200).all()
        filas_op = [{"Trazabilidad": o.documento.trz if o.documento else "",
                     "Artículo": o.articulo, "Cantidad": o.cantidad,
                     "Componentes consumidos": len(o.consumos),
                     "Destino": o.ubicacion_destino or "", "Fecha": o.fecha}
                    for o in ops]

    c = st.columns(4)
    color = "#16a34a" if cmp_["cumplimiento_pct"] >= 90 else (
        "#f59e0b" if cmp_["cumplimiento_pct"] >= 70 else "#e02424")
    ui.kpi(c[0], "Cumplimiento MPS", f"{cmp_['cumplimiento_pct']:.0f}%",
           "programado vs. ejecutado", color)
    ui.kpi(c[1], "Programado", f"{cmp_['programado']:,.0f}", "unidades")
    ui.kpi(c[2], "Ejecutado", f"{cmp_['ejecutado']:,.0f}", "unidades", "#16a34a")
    ui.kpi(c[3], "Programas", cmp_["ordenes"])

    st.divider()
    if filas:
        st.markdown("##### Programa maestro")
        st.dataframe(pd.DataFrame(filas).drop(columns=["_id"]),
                     use_container_width=True, hide_index=True)
    if filas_op:
        st.markdown("##### Órdenes de producción ejecutadas")
        st.dataframe(pd.DataFrame(filas_op), use_container_width=True, hide_index=True,
                     column_config={"Fecha": st.column_config.DatetimeColumn(
                         "Fecha", format="DD/MM/YYYY HH:mm")})
    if not filas and not filas_op:
        st.info("Aún no hay programación de producción.")


def _programar(user):
    if not puede(user, "produccion_programar"):
        st.info("Solo Planeación sugiere qué se debe transformar.")
        return
    pid = ui.selector_proveedor(user, key="mps_prov")
    if not pid:
        return

    modo = st.radio("Captura", ["Manual", "Cargue masivo"], horizontal=True, key="m_mps")
    if modo == "Cargue masivo":
        ui.boton_plantilla("mps", key="mps")
        arch = st.file_uploader("Archivo del MPS", type=["csv", "xlsx", "xls"], key="up_mps")
        df = ui.leer_archivo_tabular(arch)
        if df is not None and st.button("Programar", type="primary"):
            okc, errores = 0, []
            with session_scope() as s:
                for _, r in df.iterrows():
                    art = str(r.get("articulo") or "").strip()
                    if not art:
                        continue
                    try:
                        f = pd.to_datetime(r.get("fecha_programada")).date()
                    except Exception:
                        f = dt.date.today()
                    try:
                        programar_mps(s, proveedor_id=pid, articulo=art,
                                      cantidad=float(r.get("cantidad") or 0),
                                      fecha_programada=f, usuario=user["email"],
                                      ubicacion_destino=str(r.get("ubicacion_destino") or ""))
                        okc += 1
                    except ReglaNegocio as e:
                        errores.append(f"{art}: {e}")
            ui.ok(f"{okc} programas creados.")
            for e in errores[:10]:
                st.warning(e, icon="⚠️")
        return

    art = st.text_input("Artículo transformado", key="mps_art",
                        help="Debe tener BOM cargado.")
    maximo, detalle = 0, []
    if art.strip():
        with session_scope() as s:
            maximo, detalle = maximo_producible(s, pid, art.strip())
        if detalle:
            st.caption(f"Máximo producible con el inventario libre: **{maximo:,.0f}** unidades")
            st.dataframe(pd.DataFrame(detalle).rename(columns={
                "componente": "Componente", "descripcion": "Descripción",
                "req_unitario": "Req. x unidad", "disponible": "Disponible",
                "comprometido": "Comprometido en MPS", "libre": "Libre",
                "max_por_componente": "Máx. por componente"}),
                use_container_width=True, hide_index=True)
        else:
            st.warning("El artículo no tiene BOM cargado: no se puede programar.", icon="⚠️")

    c1, c2, c3 = st.columns(3)
    cant = c1.number_input("Cantidad a transformar", min_value=0.0, step=1.0)
    fecha = c2.date_input("Fecha programada", dt.date.today(), key="mps_f")
    dest = c3.selectbox("Ubicación destino", [""] + ui.catalogo_ubicaciones(pid),
                        key="mps_ub")
    obs = st.text_area("Observaciones", height=68, key="mps_obs")

    if cant > maximo and detalle:
        st.error(f"La cantidad excede el máximo producible ({maximo:,.0f}). "
                 f"El sistema no permitirá programar de más.", icon="⚠️")

    if st.button("Programar producción", type="primary", use_container_width=True):
        try:
            with session_scope() as s:
                m = programar_mps(s, proveedor_id=pid, articulo=art.strip(),
                                  cantidad=cant, fecha_programada=fecha,
                                  usuario=user["email"], ubicacion_destino=dest,
                                  observaciones=obs or None)
                trz = m.documento.trz
            ui.ok(f"Programa **{trz}** creado.")
        except ReglaNegocio as e:
            ui.err(str(e))


def _ejecutar(user):
    if not puede(user, "produccion_ejecutar"):
        st.info("La transformación la ejecuta el proveedor.")
        return
    pid = ui.selector_proveedor(user, key="op_prov")
    if not pid:
        return

    with session_scope() as s:
        abiertos = s.query(ProgramaProduccion).filter(
            ProgramaProduccion.proveedor_id == pid,
            ProgramaProduccion.estado.in_(("PROGRAMADO", "EN_PROCESO"))).order_by(
            ProgramaProduccion.fecha_programada).all()
        op = {f"{m.documento.trz} · {m.articulo} · pendiente {m.pendiente:,.0f}": m.id
              for m in abiertos if m.documento}
        info = {m.id: (m.articulo, m.pendiente, m.ubicacion_destino) for m in abiertos}

    if not op:
        st.info("No hay programas de producción abiertos.")
        return

    sel = st.selectbox("Programa de producción", list(op))
    mid = op[sel]
    art, pend, dest_def = info[mid]

    with session_scope() as s:
        lineas = explosion_bom(s, art, pid)
        det = [{"Componente": b.componente, "Descripción": b.desc_componente,
                "Req. x unidad": b.cantidad,
                "Disponible": saldo_articulo(s, pid, b.componente, "CRUDO", "DISPONIBLE")}
               for b in lineas]

    c1, c2, c3 = st.columns(3)
    cant = c1.number_input("Cantidad a producir", min_value=0.0, max_value=float(pend),
                           value=float(pend), step=1.0)
    origen = c2.selectbox("Ubicación origen (componentes)",
                          [""] + ui.catalogo_ubicaciones(pid), key="op_or")
    ubics = [""] + ui.catalogo_ubicaciones(pid)
    idx = ubics.index(dest_def) if dest_def in ubics else 0
    destino = c3.selectbox("Ubicación destino (producto)", ubics, index=idx, key="op_de")

    if det:
        d = pd.DataFrame(det)
        d["Requerido total"] = d["Req. x unidad"] * cant
        d["Alcanza"] = d["Disponible"] >= d["Requerido total"]
        st.dataframe(d, use_container_width=True, hide_index=True)
        if not d["Alcanza"].all():
            st.error("No alcanza el inventario crudo para esta cantidad.", icon="⚠️")

    if st.button("Ejecutar transformación", type="primary", use_container_width=True):
        try:
            with session_scope() as s:
                o = ejecutar_produccion(s, mps_id=mid, cantidad=cant,
                                        usuario=user["email"],
                                        ubicacion_origen=origen,
                                        ubicacion_destino=destino)
                trz, n = o.documento.trz, len(o.consumos)
            ui.ok(f"Orden **{trz}** ejecutada: se consumieron {n} componentes del "
                  f"inventario crudo y se produjeron {cant:,.0f} unidades de {art} "
                  f"en inventario **PROCESADO**.")
            st.rerun()
        except ReglaNegocio as e:
            ui.err(str(e))


def _bom(user):
    pid = alcance_proveedor(user)
    st.caption("Relación de subcontratación: un artículo se transforma en otro "
               "(ensamble o relación 1 a 1).")
    art = st.text_input("Artículo transformado", key="bom_art")
    if not art.strip():
        with session_scope() as s:
            q = s.query(Bom.articulo_transformado, Bom.desc_transformado,
                        Bom.proveedor_nombre)
            if pid:
                from core.models import Proveedor
                p = s.get(Proveedor, pid)
                if p:
                    q = q.filter(Bom.proveedor_codigo == p.codigo)
            rows = q.distinct().limit(300).all()
        if rows:
            st.markdown("###### Artículos con BOM disponible")
            st.dataframe(pd.DataFrame(rows, columns=["Artículo", "Descripción",
                                                     "Proveedor"]),
                         use_container_width=True, hide_index=True, height=340)
        return

    with session_scope() as s:
        lineas = explosion_bom(s, art.strip(), pid)
        if not lineas:
            st.warning("Ese artículo no tiene BOM cargado.")
            return
        prov = pid or None
        det = [{"Secuencia": b.secuencia, "Componente": b.componente,
                "Descripción": b.desc_componente, "Cantidad x unidad": b.cantidad,
                "Disponible (crudo)": saldo_articulo(s, prov, b.componente,
                                                     "CRUDO", "DISPONIBLE")
                if prov else None} for b in lineas]
        maximo, _ = maximo_producible(s, prov, art.strip()) if prov else (0, [])
    st.dataframe(pd.DataFrame(det), use_container_width=True, hide_index=True)
    if prov:
        st.metric("Máximo producible con el inventario libre", f"{maximo:,.0f}")
