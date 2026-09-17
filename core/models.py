"""
Modelo de datos - App Proveedores de Transformación (AKT MOTOS).

Portable entre SQLite (local) y PostgreSQL (Supabase).
Todo documento lleva un ID de trazabilidad (TRZ-...) para medir antigüedad.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Integer,
    LargeBinary, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def now():
    return dt.datetime.utcnow()


# =========================================================================
# MAESTROS
# =========================================================================

class Proveedor(Base):
    __tablename__ = "proveedores"
    id = Column(Integer, primary_key=True)
    codigo = Column(String(40), unique=True, nullable=False, index=True)
    nombre = Column(String(200), nullable=False)
    nit = Column(String(40))
    # Acuerdo comercial: margen de tolerancia de avería (%)
    tolerancia_averia_pct = Column(Float, default=1.0)
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=now)


class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    email = Column(String(160), unique=True, nullable=False, index=True)
    nombre = Column(String(160), nullable=False)
    # PROVEEDOR | RECIBO_AKT | INVENTARIOS | PLANEACION
    rol = Column(String(30), nullable=False)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"))
    password_hash = Column(String(200), nullable=False)
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=now)

    proveedor = relationship("Proveedor")


class Articulo(Base):
    __tablename__ = "articulos"
    id = Column(Integer, primary_key=True)
    codigo = Column(String(60), unique=True, nullable=False, index=True)
    descripcion = Column(String(300), nullable=False)
    familia = Column(String(60))
    desc_familia = Column(String(160))
    grupo_articulo = Column(String(60))
    categoria = Column(String(80))
    color = Column(String(80))
    tipo = Column(String(20), default="CRUDO")     # CRUDO | TRANSFORMADO
    unidad = Column(String(20), default="UND")
    activo = Column(Boolean, default=True)
    creado_en = Column(DateTime, default=now)


class Ubicacion(Base):
    """WMS opcional: detalle a nivel de ubicación en todo el flujo."""
    __tablename__ = "ubicaciones"
    id = Column(Integer, primary_key=True)
    codigo = Column(String(80), unique=True, nullable=False, index=True)
    un = Column(String(20), default="MOTOS")
    rol = Column(String(20))                        # ORIGEN | DESTINO
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"))
    cerrada = Column(Boolean, default=False)
    inspeccion = Column(Boolean, default=False)
    restringida = Column(Boolean, default=False)
    activo = Column(Boolean, default=True)


class Bom(Base):
    """Subcontratación: artículo transformado <- componentes que consume."""
    __tablename__ = "bom"
    id = Column(Integer, primary_key=True)
    articulo_transformado = Column(String(60), nullable=False, index=True)
    desc_transformado = Column(String(300))
    componente = Column(String(60), nullable=False, index=True)
    desc_componente = Column(String(300))
    cantidad = Column(Float, default=1.0)
    secuencia = Column(Integer, default=0)
    fase = Column(String(20))
    proveedor_codigo = Column(String(40), index=True)
    proveedor_nombre = Column(String(200))
    activo = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("articulo_transformado", "componente", "secuencia",
                         name="uq_bom_linea"),
    )


class OrdenCompra(Base):
    """OC abiertas contra las cuales se hace match del BIN a BIN."""
    __tablename__ = "ordenes_compra"
    id = Column(Integer, primary_key=True)
    numero = Column(String(60), nullable=False, index=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False)
    articulo = Column(String(60), nullable=False, index=True)
    cantidad = Column(Float, nullable=False)
    cantidad_recibida = Column(Float, default=0.0)
    estado = Column(String(20), default="ABIERTA")   # ABIERTA | CERRADA
    fecha = Column(Date, default=dt.date.today)
    creado_en = Column(DateTime, default=now)

    proveedor = relationship("Proveedor")

    @property
    def pendiente(self):
        return (self.cantidad or 0) - (self.cantidad_recibida or 0)


# =========================================================================
# ARCHIVOS / EVIDENCIAS
# =========================================================================

class Archivo(Base):
    __tablename__ = "archivos"
    id = Column(Integer, primary_key=True)
    nombre = Column(String(260), nullable=False)
    mime = Column(String(120))
    tamano = Column(Integer)
    # Fallback local/SQLite. En producción se prefiere Supabase Storage.
    contenido = Column(LargeBinary)
    storage_path = Column(String(500))
    external_url = Column(String(1000))
    sha256 = Column(String(64), index=True)
    subido_por = Column(String(160))
    subido_en = Column(DateTime, default=now)


# =========================================================================
# TRAZABILIDAD DOCUMENTAL
# =========================================================================

class ConsecutivoDocumento(Base):
    """Contador atómico por tipo/año para evitar TRZ duplicados en concurrencia."""
    __tablename__ = "consecutivos_documento"
    clave = Column(String(40), primary_key=True)
    valor = Column(Integer, nullable=False, default=0)


class Documento(Base):
    """
    Cabecera de trazabilidad. Cada documento del flujo (BIN a BIN, factura,
    registro, conteo, avería, MPS, despacho, ajuste) cuelga de aquí y hereda
    un consecutivo TRZ-AAAA-###### para poder medir la antigüedad.
    """
    __tablename__ = "documentos"
    id = Column(Integer, primary_key=True)
    trz = Column(String(30), unique=True, nullable=False, index=True)
    tipo = Column(String(30), nullable=False, index=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    referencia = Column(String(120))                  # No. bin, factura, OC...
    fecha_documento = Column(Date, default=dt.date.today)
    archivo_id = Column(Integer, ForeignKey("archivos.id"))
    # Resultado del reconocimiento documental (PDF/imagen).
    extraccion_json = Column(Text)
    confianza_extraccion = Column(Float)
    estado_extraccion = Column(String(30))  # EXTRAIDO | REVISION | MANUAL | ERROR
    creado_por = Column(String(160))
    creado_en = Column(DateTime, default=now, index=True)
    observaciones = Column(Text)

    proveedor = relationship("Proveedor")
    archivo = relationship("Archivo")
    adjuntos = relationship("DocumentoArchivo", back_populates="documento",
                            cascade="all, delete-orphan")

    @property
    def antiguedad_dias(self):
        base = self.creado_en or now()
        return (now() - base).days


class DocumentoArchivo(Base):
    """Permite conservar factura, BIN y otros soportes sin sobrescribir archivos."""
    __tablename__ = "documento_archivos"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"), nullable=False, index=True)
    archivo_id = Column(Integer, ForeignKey("archivos.id"), nullable=False)
    tipo = Column(String(30), default="SOPORTE")
    creado_en = Column(DateTime, default=now)

    documento = relationship("Documento", back_populates="adjuntos")
    archivo = relationship("Archivo")


# =========================================================================
# RECIBO
# =========================================================================

class Recibo(Base):
    __tablename__ = "recibos"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"), nullable=False)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False, index=True)
    # Proveedor que despacha originalmente el material (distinto al transformador).
    proveedor_origen_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    proveedor_origen_nombre = Column(String(200))
    proveedor_origen_nit = Column(String(40))
    # BIN_A_BIN (materia prima cruda desde MOTOS) | FACTURA (proveedor origen) | REGISTRO
    origen = Column(String(20), nullable=False)
    # BORRADOR | SELLADO | PENDIENTE_MATCH | NOVEDAD | CERRADA
    estado = Column(String(20), default="BORRADOR", index=True)
    orden_compra_id = Column(Integer, ForeignKey("ordenes_compra.id"))
    referencia_bin = Column(String(120))
    es_reproceso = Column(Boolean, default=False)     # garantías / calidad
    sellado_por = Column(String(160))                 # proveedor certifica recepción
    sellado_en = Column(DateTime)
    ubicacion_destino = Column(String(80))            # WMS opcional
    creado_por = Column(String(160))
    creado_en = Column(DateTime, default=now, index=True)
    cerrado_en = Column(DateTime)

    documento = relationship("Documento")
    proveedor = relationship("Proveedor", foreign_keys=[proveedor_id])
    proveedor_origen = relationship("Proveedor", foreign_keys=[proveedor_origen_id])
    orden_compra = relationship("OrdenCompra")
    lineas = relationship("ReciboLinea", back_populates="recibo",
                          cascade="all, delete-orphan")


class ReciboLinea(Base):
    __tablename__ = "recibo_lineas"
    id = Column(Integer, primary_key=True)
    recibo_id = Column(Integer, ForeignKey("recibos.id"), nullable=False, index=True)
    articulo = Column(String(60), nullable=False, index=True)
    descripcion = Column(String(300))
    cantidad_documento = Column(Float, default=0.0)
    cantidad_fisica = Column(Float, default=0.0)
    lote = Column(String(60))
    serial = Column(String(60))
    ubicacion_desde = Column(String(80))
    ubicacion_hasta = Column(String(80))
    # Match por línea: un recibo puede contener varias OC.
    orden_compra_id = Column(Integer, ForeignKey("ordenes_compra.id"), index=True)
    cantidad_match = Column(Float, default=0.0)
    cantidad_restringida = Column(Float, default=0.0)
    estado_match = Column(String(20), default="PENDIENTE")  # PENDIENTE|COINCIDE|FALTANTE|SOBRANTE
    # DISPONIBLE | RESTRINGIDO
    condicion = Column(String(20), default="DISPONIBLE")
    procesada = Column(Boolean, default=False)

    recibo = relationship("Recibo", back_populates="lineas")
    orden_compra = relationship("OrdenCompra")

    @property
    def diferencia(self):
        return (self.cantidad_fisica or 0) - (self.cantidad_documento or 0)


# =========================================================================
# NOVEDADES Y COLA DE TRABAJO
# =========================================================================

class Novedad(Base):
    __tablename__ = "novedades"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    recibo_id = Column(Integer, ForeignKey("recibos.id"), index=True)
    recibo_linea_id = Column(Integer, ForeignKey("recibo_lineas.id"))
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    tipo = Column(String(20), nullable=False)          # FALTANTE|SOBRANTE|AVERIA
    cantidad = Column(Float, default=0.0)
    # ORIGEN | MANIPULACION | PUESTA_A_PUNTO | AKT
    motivo = Column(String(30))
    # RECEPCION = diferencia contra OC; MANUAL = hallazgo físico/operativo.
    origen_novedad = Column(String(20), default="MANUAL")
    condicion_resultante = Column(String(20), default="RESTRINGIDO")
    evidencia_id = Column(Integer, ForeignKey("archivos.id"))
    # ABIERTA | EN_COLA_INVENTARIOS | AJUSTADA | CERRADA
    estado = Column(String(30), default="ABIERTA", index=True)
    documento_ajuste = Column(String(20))              # TD90 | TD96
    numero_ajuste = Column(String(60))
    ajustada_por = Column(String(160))
    ajustada_en = Column(DateTime)
    creado_por = Column(String(160))
    creado_en = Column(DateTime, default=now, index=True)
    observaciones = Column(Text)

    documento = relationship("Documento")
    evidencia = relationship("Archivo")
    proveedor = relationship("Proveedor")


# =========================================================================
# INVENTARIO
# =========================================================================

class Inventario(Base):
    """Saldo por proveedor / artículo / ubicación / estado / condición."""
    __tablename__ = "inventario"
    id = Column(Integer, primary_key=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False, index=True)
    articulo = Column(String(60), nullable=False, index=True)
    ubicacion = Column(String(80), default="")
    estado = Column(String(20), default="CRUDO")       # CRUDO | PROCESADO
    condicion = Column(String(20), default="DISPONIBLE")  # DISPONIBLE | RESTRINGIDO
    cantidad = Column(Float, default=0.0)
    actualizado_en = Column(DateTime, default=now, onupdate=now)

    proveedor = relationship("Proveedor")

    __table_args__ = (
        UniqueConstraint("proveedor_id", "articulo", "ubicacion", "estado",
                         "condicion", name="uq_saldo"),
    )


class MovimientoInventario(Base):
    __tablename__ = "movimientos_inventario"
    id = Column(Integer, primary_key=True)
    fecha = Column(DateTime, default=now, index=True)
    # ENTRADA|SALIDA|TRANSFORMACION_CONSUMO|TRANSFORMACION_PRODUCCION|
    # AJUSTE|DESPACHO|CONTEO|RECLASIFICACION
    tipo = Column(String(30), nullable=False, index=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    ubicacion = Column(String(80))
    estado = Column(String(20))
    condicion = Column(String(20))
    cantidad = Column(Float)                           # +entra / -sale
    saldo_resultante = Column(Float)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    referencia = Column(String(120))
    usuario = Column(String(160))

    documento = relationship("Documento")


# =========================================================================
# CONTEOS CÍCLICOS
# =========================================================================

class ConteoProgramado(Base):
    __tablename__ = "conteos_programados"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    ubicacion = Column(String(80))
    fecha_programada = Column(Date, index=True)
    prioridad = Column(Integer, default=3)             # 1 alta .. 5 baja
    # PROGRAMADO | EJECUTADO | VENCIDO | CANCELADO
    estado = Column(String(20), default="PROGRAMADO", index=True)
    creado_por = Column(String(160))
    creado_en = Column(DateTime, default=now)

    proveedor = relationship("Proveedor")
    documento = relationship("Documento")


class ConteoEjecutado(Base):
    __tablename__ = "conteos_ejecutados"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    programado_id = Column(Integer, ForeignKey("conteos_programados.id"))
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    ubicacion = Column(String(80))
    cantidad_sistema = Column(Float, default=0.0)
    cantidad_fisica = Column(Float, default=0.0)
    diferencia = Column(Float, default=0.0)
    es_aleatorio = Column(Boolean, default=False)
    genera_cola = Column(Boolean, default=False)
    # REGISTRADO | EN_COLA_INVENTARIOS | ANALIZADO | AJUSTADO
    estado = Column(String(30), default="REGISTRADO", index=True)
    fecha = Column(DateTime, default=now, index=True)
    ejecutado_por = Column(String(160))
    observaciones = Column(Text)

    programado = relationship("ConteoProgramado")
    proveedor = relationship("Proveedor")
    documento = relationship("Documento")


class FotoInventarioERP(Base):
    """Foto del inventario del ERP para comparar contra la app y priorizar conteos."""
    __tablename__ = "foto_inventario_erp"
    id = Column(Integer, primary_key=True)
    corte = Column(Date, default=dt.date.today, index=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    ubicacion = Column(String(80))
    cantidad_erp = Column(Float, default=0.0)
    cargado_por = Column(String(160))
    cargado_en = Column(DateTime, default=now)


# =========================================================================
# AVERÍAS
# =========================================================================

class Averia(Base):
    __tablename__ = "averias"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    cantidad = Column(Float, default=0.0)
    # RECIBO | ALMACENAMIENTO | PRODUCCION
    momento = Column(String(30), default="PRODUCCION", index=True)
    # ORIGEN | MANIPULACION | PUESTA_A_PUNTO | AKT
    motivo = Column(String(30), nullable=False, index=True)
    evidencia_id = Column(Integer, ForeignKey("archivos.id"))
    estado_inventario = Column(String(20), default="CRUDO")
    # REGISTRADA | EN_COLA_INVENTARIOS | AJUSTADA
    estado = Column(String(30), default="REGISTRADA", index=True)
    documento_ajuste = Column(String(20))
    fecha = Column(DateTime, default=now, index=True)
    registrado_por = Column(String(160))
    observaciones = Column(Text)

    proveedor = relationship("Proveedor")
    evidencia = relationship("Archivo")
    documento = relationship("Documento")


# =========================================================================
# PRODUCCIÓN (MPS) Y SUBCONTRATACIÓN
# =========================================================================

class ProgramaProduccion(Base):
    """MPS: planeación sugiere qué transformar; valida existencias."""
    __tablename__ = "mps"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False, index=True)
    articulo = Column(String(60), nullable=False, index=True)   # transformado
    cantidad_programada = Column(Float, default=0.0)
    cantidad_ejecutada = Column(Float, default=0.0)
    fecha_programada = Column(Date, index=True)
    # PROGRAMADO | EN_PROCESO | EJECUTADO | CERRADO | CANCELADO
    estado = Column(String(20), default="PROGRAMADO", index=True)
    ubicacion_destino = Column(String(80))
    creado_por = Column(String(160))
    creado_en = Column(DateTime, default=now)
    observaciones = Column(Text)

    proveedor = relationship("Proveedor")
    documento = relationship("Documento")

    @property
    def pendiente(self):
        return (self.cantidad_programada or 0) - (self.cantidad_ejecutada or 0)

    @property
    def cumplimiento(self):
        if not self.cantidad_programada:
            return 0.0
        return 100.0 * (self.cantidad_ejecutada or 0) / self.cantidad_programada


class OrdenProduccion(Base):
    """Ejecución de la transformación: consume componentes, produce transformado."""
    __tablename__ = "ordenes_produccion"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"))
    mps_id = Column(Integer, ForeignKey("mps.id"), index=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    cantidad = Column(Float, default=0.0)
    ubicacion_destino = Column(String(80))
    estado = Column(String(20), default="EJECUTADA")
    fecha = Column(DateTime, default=now, index=True)
    ejecutado_por = Column(String(160))

    mps = relationship("ProgramaProduccion")
    consumos = relationship("ConsumoProduccion", back_populates="orden",
                            cascade="all, delete-orphan")
    documento = relationship("Documento")


class ConsumoProduccion(Base):
    __tablename__ = "consumos_produccion"
    id = Column(Integer, primary_key=True)
    orden_id = Column(Integer, ForeignKey("ordenes_produccion.id"), nullable=False)
    componente = Column(String(60), index=True)
    cantidad = Column(Float, default=0.0)
    ubicacion = Column(String(80))

    orden = relationship("OrdenProduccion", back_populates="consumos")


# =========================================================================
# DESPACHO
# =========================================================================

class Despacho(Base):
    __tablename__ = "despachos"
    id = Column(Integer, primary_key=True)
    documento_id = Column(Integer, ForeignKey("documentos.id"), nullable=False)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), nullable=False, index=True)
    lote = Column(String(60), index=True)              # plan de ensamble / lote
    plan_ensamble = Column(String(120))
    # BORRADOR | DESPACHADO | ANULADO
    estado = Column(String(20), default="BORRADOR", index=True)
    ubicacion_origen = Column(String(80))
    creado_por = Column(String(160))
    creado_en = Column(DateTime, default=now, index=True)
    despachado_en = Column(DateTime)

    documento = relationship("Documento")
    proveedor = relationship("Proveedor")
    lineas = relationship("DespachoLinea", back_populates="despacho",
                          cascade="all, delete-orphan")


class DespachoLinea(Base):
    __tablename__ = "despacho_lineas"
    id = Column(Integer, primary_key=True)
    despacho_id = Column(Integer, ForeignKey("despachos.id"), nullable=False, index=True)
    articulo = Column(String(60), nullable=False, index=True)
    descripcion = Column(String(300))
    cantidad = Column(Float, default=0.0)
    ubicacion = Column(String(80))

    despacho = relationship("Despacho", back_populates="lineas")


# =========================================================================
# DDMRP (FASE 2)
# =========================================================================

class DdmrpParametro(Base):
    __tablename__ = "ddmrp_parametros"
    id = Column(Integer, primary_key=True)
    proveedor_id = Column(Integer, ForeignKey("proveedores.id"), index=True)
    articulo = Column(String(60), index=True)
    consumo_promedio_dia = Column(Float, default=0.0)
    lead_time_dias = Column(Integer, default=7)
    factor_variabilidad = Column(Float, default=0.5)
    factor_lead_time = Column(Float, default=0.5)
    moq = Column(Float, default=0.0)
    zona_roja = Column(Float, default=0.0)
    zona_amarilla = Column(Float, default=0.0)
    zona_verde = Column(Float, default=0.0)
    activo = Column(Boolean, default=True)
    actualizado_en = Column(DateTime, default=now, onupdate=now)

    __table_args__ = (
        UniqueConstraint("proveedor_id", "articulo", name="uq_ddmrp"),
    )


class Auditoria(Base):
    __tablename__ = "auditoria"
    id = Column(Integer, primary_key=True)
    fecha = Column(DateTime, default=now, index=True)
    usuario = Column(String(160))
    rol = Column(String(30))
    accion = Column(String(80))
    entidad = Column(String(60))
    entidad_id = Column(String(60))
    detalle = Column(Text)


Index("ix_inv_busqueda", Inventario.proveedor_id, Inventario.articulo,
      Inventario.estado, Inventario.condicion)
