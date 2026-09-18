"""Maestros: artículos, BOM, ubicaciones, proveedores, órdenes de compra y usuarios.
Todas las tablas se pueden consultar, editar y cargar masivamente con plantilla."""
import datetime as dt

import pandas as pd
import streamlit as st
from sqlalchemy import func

from core import ui
from core.auth import ROLES, hash_password, puede
from core.db import session_scope
from core.models import (Articulo, Bom, Inventario, OrdenCompra, Proveedor,
                         Ubicacion, Usuario)
from core.services import campos_faltantes_proveedor


def render(user):
    ui.encabezado("Maestros", "Consulta, edición y cargue masivo con plantilla de ejemplo")
    pestanas, funciones = [], []
    if puede(user, "maestro_articulos"):
        pestanas.append("📦 Artículos"); funciones.append(_articulos)
    if puede(user, "maestro_bom"):
        pestanas.append("🧩 BOM"); funciones.append(_bom)
    if puede(user, "maestro_ubicaciones"):
        pestanas.append("📍 Ubicaciones"); funciones.append(_ubicaciones)
    if puede(user, "maestro_proveedores"):
        pestanas.append("🏭 Proveedores"); funciones.append(_proveedores)
    if puede(user, "maestro_oc"):
        pestanas.append("🧾 Órdenes de compra"); funciones.append(_oc)
    if puede(user, "maestro_usuarios"):
        pestanas.append("👤 Usuarios"); funciones.append(_usuarios)
        pestanas.append("🧪 Datos de prueba"); funciones.append(_demo)
    if not pestanas:
        st.warning("Su rol no administra maestros.")
        return
    for tab, fn in zip(st.tabs(pestanas), funciones):
        with tab:
            fn(user)


def _cargue(nombre, procesar, ayuda=""):
    """Bloque estándar de cargue masivo con plantilla descargable."""
    with st.expander("⬆️ Cargue masivo", expanded=False):
        if ayuda:
            st.caption(ayuda)
        ui.boton_plantilla(nombre, key=f"pl_{nombre}")
        arch = st.file_uploader("Archivo", type=["csv", "xlsx", "xls"], key=f"up_{nombre}")
        df = ui.leer_archivo_tabular(arch)
        if df is not None:
            st.dataframe(df.head(15), use_container_width=True, hide_index=True)
            st.caption(f"{len(df)} filas leídas.")
            if st.button("Procesar cargue", type="primary", key=f"btn_{nombre}"):
                try:
                    creados, actualizados, errores = procesar(df)
                    ui.ok(f"{creados} creados · {actualizados} actualizados.")
                    for e in errores[:10]:
                        st.warning(e, icon="⚠️")
                    ui.limpiar_cache()
                    st.rerun()
                except Exception as e:
                    ui.err(f"Error en el cargue: {e}")


def _b(v, d=True):
    s = str(v).strip().lower()
    if s in ("", "nan", "none"):
        return d
    return s in ("true", "1", "y", "si", "sí", "yes", "activo")


