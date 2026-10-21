"""Autenticación y control de acceso por rol."""
from __future__ import annotations

import hashlib
import os
import hmac

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
    # Inventarios actúa como administrador operativo de la aplicación.
    # El comodín evita que un permiso nuevo quede accidentalmente por fuera.
    "INVENTARIOS": {"*"},
    "PLANEACION": {
        "inicio", "recibo", "novedades", "almacenamiento", "averias",
        "produccion", "produccion_programar", "despacho", "inventario",
        "consulta_documentos", "maestros", "maestro_articulos", "maestro_bom",
        "ddmrp", "panel_control",
    },
}


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith("sha256$"):
        _, salt, h = hashed.split("$", 2)
        return hmac.compare_digest(hashlib.sha256((salt + password).encode()).hexdigest(), h)
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
        if u.rol not in ROLES or (u.rol == "PROVEEDOR" and (not prov or not prov.activo)):
            return None
        if u.password_hash.startswith("sha256$"):
            u.password_hash = hash_password(password)
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
    permisos = PERMISOS.get(user.get("rol", ""), set())
    return "*" in permisos or permiso in permisos


def alcance_proveedor(user: dict | None) -> int | None:
    """Un PROVEEDOR solo ve lo suyo; los demás roles ven todo (None)."""
    if user and user.get("rol") == "PROVEEDOR":
        proveedor_id = user.get("proveedor_id")
        if not proveedor_id:
            raise PermissionError("Proveedor sin empresa asignada: acceso bloqueado.")
        return proveedor_id
    return None


def refrescar_usuario(user: dict | None) -> dict | None:
    """Revoca sesiones con usuario/rol/proveedor desactivado o modificado."""
    if not user or not user.get("id"):
        return None
    with session_scope() as s:
        u = s.get(Usuario, user["id"])
        if not u or not u.activo or u.rol not in ROLES:
            return None
        if u.rol != user.get("rol") or u.proveedor_id != user.get("proveedor_id"):
            return None
        prov = s.get(Proveedor, u.proveedor_id) if u.proveedor_id else None
        if u.rol == "PROVEEDOR" and (not prov or not prov.activo):
            return None
        return {**user, "nombre": u.nombre, "email": u.email,
                "proveedor_codigo": prov.codigo if prov else None,
                "proveedor_nombre": prov.nombre if prov else None}
