"""Pruebas end-to-end de integridad del flujo operativo."""
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))

from core.db import init_db, session_scope  # noqa: E402
from core.document_ai import estructurar  # noqa: E402
from core.models import (Articulo, Averia, Bom, Inventario, MovimientoInventario,
                         Novedad, OrdenCompra, ProgramaProduccion, Proveedor,
                         Recibo, Ubicacion)  # noqa: E402
import core.services as sv  # noqa: E402

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


print("\n=== PREPARACIÓN ===")
init_db()
with session_scope() as s:
    p = Proveedor(codigo="VDR0013714", nombre="Transformador Principal",
                  tolerancia_averia_pct=1.0, activo=True)
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
