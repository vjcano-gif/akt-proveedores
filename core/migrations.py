"""Migraciones ligeras e idempotentes para cambios incrementales del esquema.

Se ejecutan al arrancar la app después de Base.metadata.create_all(). No reemplaza
una herramienta formal de migraciones, pero evita que create_all deje tablas
existentes sin columnas nuevas.
"""
from sqlalchemy import inspect, text


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _add_column(engine, table, column, ddl):
    if column in _columns(engine, table):
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _migrar_unique_bom(engine):
    """Incluye proveedor_codigo en la unicidad del BOM."""
    insp = inspect(engine)
    uniques = {u.get("name"): tuple(u.get("column_names") or [])
               for u in insp.get_unique_constraints("bom")}
    correcto = ("articulo_transformado", "componente", "secuencia", "proveedor_codigo")
    if any(cols == correcto for cols in uniques.values()):
        return

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bom DROP CONSTRAINT IF EXISTS uq_bom_linea"))
            conn.execute(text("ALTER TABLE bom DROP CONSTRAINT IF EXISTS uq_bom_linea_proveedor"))
            conn.execute(text(
                "ALTER TABLE bom ADD CONSTRAINT uq_bom_linea_proveedor "
                "UNIQUE (articulo_transformado, componente, secuencia, proveedor_codigo)"
            ))
        return

    if engine.dialect.name == "sqlite":
        # SQLite no permite DROP CONSTRAINT: se reconstruye preservando datos.
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS bom_mig (
                    id INTEGER PRIMARY KEY,
                    articulo_transformado VARCHAR(60) NOT NULL,
                    desc_transformado VARCHAR(300),
                    componente VARCHAR(60) NOT NULL,
                    desc_componente VARCHAR(300),
                    cantidad FLOAT,
                    secuencia INTEGER,
                    fase VARCHAR(20),
                    proveedor_codigo VARCHAR(40),
                    proveedor_nombre VARCHAR(200),
                    activo BOOLEAN,
                    CONSTRAINT uq_bom_linea_proveedor UNIQUE
                      (articulo_transformado, componente, secuencia, proveedor_codigo)
                )
            """))
            conn.execute(text("""
                INSERT OR IGNORE INTO bom_mig
                (id, articulo_transformado, desc_transformado, componente,
                 desc_componente, cantidad, secuencia, fase, proveedor_codigo,
                 proveedor_nombre, activo)
                SELECT id, articulo_transformado, desc_transformado, componente,
                       desc_componente, cantidad, secuencia, fase, proveedor_codigo,
                       proveedor_nombre, activo
                FROM bom
            """))
            conn.execute(text("DROP TABLE bom"))
            conn.execute(text("ALTER TABLE bom_mig RENAME TO bom"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_bom_articulo_transformado "
                "ON bom (articulo_transformado)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_bom_componente ON bom (componente)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_bom_proveedor_codigo ON bom (proveedor_codigo)"
            ))
            conn.execute(text("PRAGMA foreign_keys=ON"))


def run_migrations(engine):
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    if "archivos" in tables:
        _add_column(engine, "archivos", "storage_path", "storage_path VARCHAR(500)")
        _add_column(engine, "archivos", "sha256", "sha256 VARCHAR(64)")

    if "proveedores" in tables:
        _add_column(engine, "proveedores", "ubicacion_destino",
                    "ubicacion_destino VARCHAR(80)")
        # Backfill conservador: si ya existe una ubicación activa asociada al proveedor,
        # úsela como principal. Se prefiere rol DESTINO; si no existe, toma la primera.
        if "ubicaciones" in tables:
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    conn.execute(text("""
                        UPDATE proveedores p
                           SET ubicacion_destino = u.codigo
                          FROM LATERAL (
                               SELECT codigo
                                 FROM ubicaciones
                                WHERE proveedor_id = p.id
                                  AND activo = TRUE
                                  AND cerrada = FALSE
                                ORDER BY CASE WHEN UPPER(COALESCE(rol,'')) = 'DESTINO'
                                              THEN 0 ELSE 1 END,
                                         id
                                LIMIT 1
                          ) u
                         WHERE (p.ubicacion_destino IS NULL OR p.ubicacion_destino = '')
                    """))
                else:
                    conn.execute(text("""
                        UPDATE proveedores
                           SET ubicacion_destino = (
                               SELECT codigo
                                 FROM ubicaciones
                                WHERE ubicaciones.proveedor_id = proveedores.id
                                  AND activo = 1
                                  AND cerrada = 0
                                ORDER BY CASE WHEN UPPER(COALESCE(rol,'')) = 'DESTINO'
                                              THEN 0 ELSE 1 END,
                                         id
                                LIMIT 1
                           )
                         WHERE (ubicacion_destino IS NULL OR ubicacion_destino = '')
                           AND EXISTS (
                               SELECT 1
                                 FROM ubicaciones
                                WHERE ubicaciones.proveedor_id = proveedores.id
                                  AND activo = 1
                                  AND cerrada = 0
                           )
                    """))

    if "recibos" in tables:
        _add_column(engine, "recibos", "proveedor_origen_id",
                    "proveedor_origen_id INTEGER")
        _add_column(engine, "recibos", "factura_origen",
                    "factura_origen VARCHAR(120)")

    if "recibo_lineas" in tables:
        _add_column(engine, "recibo_lineas", "orden_compra_id",
                    "orden_compra_id INTEGER")
        _add_column(engine, "recibo_lineas", "cantidad_match",
                    "cantidad_match FLOAT DEFAULT 0")
        _add_column(engine, "recibo_lineas", "estado_match",
                    "estado_match VARCHAR(20)")

    if "bom" in tables:
        _migrar_unique_bom(engine)

    # Índices de apoyo. CREATE INDEX IF NOT EXISTS funciona en SQLite y PostgreSQL.
    with engine.begin() as conn:
        if "archivos" in tables:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_archivos_sha256 ON archivos (sha256)"
            ))
        if "recibo_lineas" in tables:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_recibo_lineas_oc "
                "ON recibo_lineas (orden_compra_id)"
            ))
        if "proveedores" in tables:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_proveedores_ubicacion_destino "
                "ON proveedores (ubicacion_destino)"
            ))
