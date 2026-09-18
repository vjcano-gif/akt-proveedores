"""Prueba de upgrade de una tabla proveedores AKT antigua sin columnas nuevas."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

url = os.environ.get("DATABASE_URL")
if not url or "postgres" not in url:
    raise SystemExit("DATABASE_URL PostgreSQL requerido.")

# Deja una instalación AKT antigua en public: firma compatible pero sin
# ubicacion_origen/ubicacion_destino.
probe = create_engine(url, future=True)
with probe.begin() as conn:
    conn.execute(text("DROP SCHEMA IF EXISTS akt_proveedores CASCADE"))
    # Limpia tablas creadas por pruebas previas.
    for table in (
        "auditoria","ddmrp_parametros","despacho_lineas","despachos",
        "consumos_produccion","ordenes_produccion","mps","averias",
        "foto_inventario_erp","conteos_ejecutados","conteos_programados",
        "movimientos_inventario","inventario","novedades","recibo_lineas",
        "recibos","documentos","consecutivos","archivos","ordenes_compra",
        "bom","ubicaciones","articulos","usuarios","proveedores"
    ):
        conn.execute(text(f'DROP TABLE IF EXISTS public."{table}" CASCADE'))

    conn.execute(text("""
        CREATE TABLE public.proveedores (
            id SERIAL PRIMARY KEY,
            codigo VARCHAR(40) UNIQUE NOT NULL,
            nombre VARCHAR(200) NOT NULL,
            nit VARCHAR(40),
            tolerancia_averia_pct DOUBLE PRECISION DEFAULT 1.0,
            activo BOOLEAN DEFAULT TRUE,
            creado_en TIMESTAMP
        )
    """))
    conn.execute(text("""
        INSERT INTO public.proveedores
            (codigo, nombre, tolerancia_averia_pct, activo)
        VALUES ('LEGACY-001', 'Proveedor legado', 1.0, TRUE)
    """))
probe.dispose()

from core.db import get_active_schema, init_db, session_scope  # noqa: E402
from core.models import Proveedor  # noqa: E402

init_db()
assert get_active_schema() == "public"

insp = inspect(create_engine(url, future=True))
cols = {c["name"] for c in insp.get_columns("proveedores", schema="public")}
assert "ubicacion_origen" in cols, cols
assert "ubicacion_destino" in cols, cols

with session_scope() as s:
    p = s.query(Proveedor).filter(Proveedor.codigo == "LEGACY-001").one()
    assert p.nombre == "Proveedor legado"
    assert p.ubicacion_origen is None
    assert p.ubicacion_destino is None

print("SCHEMA_UPGRADE_OK")

# Limpieza para test_flujo.
cleanup = create_engine(url, future=True)
with cleanup.begin() as conn:
    for table in (
        "auditoria","ddmrp_parametros","despacho_lineas","despachos",
        "consumos_produccion","ordenes_produccion","mps","averias",
        "foto_inventario_erp","conteos_ejecutados","conteos_programados",
        "movimientos_inventario","inventario","novedades","recibo_lineas",
        "recibos","documentos","consecutivos","archivos","ordenes_compra",
        "bom","ubicaciones","articulos","usuarios","proveedores"
    ):
        conn.execute(text(f'DROP TABLE IF EXISTS public."{table}" CASCADE'))
cleanup.dispose()
