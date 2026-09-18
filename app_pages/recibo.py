"""Recibo de mercancía: documento -> revisión -> match por línea con OC -> inventario."""
import datetime as dt
import hashlib

import pandas as pd
import streamlit as st

from core import ui
from core.auth import alcance_proveedor, puede
from core.db import session_scope
from core.document_ai import analizar_documento, completar_con_catalogo
from core.models import Articulo, OrdenCompra, Proveedor, Recibo
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


def _nit_normalizado(valor):
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


def _fecha_ocr(extr):
    raw = str(((extr or {}).get("fecha") or {}).get("valor") or "").strip()
    if not raw:
        return dt.date.today()
    try:
        ts = pd.to_datetime(raw, dayfirst=True, errors="raise")
        return ts.date()
    except Exception:
        try:
            ts = pd.to_datetime(raw, errors="raise")
            return ts.date()
        except Exception:
            return dt.date.today()


def _enriquecer_extraccion(extr, proveedor_id):
    """Cruza OCR con maestros y OC para autocompletar líneas con datos reales."""
    if not extr or not extr.get("ocr_ok"):
        return extr

    with session_scope() as s:
        articulos = s.query(Articulo).filter(Articulo.activo.is_(True)).all()
        catalogo = {a.codigo: (a.descripcion or "") for a in articulos}
        completar_con_catalogo(extr, catalogo)

        # Si el documento trae OC, úsela como respaldo para líneas que el OCR
        # no pudo leer completamente. Nunca sobreescribe una cantidad OCR > 0.
        oc_num = str(((extr.get("orden_compra") or {}).get("valor") or "")).strip()
        if oc_num:
            ocs = s.query(OrdenCompra).filter(
                OrdenCompra.proveedor_id == proveedor_id,
                OrdenCompra.numero == oc_num,
                OrdenCompra.estado == "ABIERTA",
            ).order_by(OrdenCompra.id).all()

            existentes = {
                str(x.get("articulo") or "").strip().upper(): x
                for x in (extr.get("lineas") or [])
                if str(x.get("articulo") or "").strip()
            }
            for oc in ocs:
                key = oc.articulo.upper()
                if key not in existentes:
                    ln = {
                        "articulo": oc.articulo,
                        "descripcion": catalogo.get(oc.articulo, ""),
                        "cantidad_documento": max(0.0, float(oc.pendiente or 0)),
                        "cantidad_fisica": max(0.0, float(oc.pendiente or 0)),
                        "confianza": 0.75,
                        "fuente": "OC_ABIERTA",
                    }
                    extr.setdefault("lineas", []).append(ln)
                    existentes[key] = ln
                elif float(existentes[key].get("cantidad_documento") or 0) <= 0:
                    existentes[key]["cantidad_documento"] = max(
                        0.0, float(oc.pendiente or 0))
                    existentes[key]["cantidad_fisica"] = max(
                        0.0, float(oc.pendiente or 0))
                    existentes[key]["fuente"] = "OC_ABIERTA"

        # Sugiere proveedor origen por NIT, solo si existe en el maestro.
        nit = _nit_normalizado(((extr.get("nit") or {}).get("valor")))
        prov_origen_id = None
        if nit:
            for p in s.query(Proveedor).filter(
                    Proveedor.activo.is_(True), Proveedor.id != proveedor_id).all():
                if _nit_normalizado(p.nit) == nit:
                    prov_origen_id = p.id
                    break
        extr["proveedor_origen_sugerido_id"] = prov_origen_id

    extr["requiere_revision"] = (
        float(extr.get("confianza_texto") or 0) < 0.85
        or not extr.get("lineas")
        or any(float(x.get("cantidad_documento") or 0) <= 0
               for x in extr.get("lineas", []))
    )
    return extr