# ------------------------------------------------------------------ artículos
def _articulos(user):
    st.caption("Solo el equipo de Inventarios y el usuario de Planeación usan este "
               "maestro. No se puede deshabilitar ni eliminar un artículo con saldo.")

    def procesar(df):
        creados = actualizados = 0
        errores = []
        with session_scope() as s:
            for _, r in df.iterrows():
                cod = str(r.get("codigo") or "").strip()
                if not cod:
                    continue
                a = s.query(Articulo).filter_by(codigo=cod).first()
                nuevo = a is None
                if nuevo:
                    a = Articulo(codigo=cod)
                    s.add(a)
                a.descripcion = str(r.get("descripcion") or a.descripcion or cod)[:300]
                a.familia = str(r.get("familia") or a.familia or "")[:60]
                a.desc_familia = str(r.get("desc_familia") or a.desc_familia or "")[:160]
                a.grupo_articulo = str(r.get("grupo_articulo") or a.grupo_articulo or "")[:60]
                a.categoria = str(r.get("categoria") or a.categoria or "")[:80]
                a.color = str(r.get("color") or a.color or "")[:80]
                a.tipo = (str(r.get("tipo") or a.tipo or "CRUDO")).upper()
                a.unidad = str(r.get("unidad") or a.unidad or "UND")[:20]
                activo = _b(r.get("activo"), True)
                if not activo and _tiene_saldo(s, cod):
                    errores.append(f"{cod}: no se puede deshabilitar, tiene saldo.")
                else:
                    a.activo = activo
                creados += nuevo
                actualizados += (not nuevo)
        return creados, actualizados, errores

    _cargue("articulos", procesar)

    c1, c2, c3 = st.columns(3)
    busq = c1.text_input("Buscar", key="art_b")
    tipo = c2.multiselect("Tipo", ["CRUDO", "TRANSFORMADO"], key="art_t")
    solo_act = c3.checkbox("Solo activos", True, key="art_a")

    with session_scope() as s:
        q = s.query(Articulo)
        if busq.strip():
            like = f"%{busq.strip()}%"
            q = q.filter((Articulo.codigo.ilike(like)) | (Articulo.descripcion.ilike(like)))
        if tipo:
            q = q.filter(Articulo.tipo.in_(tipo))
        if solo_act:
            q = q.filter(Articulo.activo.is_(True))
        total = q.count()
        arts = q.order_by(Articulo.codigo).limit(500).all()
        filas = [{"codigo": a.codigo, "descripcion": a.descripcion,
                  "familia": a.familia or "", "categoria": a.categoria or "",
                  "tipo": a.tipo, "unidad": a.unidad, "activo": a.activo}
                 for a in arts]
    st.caption(f"{total:,} artículos · mostrando {len(filas)}")
    if not filas:
        st.info("Sin resultados.")
        return

    ed = st.data_editor(pd.DataFrame(filas), use_container_width=True, height=380,
                        hide_index=True, key="ed_art", num_rows="fixed",
                        disabled=["codigo"],
                        column_config={"activo": st.column_config.CheckboxColumn("Activo")})
    if st.button("Guardar cambios", type="primary", key="sv_art"):
        n, errores = 0, []
        with session_scope() as s:
            for _, r in ed.iterrows():
                a = s.query(Articulo).filter_by(codigo=r["codigo"]).first()
                if not a:
                    continue
                if not r["activo"] and a.activo and _tiene_saldo(s, a.codigo):
                    errores.append(f"{a.codigo}: tiene saldo, no se deshabilita.")
                    continue
                a.descripcion = r["descripcion"]
                a.familia = r["familia"]
                a.categoria = r["categoria"]
                a.tipo = r["tipo"]
                a.unidad = r["unidad"]
                a.activo = bool(r["activo"])
                n += 1
        ui.limpiar_cache()
        ui.ok(f"{n} artículos actualizados.")
        for e in errores:
            st.warning(e, icon="⚠️")

    st.download_button("Exportar a Excel", ui.exportar_excel({"articulos": pd.DataFrame(filas)}),
                       "articulos.xlsx", key="exp_art")


def _tiene_saldo(s, codigo):
    v = s.query(func.coalesce(func.sum(Inventario.cantidad), 0.0)).filter(
        Inventario.articulo == codigo).scalar()
    return abs(float(v or 0)) > 1e-6


# ------------------------------------------------------------------------ BOM
def _bom(user):
    st.caption("Subcontratación: un artículo se transforma en otro (ensamble o "
               "relación 1 a 1).")

    def procesar(df):
        creados = actualizados = 0
        with session_scope() as s:
            for _, r in df.iterrows():
                at = str(r.get("articulo_transformado") or "").strip()
                cp = str(r.get("componente") or "").strip()
                if not at or not cp:
                    continue
                try:
                    seq = int(float(r.get("secuencia") or 0))
                except ValueError:
                    seq = 0
                b = s.query(Bom).filter_by(articulo_transformado=at, componente=cp,
                                           secuencia=seq).first()
                nuevo = b is None
                if nuevo:
                    b = Bom(articulo_transformado=at, componente=cp, secuencia=seq)
                    s.add(b)
                try:
                    b.cantidad = float(r.get("cantidad") or 1)
                except ValueError:
                    b.cantidad = 1.0
                b.desc_transformado = str(r.get("desc_transformado") or b.desc_transformado or "")[:300]
                b.desc_componente = str(r.get("desc_componente") or b.desc_componente or "")[:300]
                b.proveedor_codigo = str(r.get("proveedor_codigo") or b.proveedor_codigo or "")[:40]
                b.activo = _b(r.get("activo"), True)
                creados += nuevo
                actualizados += (not nuevo)
        return creados, actualizados, []

    _cargue("bom", procesar)

    c1, c2 = st.columns(2)
    busq = c1.text_input("Buscar artículo transformado o componente", key="bom_b")
    solo_act = c2.checkbox("Solo activos", True, key="bom_a")
    with session_scope() as s:
        q = s.query(Bom)
        if busq.strip():
            like = f"%{busq.strip()}%"
            q = q.filter((Bom.articulo_transformado.ilike(like))
                         | (Bom.componente.ilike(like))
                         | (Bom.desc_transformado.ilike(like)))
        if solo_act:
            q = q.filter(Bom.activo.is_(True))
        total = q.count()
        rows = q.order_by(Bom.articulo_transformado, Bom.secuencia).limit(500).all()
        filas = [{"id": b.id, "articulo_transformado": b.articulo_transformado,
                  "desc_transformado": b.desc_transformado or "",
                  "componente": b.componente, "desc_componente": b.desc_componente or "",
                  "cantidad": b.cantidad, "secuencia": b.secuencia,
                  "proveedor_codigo": b.proveedor_codigo or "", "activo": b.activo}
                 for b in rows]
    st.caption(f"{total:,} líneas de BOM · mostrando {len(filas)}")
    if not filas:
        st.info("Sin resultados.")
        return
    ed = st.data_editor(pd.DataFrame(filas), use_container_width=True, height=380,
                        hide_index=True, key="ed_bom", num_rows="fixed",
                        disabled=["id", "articulo_transformado", "componente"])
    if st.button("Guardar cambios", type="primary", key="sv_bom"):
        n = 0
        with session_scope() as s:
            for _, r in ed.iterrows():
                b = s.get(Bom, int(r["id"]))
                if not b:
                    continue
                b.cantidad = float(r["cantidad"])
                b.secuencia = int(r["secuencia"])
                b.activo = bool(r["activo"])
                b.desc_transformado = r["desc_transformado"]
                b.desc_componente = r["desc_componente"]
                n += 1
        ui.ok(f"{n} líneas actualizadas.")


