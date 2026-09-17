"""Migraciones ligeras e idempotentes para instalaciones existentes.

No reemplaza un sistema formal de migraciones, pero garantiza que una base ya creada
con versiones anteriores incorpore las columnas críticas sin pérdida de datos.
"""
from __future__ import annotations

from sqlalchemy import inspect, text


def _add_column(conn, table: str, column: str, ddl: str):
    cols = {c["name"] for c in inspect(conn).get_columns(table)}
    if column not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def run_migrations(engine):
    with engine.begin() as conn:
        # archivos
        _add_column(conn, "archivos", "storage_path", "VARCHAR(500)")
        _add_column(conn, "archivos", "external_url", "VARCHAR(1000)")
        _add_column(conn, "archivos", "sha256", "VARCHAR(64)")

        # documentos
        _add_column(conn, "documentos", "extraccion_json", "TEXT")
        _add_column(conn, "documentos", "confianza_extraccion", "FLOAT")
        _add_column(conn, "documentos", "estado_extraccion", "VARCHAR(30)")

        # recibos
        _add_column(conn, "recibos", "proveedor_origen_id", "INTEGER")
        _add_column(conn, "recibos", "referencia_bin", "VARCHAR(120)")

        # líneas de recibo
        _add_column(conn, "recibo_lineas", "orden_compra_id", "INTEGER")
        _add_column(conn, "recibo_lineas", "cantidad_match", "FLOAT DEFAULT 0")
        _add_column(conn, "recibo_lineas", "cantidad_restringida", "FLOAT DEFAULT 0")
        _add_column(conn, "recibo_lineas", "estado_match", "VARCHAR(20) DEFAULT 'PENDIENTE'")

        # novedades
        _add_column(conn, "novedades", "origen_novedad", "VARCHAR(20) DEFAULT 'MANUAL'")

        # Índices no destructivos. IF NOT EXISTS funciona en PostgreSQL y SQLite modernos.
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recibos_proveedor_origen ON recibos (proveedor_origen_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recibo_lineas_oc ON recibo_lineas (orden_compra_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_archivos_sha256 ON archivos (sha256)"))
