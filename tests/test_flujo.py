"""Pruebas end-to-end de integridad del flujo operativo."""
import datetime as dt
import io
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))

from core.db import init_db, session_scope  # noqa: E402
from core.document_ai import (analizar_documento, completar_con_catalogo,
                              estructurar, inferir_origen, lineas_desde_catalogo,
                              lineas_bin_desde_texto, _texto_rapid_ordenado,
                              _extraer_bin_columnas_resultado)  # noqa: E402
from core.models import (Articulo, Averia, Bom, Inventario, MovimientoInventario,
                         Novedad, OrdenCompra, ProgramaProduccion, Proveedor,
                         Recibo, Ubicacion)  # noqa: E402
import core.services as sv  # noqa: E402
from core.auth import puede  # noqa: E402

OK, FAIL = [], []


def check(nombre, cond, extra=""):
    (OK if cond else FAIL).append(nombre)
    print(("  ✓ " if cond else "  ✗ ") + nombre + (f" [{extra}]" if extra else ""))


def esperar_error(nombre, fn, fragmento=""):
    try:
        fn()
        check(nombre, False, "no lanzó error")
    except sv.ReglaNegocio as e:
        ok = fragmento.lower() in str(e).lower() if fragmento else True
        check(nombre, ok, str(e)[:120])


print("\n=== 0A. PERMISOS INVENTARIOS ===")
inv_user = {"rol": "INVENTARIOS"}
for permiso in (
    "recibo_registrar", "recibo_sellar", "recibo_adjuntar_bin", "recibo_match_oc",
    "novedades_registrar", "novedades_ajustar", "conteos_programar",
    "conteos_ejecutar", "averias_registrar", "averias_ajustar",
    "produccion_programar", "produccion_ejecutar", "despacho_registrar",
    "maestros", "maestro_articulos", "maestro_bom", "maestro_ubicaciones",
    "maestro_proveedores", "maestro_oc", "maestro_usuarios", "ddmrp",
    "panel_control", "permiso_futuro_que_no_existe_aun",
):
    check(f"Inventarios permite {permiso}", puede(inv_user, permiso))

check("Proveedor no hereda comodín",
      not puede({"rol": "PROVEEDOR"}, "maestro_usuarios"))

print("\n=== PREPARACIÓN ===")
init_db()
with session_scope() as s:
    p = Proveedor(codigo="VDR0013714", nombre="Transformador Principal",
                  tolerancia_averia_pct=1.0,
                  ubicacion_origen="UB-ORIGEN",
                  ubicacion_destino="UB-PROV-01", activo=True)
    origen = Proveedor(codigo="VDRORIGEN", nombre="Proveedor Origen", activo=True)
    alt = Proveedor(codigo="VDRALT", nombre="Transformador Alterno", activo=True)
    s.add_all([p, origen, alt]); s.flush()
    PID, ORIGEN_ID, ALT_ID = p.id, origen.id, alt.id

    for cod, pid, cerrada, restringida in (
        ("UB-ORIGEN", PID, False, False),
        ("UB-PROV-01", PID, False, False),
        ("UB-PROC-01", PID, False, False),
        ("UB-CERRADA", PID, True, False),
        ("UB-RESTRINGIDA", PID, False, True),
    ):
        s.add(Ubicacion(codigo=cod, proveedor_id=pid, activo=True,
                        cerrada=cerrada, restringida=restringida))

    for cod, desc, tipo in (
        ("TR-100", "Carenaje Pintado", "TRANSFORMADO"),
        ("CP-A", "Carenaje Crudo", "CRUDO"),
        ("CP-B", "Calca", "CRUDO"),
        ("CP-C", "Soporte", "CRUDO"),
    ):
        s.add(Articulo(codigo=cod, descripcion=desc, tipo=tipo))

    s.add_all([
        Bom(articulo_transformado="TR-100", componente="CP-A", cantidad=2,
            secuencia=10, proveedor_codigo="VDR0013714", activo=True),
        Bom(articulo_transformado="TR-100", componente="CP-B", cantidad=1,
            secuencia=20, proveedor_codigo="VDR0013714", activo=True),
        Bom(articulo_transformado="TR-100", componente="CP-A", cantidad=99,
            secuencia=10, proveedor_codigo="VDRALT", activo=True),
    ])

    for numero, art, qty in (
        ("OC-A-100", "CP-A", 100),
        ("OC-B-60", "CP-B", 60),
        ("OC-C-10", "CP-C", 10),
        ("OC-B-10", "CP-B", 10),
    ):
        s.add(OrdenCompra(numero=numero, proveedor_id=PID, articulo=art,
                          cantidad=qty, estado="ABIERTA", fecha=dt.date.today()))
    s.flush()
    OCS = {o.numero: o.id for o in s.query(OrdenCompra).all()}