# ----------------------------------------------------------------- ubicaciones
def _ubicaciones(user):
    st.caption("WMS opcional: detalle a nivel de ubicación. Asigne ubicaciones al "
               "proveedor para validar las columnas Desde/Hasta del BIN a BIN.")

    def procesar(df):
        creados = actualizados = 0
        with session_scope() as s:
            provs = {p.codigo: p.id for p in s.query(Proveedor).all()}
            for _, r in df.iterrows():
                cod = str(r.get("codigo") or "").strip().upper()
                if not cod:
                    continue
                u = s.query(Ubicacion).filter_by(codigo=cod).first()
                nuevo = u is None
                if nuevo:
                    u = Ubicacion(codigo=cod)
                    s.add(u)
                u.un = str(r.get("un") or u.un or "MOTOS")[:20]
                u.rol = str(r.get("rol") or u.rol or "")[:20]
                pc = str(r.get("proveedor_codigo") or "").strip()
                if pc:
                    u.proveedor_id = provs.get(pc, u.proveedor_id)
                u.cerrada = _b(r.get("cerrada"), False)
                u.inspeccion = _b(r.get("inspeccion"), False)
                u.restringida = _b(r.get("restringida"), False)
                u.activo = _b(r.get("activo"), True)
                creados += nuevo
                actualizados += (not nuevo)
        return creados, actualizados, []

    _cargue("ubicaciones", procesar)

    with session_scope() as s:
        provs = {p.id: f"{p.nombre} ({p.codigo})" for p in s.query(Proveedor).all()}
        ubis = s.query(Ubicacion).order_by(Ubicacion.codigo).limit(500).all()
        filas = [{"id": u.id, "codigo": u.codigo, "un": u.un, "rol": u.rol or "",
                  "proveedor": provs.get(u.proveedor_id, ""), "cerrada": u.cerrada,
                  "inspeccion": u.inspeccion, "restringida": u.restringida,
                  "activo": u.activo} for u in ubis]
    if not filas:
        st.info("Sin ubicaciones.")
        return
    opciones = [""] + sorted(provs.values())
    ed = st.data_editor(pd.DataFrame(filas), use_container_width=True, height=380,
                        hide_index=True, key="ed_ubi", num_rows="fixed",
                        disabled=["id", "codigo"],
                        column_config={"proveedor": st.column_config.SelectboxColumn(
                            "Proveedor asignado", options=opciones)})
    if st.button("Guardar cambios", type="primary", key="sv_ubi"):
        inv = {v: k for k, v in provs.items()}
        n = 0
        with session_scope() as s:
            for _, r in ed.iterrows():
                u = s.get(Ubicacion, int(r["id"]))
                if not u:
                    continue
                u.proveedor_id = inv.get(r["proveedor"])
                u.cerrada = bool(r["cerrada"])
                u.inspeccion = bool(r["inspeccion"])
                u.restringida = bool(r["restringida"])
                u.activo = bool(r["activo"])
                u.rol = r["rol"]
                n += 1
        ui.limpiar_cache()
        ui.ok(f"{n} ubicaciones actualizadas.")


