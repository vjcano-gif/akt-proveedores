"""
AKT MOTOS — App para Proveedores de Transformación
Flujo: RECIBO -> ALMACENAMIENTO -> PRODUCCIÓN -> DESPACHO
"""
import streamlit as st

st.set_page_config(page_title="AKT · Proveedores de Transformación",
                   page_icon="🏍️", layout="wide",
                   initial_sidebar_state="expanded")

from core import ui                                  # noqa: E402
from core.auth import ROLES, autenticar, puede       # noqa: E402
from core.db import db_label, healthcheck, init_db   # noqa: E402


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
    _arranque()
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
