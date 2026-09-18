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
        "Cada proveedor operativo debe tener una ubicación destino designada. "
        "Los recibos siempre se registran en esa ubicación y el operador no la elige.")

    def _asignar_ubicacion(s, p, codigo_ubicacion, errores, contexto):
        cod = str(codigo_ubicacion or "").strip().upper()
        if not cod:
            errores.append(f"{contexto}: la ubicación destino es obligatoria.")
            return False
        u = s.query(Ubicacion).filter(Ubicacion.codigo == cod).first()
        if not u:
            errores.append(f"{contexto}: la ubicación {cod} no existe.")
            return False
        if not u.activo or u.cerrada:
            errores.append(f"{contexto}: la ubicación {cod} está inactiva o cerrada.")
            return False
        if u.proveedor_id not in (None, p.id):
            otro = s.get(Proveedor, u.proveedor_id)
            errores.append(
                f"{contexto}: la ubicación {cod} ya pertenece a "
                f"{otro.nombre if otro else 'otro proveedor'}.")
            return False
        u.proveedor_id = p.id
        u.rol = "DESTINO"
        p.ubicacion_destino_id = u.id
        return True

    def procesar(df):
        creados = actualizados = 0
        errores = []
        with session_scope() as s:
            for _, r in df.iterrows():
                cod = str(r.get("codigo") or "").strip()
                if not cod:
                    continue
                p = s.query(Proveedor).filter_by(codigo=cod).first()
                nuevo = p is None
                if nuevo:
                    p = Proveedor(codigo=cod)
                    s.add(p)
                    s.flush()
                p.nombre = str(r.get("nombre") or p.nombre or cod)[:200]
                p.nit = str(r.get("nit") or p.nit or "")[:40]
                try:
                    p.tolerancia_averia_pct = float(
                        r.get("tolerancia_averia_pct")
                        or p.tolerancia_averia_pct or 1.0)
                except ValueError:
                    pass
                p.activo = _b(r.get("activo"), True)

                ubic = str(r.get("ubicacion_destino") or "").strip()
                if ubic:
                    _asignar_ubicacion(s, p, ubic, errores, cod)
                elif nuevo or not p.ubicacion_destino_id:
                    errores.append(f"{cod}: la ubicación destino es obligatoria.")

                creados += nuevo
                actualizados += (not nuevo)
        return creados, actualizados, errores

    _cargue(
        "proveedores", procesar,
        ayuda="La columna ubicacion_destino es obligatoria para proveedores operativos.")

    with session_scope() as s:
        ubicaciones = s.query(Ubicacion).filter(
            Ubicacion.activo.is_(True),
            Ubicacion.cerrada.is_(False),
        ).order_by(Ubicacion.codigo).all()
        ubic_por_codigo = {u.codigo: u for u in ubicaciones}
        opciones_ubic = [""] + [u.codigo for u in ubicaciones]

    with st.expander("➕ Crear proveedor"):
        c1, c2, c3 = st.columns(3)
        nuevo_codigo = c1.text_input("Código", key="prov_new_cod")
        nuevo_nombre = c2.text_input("Nombre", key="prov_new_nom")
        nuevo_nit = c3.text_input("NIT", key="prov_new_nit")
        c1, c2 = st.columns(2)
        nueva_ubic = c1.selectbox(
            "Ubicación destino *", opciones_ubic, key="prov_new_ubi")
        nueva_tol = c2.number_input(
            "Tolerancia avería %", 0.0, 100.0, 1.0, step=0.1,
            key="prov_new_tol")
        if st.button("Crear proveedor", type="primary", key="prov_new_btn"):
            if not nuevo_codigo.strip() or not nuevo_nombre.strip() or not nueva_ubic:
                ui.err("Código, nombre y ubicación destino son obligatorios.")
            else:
                with session_scope() as s:
                    if s.query(Proveedor).filter_by(
                            codigo=nuevo_codigo.strip()).first():
                        ui.err("Ese código de proveedor ya existe.")
                    else:
                        u = s.query(Ubicacion).filter_by(codigo=nueva_ubic).first()
                        if not u or not u.activo or u.cerrada:
                            ui.err("La ubicación seleccionada no está disponible.")
                        elif u.proveedor_id is not None:
                            ui.err("La ubicación ya está asignada a otro proveedor.")
                        else:
                            p = Proveedor(
                                codigo=nuevo_codigo.strip(),
                                nombre=nuevo_nombre.strip(),
                                nit=nuevo_nit.strip() or None,
                                tolerancia_averia_pct=float(nueva_tol),
                                activo=True)
                            s.add(p)
                            s.flush()
                            u.proveedor_id = p.id
                            u.rol = "DESTINO"
                            p.ubicacion_destino_id = u.id
                            ui.ok("Proveedor creado con ubicación destino asignada.")
                            ui.limpiar_cache()
                            st.rerun()

    with session_scope() as s:
        provs = s.query(Proveedor).order_by(Proveedor.nombre).all()
        filas = [{
            "id": p.id,
            "codigo": p.codigo,
            "nombre": p.nombre,
            "nit": p.nit or "",
            "ubicacion_destino": (
                p.ubicacion_destino.codigo if p.ubicacion_destino else ""),
            "tolerancia_averia_pct": p.tolerancia_averia_pct,
            "activo": p.activo,
        } for p in provs]

    if not filas:
        st.info("No hay proveedores.")
        return

    ed = st.data_editor(
        pd.DataFrame(filas),
        use_container_width=True,
        height=380,
        hide_index=True,
        key="ed_prov",
        num_rows="fixed",
        disabled=["id", "codigo"],
        column_config={
            "ubicacion_destino": st.column_config.SelectboxColumn(
                "Ubicación destino *", options=opciones_ubic, required=True),
            "activo": st.column_config.CheckboxColumn("Activo"),
        })

    if st.button("Guardar cambios", type="primary", key="sv_prov"):
        n, errores = 0, []
        with session_scope() as s:
            for _, r in ed.iterrows():
                p = s.get(Proveedor, int(r["id"]))
                if not p:
                    continue
                p.nombre = r["nombre"]
                p.nit = r["nit"]
                p.tolerancia_averia_pct = float(r["tolerancia_averia_pct"])
                p.activo = bool(r["activo"])
                if not _asignar_ubicacion(
                        s, p, r["ubicacion_destino"], errores, p.codigo):
                    continue
                n += 1
        ui.limpiar_cache()
        if n:
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