# ----------------------------------------------------------------- proveedores
def _proveedores(user):
    st.caption(
        "Cada proveedor operativo debe tener dos ubicaciones obligatorias del BIN: "
        "DESDE (control con alerta) y HASTA (control bloqueante).")

    def _faltantes_valores(codigo, nombre, nit, desde, hasta, tolerancia):
        return campos_faltantes_proveedor(
            codigo=codigo,
            nombre=nombre,
            nit=nit,
            ubicacion_origen=desde,
            ubicacion_destino=hasta,
            tolerancia_averia_pct=tolerancia,
        )

    def procesar(df):
        creados = actualizados = 0
        errores = []
        with session_scope() as s:
            ubicaciones = {
                u.codigo: u for u in s.query(Ubicacion).filter(
                    Ubicacion.activo.is_(True)).all()
            }
            for _, r in df.iterrows():
                cod = str(r.get("codigo") or "").strip()
                if not cod:
                    continue
                p = s.query(Proveedor).filter_by(codigo=cod).first()
                nuevo = p is None

                nombre = str(r.get("nombre") or (p.nombre if p else "") or "").strip()
                nit = str(r.get("nit") or (p.nit if p else "") or "").strip()
                desde = str(
                    r.get("ubicacion_desde")
                    or r.get("ubicacion_origen")
                    or (p.ubicacion_origen if p else "")
                    or ""
                ).strip().upper()
                hasta = str(
                    r.get("ubicacion_hasta")
                    or r.get("ubicacion_destino")
                    or (p.ubicacion_destino if p else "")
                    or ""
                ).strip().upper()
                try:
                    tolerancia = float(
                        r.get("tolerancia_averia_pct")
                        if str(r.get("tolerancia_averia_pct") or "").strip().lower()
                           not in ("", "nan", "none")
                        else ((p.tolerancia_averia_pct if p else None) or 1.0)
                    )
                except (TypeError, ValueError):
                    tolerancia = None

                activo_objetivo = _b(
                    r.get("activo"),
                    bool(p.activo) if p is not None else True)

                # Para cualquier proveedor exigimos al menos código y nombre.
                if not nombre:
                    errores.append(f"{cod}: nombre obligatorio.")
                    continue

                faltan = _faltantes_valores(
                    cod, nombre, nit, desde, hasta, tolerancia)
                if activo_objetivo and faltan:
                    errores.append(
                        f"{cod}: NO se activa. Faltan: {', '.join(faltan)}.")
                    activo_objetivo = False

                # Las ubicaciones son obligatorias para estar activo. Un proveedor
                # inactivo puede conservarlas vacías mientras termina parametrización.
                if desde:
                    if desde not in ubicaciones:
                        errores.append(f"{cod}: DESDE {desde} no existe o está inactiva.")
                        activo_objetivo = False
                        desde = ""
                    elif ubicaciones[desde].cerrada:
                        errores.append(f"{cod}: DESDE {desde} está cerrada.")
                        activo_objetivo = False
                        desde = ""
                if hasta:
                    if hasta not in ubicaciones:
                        errores.append(f"{cod}: HASTA {hasta} no existe o está inactiva.")
                        activo_objetivo = False
                        hasta = ""
                    elif ubicaciones[hasta].cerrada:
                        errores.append(f"{cod}: HASTA {hasta} está cerrada.")
                        activo_objetivo = False
                        hasta = ""
                    elif ubicaciones[hasta].proveedor_id and (
                            p is None or ubicaciones[hasta].proveedor_id != p.id):
                        errores.append(
                            f"{cod}: HASTA {hasta} pertenece a otro proveedor.")
                        activo_objetivo = False
                        hasta = ""

                if nuevo:
                    p = Proveedor(codigo=cod)
                    s.add(p)
                    s.flush()

                # Libera el HASTA anterior si el proveedor cambió de ubicación.
                anterior = str(p.ubicacion_destino or "").strip().upper()
                if anterior and anterior != hasta:
                    u_ant = ubicaciones.get(anterior)
                    if u_ant and u_ant.proveedor_id == p.id:
                        u_ant.proveedor_id = None
                        if (u_ant.rol or "").upper() == "DESTINO":
                            u_ant.rol = None

                p.nombre = nombre[:200]
                p.nit = nit[:40]
                p.ubicacion_origen = desde or None
                p.ubicacion_destino = hasta or None
                p.tolerancia_averia_pct = tolerancia if tolerancia is not None else 1.0
                p.activo = bool(activo_objetivo)

                if hasta:
                    ubicaciones[hasta].proveedor_id = p.id
                    ubicaciones[hasta].rol = "DESTINO"
                creados += nuevo
                actualizados += (not nuevo)

        return creados, actualizados, errores

    _cargue(
        "proveedores", procesar,
        ayuda=(
            "Para activar un proveedor son obligatorios: código, nombre, NIT, "
            "ubicacion_desde, ubicacion_hasta y tolerancia_averia_pct. "
            "Los inactivos pueden quedar incompletos mientras se parametrizan."))

    with session_scope() as s:
        ubicaciones_activas = [
            u.codigo for u in s.query(Ubicacion).filter(
                Ubicacion.activo.is_(True),
                Ubicacion.cerrada.is_(False)
            ).order_by(Ubicacion.codigo).all()
        ]

    with st.expander("➕ Crear proveedor", expanded=False):
        c1, c2 = st.columns(2)
        cod_new = c1.text_input("Código", key="prov_new_cod")
        nom_new = c2.text_input("Nombre", key="prov_new_nom")

        c3, c4 = st.columns(2)
        nit_new = c3.text_input("NIT", key="prov_new_nit")
        tol_new = c4.number_input(
            "Tolerancia de avería (%)", min_value=0.0, max_value=100.0,
            value=1.0, step=0.1, key="prov_new_tol")

        c5, c6 = st.columns(2)
        desde_new = c5.selectbox(
            "Ubicación DESDE *",
            [""] + ubicaciones_activas,
            key="prov_new_desde",
            help="Valor esperado en la columna DESDE del BIN. Si difiere, alerta.")
        hasta_new = c6.selectbox(
            "Ubicación HASTA *",
            [""] + ubicaciones_activas,
            key="prov_new_hasta",
            help="Valor esperado en HASTA. Si difiere, el recibo se bloquea.")

        if st.button("Crear proveedor", type="primary", key="prov_new_btn"):
            faltan_new = _faltantes_valores(
                cod_new, nom_new, nit_new, desde_new, hasta_new, tol_new)
            if faltan_new:
                ui.err(
                    "No se puede crear un proveedor activo. Faltan: "
                    + ", ".join(faltan_new) + ".")
            else:
                try:
                    with session_scope() as s:
                        if s.query(Proveedor).filter_by(codigo=cod_new.strip()).first():
                            raise ValueError("Ya existe un proveedor con ese código.")
                        u_desde = s.query(Ubicacion).filter_by(codigo=desde_new).first()
                        u_hasta = s.query(Ubicacion).filter_by(codigo=hasta_new).first()
                        if not u_desde or not u_desde.activo or u_desde.cerrada:
                            raise ValueError("La ubicación DESDE no está disponible.")
                        if not u_hasta or not u_hasta.activo or u_hasta.cerrada:
                            raise ValueError("La ubicación HASTA no está disponible.")
                        if u_hasta.proveedor_id:
                            raise ValueError("La ubicación HASTA ya está asignada a otro proveedor.")

                        p = Proveedor(
                            codigo=cod_new.strip(),
                            nombre=nom_new.strip(),
                            nit=nit_new.strip(),
                            tolerancia_averia_pct=float(tol_new),
                            ubicacion_origen=desde_new,
                            ubicacion_destino=hasta_new,
                            activo=True)
                        s.add(p)
                        s.flush()
                        u_hasta.proveedor_id = p.id
                        u_hasta.rol = "DESTINO"

                    ui.limpiar_cache()
                    ui.ok("Proveedor creado con DESDE y HASTA asignados.")
                    st.rerun()
                except Exception as e:
                    ui.err(str(e))

    with session_scope() as s:
        provs = s.query(Proveedor).order_by(Proveedor.nombre).all()

    if not provs:
        st.info("No hay proveedores.")
        return

    total = len(provs)
    activos = sum(1 for p in provs if bool(p.activo))
    inactivos = total - activos

    estado_filtro = st.segmented_control(
        "Estado del proveedor",
        options=["Activos", "Inactivos", "Todos"],
        default="Activos",
        key="prov_filtro_estado",
        help=(
            f"Activos: {activos} · Inactivos: {inactivos} · Total: {total}. "
            "El filtro solo cambia la vista; el estado se modifica en la columna Activo."
        ),
    )

    if estado_filtro == "Activos":
        provs_visibles = [p for p in provs if bool(p.activo)]
    elif estado_filtro == "Inactivos":
        provs_visibles = [p for p in provs if not bool(p.activo)]
    else:
        provs_visibles = provs

    st.caption(
        f"Mostrando **{len(provs_visibles)}** de **{total}** proveedores · "
        f"{activos} activos · {inactivos} inactivos."
    )

    filas = [{
        "id": p.id,
        "codigo": p.codigo,
        "nombre": p.nombre,
        "nit": p.nit or "",
        "ubicacion_desde": p.ubicacion_origen or "",
        "ubicacion_hasta": p.ubicacion_destino or "",
        "tolerancia_averia_pct": p.tolerancia_averia_pct,
        "listo_para_activar": not _faltantes_valores(
            p.codigo, p.nombre, p.nit or "", p.ubicacion_origen or "",
            p.ubicacion_destino or "", p.tolerancia_averia_pct),
        "activo": p.activo,
    } for p in provs_visibles]

    if not filas:
        st.info(f"No hay proveedores en el segmento **{estado_filtro}**.")
        return

    ed = st.data_editor(
        pd.DataFrame(filas),
        use_container_width=True,
        height=380,
        hide_index=True,
        key="ed_prov",
        num_rows="fixed",
        disabled=["id", "codigo", "listo_para_activar"],
        column_config={
            "ubicacion_desde": st.column_config.SelectboxColumn(
                "Ubicación DESDE", options=[""] + ubicaciones_activas, required=True),
            "ubicacion_hasta": st.column_config.SelectboxColumn(
                "Ubicación HASTA", options=[""] + ubicaciones_activas, required=True),
            "listo_para_activar": st.column_config.CheckboxColumn(
                "Listo para activar",
                help="Solo puede estar Activo cuando todos los campos obligatorios están completos."),
            "activo": st.column_config.CheckboxColumn("Activo"),
        })

    incompletos = []
    for _, r in ed.iterrows():
        faltan = _faltantes_valores(
            r.get("codigo"), r.get("nombre"), r.get("nit"),
            r.get("ubicacion_desde"), r.get("ubicacion_hasta"),
            r.get("tolerancia_averia_pct"))
        if faltan:
            incompletos.append((str(r["codigo"]), faltan))
    if incompletos:
        resumen = "; ".join(
            f"{cod}: {', '.join(faltan)}"
            for cod, faltan in incompletos[:8])
        st.warning(
            "Proveedores incompletos (no pueden activarse): "
            + resumen
            + ("…" if len(incompletos) > 8 else ""))

    if st.button("Guardar cambios", type="primary", key="sv_prov"):
        n, errores = 0, []
        with session_scope() as s:
            ubicaciones = {
                u.codigo: u for u in s.query(Ubicacion).filter(
                    Ubicacion.activo.is_(True)).all()
            }
            for _, r in ed.iterrows():
                p = s.get(Proveedor, int(r["id"]))
                if not p:
                    continue

                nombre = str(r.get("nombre") or "").strip()
                nit = str(r.get("nit") or "").strip()
                desde = str(r.get("ubicacion_desde") or "").strip().upper()
                hasta = str(r.get("ubicacion_hasta") or "").strip().upper()
                try:
                    tolerancia = float(r.get("tolerancia_averia_pct"))
                except (TypeError, ValueError):
                    tolerancia = None
                activar = bool(r.get("activo"))

                faltan = _faltantes_valores(
                    p.codigo, nombre, nit, desde, hasta, tolerancia)

                # Regla dura: nunca persistir Activo=True si falta un campo.
                if activar and faltan:
                    p.activo = False
                    errores.append(
                        f"{p.codigo}: NO se activó. Faltan: {', '.join(faltan)}.")
                    # Permite guardar lo que sí fue diligenciado para completar
                    # progresivamente el maestro.
                    activar = False

                u_desde = ubicaciones.get(desde) if desde else None
                u_hasta = ubicaciones.get(hasta) if hasta else None

                if desde and (not u_desde or u_desde.cerrada):
                    errores.append(f"{p.codigo}: DESDE {desde} no disponible.")
                    activar = False
                    p.activo = False
                    continue
                if hasta and (not u_hasta or u_hasta.cerrada):
                    errores.append(f"{p.codigo}: HASTA {hasta} no disponible.")
                    activar = False
                    p.activo = False
                    continue
                if u_hasta and u_hasta.proveedor_id and u_hasta.proveedor_id != p.id:
                    errores.append(
                        f"{p.codigo}: HASTA {hasta} pertenece a otro proveedor.")
                    activar = False
                    p.activo = False
                    continue

                anterior = str(p.ubicacion_destino or "").strip().upper()
                if anterior and anterior != hasta:
                    u_ant = ubicaciones.get(anterior)
                    if u_ant and u_ant.proveedor_id == p.id:
                        u_ant.proveedor_id = None
                        if (u_ant.rol or "").upper() == "DESTINO":
                            u_ant.rol = None

                p.nombre = nombre
                p.nit = nit
                if tolerancia is not None:
                    p.tolerancia_averia_pct = tolerancia
                p.ubicacion_origen = desde or None
                p.ubicacion_destino = hasta or None
                p.activo = bool(activar and not faltan)

                if u_hasta:
                    u_hasta.proveedor_id = p.id
                    u_hasta.rol = "DESTINO"
                n += 1

        ui.limpiar_cache()
        ui.ok(f"{n} proveedores actualizados.")
        for e in errores:
            st.warning(e, icon="⚠️")


