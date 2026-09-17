-- Esquema PostgreSQL / Supabase — App Proveedores de Transformación (AKT MOTOS)
-- Generado desde el modelo de la aplicación. Ejecutar en Supabase -> SQL Editor.
-- La app también crea estas tablas sola al arrancar; este script es por si
-- se prefiere crearlas antes o revisarlas.

CREATE TABLE IF NOT EXISTS archivos (
	id SERIAL NOT NULL, 
	nombre VARCHAR(260) NOT NULL, 
	mime VARCHAR(120), 
	tamano INTEGER, 
	contenido BYTEA, 
	storage_path VARCHAR(500),
	sha256 VARCHAR(64),
	subido_por VARCHAR(160), 
	subido_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS articulos (
	id SERIAL NOT NULL, 
	codigo VARCHAR(60) NOT NULL, 
	descripcion VARCHAR(300) NOT NULL, 
	familia VARCHAR(60), 
	desc_familia VARCHAR(160), 
	grupo_articulo VARCHAR(60), 
	categoria VARCHAR(80), 
	color VARCHAR(80), 
	tipo VARCHAR(20), 
	unidad VARCHAR(20), 
	activo BOOLEAN, 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS auditoria (
	id SERIAL NOT NULL, 
	fecha TIMESTAMP WITHOUT TIME ZONE, 
	usuario VARCHAR(160), 
	rol VARCHAR(30), 
	accion VARCHAR(80), 
	entidad VARCHAR(60), 
	entidad_id VARCHAR(60), 
	detalle TEXT, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS bom (
	id SERIAL NOT NULL, 
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
	PRIMARY KEY (id), 
	CONSTRAINT uq_bom_linea UNIQUE (articulo_transformado, componente, secuencia)
);

CREATE TABLE IF NOT EXISTS proveedores (
	id SERIAL NOT NULL, 
	codigo VARCHAR(40) NOT NULL, 
	nombre VARCHAR(200) NOT NULL, 
	nit VARCHAR(40), 
	tolerancia_averia_pct FLOAT, 
	activo BOOLEAN, 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS ddmrp_parametros (
	id SERIAL NOT NULL, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	consumo_promedio_dia FLOAT, 
	lead_time_dias INTEGER, 
	factor_variabilidad FLOAT, 
	factor_lead_time FLOAT, 
	moq FLOAT, 
	zona_roja FLOAT, 
	zona_amarilla FLOAT, 
	zona_verde FLOAT, 
	activo BOOLEAN, 
	actualizado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_ddmrp UNIQUE (proveedor_id, articulo), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS consecutivos (
	clave VARCHAR(40) NOT NULL,
	valor INTEGER NOT NULL DEFAULT 0,
	actualizado_en TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (clave)
);

CREATE TABLE IF NOT EXISTS documentos (
	id SERIAL NOT NULL, 
	trz VARCHAR(30) NOT NULL, 
	tipo VARCHAR(30) NOT NULL, 
	proveedor_id INTEGER, 
	referencia VARCHAR(120), 
	fecha_documento DATE, 
	archivo_id INTEGER, 
	creado_por VARCHAR(160), 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	observaciones TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id), 
	FOREIGN KEY(archivo_id) REFERENCES archivos (id)
);

CREATE TABLE IF NOT EXISTS foto_inventario_erp (
	id SERIAL NOT NULL, 
	corte DATE, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	ubicacion VARCHAR(80), 
	cantidad_erp FLOAT, 
	cargado_por VARCHAR(160), 
	cargado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS inventario (
	id SERIAL NOT NULL, 
	proveedor_id INTEGER NOT NULL, 
	articulo VARCHAR(60) NOT NULL, 
	ubicacion VARCHAR(80), 
	estado VARCHAR(20), 
	condicion VARCHAR(20), 
	cantidad FLOAT, 
	actualizado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_saldo UNIQUE (proveedor_id, articulo, ubicacion, estado, condicion), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS ordenes_compra (
	id SERIAL NOT NULL, 
	numero VARCHAR(60) NOT NULL, 
	proveedor_id INTEGER NOT NULL, 
	articulo VARCHAR(60) NOT NULL, 
	cantidad FLOAT NOT NULL, 
	cantidad_recibida FLOAT, 
	estado VARCHAR(20), 
	fecha DATE, 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS ubicaciones (
	id SERIAL NOT NULL, 
	codigo VARCHAR(80) NOT NULL, 
	un VARCHAR(20), 
	rol VARCHAR(20), 
	proveedor_id INTEGER, 
	cerrada BOOLEAN, 
	inspeccion BOOLEAN, 
	restringida BOOLEAN, 
	activo BOOLEAN, 
	PRIMARY KEY (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS usuarios (
	id SERIAL NOT NULL, 
	email VARCHAR(160) NOT NULL, 
	nombre VARCHAR(160) NOT NULL, 
	rol VARCHAR(30) NOT NULL, 
	proveedor_id INTEGER, 
	password_hash VARCHAR(200) NOT NULL, 
	activo BOOLEAN, 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS averias (
	id SERIAL NOT NULL, 
	documento_id INTEGER, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	cantidad FLOAT, 
	momento VARCHAR(30), 
	motivo VARCHAR(30) NOT NULL, 
	evidencia_id INTEGER, 
	estado_inventario VARCHAR(20), 
	estado VARCHAR(30), 
	documento_ajuste VARCHAR(20), 
	fecha TIMESTAMP WITHOUT TIME ZONE, 
	registrado_por VARCHAR(160), 
	observaciones TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id), 
	FOREIGN KEY(evidencia_id) REFERENCES archivos (id)
);

CREATE TABLE IF NOT EXISTS conteos_programados (
	id SERIAL NOT NULL, 
	documento_id INTEGER, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	ubicacion VARCHAR(80), 
	fecha_programada DATE, 
	prioridad INTEGER, 
	estado VARCHAR(20), 
	creado_por VARCHAR(160), 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS despachos (
	id SERIAL NOT NULL, 
	documento_id INTEGER NOT NULL, 
	proveedor_id INTEGER NOT NULL, 
	lote VARCHAR(60), 
	plan_ensamble VARCHAR(120), 
	estado VARCHAR(20), 
	ubicacion_origen VARCHAR(80), 
	creado_por VARCHAR(160), 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	despachado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS movimientos_inventario (
	id SERIAL NOT NULL, 
	fecha TIMESTAMP WITHOUT TIME ZONE, 
	tipo VARCHAR(30) NOT NULL, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	ubicacion VARCHAR(80), 
	estado VARCHAR(20), 
	condicion VARCHAR(20), 
	cantidad FLOAT, 
	saldo_resultante FLOAT, 
	documento_id INTEGER, 
	referencia VARCHAR(120), 
	usuario VARCHAR(160), 
	PRIMARY KEY (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id)
);

CREATE TABLE IF NOT EXISTS mps (
	id SERIAL NOT NULL, 
	documento_id INTEGER, 
	proveedor_id INTEGER NOT NULL, 
	articulo VARCHAR(60) NOT NULL, 
	cantidad_programada FLOAT, 
	cantidad_ejecutada FLOAT, 
	fecha_programada DATE, 
	estado VARCHAR(20), 
	ubicacion_destino VARCHAR(80), 
	creado_por VARCHAR(160), 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	observaciones TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS recibos (
	id SERIAL NOT NULL, 
	documento_id INTEGER NOT NULL, 
	proveedor_id INTEGER NOT NULL,
	proveedor_origen_id INTEGER,
	factura_origen VARCHAR(120),
	origen VARCHAR(20) NOT NULL, 
	estado VARCHAR(20), 
	orden_compra_id INTEGER, 
	es_reproceso BOOLEAN, 
	sellado_por VARCHAR(160), 
	sellado_en TIMESTAMP WITHOUT TIME ZONE, 
	ubicacion_destino VARCHAR(80), 
	creado_por VARCHAR(160), 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	cerrado_en TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id),
	FOREIGN KEY(proveedor_origen_id) REFERENCES proveedores (id),
	FOREIGN KEY(orden_compra_id) REFERENCES ordenes_compra (id)
);

CREATE TABLE IF NOT EXISTS conteos_ejecutados (
	id SERIAL NOT NULL, 
	documento_id INTEGER, 
	programado_id INTEGER, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	ubicacion VARCHAR(80), 
	cantidad_sistema FLOAT, 
	cantidad_fisica FLOAT, 
	diferencia FLOAT, 
	es_aleatorio BOOLEAN, 
	genera_cola BOOLEAN, 
	estado VARCHAR(30), 
	fecha TIMESTAMP WITHOUT TIME ZONE, 
	ejecutado_por VARCHAR(160), 
	observaciones TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(programado_id) REFERENCES conteos_programados (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS despacho_lineas (
	id SERIAL NOT NULL, 
	despacho_id INTEGER NOT NULL, 
	articulo VARCHAR(60) NOT NULL, 
	descripcion VARCHAR(300), 
	cantidad FLOAT, 
	ubicacion VARCHAR(80), 
	PRIMARY KEY (id), 
	FOREIGN KEY(despacho_id) REFERENCES despachos (id)
);

CREATE TABLE IF NOT EXISTS ordenes_produccion (
	id SERIAL NOT NULL, 
	documento_id INTEGER, 
	mps_id INTEGER, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	cantidad FLOAT, 
	ubicacion_destino VARCHAR(80), 
	estado VARCHAR(20), 
	fecha TIMESTAMP WITHOUT TIME ZONE, 
	ejecutado_por VARCHAR(160), 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(mps_id) REFERENCES mps (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id)
);

CREATE TABLE IF NOT EXISTS recibo_lineas (
	id SERIAL NOT NULL, 
	recibo_id INTEGER NOT NULL, 
	articulo VARCHAR(60) NOT NULL, 
	descripcion VARCHAR(300), 
	cantidad_documento FLOAT, 
	cantidad_fisica FLOAT, 
	lote VARCHAR(60), 
	serial VARCHAR(60), 
	ubicacion_desde VARCHAR(80), 
	ubicacion_hasta VARCHAR(80),
	orden_compra_id INTEGER,
	cantidad_match FLOAT DEFAULT 0,
	estado_match VARCHAR(20),
	condicion VARCHAR(20), 
	procesada BOOLEAN, 
	PRIMARY KEY (id),
	FOREIGN KEY(recibo_id) REFERENCES recibos (id),
	FOREIGN KEY(orden_compra_id) REFERENCES ordenes_compra (id)
);

CREATE TABLE IF NOT EXISTS consumos_produccion (
	id SERIAL NOT NULL, 
	orden_id INTEGER NOT NULL, 
	componente VARCHAR(60), 
	cantidad FLOAT, 
	ubicacion VARCHAR(80), 
	PRIMARY KEY (id), 
	FOREIGN KEY(orden_id) REFERENCES ordenes_produccion (id)
);

CREATE TABLE IF NOT EXISTS novedades (
	id SERIAL NOT NULL, 
	documento_id INTEGER, 
	recibo_id INTEGER, 
	recibo_linea_id INTEGER, 
	proveedor_id INTEGER, 
	articulo VARCHAR(60), 
	tipo VARCHAR(20) NOT NULL, 
	cantidad FLOAT, 
	motivo VARCHAR(30), 
	condicion_resultante VARCHAR(20), 
	evidencia_id INTEGER, 
	estado VARCHAR(30), 
	documento_ajuste VARCHAR(20), 
	numero_ajuste VARCHAR(60), 
	ajustada_por VARCHAR(160), 
	ajustada_en TIMESTAMP WITHOUT TIME ZONE, 
	creado_por VARCHAR(160), 
	creado_en TIMESTAMP WITHOUT TIME ZONE, 
	observaciones TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id), 
	FOREIGN KEY(recibo_id) REFERENCES recibos (id), 
	FOREIGN KEY(recibo_linea_id) REFERENCES recibo_lineas (id), 
	FOREIGN KEY(proveedor_id) REFERENCES proveedores (id), 
	FOREIGN KEY(evidencia_id) REFERENCES archivos (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_articulos_codigo ON articulos (codigo);
CREATE INDEX IF NOT EXISTS ix_auditoria_fecha ON auditoria (fecha);
CREATE INDEX IF NOT EXISTS ix_bom_proveedor_codigo ON bom (proveedor_codigo);
CREATE INDEX IF NOT EXISTS ix_bom_articulo_transformado ON bom (articulo_transformado);
CREATE INDEX IF NOT EXISTS ix_bom_componente ON bom (componente);
CREATE UNIQUE INDEX IF NOT EXISTS ix_proveedores_codigo ON proveedores (codigo);
CREATE INDEX IF NOT EXISTS ix_ddmrp_parametros_articulo ON ddmrp_parametros (articulo);
CREATE INDEX IF NOT EXISTS ix_ddmrp_parametros_proveedor_id ON ddmrp_parametros (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_documentos_proveedor_id ON documentos (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_documentos_tipo ON documentos (tipo);
CREATE INDEX IF NOT EXISTS ix_documentos_creado_en ON documentos (creado_en);
CREATE UNIQUE INDEX IF NOT EXISTS ix_documentos_trz ON documentos (trz);
CREATE INDEX IF NOT EXISTS ix_foto_inventario_erp_corte ON foto_inventario_erp (corte);
CREATE INDEX IF NOT EXISTS ix_foto_inventario_erp_articulo ON foto_inventario_erp (articulo);
CREATE INDEX IF NOT EXISTS ix_foto_inventario_erp_proveedor_id ON foto_inventario_erp (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_inv_busqueda ON inventario (proveedor_id, articulo, estado, condicion);
CREATE INDEX IF NOT EXISTS ix_inventario_articulo ON inventario (articulo);
CREATE INDEX IF NOT EXISTS ix_inventario_proveedor_id ON inventario (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_ordenes_compra_numero ON ordenes_compra (numero);
CREATE INDEX IF NOT EXISTS ix_ordenes_compra_articulo ON ordenes_compra (articulo);
CREATE UNIQUE INDEX IF NOT EXISTS ix_ubicaciones_codigo ON ubicaciones (codigo);
CREATE UNIQUE INDEX IF NOT EXISTS ix_usuarios_email ON usuarios (email);
CREATE INDEX IF NOT EXISTS ix_averias_momento ON averias (momento);
CREATE INDEX IF NOT EXISTS ix_averias_proveedor_id ON averias (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_averias_articulo ON averias (articulo);
CREATE INDEX IF NOT EXISTS ix_averias_motivo ON averias (motivo);
CREATE INDEX IF NOT EXISTS ix_averias_fecha ON averias (fecha);
CREATE INDEX IF NOT EXISTS ix_averias_estado ON averias (estado);
CREATE INDEX IF NOT EXISTS ix_conteos_programados_fecha_programada ON conteos_programados (fecha_programada);
CREATE INDEX IF NOT EXISTS ix_conteos_programados_proveedor_id ON conteos_programados (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_conteos_programados_articulo ON conteos_programados (articulo);
CREATE INDEX IF NOT EXISTS ix_conteos_programados_estado ON conteos_programados (estado);
CREATE INDEX IF NOT EXISTS ix_despachos_proveedor_id ON despachos (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_despachos_lote ON despachos (lote);
CREATE INDEX IF NOT EXISTS ix_despachos_creado_en ON despachos (creado_en);
CREATE INDEX IF NOT EXISTS ix_despachos_estado ON despachos (estado);
CREATE INDEX IF NOT EXISTS ix_movimientos_inventario_proveedor_id ON movimientos_inventario (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_movimientos_inventario_tipo ON movimientos_inventario (tipo);
CREATE INDEX IF NOT EXISTS ix_movimientos_inventario_fecha ON movimientos_inventario (fecha);
CREATE INDEX IF NOT EXISTS ix_movimientos_inventario_articulo ON movimientos_inventario (articulo);
CREATE INDEX IF NOT EXISTS ix_mps_articulo ON mps (articulo);
CREATE INDEX IF NOT EXISTS ix_mps_fecha_programada ON mps (fecha_programada);
CREATE INDEX IF NOT EXISTS ix_mps_estado ON mps (estado);
CREATE INDEX IF NOT EXISTS ix_mps_proveedor_id ON mps (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_recibos_proveedor_id ON recibos (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_recibos_creado_en ON recibos (creado_en);
CREATE INDEX IF NOT EXISTS ix_recibos_estado ON recibos (estado);
CREATE INDEX IF NOT EXISTS ix_conteos_ejecutados_estado ON conteos_ejecutados (estado);
CREATE INDEX IF NOT EXISTS ix_conteos_ejecutados_articulo ON conteos_ejecutados (articulo);
CREATE INDEX IF NOT EXISTS ix_conteos_ejecutados_proveedor_id ON conteos_ejecutados (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_conteos_ejecutados_fecha ON conteos_ejecutados (fecha);
CREATE INDEX IF NOT EXISTS ix_despacho_lineas_despacho_id ON despacho_lineas (despacho_id);
CREATE INDEX IF NOT EXISTS ix_despacho_lineas_articulo ON despacho_lineas (articulo);
CREATE INDEX IF NOT EXISTS ix_ordenes_produccion_proveedor_id ON ordenes_produccion (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_ordenes_produccion_fecha ON ordenes_produccion (fecha);
CREATE INDEX IF NOT EXISTS ix_ordenes_produccion_mps_id ON ordenes_produccion (mps_id);
CREATE INDEX IF NOT EXISTS ix_ordenes_produccion_articulo ON ordenes_produccion (articulo);
CREATE INDEX IF NOT EXISTS ix_recibo_lineas_recibo_id ON recibo_lineas (recibo_id);
CREATE INDEX IF NOT EXISTS ix_recibo_lineas_articulo ON recibo_lineas (articulo);
CREATE INDEX IF NOT EXISTS ix_consumos_produccion_componente ON consumos_produccion (componente);
CREATE INDEX IF NOT EXISTS ix_novedades_recibo_id ON novedades (recibo_id);
CREATE INDEX IF NOT EXISTS ix_novedades_proveedor_id ON novedades (proveedor_id);
CREATE INDEX IF NOT EXISTS ix_novedades_estado ON novedades (estado);
CREATE INDEX IF NOT EXISTS ix_novedades_articulo ON novedades (articulo);
CREATE INDEX IF NOT EXISTS ix_novedades_creado_en ON novedades (creado_en);
CREATE INDEX IF NOT EXISTS ix_archivos_sha256 ON archivos (sha256);
CREATE INDEX IF NOT EXISTS ix_recibos_proveedor_origen_id ON recibos (proveedor_origen_id);
CREATE INDEX IF NOT EXISTS ix_recibo_lineas_oc ON recibo_lineas (orden_compra_id);
