"""
AKT MOTOS — App para Proveedores de Transformación
Flujo: RECIBO -> ALMACENAMIENTO -> PRODUCCIÓN -> DESPACHO
"""
import importlib
import logging
import uuid

import streamlit as st

logger = logging.getLogger("akt.startup")

st.set_page_config(page_title="AKT · Proveedores de Transformación",
                   page_icon="🏍️", layout="wide",
                   initial_sidebar_state="expanded")

# Dependencias internas: se cargan de forma controlada dentro de main().
# Evita que una excepción en un módulo secundario derribe la app antes de que
# Streamlit pueda mostrar una pantalla de recuperación.
ui = None
ROLES = {}
autenticar = None
puede = None
database_connection_hint = None
db_label = None
healthcheck = None
init_db = None


def _cargar_core():
    global ui, ROLES, autenticar, puede
    global database_connection_hint, db_label, healthcheck, init_db

    importlib.invalidate_caches()
    from core import ui as _ui
    from core.auth import ROLES as _ROLES, autenticar as _autenticar, puede as _puede
    from core.db import (
        database_connection_hint as _database_connection_hint,
        db_label as _db_label,
        healthcheck as _healthcheck,
        init_db as _init_db,
    )

    ui = _ui
    ROLES = _ROLES
    autenticar = _autenticar
    puede = _puede
    database_connection_hint = _database_connection_hint
    db_label = _db_label
    healthcheck = _healthcheck
    init_db = _init_db


@st.cache_resource(show_spinner="Preparando la base de datos...")
def _arranque():
    init_db()
    from core.db import session_scope
    from core.models import Usuario
    with session_scope() as s:
        vacio = s.query(Usuario.id).first() is None
    if vacio:
        from core.seed import sembrar
        sembrar()
    return True


def pantalla_login():
    ui.aplicar_estilos()
    _, c, _ = st.columns([1, 1.15, 1])
    with c:
        st.markdown(
            '<div class="akt-header"><h1>🏍️ AKT MOTOS · Proveedores de '
            'Transformación</h1><p>Recibo · Almacenamiento · Producción · '
            'Despacho</p></div>', unsafe_allow_html=True)
        with st.form("login"):
            email = st.text_input("Correo", placeholder="usuario@akt.com")
            pwd = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar", use_container_width=True, type="primary"):
                u = autenticar(email, pwd)
                if u:
                    st.session_state.user = u
                    st.rerun()
                else:
                    st.error("Credenciales inválidas o usuario inactivo.")
        from core.seed import demo_mode
        if demo_mode():
            with st.expander("Usuarios de demostración"):
                st.markdown(
                    "| Correo | Rol | Contraseña |\n|---|---|---|\n"
                    "| proveedor@akt.com | Proveedor | akt2026 |\n"
                    "| recibo@akt.com | Recibo AKT | akt2026 |\n"
                    "| inventarios@akt.com | Inventarios | akt2026 |\n"
                    "| planeacion@akt.com | Planeación | akt2026 |")
        okdb, dialecto = healthcheck()
        st.caption(("🟢 " if okdb else "🔴 ") + db_label())


def main():
    try:
        _cargar_core()
    except Exception as exc:
        error_id = uuid.uuid4().hex[:10]
        logger.exception(
            "Fallo cargando núcleo de la app. error_id=%s tipo=%s",
            error_id, type(exc).__name__,
        )
        st.error(
            "La aplicación no pudo cargar uno de sus componentes internos."
        )
        st.info(
            "El error fue aislado antes de iniciar la operación. "
            "No se ejecutaron movimientos ni escrituras de negocio."
        )
        st.caption(f"Referencia de soporte: {error_id}")
        if st.button("Reintentar arranque", type="primary"):
            importlib.invalidate_caches()
            st.rerun()
        st.stop()

    try:
        _arranque()
    except Exception as exc:
        from sqlalchemy.exc import OperationalError
        if isinstance(exc, OperationalError) or isinstance(
                getattr(exc, "__cause__", None), OperationalError):
            ui.aplicar_estilos()
            st.error(
                "No fue posible conectar con la base de datos PostgreSQL/Supabase."
            )
            st.info(database_connection_hint())
            st.caption(
                "La aplicación no cambia automáticamente a SQLite porque eso "
                "ocultaría el problema y podría mostrar una base distinta a la "
                "productiva. Revise el secreto de conexión y reinicie la app."
            )
            if st.button("Reintentar conexión", type="primary"):
                _arranque.clear()
                st.rerun()
            st.stop()
        raise

    if "user" not in st.session_state:
        pantalla_login()
        return

    user = st.session_state.user
    ui.aplicar_estilos()

    from app_pages import (almacenamiento, averias, ddmrp, despacho, inicio,
                           inventario, maestros, novedades, produccion, recibo)

    def p(fn, titulo, icono, permiso=None):
        if permiso and not puede(user, permiso):
            return None
        return st.Page(lambda f=fn: f(st.session_state.user),
                       title=titulo, icon=icono, url_path=titulo.lower()
                       .replace(" ", "-").replace("ó", "o").replace("í", "i")
                       .replace("á", "a").replace("é", "e"))

    secciones = {
        "Flujo de materiales": [
            p(inicio.render, "Inicio", ":material/home:"),
            p(recibo.render, "Recibo", ":material/inventory:"),
            p(novedades.render, "Novedades", ":material/report_problem:"),
            p(almacenamiento.render, "Almacenamiento", ":material/warehouse:"),
            p(averias.render, "Averias", ":material/build:"),
            p(produccion.render, "Produccion", ":material/precision_manufacturing:"),
            p(despacho.render, "Despacho", ":material/local_shipping:"),
        ],
        "Control": [
            p(inventario.render, "Inventario", ":material/analytics:"),
            p(ddmrp.render, "DDMRP", ":material/speed:", "ddmrp"),
        ],
        "Configuración": [
            p(maestros.render, "Maestros", ":material/settings:", "maestros"),
        ],
    }
    secciones = {k: [x for x in v if x] for k, v in secciones.items()}
    secciones = {k: v for k, v in secciones.items() if v}

    with st.sidebar:
        st.markdown(f"### 🏍️ AKT MOTOS")
        st.markdown(f"**{user['nombre']}**")
        st.caption(ROLES.get(user["rol"], user["rol"]))
        if user.get("proveedor_nombre"):
            st.caption(f"🏭 {user['proveedor_nombre']}")
        st.divider()

    nav = st.navigation(secciones, position="sidebar")

    with st.sidebar:
        st.divider()
        if st.button("Cerrar sesión", use_container_width=True):
            del st.session_state.user
            st.rerun()
        st.caption(db_label())

    nav.run()


main()