# ----------------------------------------------------------- órdenes de compra
def _oc(user):
    st.caption("Las órdenes ABIERTAS son contra las que el equipo de Recibo hace el "
               "match del BIN a BIN.")

    def procesar(df):
        creados = actualizados = 0
        errores = []
        with session_scope() as s:
            provs = {p.codigo: p.id for p in s.query(Proveedor).all()}
            for _, r in df.iterrows():
                num = str(r.get("numero") or "").strip()
                art = str(r.get("articulo") or "").strip()
                pc = str(r.get("proveedor_codigo") or "").strip()
                if not num or not art:
                    continue
                if pc not in provs:
                    errores.append(f"{num}: proveedor {pc} no existe.")
                    continue
                o = s.query(OrdenCompra).filter_by(numero=num, articulo=art).first()
                nuevo = o is None
                if nuevo:
                    o = OrdenCompra(numero=num, articulo=art)
                    s.add(o)
                o.proveedor_id = provs[pc]
                try:
                    o.cantidad = float(r.get("cantidad") or 0)
                except ValueError:
                    o.cantidad = 0.0
                o.estado = (str(r.get("estado") or "ABIERTA")).upper()
                try:
                    o.fecha = pd.to_datetime(r.get("fecha")).date()
                except Exception:
                    o.fecha = dt.date.today()
                creados += nuevo
                actualizados += (not nuevo)
        return creados, actualizados, errores

    _cargue("ordenes_compra", procesar)

    with st.expander("➕ Crear orden de compra"):
        pid = ui.selector_proveedor(user, key="oc_prov")
        c1, c2, c3, c4 = st.columns(4)
        num = c1.text_input("Número", key="oc_num")
        art = c2.text_input("Artículo", key="oc_art")
        cant = c3.number_input("Cantidad", 0.0, 1e9, 0.0, step=1.0, key="oc_cant")
        fecha = c4.date_input("Fecha", dt.date.today(), key="oc_f")
        if st.button("Crear OC", type="primary", key="oc_new"):
            if not num.strip() or not art.strip() or cant <= 0:
                ui.err("Complete número, artículo y cantidad.")
            else:
                with session_scope() as s:
                    s.add(OrdenCompra(numero=num.strip(), proveedor_id=pid,
                                      articulo=art.strip(), cantidad=cant,
                                      estado="ABIERTA", fecha=fecha))
                ui.ok("Orden de compra creada.")
                st.rerun()

    c1, c2 = st.columns(2)
    est = c1.multiselect("Estado", ["ABIERTA", "CERRADA"], default=["ABIERTA"], key="oc_e")
    busq = c2.text_input("Buscar número o artículo", key="oc_b")
    with session_scope() as s:
        q = s.query(OrdenCompra)
        if est:
            q = q.filter(OrdenCompra.estado.in_(est))
        if busq.strip():
            like = f"%{busq.strip()}%"
            q = q.filter((OrdenCompra.numero.ilike(like)) | (OrdenCompra.articulo.ilike(like)))
        ocs = q.order_by(OrdenCompra.fecha.desc()).limit(500).all()
        filas = [{"Número": o.numero, "Proveedor": o.proveedor.nombre if o.proveedor else "",
                  "Artículo": o.articulo, "Cantidad": o.cantidad,
                  "Recibido": o.cantidad_recibida, "Pendiente": o.pendiente,
                  "Estado": o.estado, "Fecha": o.fecha} for o in ocs]
    if filas:
        st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)
    else:
        st.info("Sin órdenes con ese filtro.")


