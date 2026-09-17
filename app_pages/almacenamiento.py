"""Almacenamiento: conteos cíclicos, foto del ERP y análisis de discrepancias."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import ConteoEjecutado, ConteoProgramado, FotoInventarioERP
from core.services import (ReglaNegocio, ajustar_conteo, cumplimiento_conteos,
                           ejecutar_conteo, programar_conteo, saldo_articulo,
                           sugerir_conteos_por_foto_erp)


def render(user):
    ui.encabezado("Almacenamiento",
                  "Conteos cíclicos programados vs. ejecutados · foto del ERP · discrepancias")
    t1, t2, t3, t4 = st.tabs(["📊 Cumplimiento", "🗓️ Programar conteos",
                              "✅ Ejecutar conteo", "📸 Foto del inventario ERP"])
    with t1:
        _cumplimiento(user)
    with t2:
        _programar(user)
    with t3:
        _ejecutar(user)
    with t4:
        _foto_erp(user)


def _cumplimiento(user):
    pid = alcance_proveedor(user)
    c1, c2 = st.columns(2)
    desde = c1.date_input("Desde", dt.date.today() - dt.timedelta(days=90))
    hasta = c2.date_input("Hasta", dt.date.today() + dt.timedelta(days=30))

    with session_scope() as s:
        cmp_ = cumplimiento_conteos(s, pid, desde, hasta)
        q = s.query(ConteoProgramado).filter(
            ConteoProgramado.fecha_programada >= desde,
            ConteoProgramado.fecha_programada <= hasta)
        qe = s.query(ConteoEjecutado)
        if pid:
            q = q.filter(ConteoProgramado.proveedor_id == pid)
            qe = qe.filter(ConteoEjecutado.proveedor_id == pid)
        progs = q.order_by(ConteoProgramado.fecha_programada).all()
        ejecs = qe.order_by(ConteoEjecutado.fecha.desc()).limit(300).all()
        prog_filas = [{"Trazabilidad": p.documento.trz if p.documento else "",
                       "Artículo": p.articulo, "Ubicación": p.ubicacion,
                       "Programado": p.fecha_programada, "Prioridad": p.prioridad,
                       "Estado": ("VENCIDO" if p.estado == "PROGRAMADO"
                                  and p.fecha_programada < dt.date.today()
                                  else p.estado), "_id": p.id} for p in progs]
        ejec_filas = [{"Trazabilidad": e.documento.trz if e.documento else "",
                       "Artículo": e.articulo, "Ubicación": e.ubicacion,
                       "Sistema": e.cantidad_sistema, "Físico": e.cantidad_fisica,
                       "Diferencia": e.diferencia,
                       "Origen": "Aleatorio" if e.es_aleatorio else "Programado",
                       "Estado": e.estado, "Fecha": e.fecha, "_id": e.id} for e in ejecs]

    c = st.columns(4)
    color = "#16a34a" if cmp_["cumplimiento_pct"] >= 90 else (
        "#f59e0b" if cmp_["cumplimiento_pct"] >= 70 else "#e02424")
    ui.kpi(c[0], "Cumplimiento", f"{cmp_['cumplimiento_pct']:.0f}%",
           "programado vs. ejecutado", color)
    ui.kpi(c[1], "Programados", cmp_["programados"])
    ui.kpi(c[2], "Ejecutados", cmp_["ejecutados"], "", "#16a34a")
    ui.kpi(c[3], "Vencidos", cmp_["vencidos"], "sin ejecutar",
           "#e02424" if cmp_["vencidos"] else "#6b7280")

    st.divider()
    izq, der = st.columns(2)
    with izq:
        st.markdown("##### Agenda de conteos")
        st.dataframe(pd.DataFrame(prog_filas).drop(columns=["_id"], errors="ignore")
                     if prog_filas else pd.DataFrame(),
                     use_container_width=True, hide_index=True)
    with der:
        st.markdown("##### Conteos ejecutados")
        st.dataframe(pd.DataFrame(ejec_filas).drop(columns=["_id"], errors="ignore")
                     if ejec_filas else pd.DataFrame(),
                     use_container_width=True, hide_index=True)

    pend = [e for e in ejec_filas if e["Estado"] == "EN_COLA_INVENTARIOS"]
    if pend and puede(user, "novedades_ajustar"):
        st.markdown("##### Ajustar conteo con discrepancia")
        sel = st.selectbox("Conteo", [f"{e['Trazabilidad']} · {e['Artículo']} · "
                                      f"dif {e['Diferencia']:+,.0f}" for e in pend])
        cid = pend[[f"{e['Trazabilidad']} · {e['Artículo']} · dif {e['Diferencia']:+,.0f}"
                    for e in pend].index(sel)]["_id"]
        c1, c2 = st.columns(2)
        doc = c1.selectbox("Documento de ajuste", ["TD90", "TD96"], key="cta_doc")
        num = c2.text_input("No. del ajuste", key="cta_num")
        if st.button("Aplicar ajuste", type="primary"):
            try:
                with session_scope() as s:
                    ajustar_conteo(s, conteo_id=cid, documento_ajuste=doc,
                                   numero_ajuste=num or "S/N", usuario=user["email"], actor=user)
                ui.ok("Conteo ajustado e inventario actualizado.")
                st.rerun()
            except ReglaNegocio as e:
                ui.err(str(e))


def _programar(user):
    if not puede(user, "conteos_programar"):
        st.info("Solo el equipo de Inventarios programa la agenda de conteos.")
        return
    pid = ui.selector_proveedor(user, key="cta_prov")
    if not pid:
        return

    modo = st.radio("Captura", ["Manual", "Cargue masivo"], horizontal=True, key="m_cta")
    if modo == "Cargue masivo":
        ui.boton_plantilla("conteos", key="cta")
        arch = st.file_uploader("Archivo de conteos", type=["csv", "xlsx", "xls"],
                                key="up_cta")
        df = ui.leer_archivo_tabular(arch)
        if df is not None and st.button("Programar los conteos del archivo",
                                        type="primary"):
            n = 0
            with session_scope() as s:
                for _, r in df.iterrows():
                    art = str(r.get("articulo") or "").strip()
                    if not art:
                        continue
                    try:
                        f = pd.to_datetime(r.get("fecha_programada")).date()
                    except Exception:
                        f = dt.date.today()
                    programar_conteo(s, proveedor_id=pid, articulo=art,
                                     ubicacion=str(r.get("ubicacion") or ""),
                                     fecha_programada=f,
                                     prioridad=int(float(r.get("prioridad") or 3)),
                                     usuario=user["email"], actor=user)
                    n += 1
            ui.ok(f"{n} conteos programados.")
        return

    c1, c2, c3, c4 = st.columns(4)
    art = c1.text_input("Artículo", key="cta_art")
    ubic = c2.selectbox("Ubicación", [""] + ui.catalogo_ubicaciones(pid), key="cta_ub")
    fecha = c3.date_input("Fecha programada", dt.date.today())
    prio = c4.selectbox("Prioridad", [1, 2, 3, 4, 5], index=2)
    if st.button("Programar conteo", type="primary"):
        if not art.strip():
            ui.err("Indique el artículo.")
            return
        with session_scope() as s:
            c = programar_conteo(s, proveedor_id=pid, articulo=art.strip(),
                                 ubicacion=ubic, fecha_programada=fecha,
                                 prioridad=prio, usuario=user["email"], actor=user)
            trz = c.documento.trz
        ui.ok(f"Conteo programado. Trazabilidad: **{trz}**")


def _ejecutar(user):
    if not puede(user, "conteos_ejecutar"):
        st.info("Su rol no ejecuta conteos.")
        return
    pid = ui.selector_proveedor(user, key="cte_prov")
    if not pid:
        return

    with session_scope() as s:
        pend = s.query(ConteoProgramado).filter(
            ConteoProgramado.proveedor_id == pid,
            ConteoProgramado.estado == "PROGRAMADO").order_by(
            ConteoProgramado.prioridad, ConteoProgramado.fecha_programada).all()
        op = {"— conteo aleatorio (no programado) —": None}
        op.update({f"{p.articulo} · {p.ubicacion or 'sin ubicación'} · "
                   f"{p.fecha_programada} · P{p.prioridad}": p.id for p in pend})
        datos = {p.id: (p.articulo, p.ubicacion) for p in pend}

    sel = st.selectbox("Conteo programado", list(op))
    prog_id = op[sel]

    c1, c2, c3 = st.columns(3)
    if prog_id:
        art = datos[prog_id][0]
        ubic = datos[prog_id][1]
        c1.text_input("Artículo", art, disabled=True)
        c2.text_input("Ubicación", ubic or "", disabled=True)
    else:
        art = c1.text_input("Artículo", key="cte_art")
        ubic = c2.selectbox("Ubicación", [""] + ui.catalogo_ubicaciones(pid), key="cte_ub")
    fisica = c3.number_input("Cantidad física contada", min_value=0.0, step=1.0)

    if art:
        with session_scope() as s:
            sis = saldo_articulo(s, pid, art, "CRUDO", "DISPONIBLE")
        st.caption(f"Saldo en sistema (crudo disponible): **{sis:,.0f}** · "
                   f"diferencia con lo contado: **{fisica - sis:+,.0f}**")

    obs = st.text_area("Observaciones", height=68, key="cte_obs")
    if st.button("Registrar conteo", type="primary", use_container_width=True):
        if not str(art).strip():
            ui.err("Indique el artículo.")
            return
        with session_scope() as s:
            ce = ejecutar_conteo(s, proveedor_id=pid, articulo=art,
                                 cantidad_fisica=fisica, ubicacion=ubic or "",
                                 programado_id=prog_id, usuario=user["email"],
                                 observaciones=obs or None)
            dif, cola, trz = ce.diferencia, ce.genera_cola, ce.documento.trz
        if cola:
            st.error(f"Conteo **{trz}** con discrepancia de {dif:+,.0f}. "
                     f"Se generó cola de trabajo para el análisis del equipo de "
                     f"Inventarios.", icon="⚠️")
        else:
            ui.ok(f"Conteo **{trz}** sin discrepancias.")


def _foto_erp(user):
    if not puede(user, "foto_erp"):
        st.info("Solo el equipo de Inventarios carga la foto del ERP.")
        return
    st.caption("Suba la foto actual del inventario del ERP para compararla contra la "
               "app y agendar los conteos en orden de prioridad.")
    pid = ui.selector_proveedor(user, key="erp_prov")
    if not pid:
        return
    ui.boton_plantilla("foto_erp", key="erp")
    c1, c2 = st.columns(2)
    corte = c1.date_input("Fecha de corte", dt.date.today())
    arch = c2.file_uploader("Archivo del ERP", type=["csv", "xlsx", "xls"], key="up_erp")
    df = ui.leer_archivo_tabular(arch)
    if df is not None:
        st.dataframe(df.head(15), use_container_width=True, hide_index=True)
        if st.button("Cargar foto del ERP", type="primary"):
            n = 0
            with session_scope() as s:
                s.query(FotoInventarioERP).filter(
                    FotoInventarioERP.corte == corte,
                    FotoInventarioERP.proveedor_id == pid).delete()
                for _, r in df.iterrows():
                    art = str(r.get("articulo") or "").strip()
                    if not art:
                        continue
                    try:
                        q = float(str(r.get("cantidad_erp") or 0).replace(",", ""))
                    except ValueError:
                        q = 0.0
                    s.add(FotoInventarioERP(
                        corte=corte, proveedor_id=pid, articulo=art,
                        ubicacion=str(r.get("ubicacion") or ""), cantidad_erp=q,
                        cargado_por=user["email"]))
                    n += 1
            ui.ok(f"{n} registros cargados.")
            st.rerun()

    st.divider()
    st.markdown("##### Comparación ERP vs. App — prioridad de conteo")
    with session_scope() as s:
        sug = sugerir_conteos_por_foto_erp(s, corte=corte, proveedor_id=pid, limite=200)
    if not sug:
        st.info("Cargue la foto del ERP para ver la comparación.")
        return
    d = pd.DataFrame(sug).drop(columns=["abs", "proveedor_id"])
    d = d.rename(columns={"articulo": "Artículo", "ubicacion": "Ubicación",
                          "erp": "ERP", "app": "App", "diferencia": "Diferencia",
                          "prioridad": "Prioridad"})
    st.dataframe(d, use_container_width=True, hide_index=True)
    if st.button("Agendar conteo para las diferencias de prioridad 1"):
        n = 0
        with session_scope() as s:
            for row in sug:
                if row["prioridad"] == 1 and abs(row["diferencia"]) > 0:
                    programar_conteo(s, proveedor_id=pid, articulo=row["articulo"],
                                     ubicacion=row["ubicacion"], prioridad=1,
                                     usuario=user["email"])
                    n += 1
        ui.ok(f"{n} conteos agendados con prioridad alta.")
