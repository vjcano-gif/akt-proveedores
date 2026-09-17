"""Recibo de mercancía: documento -> revisión -> match por línea con OC -> inventario."""
import datetime as dt

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.document_ai import analizar_documento
from core.models import OrdenCompra, Proveedor, Recibo
from core.services import (
    LineaRecibo, ReglaNegocio, confirmar_recibo_simple, crear_recibo,
    guardar_archivo, leer_archivo, match_recibo_lineas,
    ordenes_compra_abiertas, sellar_recibo, sugerir_oc_por_linea,
    validar_bin_a_bin,
)

ORIGENES = {
    "BIN_A_BIN": "BIN a BIN — materia prima cruda desde AKT MOTOS",
    "FACTURA": "Factura — producto desde otro proveedor ORIGEN",
    "REGISTRO": "Registro manual (sin OC)",
}


def render(user):
    ui.encabezado(
        "Recibo de mercancía",
        "Documento → revisión → match por línea contra OC → inventario disponible/restringido")
    t1, t2, t3 = st.tabs([
        "📋 Documentos", "➕ Registrar / leer documento", "🔗 Match por línea con OC"
    ])
    with t1:
        _consulta(user)
    with t2:
        _registrar(user)
    with t3:
        _match(user)


def _consulta(user):
    pid = alcance_proveedor(user)
    c1, c2, c3 = st.columns(3)
    estado = c1.multiselect(
        "Estado", ["BORRADOR", "PENDIENTE_MATCH", "NOVEDAD", "CERRADA"], default=[])
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
            filas.append({
                "Trazabilidad": r.documento.trz if r.documento else "",
                "Origen": r.origen,
                "Referencia": r.documento.referencia if r.documento else "",
                "Transformador": r.proveedor.nombre if r.proveedor else "",
                "Proveedor origen": r.proveedor_origen.nombre if r.proveedor_origen else "",
                "Estado": r.estado,
                "Líneas": len(r.lineas),
                "Cantidad física": sum(float(l.cantidad_fisica or 0) for l in r.lineas),
                "Reproceso": "Sí" if r.es_reproceso else "",
                "Fecha": r.creado_en,
                "_id": r.id,
            })

    if not filas:
        st.info("No hay recibos con ese filtro.")
        return

    df = pd.DataFrame(filas)
    st.dataframe(
        df.drop(columns=["_id"]), use_container_width=True, hide_index=True,
        column_config={"Fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm")})
    st.download_button(
        "Exportar a Excel", ui.exportar_excel({"recibos": df.drop(columns=["_id"])}),
        "recibos.xlsx", key="exp_rec")

    sel = st.selectbox("Ver detalle", ["—"] + [x["Trazabilidad"] for x in filas])
    if sel == "—":
        return
    rid = next(x["_id"] for x in filas if x["Trazabilidad"] == sel)

    with session_scope() as s:
        r = s.get(Recibo, rid)
        st.markdown(
            f"**{r.documento.trz}** · {ORIGENES.get(r.origen, r.origen)} · "
            + ui.pill(r.estado), unsafe_allow_html=True)
        c = st.columns(5)
        c[0].metric("Transformador", r.proveedor.codigo if r.proveedor else "—")
        c[1].metric("Proveedor origen", r.proveedor_origen.codigo if r.proveedor_origen else "—")
        c[2].metric("Referencia", r.documento.referencia or "—")
        c[3].metric("Fecha documento", str(r.documento.fecha_documento))
        c[4].metric("Antigüedad", f"{r.documento.antiguedad_dias} días")

        detalle = []
        for l in r.lineas:
            detalle.append({
                "Artículo": l.articulo, "Descripción": l.descripcion,
                "Cant. documento": l.cantidad_documento, "Cant. física": l.cantidad_fisica,
                "OC": l.orden_compra.numero if l.orden_compra else "",
                "Aceptado": l.cantidad_match or 0,
                "Match": l.estado_match or "",
                "Desde": l.ubicacion_desde, "Hasta": l.ubicacion_hasta,
                "Lote": l.lote,
            })
        st.dataframe(pd.DataFrame(detalle), use_container_width=True, hide_index=True)

        if r.documento.archivo:
            a = r.documento.archivo
            contenido = leer_archivo(a)
            if contenido:
                st.download_button(
                    f"Descargar soporte: {a.nombre}", contenido,
                    file_name=a.nombre, key=f"dl_{rid}")

        if (r.origen == "FACTURA" and r.estado == "BORRADOR"
                and puede(user, "recibo_sellar")
                and (not pid or r.proveedor_id == pid)):
            st.info(
                "El proveedor de transformación debe certificar la recepción. "
                "Después el documento pasa a PENDIENTE_MATCH; aún no afecta inventario.")
            if st.button("Sellar / certificar recepción", type="primary", key=f"sl_{rid}"):
                try:
                    sellar_recibo(s, rid, user["email"])
                    ui.ok("Recepción certificada. Quedó PENDIENTE_MATCH.")
                    st.rerun()
                except ReglaNegocio as e:
                    ui.err(str(e))