# -------------------------------------------------------------------- usuarios
def _usuarios(user):
    with session_scope() as s:
        provs = {p.id: f"{p.nombre} ({p.codigo})" for p in s.query(Proveedor).filter(
            Proveedor.activo.is_(True)).all()}
        us = s.query(Usuario).order_by(Usuario.rol, Usuario.nombre).all()
        filas = [{"id": u.id, "email": u.email, "nombre": u.nombre, "rol": u.rol,
                  "proveedor": provs.get(u.proveedor_id, ""), "activo": u.activo}
                 for u in us]

    with st.expander("➕ Crear usuario"):
        c1, c2 = st.columns(2)
        email = c1.text_input("Correo", key="us_mail")
        nombre = c2.text_input("Nombre", key="us_nom")
        c1, c2, c3 = st.columns(3)
        rol = c1.selectbox("Rol", list(ROLES), format_func=lambda r: ROLES[r], key="us_rol")
        prov = c2.selectbox("Proveedor (solo rol PROVEEDOR)", [""] + sorted(provs.values()),
                            key="us_prov")
        pwd = c3.text_input("Contraseña", type="password", key="us_pwd")
        if st.button("Crear usuario", type="primary", key="us_new"):
            if not email.strip() or not pwd:
                ui.err("Correo y contraseña son obligatorios.")
            elif rol == "PROVEEDOR" and not prov:
                ui.err("Un usuario con rol PROVEEDOR debe tener proveedor asignado.")
            else:
                inv = {v: k for k, v in provs.items()}
                with session_scope() as s:
                    if s.query(Usuario).filter_by(email=email.strip().lower()).first():
                        ui.err("Ese correo ya existe.")
                    else:
                        s.add(Usuario(email=email.strip().lower(), nombre=nombre or email,
                                      rol=rol, proveedor_id=inv.get(prov),
                                      password_hash=hash_password(pwd), activo=True))
                        ui.ok("Usuario creado.")
                        st.rerun()

    ed = st.data_editor(pd.DataFrame(filas), use_container_width=True, hide_index=True,
                        key="ed_us", num_rows="fixed", disabled=["id", "email"],
                        column_config={
                            "rol": st.column_config.SelectboxColumn("Rol", options=list(ROLES)),
                            "proveedor": st.column_config.SelectboxColumn(
                                "Proveedor", options=[""] + sorted(provs.values()))})
    if st.button("Guardar cambios", type="primary", key="sv_us"):
        inv = {v: k for k, v in provs.items()}
        n, errores = 0, []
        with session_scope() as s:
            for _, r in ed.iterrows():
                u = s.get(Usuario, int(r["id"]))
                if not u:
                    continue
                if r["rol"] == "PROVEEDOR" and not r["proveedor"]:
                    errores.append(f"{u.email}: el rol PROVEEDOR exige proveedor asignado.")
                    continue
                u.nombre = r["nombre"]
                u.rol = r["rol"]
                u.proveedor_id = inv.get(r["proveedor"])
                u.activo = bool(r["activo"])
                n += 1
        ui.ok(f"{n} usuarios actualizados.")
        for e in errores:
            st.warning(e, icon="⚠️")

    st.markdown("##### Restablecer contraseña")
    c1, c2 = st.columns(2)
    sel = c1.selectbox("Usuario", [f["email"] for f in filas], key="rp_us")
    nueva = c2.text_input("Nueva contraseña", type="password", key="rp_pwd")
    if st.button("Restablecer", key="rp_btn"):
        if not nueva:
            ui.err("Indique la nueva contraseña.")
        else:
            with session_scope() as s:
                u = s.query(Usuario).filter_by(email=sel).first()
                u.password_hash = hash_password(nueva)
            ui.ok("Contraseña actualizada.")


