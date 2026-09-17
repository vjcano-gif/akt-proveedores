"""Autenticación y control de acceso por rol."""
from __future__ import annotations

import hashlib
import os

from core.db import session_scope
from core.models import Usuario, Proveedor

ROLES = {
    "PROVEEDOR": "Proveedor de transformación",
    "RECIBO_AKT": "Equipo de Recibo AKT",
    "INVENTARIOS": "Equipo de Inventarios",
    "PLANEACION": "Planeación",
}

# Qué puede ver/hacer cada rol
PERMISOS = {
    "PROVEEDOR": {
        "inicio", "recibo", "recibo_registrar", "recibo_sellar", "novedades",
        "novedades_registrar", "almacenamiento", "conteos_ejecutar", "averias",
        "averias_registrar", "produccion", "produccion_ejecutar", "despacho",
        "despacho_registrar", "inventario", "consulta_documentos",
    },
    "RECIBO_AKT": {
        "inicio", "recibo", "recibo_adjuntar_bin", "recibo_match_oc",
        "recibo_registrar", "novedades", "novedades_registrar",
        "almacenamiento", "averias", "produccion", "despacho", "inventario",
        "consulta_documentos", "maestro_oc",
    },
    "INVENTARIOS": {
        "inicio", "recibo", "novedades", "novedades_ajustar", "almacenamiento",
        "conteos_programar", "conteos_ejecutar", "foto_erp", "averias",
        "averias_ajustar", "produccion", "despacho", "inventario",
        "consulta_documentos", "maestros", "maestro_articulos",
        "maestro_ubicaciones", "maestro_proveedores", "maestro_usuarios",
        "maestro_oc", "maestro_bom", "ddmrp", "panel_control",
    },
    "PLANEACION": {
        "inicio", "recibo", "novedades", "almacenamiento", "averias",
        "produccion", "produccion_programar", "despacho", "inventario",
        "consulta_documentos", "maestros", "maestro_articulos", "maestro_bom",
        "ddmrp", "panel_control",
    },
}


def hash_password(password: str) -> str:
    try:
        import bcrypt
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    except Exception:
        salt = os.urandom(16).hex()
        h = hashlib.sha256((salt + password).encode()).hexdigest()
        return f"sha256${salt}${h}"


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith("sha256$"):
        _, salt, h = hashed.split("$", 2)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    try:
        import bcrypt
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def autenticar(email: str, password: str) -> dict | None:
    email = (email or "").strip().lower()
    with session_scope() as s:
        u = s.query(Usuario).filter(Usuario.email == email).first()
        if not u or not u.activo or not verify_password(password, u.password_hash):
            return None
        prov = s.get(Proveedor, u.proveedor_id) if u.proveedor_id else None
        return {
            "id": u.id,
            "email": u.email,
            "nombre": u.nombre,
            "rol": u.rol,
            "proveedor_id": u.proveedor_id,
            "proveedor_codigo": prov.codigo if prov else None,
            "proveedor_nombre": prov.nombre if prov else None,
        }


def puede(user: dict | None, permiso: str) -> bool:
    if not user:
        return False
    return permiso in PERMISOS.get(user.get("rol", ""), set())


def alcance_proveedor(user: dict | None) -> int | None:
    """Un PROVEEDOR solo ve lo suyo; los demás roles ven todo (None)."""
    if user and user.get("rol") == "PROVEEDOR":
        return user.get("proveedor_id")
    return None



def _setting(name: str, default=None):
    try:
        import streamlit as st
        value = st.secrets.get(name)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.environ.get(name, default)


def ensure_bootstrap_user() -> bool:
    """Crea el primer usuario INVENTARIOS solo desde secretos de despliegue.

    Nunca usa credenciales predeterminadas en PostgreSQL/Supabase.
    """
    email = str(_setting("BOOTSTRAP_ADMIN_EMAIL", "") or "").strip().lower()
    password = str(_setting("BOOTSTRAP_ADMIN_PASSWORD", "") or "")
    nombre = str(_setting("BOOTSTRAP_ADMIN_NAME", "Administrador Inventarios") or
                 "Administrador Inventarios").strip()
    if not email or not password:
        return False
    if len(password) < 12:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD debe tener al menos 12 caracteres.")
    with session_scope() as s:
        if s.query(Usuario.id).first() is not None:
            return False
        s.add(Usuario(
            email=email, nombre=nombre, rol="INVENTARIOS", proveedor_id=None,
            password_hash=hash_password(password), activo=True))
    return True