print("\n=== 0B. REGLAS DESDE / HASTA DEL BIN ===")
with session_scope() as s:
    esperar_error(
        "HASTA distinto al maestro bloquea el BIN",
        lambda: sv.crear_recibo(
            s, proveedor_id=PID, origen="BIN_A_BIN", usuario="test",
            ubicacion_destino="UB-PROV-01",
            lineas=[sv.LineaRecibo(
                "CP-A", "Carenaje", 1, 1,
                ubicacion_desde="UB-ORIGEN",
                ubicacion_hasta="UB-PROC-01")]),
        "HASTA")

with session_scope() as s:
    r_alerta = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN",
        referencia="BIN-DESDE-ALERTA", usuario="test",
        ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo(
            "CP-A", "Carenaje", 1, 1,
            ubicacion_desde="UB-PROC-01",
            ubicacion_hasta="UB-PROV-01")])
    alertas = sv.validar_bin_a_bin(s, r_alerta, PID)
    check("DESDE distinto genera alerta pero no bloquea",
          bool(alertas) and "DESDE" in alertas[0], str(alertas))
    check("HASTA correcto permite crear el BIN",
          r_alerta.estado == "PENDIENTE_MATCH")

print("\n=== 0C. CANTIDAD FÍSICA CERO ES VÁLIDA ===")
with session_scope() as s:
    r0 = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-CERO",
        usuario="test", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo(
            "CP-C", "Soporte", cantidad_documento=10, cantidad_fisica=0,
            ubicacion_desde="UB-ORIGEN",
            ubicacion_hasta="UB-PROV-01")])
    check("Cantidad física cero no se reemplaza por documento",
          float(r0.lineas[0].cantidad_fisica) == 0.0)

print("\n=== 1. BIN -> MATCH -> INVENTARIO ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-100",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-A", "Carenaje Crudo", 100, 100,
                               ubicacion_desde="UB-ORIGEN",
                               ubicacion_hasta="UB-PROV-01")])
    RB1 = r.id
    check("BIN nace PENDIENTE_MATCH", r.estado == "PENDIENTE_MATCH")
    check("BIN no afecta inventario antes del match",
          sv.saldo_articulo(s, PID, "CP-A") == 0)

with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=RB1, orden_compra_id=OCS["OC-A-100"],
        usuario="recibo@akt.com")
    check("Match exacto cierra recibo", res["estado"] == "CERRADA")
    check("Match exacto ingresa 100 disponible",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 100)

print("\n=== 2. FACTURA MULTILÍNEA / MÚLTIPLES OC ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, proveedor_origen_id=ORIGEN_ID,
        factura_origen="FV-9001", origen="FACTURA", referencia="FV-9001",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[
            sv.LineaRecibo("CP-B", "Calca", 60, 55, ubicacion_hasta="UB-PROV-01"),
            sv.LineaRecibo("CP-C", "Soporte", 10, 10, ubicacion_hasta="UB-PROV-01"),
        ])
    RB2 = r.id
    check("Proveedor origen queda separado del transformador",
          r.proveedor_origen_id == ORIGEN_ID and r.proveedor_id == PID)
    sv.sellar_recibo(s, RB2, "proveedor@akt.com")
    check("Factura sellada pasa a PENDIENTE_MATCH",
          s.get(Recibo, RB2).estado == "PENDIENTE_MATCH")
    lineas = {l.articulo: l.id for l in r.lineas}