# --------------------------------------------------------------- datos demo
def _demo(user):
    st.caption("Genera un escenario completo de prueba (recibos, novedades, conteos, "
               "averías, producción y despacho) para validar la app antes de operar.")
    st.warning("Úselo solo en un ambiente de pruebas: crea movimientos reales de "
               "inventario en la base de datos conectada.", icon="⚠️")
    pid = ui.selector_proveedor(user, key="demo_prov")
    if st.button("Generar escenario de prueba", type="primary", key="demo_btn"):
        from core.demo import generar
        try:
            res = generar(proveedor_id=pid)
            ui.ok("Escenario generado.")
            st.json(res)
        except Exception as e:
            ui.err(f"No se pudo generar: {e}")

    st.divider()
    st.markdown("##### Reiniciar movimientos")
    st.caption("Borra recibos, novedades, conteos, averías, producción, despachos, "
               "documentos e inventario. **No borra** artículos, BOM, ubicaciones, "
               "proveedores ni usuarios.")
    confirmar = st.text_input('Escriba BORRAR para confirmar', key="demo_conf")
    if st.button("Reiniciar movimientos", key="demo_reset"):
        if confirmar.strip().upper() != "BORRAR":
            ui.err("Escriba BORRAR para confirmar.")
            return
        from core.models import (Archivo, Averia, ConsumoProduccion, ConteoEjecutado,
                                 ConteoProgramado, Despacho, DespachoLinea, Documento,
                                 FotoInventarioERP, Inventario as Inv,
                                 MovimientoInventario, Novedad, OrdenProduccion,
                                 ProgramaProduccion, Recibo, ReciboLinea)
        with session_scope() as s:
            for M in (ConsumoProduccion, OrdenProduccion, ProgramaProduccion,
                      DespachoLinea, Despacho, ConteoEjecutado, ConteoProgramado,
                      FotoInventarioERP, Averia, Novedad, ReciboLinea, Recibo,
                      MovimientoInventario, Inv, Documento, Archivo):
                s.query(M).delete()
            s.query(OrdenCompra).update({OrdenCompra.cantidad_recibida: 0,
                                         OrdenCompra.estado: "ABIERTA"})
        ui.ok("Movimientos reiniciados.")
        st.rerun()
