"""Prueba de aislamiento cuando public ya pertenece a otra aplicación."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

url = os.environ.get("DATABASE_URL")
if not url or "postgres" not in url:
    raise SystemExit("DATABASE_URL PostgreSQL requerido para esta prueba.")

# Prepara una colisión realista en public.
probe = create_engine(url, future=True)
with probe.begin() as conn:
    conn.execute(text("DROP SCHEMA IF EXISTS akt_proveedores CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS public.proveedores CASCADE"))
    conn.execute(text("""
        CREATE TABLE public.proveedores (
            id UUID PRIMARY KEY,
            user_id UUID,
            nombre TEXT NOT NULL,
            nit TEXT,
            activo BOOLEAN DEFAULT TRUE
        )
    """))
probe.dispose()

# Importar después de preparar la colisión.
from core.db import get_active_schema, get_engine, init_db  # noqa: E402

init_db()
engine = get_engine()
schema = get_active_schema()

assert schema == "akt_proveedores", schema

insp = inspect(engine)
akt_tables = set(insp.get_table_names(schema="akt_proveedores"))
assert "proveedores" in akt_tables
assert "usuarios" in akt_tables
assert "recibos" in akt_tables

akt_cols = {
    c["name"] for c in insp.get_columns(
        "proveedores", schema="akt_proveedores")
}
assert {"codigo", "nombre", "tolerancia_averia_pct", "ubicacion_destino"} <= akt_cols

public_cols = {
    c["name"] for c in insp.get_columns("proveedores", schema="public")
}
assert "codigo" not in public_cols
assert "ubicacion_destino" not in public_cols
assert {"id", "user_id", "nombre", "nit", "activo"} <= public_cols

# Confirma que las consultas ORM resuelven al schema de AKT.
from core.db import session_scope  # noqa: E402
from core.models import Proveedor  # noqa: E402

with session_scope() as s:
    s.add(Proveedor(
        codigo="TEST-SCHEMA",
        nombre="Proveedor aislado",
        tolerancia_averia_pct=1.0,
        ubicacion_destino=None,
        activo=True,
    ))

with engine.connect() as conn:
    total_akt = conn.execute(text(
        "SELECT count(*) FROM akt_proveedores.proveedores "
        "WHERE codigo='TEST-SCHEMA'"
    )).scalar_one()
    total_public = conn.execute(text(
        "SELECT count(*) FROM public.proveedores"
    )).scalar_one()

assert total_akt == 1
assert total_public == 0

print("SCHEMA_ISOLATION_OK")

# Limpieza para que la prueba end-to-end normal pueda usar una base vacía.
cleanup = create_engine(url, future=True)
with cleanup.begin() as conn:
    conn.execute(text("DROP SCHEMA IF EXISTS akt_proveedores CASCADE"))
    conn.execute(text("DROP TABLE IF EXISTS public.proveedores CASCADE"))
cleanup.dispose()