def _extraer_documento(soporte):
    if soporte is None:
        return None
    clave = f"extract_{soporte.name}_{soporte.size}"
    if st.button("Leer documento automáticamente", key=f"btn_{clave}"):
        with st.spinner("Leyendo documento y buscando artículos/cantidades..."):
            st.session_state[clave] = analizar_documento(
                soporte.name, soporte.getvalue(), soporte.type)
    return st.session_state.get(clave)


def _registrar(user):
    if not puede(user, "recibo_registrar"):
        st.warning("Su rol no registra recibos.")
        return

    pid = ui.selector_proveedor(user, key="rec_prov")
    if not pid:
        return

    origen = st.radio(
        "Origen del recibo", list(ORIGENES),
        format_func=lambda k: ORIGENES[k], horizontal=False)

    soporte = st.file_uploader(
        "Documento de entrada (PDF o foto)", type=["pdf", "png", "jpg", "jpeg"],
        key="rec_soporte")
    extr = _extraer_documento(soporte)

    if extr:
        cf = int(100 * float(extr.get("confianza_texto") or 0))
        metodo = extr.get("metodo", "")
        if extr.get("requiere_revision"):
            st.warning(
                f"Lectura {metodo} con confianza aproximada {cf}%. "
                "Revise los datos antes de crear el recibo.")
        else:
            st.success(f"Documento leído por {metodo}. Confianza de texto: {cf}%.")
        with st.expander("Texto detectado"):
            st.text(extr.get("texto", "")[:12000])

    prov_origen_id = None
    if origen == "FACTURA":
        with session_scope() as s:
            provs = s.query(Proveedor).filter(
                Proveedor.activo.is_(True), Proveedor.id != pid
            ).order_by(Proveedor.nombre).all()
            ops = {"— No identificado —": None}
            ops.update({f"{p.nombre} ({p.codigo})": p.id for p in provs})
        sel_po = st.selectbox("Proveedor ORIGEN", list(ops), key="rec_prov_origen")
        prov_origen_id = ops[sel_po]

    ref_sugerida = ""
    if extr:
        ref_sugerida = str((extr.get("referencia") or {}).get("valor") or "")
    c1, c2, c3 = st.columns(3)
    referencia = c1.text_input(
        "Referencia / No. documento", value=ref_sugerida,
        placeholder="BIN2686958 · FV-3-8619", key="rec_ref")
    fecha_doc = c2.date_input("Fecha del documento", dt.date.today(), key="rec_fecha")
    ubicaciones = [""] + ui.catalogo_ubicaciones(pid)
    ubic_dest = c3.selectbox("Ubicación destino", ubicaciones, key="rec_dest")

    c1, c2 = st.columns([1, 2])
    reproceso = c1.checkbox("Es reproceso (garantía/calidad)", key="rec_repro")
    obs = c2.text_input("Observación general", key="rec_obs")

    st.markdown("##### Líneas del recibo")
    modo = st.radio(
        "Captura", ["Documento leído", "Manual", "Cargue masivo"],
        index=0 if extr and extr.get("lineas") else 1,
        horizontal=True, key="modo_rec")

    lineas_df = None
    if modo == "Cargue masivo":
        ui.boton_plantilla("recibo_lineas", key="rec")
        arch = st.file_uploader(
            "Archivo de líneas", type=["csv", "xlsx", "xls"], key="up_rec")
        lineas_df = ui.leer_archivo_tabular(arch)
    elif modo == "Documento leído":
        lineas = extr.get("lineas", []) if extr else []
        if not lineas:
            st.info("No se identificaron líneas automáticamente. Use captura Manual.")
        base = pd.DataFrame(lineas or [{
            "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
            "cantidad_fisica": 0.0, "lote": "", "serial": "",
            "ubicacion_desde": "", "ubicacion_hasta": ubic_dest,
        }])
        for col in ("lote", "serial", "ubicacion_desde", "ubicacion_hasta"):
            if col not in base.columns:
                base[col] = ubic_dest if col == "ubicacion_hasta" else ""
        base = base.drop(columns=["confianza"], errors="ignore")
        lineas_df = st.data_editor(
            base, num_rows="dynamic", use_container_width=True, key="ed_rec_ai")
    else:
        base = pd.DataFrame([{
            "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
            "cantidad_fisica": 0.0, "lote": "", "serial": "",
            "ubicacion_desde": "", "ubicacion_hasta": ubic_dest,
        }])
        lineas_df = st.data_editor(
            base, num_rows="dynamic", use_container_width=True, key="ed_rec_manual")

    if lineas_df is not None and not lineas_df.empty:
        st.caption("Los datos son una propuesta. Confirme artículo y cantidad física antes de guardar.")

    if st.button("Crear recibo", type="primary", use_container_width=True):
        if lineas_df is None or lineas_df.empty:
            ui.err("Debe capturar al menos una línea.")
            return

        def num(v):
            try:
                return float(str(v).replace(",", "") or 0)
            except (TypeError, ValueError):
                return 0.0

        lineas = []
        for _, row in lineas_df.iterrows():
            cod = str(row.get("articulo") or "").strip()
            if not cod:
                continue
            lineas.append(LineaRecibo(
                articulo=cod,
                descripcion=str(row.get("descripcion") or ""),
                cantidad_documento=num(row.get("cantidad_documento")),
                cantidad_fisica=num(row.get("cantidad_fisica")),
                lote=str(row.get("lote") or ""),
                serial=str(row.get("serial") or ""),
                ubicacion_desde=str(row.get("ubicacion_desde") or ""),
                ubicacion_hasta=str(row.get("ubicacion_hasta") or ubic_dest or "")))
        if not lineas:
            ui.err("Ninguna línea tiene artículo.")
            return

        try:
            with session_scope() as s:
                arch_id = None
                if soporte is not None:
                    arch_id = guardar_archivo(
                        s, soporte.name, soporte.getvalue(), soporte.type,
                        user["email"]).id
                r = crear_recibo(
                    s, proveedor_id=pid, origen=origen, lineas=lineas,
                    referencia=referencia or None, usuario=user["email"],
                    es_reproceso=reproceso, ubicacion_destino=ubic_dest,
                    archivo_id=arch_id, fecha_documento=fecha_doc,
                    observaciones=obs or None, proveedor_origen_id=prov_origen_id,
                    factura_origen=referencia if origen == "FACTURA" else None)
                trz, rid = r.documento.trz, r.id

                alertas = validar_bin_a_bin(s, r, pid) if origen == "BIN_A_BIN" else []
                if alertas:
                    raise ReglaNegocio(" | ".join(alertas))

                if origen == "REGISTRO":
                    confirmar_recibo_simple(s, rid, user["email"])
                    msg = "Registro confirmado e ingresado al inventario."
                elif origen == "BIN_A_BIN":
                    msg = "BIN creado PENDIENTE_MATCH. No afecta inventario hasta validar las OC."
                else:
                    msg = "Factura creada en BORRADOR. El proveedor debe sellarla antes del match."

            ui.ok(f"{msg} Trazabilidad: **{trz}**")
        except ReglaNegocio as e:
            ui.err(str(e))


