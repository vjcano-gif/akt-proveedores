-- 2026-09-17: correcciones de recibo, trazabilidad, adjuntos y extracción documental.
-- Idempotente para PostgreSQL/Supabase.

ALTER TABLE archivos ADD COLUMN IF NOT EXISTS storage_path VARCHAR(500);
ALTER TABLE archivos ADD COLUMN IF NOT EXISTS external_url VARCHAR(1000);
ALTER TABLE archivos ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64);

ALTER TABLE documentos ADD COLUMN IF NOT EXISTS extraccion_json TEXT;
ALTER TABLE documentos ADD COLUMN IF NOT EXISTS confianza_extraccion FLOAT;
ALTER TABLE documentos ADD COLUMN IF NOT EXISTS estado_extraccion VARCHAR(30);

ALTER TABLE recibos ADD COLUMN IF NOT EXISTS proveedor_origen_id INTEGER;
ALTER TABLE recibos ADD COLUMN IF NOT EXISTS referencia_bin VARCHAR(120);

ALTER TABLE recibo_lineas ADD COLUMN IF NOT EXISTS orden_compra_id INTEGER;
ALTER TABLE recibo_lineas ADD COLUMN IF NOT EXISTS cantidad_match FLOAT DEFAULT 0;
ALTER TABLE recibo_lineas ADD COLUMN IF NOT EXISTS cantidad_restringida FLOAT DEFAULT 0;
ALTER TABLE recibo_lineas ADD COLUMN IF NOT EXISTS estado_match VARCHAR(20) DEFAULT 'PENDIENTE';

ALTER TABLE novedades ADD COLUMN IF NOT EXISTS origen_novedad VARCHAR(20) DEFAULT 'MANUAL';

CREATE TABLE IF NOT EXISTS consecutivos_documento (
    clave VARCHAR(40) PRIMARY KEY,
    valor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documento_archivos (
    id BIGSERIAL PRIMARY KEY,
    documento_id INTEGER NOT NULL REFERENCES documentos(id),
    archivo_id INTEGER NOT NULL REFERENCES archivos(id),
    tipo VARCHAR(30) DEFAULT 'SOPORTE',
    creado_en TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_archivos_sha256 ON archivos (sha256);
CREATE INDEX IF NOT EXISTS ix_documento_archivos_documento_id ON documento_archivos (documento_id);
CREATE INDEX IF NOT EXISTS ix_recibos_proveedor_origen ON recibos (proveedor_origen_id);
CREATE INDEX IF NOT EXISTS ix_recibo_lineas_oc ON recibo_lineas (orden_compra_id);

DO $$ BEGIN
    ALTER TABLE recibos
      ADD CONSTRAINT fk_recibos_proveedor_origen
      FOREIGN KEY (proveedor_origen_id) REFERENCES proveedores(id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE recibo_lineas
      ADD CONSTRAINT fk_recibo_lineas_oc
      FOREIGN KEY (orden_compra_id) REFERENCES ordenes_compra(id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
