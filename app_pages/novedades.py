"""Novedades: faltantes, sobrantes y averías; cola de trabajo y ajustes TD90/TD96."""
import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.models import Novedad, Recibo
from core.services import (ReglaNegocio, ajustar_novedad, guardar_archivo,\n                           leer_archivo, registrar_novedad)


def render(user):
    ui.encabezado("Novedades",
                  "Faltantes, sobrantes y averías · derivan producto DISPONIBLE o RESTRINGIDO")
    t1, t2, t3 = st.tabs(["📋 Panel", "➕ Registrar novedad", "🧾 Cola de Inventarios"])
    with t1:
        _panel(user)
    with t2:
        _registrar(user)
    with t3:
        _cola(user)


def _consultar(pid, estados=None):
    with session_scope() as s:
        q = s.query(Novedad)
        if pid:
            q = q.filter(Novedad.proveedor_id == pid)
        if estados:
            q = q.filter(Novedad.estado.in_(estados))
        novs = q.order_by(Novedad.creado_en.desc()).limit(500).all()
        return [{
            "Trazabilidad": n.documento.trz if n.documento else "",
            "Tipo": n.tipo, "Artículo": n.articulo, "Cantidad": n.cantidad,
            "Motivo": n.motivo or "", "Condición": n.condicion_resultante,
            "Estado": n.estado, "Ajuste": n.documento_ajuste or "",
            "No. ajuste": n.numero_ajuste or "",
            "Proveedor": n.proveedor.nombre if n.proveedor else "",
            "Evidencia": "Sí" if n.evidencia_id else "",
            "Fecha": n.creado_en, "_id": n.id,
        } for n in novs]


def _panel(user):
    pid = alcance_proveedor(user)
    filas = _consultar(pid)
    if not filas:
        st.info("No hay novedades registradas.")
        return
    df = pd.DataFrame(filas)
    c = st.columns(4)
    ui.kpi(c[0], "Total", len(df))
    ui.kpi(c[1], "Faltantes", int((df.Tipo == "FALTANTE").sum()), "", "#f59e0b")
    ui.kpi(c[2], "Sobrantes", int((df.Tipo == "SOBRANTE").sum()), "", "#2563eb")
    ui.kpi(c[3], "Averías", int((df.Tipo == "AVERIA").sum()), "", "#e02424")
    st.divider()
    fe = st.multiselect("Filtrar estado", sorted(df.Estado.unique()))
    if fe:
        df = df[df.Estado.isin(fe)]
    st.dataframe(df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
                 column_config={"Fecha": st.column_config.DatetimeColumn(
                     "Fecha", format="DD/MM/YYYY HH:mm")})
    st.download_button("Exportar a Excel",
                       ui.exportar_excel({"novedades": df.drop(columns=["_id"])}),
                       "novedades.xlsx", key="exp_nov")


def _registrar(user):
    if not puede(user, "novedades_registrar"):
        st.warning("Su rol no registra novedades.")
        return
    pid = ui.selector_proveedor(user, key="nov_prov")
    if not pid:
        return

    with session_scope() as s:
        recs = s.query(Recibo).filter(Recibo.proveedor_id == pid).order_by(
            Recibo.creado_en.desc()).limit(100).all()
        op_rec = {"— sin recibo asociado —": None}
        op_rec.update({f"{r.documento.trz} · {r.origen}": r.id for r in recs if r.documento})

    c1, c2, c3 = st.columns(3)
    tipo = c1.selectbox("Tipo", ["FALTANTE", "SOBRANTE", "AVERIA"])
    articulo = c2.text_input("Artículo")
    cantidad = c3.number_input("Cantidad", min_value=0.0, step=1.0)

    c1, c2 = st.columns(2)
    motivo = c1.selectbox("Motivo", ["ORIGEN", "MANIPULACION", "PUESTA_A_PUNTO", "AKT"])
    rec = c2.selectbox("Recibo relacionado", list(op_rec))

    obs = st.text_area("Observaciones", height=70)
    evid = None
    if tipo == "AVERIA":
        st.warning("Una avería exige evidencia fotográfica de la destrucción y genera "
                   "cola de trabajo para Inventarios (ajuste TD90 / TD96).", icon="📷")
        evid = st.file_uploader("Evidencia fotográfica de la destrucción",
                                type=["png", "jpg", "jpeg", "pdf"], key="ev_nov")

    if st.button("Registrar novedad", type="primary", use_container_width=True):
        try:
            with session_scope() as s:
                eid = None
                if evid is not None:
                    eid = guardar_archivo(s, evid.name, evid.getvalue(), evid.type,
                                          user["email"]).id
                n = registrar_novedad(s, recibo_id=op_rec[rec], proveedor_id=pid,
                                      articulo=articulo, tipo=tipo, cantidad=cantidad,
                                      motivo=motivo, usuario=user["email"],
                                      evidencia_id=eid, observaciones=obs or None)
                trz, cond, est = n.documento.trz, n.condicion_resultante, n.estado
            ui.ok(f"Novedad **{trz}** registrada. Producto resultante: **{cond}**. "
                  f"Estado: {est}.")
        except ReglaNegocio as e:
            ui.err(str(e))


def _cola(user):
    pid = alcance_proveedor(user)
    filas = [f for f in _consultar(pid, ["ABIERTA", "EN_COLA_INVENTARIOS"])]
    if not filas:
        st.success("No hay novedades pendientes de ajuste.", icon="✅")
        return
    st.dataframe(pd.DataFrame(filas).drop(columns=["_id"]),
                 use_container_width=True, hide_index=True)

    if not puede(user, "novedades_ajustar"):
        st.info("Solo el equipo de Inventarios monta el documento de ajuste.")
        return

    st.markdown("##### Montar documento de ajuste")
    sel = st.selectbox("Novedad", [f["Trazabilidad"] for f in filas])
    nid = next(f["_id"] for f in filas if f["Trazabilidad"] == sel)

    with session_scope() as s:
        n = s.get(Novedad, nid)
        st.caption(f"{n.tipo} · {n.articulo} · {n.cantidad:,.0f} · motivo {n.motivo}")
        if n.evidencia_id and n.evidencia:
            st.download_button(f"Ver evidencia: {n.evidencia.nombre}",
                               leer_archivo(n.evidencia),
                               file_name=n.evidencia.nombre, key=f"ev_{nid}")
        efecto = {"AVERIA": "descuenta el producto restringido (destrucción)",
                  "FALTANTE": "cierra la diferencia documental sin volver a descontar inventario",
                  "SOBRANTE": "libera el sobrante de RESTRINGIDO a DISPONIBLE"}[n.tipo]
        st.info(f"Al ajustar, el sistema {efecto}.")

    c1, c2 = st.columns(2)
    doc = c1.selectbox("Documento de ajuste", ["TD90", "TD96"])
    num = c2.text_input("No. del ajuste", placeholder="AJ-0001")

    if st.button("Aplicar ajuste y cerrar novedad", type="primary",
                 use_container_width=True):
        if not num.strip():
            ui.err("Indique el número del ajuste.")
            return
        try:
            with session_scope() as s:
                ajustar_novedad(s, novedad_id=nid, documento_ajuste=doc,
                                numero_ajuste=num.strip(), usuario=user["email"])
            ui.ok(f"Novedad cerrada con {doc} {num}. Efecto de inventario aplicado según el tipo.")
            st.rerun()
        except ReglaNegocio as e:
            ui.err(str(e))