def _match(user):
    if not puede(user, "recibo_match_oc"):
        st.info("Solo el equipo de Recibo AKT hace el match contra órdenes de compra.")
        return

    with session_scope() as s:
        pendientes = s.query(Recibo).filter(
            Recibo.estado == "PENDIENTE_MATCH",
            Recibo.origen.in_(("BIN_A_BIN", "FACTURA"))
        ).order_by(Recibo.creado_en.desc()).limit(300).all()
        opciones = {
            f"{r.documento.trz} · {r.proveedor.nombre} · {r.origen}": r.id
            for r in pendientes if r.documento
        }

    if not opciones:
        st.info("No hay recibos pendientes de match.")
        return

    sel = st.selectbox("Recibo pendiente", list(opciones), key="match_rec")
    rid = opciones[sel]

    with session_scope() as s:
        r = s.get(Recibo, rid)
        sugerencias = sugerir_oc_por_linea(s, rid)
        st.caption(
            "Cada línea se valida contra su propia OC. La aplicación sugiere la OC abierta "
            "más antigua del mismo artículo; usted debe confirmarla.")
        if r.proveedor_origen:
            st.caption(f"Proveedor origen: **{r.proveedor_origen.nombre}**")

        asignaciones = {}
        bloqueado = False
        for sg in sugerencias:
            st.markdown(f"**{sg['articulo']}** · físico: {sg['cantidad_fisica']:,.2f}")
            alts = sg["alternativas"]
            if not alts:
                st.error("No existe una OC ABIERTA para este artículo.")
                bloqueado = True
                continue
            etiquetas = {
                f"{num} · pendiente {pend:,.2f}": oid for oid, num, pend in alts
            }
            sugerida_label = next(
                (k for k, v in etiquetas.items() if v == sg["oc_sugerida_id"]),
                list(etiquetas)[0])
            labels = list(etiquetas)
            idx = labels.index(sugerida_label)
            elegido = st.selectbox(
                "OC asignada", labels, index=idx, key=f"oc_linea_{sg['linea_id']}")
            asignaciones[int(sg["linea_id"])] = int(etiquetas[elegido])
            st.divider()

    ref_bin = st.text_input(
        "No. BIN / referencia logística (opcional)", key=f"ref_bin_{rid}")
    arch = st.file_uploader(
        "Soporte adicional del match (PDF o foto, opcional)",
        type=["pdf", "png", "jpg", "jpeg"], key=f"up_bin_{rid}")

    if st.button(
        "Validar líneas, actualizar OC e ingresar inventario",
        type="primary", use_container_width=True, disabled=bloqueado):
        try:
            with session_scope() as s:
                aid = None
                if arch is not None:
                    aid = guardar_archivo(
                        s, arch.name, arch.getvalue(), arch.type, user["email"]).id
                res = match_recibo_lineas(
                    s, recibo_id=rid, asignaciones=asignaciones,
                    usuario=user["email"], archivo_id=aid,
                    referencia_bin=ref_bin or None)

            df = pd.DataFrame(res["lineas"])
            st.dataframe(df, use_container_width=True, hide_index=True)
            if res["estado"] == "CERRADA":
                ui.ok("Todas las líneas hicieron match exacto. Recibo CERRADO.")
            else:
                st.warning(
                    f"Recibo en **{res['estado']}**. Las diferencias quedaron como "
                    "novedades; los sobrantes están RESTRINGIDOS y los faltantes no "
                    "descuentan inventario por segunda vez.")
        except ReglaNegocio as e:
            ui.err(str(e))