with session_scope() as s:
    res = sv.match_recibo_lineas(
        s, recibo_id=RB2,
        asignaciones={
            lineas["CP-B"]: OCS["OC-B-60"],
            lineas["CP-C"]: OCS["OC-C-10"],
        },
        usuario="recibo@akt.com")
    check("Un recibo soporta varias OC", len({x["oc"] for x in res["lineas"]}) == 2)
    check("Faltante deja recibo en NOVEDAD", res["estado"] == "NOVEDAD")
    nov_b = s.query(Novedad).filter_by(recibo_id=RB2, articulo="CP-B").one()
    NOV_FALT = nov_b.id
    check("Faltante automático = 5", nov_b.tipo == "FALTANTE" and nov_b.cantidad == 5)
    check("Solo ingresa lo físico recibido (55)",
          sv.saldo_articulo(s, PID, "CP-B") == 55)
    check("Línea exacta CP-C ingresa 10",
          sv.saldo_articulo(s, PID, "CP-C") == 10)

with session_scope() as s:
    sv.ajustar_novedad(s, novedad_id=NOV_FALT, documento_ajuste="TD90",
                       numero_ajuste="AJ-FALT-1", usuario="inventarios@akt.com")
    check("Cerrar faltante NO descuenta inventario otra vez",
          sv.saldo_articulo(s, PID, "CP-B") == 55)

print("\n=== 3. SOBRANTE SEGREGADO ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-SOB",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-B", "Calca", 10, 13,
                               ubicacion_desde="UB-ORIGEN",
                               ubicacion_hasta="UB-PROV-01")])
    RB3, LIN3 = r.id, r.lineas[0].id

with session_scope() as s:
    res = sv.match_recibo_lineas(
        s, recibo_id=RB3, asignaciones={LIN3: OCS["OC-B-10"]},
        usuario="recibo@akt.com")
    nov = s.query(Novedad).filter_by(recibo_id=RB3, tipo="SOBRANTE").one()
    NOV_SOB = nov.id
    check("Sobrante de 3 genera novedad", nov.cantidad == 3)
    check("Solo 10 pasan a DISPONIBLE", sv.saldo_articulo(s, PID, "CP-B") == 65)
    check("3 quedan RESTRINGIDAS",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "RESTRINGIDO") == 3)

with session_scope() as s:
    sv.ajustar_novedad(s, novedad_id=NOV_SOB, documento_ajuste="TD96",
                       numero_ajuste="AJ-SOB-1", usuario="inventarios@akt.com")
    check("Aprobación del sobrante lo libera a DISPONIBLE",
          sv.saldo_articulo(s, PID, "CP-B") == 68)
    check("Ya no queda sobrante restringido",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "RESTRINGIDO") == 0)

print("\n=== 4. REGISTRO MANUAL ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="REGISTRO", usuario="proveedor@akt.com",
        ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-B", "Calca", 30, 30,
                               ubicacion_hasta="UB-PROV-01")])
    sv.confirmar_recibo_simple(s, r.id, "proveedor@akt.com")
    check("Registro manual suma 30 (68 -> 98)",
          sv.saldo_articulo(s, PID, "CP-B") == 98)

print("\n=== 5. UBICACIONES ===")
with session_scope() as s:
    esperar_error(
        "Ubicación cerrada rechaza movimientos",
        lambda: sv.mover_inventario(
            s, proveedor_id=PID, articulo="CP-A", cantidad=1, tipo="ENTRADA",
            ubicacion="UB-CERRADA"),
        "cerrada")
    esperar_error(
        "Ubicación restringida no acepta DISPONIBLE",
        lambda: sv.mover_inventario(
            s, proveedor_id=PID, articulo="CP-A", cantidad=1, tipo="ENTRADA",
            ubicacion="UB-RESTRINGIDA", condicion="DISPONIBLE"),
        "restringida")