def _extraer_documento(soporte, proveedor_id):
    """Procesa automáticamente un archivo nuevo; no requiere botón intermedio."""
    if soporte is None:
        return None, "manual"

    contenido = soporte.getvalue()
    digest = hashlib.sha256(contenido).hexdigest()[:16]
    clave = f"extract_{soporte.name}_{digest}_{proveedor_id}"

    if clave not in st.session_state:
        with st.spinner("Procesando documento automáticamente..."):
            try:
                extr = analizar_documento(soporte.name, contenido, soporte.type)
                st.session_state[clave] = _enriquecer_extraccion(extr, proveedor_id)
            except Exception as e:
                st.session_state[clave] = {
                    "ocr_ok": False,
                    "texto": "",
                    "lineas": [],
                    "metodo": "ERROR",
                    "confianza_texto": 0.0,
                    "requiere_revision": True,
                    "diagnostico": f"{type(e).__name__}: {e}",
                }

    return st.session_state.get(clave), digest


def _registrar(user):
    if not puede(user, "recibo_registrar"):
        st.warning("Su rol no registra recibos.")
        return

    pid = ui.selector_proveedor(user, key="rec_prov")
    if not pid:
        return

    soporte = st.file_uploader(
        "Documento de entrada (PDF o foto)",
        type=["pdf", "png", "jpg", "jpeg"],
        key="rec_soporte",
        help="Al cargar el archivo se procesa automáticamente y se completan los campos detectados.")

    extr, digest = _extraer_documento(soporte, pid)
    suffix = digest or "manual"

    origenes = list(ORIGENES)
    origen_sugerido = (extr or {}).get("origen_sugerido")
    indice_origen = origenes.index(origen_sugerido) if origen_sugerido in origenes else 0
    origen = st.radio(
        "Origen del recibo", origenes,
        index=indice_origen,
        format_func=lambda k: ORIGENES[k],
        horizontal=False,
        key=f"rec_origen_{suffix}")

    if extr:
        cf = int(100 * float(extr.get("confianza_texto") or 0))
        metodo = extr.get("metodo", "")
        diagnostico = str(extr.get("diagnostico") or "").strip()

        if not extr.get("ocr_ok"):
            st.error(
                "No fue posible extraer texto del documento. "
                "Puede continuar con captura manual o revisar el diagnóstico.")
            if diagnostico:
                with st.expander("Diagnóstico OCR"):
                    st.code(diagnostico)
        elif extr.get("requiere_revision"):
            st.warning(
                f"Documento procesado automáticamente por {metodo} con confianza "
                f"aproximada {cf}%. Revise los campos marcados antes de crear el recibo.")
        else:
            st.success(
                f"Documento procesado automáticamente por {metodo}. "
                f"Confianza aproximada: {cf}%.")

        if diagnostico:
            st.caption(f"Diagnóstico OCR: {diagnostico}")

        with st.expander("Texto detectado", expanded=False):
            texto_ocr = extr.get("texto", "")
            st.text(texto_ocr[:12000] if texto_ocr else "Sin texto detectado.")

        oc_detectada = str(((extr.get("orden_compra") or {}).get("valor") or "")).strip()
        if oc_detectada:
            st.caption(f"OC detectada: **{oc_detectada}**")

    # Proveedor origen: si el NIT coincide con el maestro, queda preseleccionado.
    prov_origen_id = None
    if origen == "FACTURA":
        with session_scope() as s:
            provs = s.query(Proveedor).filter(
                Proveedor.activo.is_(True), Proveedor.id != pid
            ).order_by(Proveedor.nombre).all()
            ops = {"— No identificado —": None}
            ops.update({f"{p.nombre} ({p.codigo})": p.id for p in provs})

        sugerido_id = (extr or {}).get("proveedor_origen_sugerido_id")
        labels = list(ops)
        sugerido_label = next(
            (label for label, value in ops.items() if value == sugerido_id),
            labels[0])
        sel_po = st.selectbox(
            "Proveedor ORIGEN", labels,
            index=labels.index(sugerido_label),
            key=f"rec_prov_origen_{suffix}")
        prov_origen_id = ops[sel_po]

    ref_sugerida = str(((extr or {}).get("referencia") or {}).get("valor") or "")
    fecha_sugerida = _fecha_ocr(extr)

    ubicaciones_validas = ui.catalogo_ubicaciones(pid)
    ubicaciones = [""] + ubicaciones_validas
    ubi_ocr = str(
        (((extr or {}).get("ubicacion_destino") or {}).get("valor") or "")
    ).strip().upper()
    ubi_default = next(
        (u for u in ubicaciones_validas if str(u).upper() == ubi_ocr), "")
    idx_ubi = ubicaciones.index(ubi_default) if ubi_default in ubicaciones else 0

    c1, c2, c3 = st.columns(3)
    referencia = c1.text_input(
        "Referencia / No. documento",
        value=ref_sugerida,
        placeholder="BIN2686958 · FV-3-8619",
        key=f"rec_ref_{suffix}")
    fecha_doc = c2.date_input(
        "Fecha del documento",
        value=fecha_sugerida,
        key=f"rec_fecha_{suffix}")
    ubic_dest = c3.selectbox(
        "Ubicación destino",
        ubicaciones,
        index=idx_ubi,
        key=f"rec_dest_{suffix}")

    c1, c2 = st.columns([1, 2])
    reproceso = c1.checkbox(
        "Es reproceso (garantía/calidad)",
        value=bool((extr or {}).get("es_reproceso_sugerido", False)),
        key=f"rec_repro_{suffix}")
    obs = c2.text_input("Observación general", key=f"rec_obs_{suffix}")

    st.markdown("##### Líneas del recibo")
    modos = ["Documento leído", "Manual", "Cargue masivo"]
    modo_default = 0 if soporte is not None else 1
    modo = st.radio(
        "Captura", modos,
        index=modo_default,
        horizontal=True,
        key=f"modo_rec_{suffix}")

    lineas_df = None
    if modo == "Cargue masivo":
        ui.boton_plantilla("recibo_lineas", key=f"rec_{suffix}")
        arch = st.file_uploader(
            "Archivo de líneas", type=["csv", "xlsx", "xls"],
            key=f"up_rec_{suffix}")
        lineas_df = ui.leer_archivo_tabular(arch)

    elif modo == "Documento leído":
        lineas = list((extr or {}).get("lineas") or [])
        if not lineas:
            st.info(
                "El OCR leyó el documento, pero no pudo identificar líneas de artículo. "
                "Puede completar la tabla manualmente.")

        ubi_desde_ocr = str(
            (((extr or {}).get("ubicacion_origen") or {}).get("valor") or "")
        ).strip().upper()
        ubi_desde = next(
            (u for u in ubicaciones_validas if str(u).upper() == ubi_desde_ocr), "")

        base = pd.DataFrame(lineas or [{
            "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
            "cantidad_fisica": 0.0, "lote": "", "serial": "",
            "ubicacion_desde": ubi_desde, "ubicacion_hasta": ubic_dest,
        }])

        for col, default in (
            ("lote", ""), ("serial", ""),
            ("ubicacion_desde", ubi_desde),
            ("ubicacion_hasta", ubic_dest),
        ):
            if col not in base.columns:
                base[col] = default
            elif default:
                base[col] = base[col].replace("", default)

        base = base.drop(columns=["confianza", "fuente"], errors="ignore")
        lineas_df = st.data_editor(
            base,
            num_rows="dynamic",
            use_container_width=True,
            key=f"ed_rec_ai_{suffix}",
            column_config={
                "articulo": st.column_config.TextColumn("Artículo", required=True),
                "descripcion": st.column_config.TextColumn("Descripción"),
                "cantidad_documento": st.column_config.NumberColumn(
                    "Cantidad documento", min_value=0.0),
                "cantidad_fisica": st.column_config.NumberColumn(
                    "Cantidad física", min_value=0.0),
            })

        detectadas = [
            x for x in lineas
            if str(x.get("articulo") or "").strip()
        ]
        if detectadas:
            st.caption(
                f"{len(detectadas)} línea(s) detectada(s) automáticamente. "
                "Confirme cantidades antes de crear el recibo.")

    else:
        base = pd.DataFrame([{
            "articulo": "", "descripcion": "", "cantidad_documento": 0.0,
            "cantidad_fisica": 0.0, "lote": "", "serial": "",
            "ubicacion_desde": "", "ubicacion_hasta": ubic_dest,
        }])
        lineas_df = st.data_editor(
            base, num_rows="dynamic", use_container_width=True,
            key=f"ed_rec_manual_{suffix}")

    if lineas_df is not None and not lineas_df.empty:
        st.caption(
            "Los datos autocompletados son una propuesta: el usuario confirma antes "
            "de que el documento afecte el flujo.")

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
