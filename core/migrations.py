"""Migraciones idempotentes, limitadas al schema activo de AKT."""
from sqlalchemy import inspect, text


def _q(engine, schema, table):
    prep = engine.dialect.identifier_preparer
    tabla = prep.quote(table)
    if schema:
        return f"{prep.quote_schema(schema)}.{tabla}"
    return tabla


def _columns(engine, table, schema=None):
    return {
        c["name"]
        for c in inspect(engine).get_columns(table, schema=schema)
    }


def _add_column(engine, table, column, ddl, schema=None):
    if column in _columns(engine, table, schema=schema):
        return
    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE {_q(engine, schema, table)} ADD COLUMN {ddl}"
        ))


def _migrar_unique_bom(engine, schema=None):
    insp = inspect(engine)
    uniques = {
        u.get("name"): tuple(u.get("column_names") or [])
        for u in insp.get_unique_constraints("bom", schema=schema)
    }
    correcto = (
        "articulo_transformado", "componente", "secuencia", "proveedor_codigo"
    )
    if any(cols == correcto for cols in uniques.values()):
        return

    qbom = _q(engine, schema, "bom")

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {qbom} DROP CONSTRAINT IF EXISTS uq_bom_linea"))
            conn.execute(text(
                f"ALTER TABLE {qbom} DROP CONSTRAINT IF EXISTS uq_bom_linea_proveedor"))
            conn.execute(text(
                f"ALTER TABLE {qbom} ADD CONSTRAINT uq_bom_linea_proveedor "
                "UNIQUE (articulo_transformado, componente, secuencia, proveedor_codigo)"
            ))
        return

    if engine.dialect.name == "sqlite":
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


def run_migrations(engine, schema=None):
    """Aplica cambios solo a las tablas del schema seleccionado."""
    insp = inspect(engine)
    tables = set(insp.get_table_names(schema=schema))

    if "archivos" in tables:
        _add_column(
            engine, "archivos", "storage_path", "storage_path VARCHAR(500)",
            schema=schema)
        _add_column(
            engine, "archivos", "sha256", "sha256 VARCHAR(64)",
            schema=schema)

    if "proveedores" in tables:
        _add_column(
            engine, "proveedores", "ubicacion_origen",
            "ubicacion_origen VARCHAR(80)", schema=schema)
        _add_column(
            engine, "proveedores", "ubicacion_destino",
            "ubicacion_destino VARCHAR(80)", schema=schema)

        if "ubicaciones" in tables:
            qprov = _q(engine, schema, "proveedores")
            qubi = _q(engine, schema, "ubicaciones")
            with engine.begin() as conn:
                provs = conn.execute(text(f"""
                    SELECT id, ubicacion_origen, ubicacion_destino
                      FROM {qprov}
                     WHERE ubicacion_origen IS NULL OR ubicacion_origen = ''
                        OR ubicacion_destino IS NULL OR ubicacion_destino = ''
                """)).fetchall()
                for pid, origen_actual, destino_actual in provs:
                    locs = conn.execute(text(f"""
                        SELECT codigo, UPPER(COALESCE(rol,'')) AS rol
                          FROM {qubi}
                         WHERE proveedor_id = :pid
                           AND activo = :activo
                           AND cerrada = :cerrada
                         ORDER BY id
                    """), {
                        "pid": pid,
                        "activo": True,
                        "cerrada": False,
                    }).fetchall()

                    origenes = [
                        codigo for codigo, rol in locs if rol == "ORIGEN"
                    ]
                    destinos = [
                        codigo for codigo, rol in locs if rol == "DESTINO"
                    ]

                    origen = origen_actual
                    destino = destino_actual
                    if not origen and len(origenes) == 1:
                        origen = origenes[0]
                    if not destino and len(destinos) == 1:
                        destino = destinos[0]
                    elif not destino and len(locs) == 1:
                        destino = locs[0][0]

                    if origen or destino:
                        conn.execute(text(f"""
                            UPDATE {qprov}
                               SET ubicacion_origen = COALESCE(:origen, ubicacion_origen),
                                   ubicacion_destino = COALESCE(:destino, ubicacion_destino)
                             WHERE id = :pid
                        """), {
                            "origen": origen or None,
                            "destino": destino or None,
                            "pid": pid,
                        })

    if "recibos" in tables:
        _add_column(
            engine, "recibos", "proveedor_origen_id",
            "proveedor_origen_id INTEGER", schema=schema)
        _add_column(
            engine, "recibos", "factura_origen",
            "factura_origen VARCHAR(120)", schema=schema)

    if "recibo_lineas" in tables:
        _add_column(
            engine, "recibo_lineas", "orden_compra_id",
            "orden_compra_id INTEGER", schema=schema)
        _add_column(
            engine, "recibo_lineas", "cantidad_match",
            "cantidad_match FLOAT DEFAULT 0", schema=schema)
        _add_column(
            engine, "recibo_lineas", "estado_match",
            "estado_match VARCHAR(20)", schema=schema)

    if "bom" in tables:
        _migrar_unique_bom(engine, schema=schema)

    with engine.begin() as conn:
        if "archivos" in tables:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_archivos_sha256 "
                f"ON {_q(engine, schema, 'archivos')} (sha256)"
            ))
        if "recibo_lineas" in tables:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_recibo_lineas_oc "
                f"ON {_q(engine, schema, 'recibo_lineas')} (orden_compra_id)"
            ))
        if "proveedores" in tables:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_proveedores_ubicacion_origen "
                f"ON {_q(engine, schema, 'proveedores')} (ubicacion_origen)"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_proveedores_ubicacion_destino "
                f"ON {_q(engine, schema, 'proveedores')} (ubicacion_destino)"
            ))