print("\n=== 6. CONTEOS Y AVERÍAS ===")
with session_scope() as s:
    cp = sv.programar_conteo(
        s, proveedor_id=PID, articulo="CP-A", ubicacion="UB-PROV-01",
        prioridad=1, usuario="inventarios@akt.com")
    ce = sv.ejecutar_conteo(
        s, proveedor_id=PID, articulo="CP-A", cantidad_fisica=98,
        ubicacion="UB-PROV-01", programado_id=cp.id, usuario="proveedor@akt.com")
    CE_ID = ce.id
    check("Conteo detecta diferencia -2", ce.diferencia == -2)

with session_scope() as s:
    sv.ajustar_conteo(s, conteo_id=CE_ID, documento_ajuste="TD96",
                      numero_ajuste="AJ-CNT", usuario="inventarios@akt.com")
    check("Conteo ajusta saldo A a 98", sv.saldo_articulo(s, PID, "CP-A") == 98)

with session_scope() as s:
    ev = sv.guardar_archivo(s, "destruccion.jpg", b"FOTO-EVIDENCIA",
                            "image/jpeg", "proveedor@akt.com")
    check("Archivo guarda SHA-256", bool(ev.sha256) and len(ev.sha256) == 64)
    av = sv.registrar_averia(
        s, proveedor_id=PID, articulo="CP-A", cantidad=3,
        motivo="MANIPULACION", momento="ALMACENAMIENTO",
        evidencia_id=ev.id, usuario="proveedor@akt.com")
    check("Avería pasa 3 a RESTRINGIDO",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "RESTRINGIDO") == 3)
    check("Disponible A queda 95", sv.saldo_articulo(s, PID, "CP-A") == 95)
    AV_ID = av.id

print("\n=== 7. BOM POR PROVEEDOR / PRODUCCIÓN ===")
with session_scope() as s:
    bom = sv.explosion_bom(s, "TR-100", PID)
    check("BOM del proveedor no mezcla BOM alterno",
          len(bom) == 2 and {b.cantidad for b in bom} == {1, 2})
    maximo, _ = sv.maximo_producible(s, PID, "TR-100")
    check("Máximo producible = 47", maximo == 47, f"max={maximo}")
    esperar_error(
        "No permite programar 48 > 47",
        lambda: sv.programar_mps(
            s, proveedor_id=PID, articulo="TR-100", cantidad=48,
            usuario="planeacion@akt.com"),
        "máximo")

with session_scope() as s:
    m = sv.programar_mps(
        s, proveedor_id=PID, articulo="TR-100", cantidad=40,
        usuario="planeacion@akt.com", ubicacion_destino="UB-PROC-01")
    MPS_ID = m.id
    check("MPS creado", m.estado == "PROGRAMADO")

with session_scope() as s:
    maximo2, _ = sv.maximo_producible(s, PID, "TR-100")
    check("MPS reserva componentes; libre baja a 7", maximo2 == 7, f"max={maximo2}")

with session_scope() as s:
    sv.ejecutar_produccion(
        s, mps_id=MPS_ID, cantidad=25, usuario="proveedor@akt.com",
        ubicacion_origen="UB-PROV-01", ubicacion_destino="UB-PROC-01")
    check("Producción consume 50 A (95 -> 45)", sv.saldo_articulo(s, PID, "CP-A") == 45)
    check("Producción consume 25 B (98 -> 73)", sv.saldo_articulo(s, PID, "CP-B") == 73)
    check("Produce 25 TR-100", sv.saldo_articulo(
        s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 25)

with session_scope() as s:
    ev = sv.guardar_archivo(s, "dest2.jpg", b"FOTO2", "image/jpeg", "prov")
    sv.registrar_averia(
        s, proveedor_id=PID, articulo="TR-100", cantidad=2,
        motivo="PUESTA_A_PUNTO", momento="PRODUCCION",
        estado_inventario="PROCESADO", evidencia_id=ev.id,
        usuario="proveedor@akt.com")
    check("Avería procesada deja 23 disponibles",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 23)

print("\n=== 8. DESPACHO ===")
with session_scope() as s:
    d = sv.crear_despacho(
        s, proveedor_id=PID, lote="LOTE-1", plan_ensamble="PLAN-1",
        usuario="proveedor@akt.com", ubicacion_origen="UB-PROC-01",
        lineas=[{"articulo": "TR-100", "cantidad": 20, "ubicacion": "UB-PROC-01"}])
    DSP_ID = d.id

with session_scope() as s:
    sv.confirmar_despacho(s, despacho_id=DSP_ID, usuario="proveedor@akt.com")
    check("Despacho deja 3 procesadas",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 3)

print("\n=== 9. TRAZABILIDAD / CONCURRENCIA LÓGICA ===")
with session_scope() as s:
    docs = [sv.crear_documento(s, "REGISTRO", proveedor_id=PID, creado_por="test")
            for _ in range(20)]
    trz = [d.trz for d in docs]
    check("TRZ no se repite en lote", len(set(trz)) == len(trz))

print("\n=== 10. EXTRACCIÓN DOCUMENTAL ===")
sample = """
FACTURA FV-3-8619
NIT 900123456-7
ORDEN DE COMPRA OC-2026-0001
FECHA 17/09/2026
CP-A Carenaje Crudo 100
CP-B Calca 55
"""
ext = estructurar(sample, 0.99)
check("Extractor identifica referencia", ext["referencia"]["valor"] is not None)
check("Extractor identifica OC", ext["orden_compra"]["valor"] == "OC-2026-0001")
check("Extractor propone líneas", len(ext["lineas"]) >= 2)
check("Extractor infiere FACTURA", inferir_origen(sample) == "FACTURA")

sample_catalogo = """
FACTURA FV-777
FECHA 17/09/2026
UBICACION DESTINO UB-PROV-01
GARANTIA
CP-A Carenaje Crudo CANTIDAD 12
"""
ext_cat = estructurar(sample_catalogo, 0.97)
ext_cat["confianza_texto"] = 0.97
ext_cat = completar_con_catalogo(
    ext_cat, {"CP-A": "Carenaje Crudo", "CP-B": "Calca"})
check("Maestro confirma artículo OCR",
      any(x["articulo"] == "CP-A" for x in ext_cat["lineas"]))
check("Maestro conserva/proponen cantidad 12",
      any(x["articulo"] == "CP-A" and float(x["cantidad_documento"]) == 12
          for x in ext_cat["lineas"]))
check("Extractor detecta ubicación destino",
      ext_cat["ubicacion_destino"]["valor"] == "UB-PROV-01")
check("Extractor detecta reproceso/garantía",
      ext_cat["es_reproceso_sugerido"] is True)

# Simula RapidOCR detectando celdas separadas en la misma fila.
fake_rapid = SimpleNamespace(
    txts=[
        "7700149386142", "Carenaje Farola 200DS+ Mp", "12",
        "7700149386173", "Cubierta Tras 200DS+ Mp", "8",
        "7700149385725", "Cubta Der Tanq Gas 200DS+ Mp", "5",
    ],
    scores=[0.99] * 9,
    boxes=[
        [[10, 10], [210, 10], [210, 40], [10, 40]],
        [[260, 10], [760, 10], [760, 40], [260, 40]],
        [[900, 10], [960, 10], [960, 40], [900, 40]],
        [[10, 70], [210, 70], [210, 100], [10, 100]],
        [[260, 70], [760, 70], [760, 100], [260, 100]],
        [[900, 70], [960, 70], [960, 100], [900, 100]],
        [[10, 130], [210, 130], [210, 160], [10, 160]],
        [[260, 130], [760, 130], [760, 160], [260, 160]],
        [[900, 130], [960, 130], [960, 160], [900, 160]],
    ],
)
texto_tabla, _ = _texto_rapid_ordenado(fake_rapid)
check("RapidOCR recompone tres filas de tabla",
      len(texto_tabla.splitlines()) == 3, texto_tabla)

catalogo_tabla = {
    "7700149386142": "Carenaje Farola 200DS+ Mp",
    "7700149386173": "Cubierta Tras 200DS+ Mp",
    "7700149385725": "Cubta Der Tanq Gas 200DS+ Mp",
}
lineas_tabla = lineas_desde_catalogo(texto_tabla, catalogo_tabla, 0.99)
cantidades = {x["articulo"]: x["cantidad_documento"] for x in lineas_tabla}
check("Tabla recupera todas las referencias", len(cantidades) == 3, str(cantidades))
check("Tabla asocia cantidad 12 y no confunde 200DS",
      cantidades.get("7700149386142") == 12.0, str(cantidades))
check("Tabla asocia cantidades 8 y 5",
      cantidades.get("7700149386173") == 8.0
      and cantidades.get("7700149385725") == 5.0, str(cantidades))
check("OCR no presume cantidad física",
      all(float(x["cantidad_fisica"]) == 0 for x in lineas_tabla))

# Tabla BIN completa: valida geometría de Código/Cantidad/DESDE/HASTA.
def _box(cx, cy, w=100, h=24):
    return [
        [cx-w/2, cy-h/2], [cx+w/2, cy-h/2],
        [cx+w/2, cy+h/2], [cx-w/2, cy+h/2],
    ]

headers_txt = [
    "Proveedor", "Código", "Descripción", "Cantidad",
    "Serial", "Lote", "Desde", "Hasta",
]
xs = [70, 210, 420, 650, 760, 850, 1010, 1260]
row_txt = [
    "CHONGQING-012", "7700149386142", "Carenaje Farola 200DS+ Mp",
    "179", "NONE", "NONE", "WSERE-PSER",
    "MOTOS-WSERE-WSER-VIPI-NTAR",
]
fake_bin = SimpleNamespace(
    txts=headers_txt + row_txt,
    scores=[0.99] * 16,
    boxes=[_box(x, 30) for x in xs] + [_box(x, 80) for x in xs],
)
filas_bin_geom = _extraer_bin_columnas_resultado(fake_bin)
check("Parser geométrico BIN obtiene una fila", len(filas_bin_geom) == 1,
      str(filas_bin_geom))
if filas_bin_geom:
    fg = filas_bin_geom[0]
    check("Parser geométrico conserva cantidad 179",
          fg["cantidad_documento"] == 179.0, str(fg))
    check("Parser geométrico conserva DESDE",
          fg["ubicacion_desde"] == "WSERE-PSER", str(fg))
    check("Parser geométrico conserva HASTA",
          fg["ubicacion_hasta"] == "MOTOS-WSERE-WSER-VIPI-NTAR", str(fg))

sample_bin_real = """
Proveedor Código Descripción Cantidad Serial Lote Desde Hasta
CHONGQING-012 7700149386142 Carenaje Farola 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149386173 Cubierta Tras 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149385725 Cubta Der Tanq Gas 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149385718 Cubta Izq Tanq Gas 200DS+ Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
CHONGQING-012 7700149386081 Guardabarro Del Frontal Mp 179 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
SANYANG IN-001 7700149603447 Cubierta manubrio JetEvo Mp 60 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
SANYANG IN-001 7700149603980 Cubta Frontal Der JetEvo Mp 60 NONE NONE WSERE-PSER MOTOS-WSERE-WSER-VIPI-NTAR
"""
catalogo_bin = {
    "7700149386142": "Carenaje Farola 200DS+ Mp",
    "7700149386173": "Cubierta Tras 200DS+ Mp",
    "7700149385725": "Cubta Der Tanq Gas 200DS+ Mp",
    "7700149385718": "Cubta Izq Tanq Gas 200DS+ Mp",
    "7700149386081": "Guardabarro Del Frontal Mp",
    "7700149603447": "Cubierta manubrio JetEvo Mp",
    "7700149603980": "Cubta Frontal Der JetEvo Mp",
}
bin_lines = lineas_bin_desde_texto(sample_bin_real, catalogo_bin, 0.99)
bin_qty = {x["articulo"]: x["cantidad_documento"] for x in bin_lines}
check("BIN real recupera todas las referencias de muestra",
      len(bin_qty) == len(catalogo_bin), str(bin_qty))
check("BIN real conserva 179 en grupo CHONGQING",
      all(bin_qty.get(k) == 179.0 for k in list(catalogo_bin)[:5]), str(bin_qty))
check("BIN real conserva 60 en grupo SANYANG",
      bin_qty.get("7700149603447") == 60.0
      and bin_qty.get("7700149603980") == 60.0, str(bin_qty))

# Una lectura heurística errónea (1) debe ser corregida por el parser BIN (179).
resultado_malo = {
    "texto": sample_bin_real,
    "confianza_texto": 0.99,
    "lineas": [{
        "articulo": "7700149386173",
        "descripcion": "Cubierta Tras 200DS+ Mp",
        "cantidad_documento": 1.0,
        "cantidad_fisica": 0.0,
        "fuente": "OCR_HEURISTICO",
    }],
}
corregido = completar_con_catalogo(resultado_malo, catalogo_bin)
q_corregida = next(
    x["cantidad_documento"] for x in corregido["lineas"]
    if x["articulo"] == "7700149386173")
check("Parser BIN corrige cantidad heurística 1 -> 179", q_corregida == 179.0)

resultado_espacial = {
    "texto": sample_bin_real,
    "confianza_texto": 0.99,
    "lineas": [],
    "bin_filas_espaciales": [{
        "articulo": "7700149386142",
        "descripcion_ocr": "Carenaje Farola 200DS+ Mp",
        "cantidad_documento": 179.0,
        "cantidad_fisica": 0.0,
        "serial": "NONE",
        "lote": "NONE",
        "ubicacion_desde": "WSERE PSER 1 1 1",
        "ubicacion_hasta": "WSERE WSER VIPI NTAR TE",
        "fuente": "BIN_ESPACIAL",
    }],
}
resultado_espacial = completar_con_catalogo(resultado_espacial, catalogo_bin)
esp = next(x for x in resultado_espacial["lineas"]
           if x["articulo"] == "7700149386142")
check("BIN espacial conserva DESDE",
      esp["ubicacion_desde"] == "WSERE PSER 1 1 1", str(esp))
check("BIN espacial conserva HASTA",
      esp["ubicacion_hasta"] == "WSERE WSER VIPI NTAR TE", str(esp))
check("BIN espacial conserva cantidad",
      esp["cantidad_documento"] == 179.0, str(esp))

print("\n=== 10B. OCR REAL SOBRE IMAGEN ===")
try:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1500, 620), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    except Exception:
        font = ImageFont.load_default()

    y = 35
    for linea in (
        "FACTURA FV-3-8619",
        "NIT 900123456-7",
        "ORDEN DE COMPRA OC-2026-0001",
        "FECHA 17/09/2026",
        "CP-A Carenaje Crudo 100",
        "CP-B Calca 55",
    ):
        draw.text((50, y), linea, fill="black", font=font)
        y += 88

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    ocr = analizar_documento("factura_prueba.png", bio.getvalue(), "image/png")
    texto_ocr = (ocr.get("texto") or "").upper()
    check("OCR real devuelve texto", bool(ocr.get("ocr_ok")), ocr.get("diagnostico", ""))
    check("OCR real reconoce FACTURA", "FACTURA" in texto_ocr, texto_ocr[:120])
    check("OCR real reconoce artículo CP-A",
          ("CP-A" in texto_ocr) or ("CP A" in texto_ocr), texto_ocr[:200])
except Exception as e:
    check("OCR real ejecuta sin excepción", False, f"{type(e).__name__}: {e}")

print("\n=== 11. KARDEX ===")
with session_scope() as s:
    malas = 0
    from sqlalchemy import func as F
    for inv in s.query(Inventario).all():
        suma = s.query(F.coalesce(F.sum(MovimientoInventario.cantidad), 0.0)).filter(
            MovimientoInventario.proveedor_id == inv.proveedor_id,
            MovimientoInventario.articulo == inv.articulo,
            MovimientoInventario.ubicacion == inv.ubicacion,
            MovimientoInventario.estado == inv.estado,
            MovimientoInventario.condicion == inv.condicion).scalar()
        if abs(float(suma or 0) - float(inv.cantidad or 0)) > 1e-6:
            malas += 1
    check("Kardex cuadra con saldos", malas == 0, f"{malas} descuadres")

print("\n" + "=" * 70)
print(f"RESULTADO: {len(OK)} OK, {len(FAIL)} fallidas")
if FAIL:
    for x in FAIL:
        print("  FALLÓ:", x)
    sys.exit(1)
print("Flujo integral validado.")
