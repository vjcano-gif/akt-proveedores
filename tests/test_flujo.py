"""Pruebas end-to-end de reglas críticas del negocio.

Ejecutar:
    python tests/test_flujo.py
"""
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")

from core.db import init_db, session_scope  # noqa: E402
from core.models import (  # noqa: E402
    Articulo, Averia, Bom, Documento, Novedad, OrdenCompra, Proveedor,
    Recibo, Ubicacion,
)
import core.services as sv  # noqa: E402

OK, FAIL = [], []


def check(nombre, cond, extra=""):
    (OK if cond else FAIL).append(nombre)
    print(("  ✓ " if cond else "  ✗ ") + nombre + (f"  [{extra}]" if extra else ""))


def esperar_error(nombre, fn, fragmento=""):
    try:
        fn()
        check(nombre, False, "no lanzó error")
    except sv.ReglaNegocio as exc:
        ok = fragmento.lower() in str(exc).lower() if fragmento else True
        check(nombre, ok, str(exc)[:120])


print("\n=== PREPARACIÓN ===")
init_db()
with session_scope() as s:
    p = Proveedor(codigo="VDR0013714", nombre="Grupo DL Plus S.A.S",
                  tolerancia_averia_pct=1.0, activo=True)
    p2 = Proveedor(codigo="VDR0099999", nombre="Transformador Dos",
                   tolerancia_averia_pct=1.0, activo=True)
    s.add_all([p, p2]); s.flush()
    PID, PID2 = p.id, p2.id

    for c, pid, restr in (
        ("UB-ORIGEN", PID, False), ("UB-PROV-01", PID, False),
        ("UB-PROC-01", PID, False), ("UB-RESTR", PID, True),
        ("UB-P2", PID2, False),
    ):
        s.add(Ubicacion(codigo=c, proveedor_id=pid, activo=True, restringida=restr))

    arts = [
        ("TR-100", "Carenaje Pintado CR4", "TRANSFORMADO"),
        ("CP-A", "Carenaje Crudo CR4", "CRUDO"),
        ("CP-B", "Calca 2D CR4", "CRUDO"),
        ("CP-C", "Componente C", "CRUDO"),
        ("CP-D", "Componente D", "CRUDO"),
        ("CP-E", "Componente E", "CRUDO"),
        ("CP-F", "Componente F", "CRUDO"),
        ("CP-X", "Componente X", "CRUDO"),
    ]
    for cod, desc, tipo in arts:
        s.add(Articulo(codigo=cod, descripcion=desc, tipo=tipo))

    s.add(Bom(articulo_transformado="TR-100", componente="CP-A", cantidad=2,
              secuencia=10, proveedor_codigo="VDR0013714", activo=True))
    s.add(Bom(articulo_transformado="TR-100", componente="CP-B", cantidad=1,
              secuencia=20, proveedor_codigo="VDR0013714", activo=True))
    s.add(Bom(articulo_transformado="TR-100", componente="CP-X", cantidad=3,
              secuencia=10, proveedor_codigo="VDR0099999", activo=True))

    for num, art, qty in (
        ("OC-001", "CP-A", 100),
        ("OC-002", "CP-B", 60),
        ("OC-003", "CP-C", 10),
        ("OC-004", "CP-D", 10),
        ("OC-005", "CP-E", 7),
        ("OC-006", "CP-F", 9),
    ):
        s.add(OrdenCompra(numero=num, proveedor_id=PID, articulo=art,
                          cantidad=qty, estado="ABIERTA", fecha=dt.date.today()))
    s.flush()
    OCS = {o.numero: o.id for o in s.query(OrdenCompra).all()}

ACT_PROV = {"rol": "PROVEEDOR", "proveedor_id": PID}
ACT_PROV2 = {"rol": "PROVEEDOR", "proveedor_id": PID2}
ACT_RECIBO = {"rol": "RECIBO_AKT", "proveedor_id": None}
ACT_INV = {"rol": "INVENTARIOS", "proveedor_id": None}
ACT_PLAN = {"rol": "PLANEACION", "proveedor_id": None}

print("\n=== 1. BIN NO IMPACTA INVENTARIO ANTES DEL MATCH ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN2686958",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        actor=ACT_PROV,
        lineas=[sv.LineaRecibo("CP-A", "Carenaje Crudo", 100, 100,
                               ubicacion_desde="UB-ORIGEN",
                               ubicacion_hasta="UB-PROV-01")])
    RB1 = r.id
    check("BIN queda PENDIENTE_MATCH", r.estado == "PENDIENTE_MATCH", r.estado)
    check("Antes del match el inventario sigue en cero",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 0)
    esperar_error(
        "No permite saltar el match confirmando BIN como registro simple",
        lambda: sv.confirmar_recibo_simple(s, r.id, "x", actor=ACT_PROV),
        "match")

with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=RB1, orden_compra_id=OCS["OC-001"],
        usuario="recibo@akt.com", actor=ACT_RECIBO)
    check("Match exacto cierra recibo", res["estado"] == "CERRADA", str(res))
    check("OC exacta queda cerrada", s.get(OrdenCompra, OCS["OC-001"]).estado == "CERRADA")
    check("Ingresa 100 físicos, una sola vez",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 100)

print("\n=== 2. FALTANTE DE RECEPCIÓN NO SE DESCUENTA DOS VECES ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="FACTURA", referencia="FV-3-8619",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        actor=ACT_PROV,
        lineas=[sv.LineaRecibo("CP-B", "Calca", 60, 55,
                               ubicacion_hasta="UB-PROV-01")])
    RB2 = r.id
    sv.sellar_recibo(s, RB2, "proveedor@akt.com", actor=ACT_PROV)

with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=RB2, orden_compra_id=OCS["OC-002"],
        usuario="recibo@akt.com", actor=ACT_RECIBO)
    NOV_FALT = res["novedad_id"]
    n = s.get(Novedad, NOV_FALT)
    check("Crea faltante de 5", n.tipo == "FALTANTE" and n.cantidad == 5)
    check("Marca origen RECEPCION", n.origen_novedad == "RECEPCION")
    check("Inventario físico queda en 55",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 55)
    oc = s.get(OrdenCompra, OCS["OC-002"])
    check("OC mantiene pendientes 5", oc.estado == "ABIERTA" and oc.pendiente == 5,
          f"estado={oc.estado} pend={oc.pendiente}")

with session_scope() as s:
    sv.ajustar_novedad(
        s, novedad_id=NOV_FALT, documento_ajuste="TD90",
        numero_ajuste="AJ-FALT-001", usuario="inventarios@akt.com", actor=ACT_INV)
    check("Cerrar faltante de recepción NO cambia 55 físicos",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 55)

print("\n=== 3. SOBRANTE QUEDA RESTRINGIDO HASTA DECISIÓN ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-SOB-001",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        actor=ACT_PROV,
        lineas=[sv.LineaRecibo("CP-C", "C", 10, 12, ubicacion_hasta="UB-PROV-01")])
    rid = r.id
with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=rid, orden_compra_id=OCS["OC-003"],
        usuario="recibo@akt.com", actor=ACT_RECIBO)
    NOV_SOB = res["novedad_id"]
    check("Solo 10 quedan disponibles",
          sv.saldo_articulo(s, PID, "CP-C", "CRUDO", "DISPONIBLE") == 10)
    check("2 quedan restringidas",
          sv.saldo_articulo(s, PID, "CP-C", "CRUDO", "RESTRINGIDO") == 2)

with session_scope() as s:
    sv.ajustar_novedad(
        s, novedad_id=NOV_SOB, documento_ajuste="TD96",
        numero_ajuste="AJ-SOB-001", usuario="inventarios@akt.com",
        accion_sobrante="LIBERAR", actor=ACT_INV)
    check("Aceptar sobrante libera 2 (total disponible 12)",
          sv.saldo_articulo(s, PID, "CP-C", "CRUDO", "DISPONIBLE") == 12)
    check("Ya no queda restringido",
          sv.saldo_articulo(s, PID, "CP-C", "CRUDO", "RESTRINGIDO") == 0)

print("\n=== 4. SOBRANTE PUEDE DEVOLVERSE ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-SOB-002",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        actor=ACT_PROV,
        lineas=[sv.LineaRecibo("CP-D", "D", 10, 13, ubicacion_hasta="UB-PROV-01")])
    rid = r.id
with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=rid, orden_compra_id=OCS["OC-004"],
        usuario="recibo@akt.com", actor=ACT_RECIBO)
    nov = res["novedad_id"]
with session_scope() as s:
    sv.ajustar_novedad(
        s, novedad_id=nov, documento_ajuste="TD96",
        numero_ajuste="AJ-DEV-001", usuario="inventarios@akt.com",
        accion_sobrante="DEVOLVER", actor=ACT_INV)
    check("Devolver sobrante conserva solo las 10 de OC",
          sv.saldo_articulo(s, PID, "CP-D", "CRUDO", "DISPONIBLE") == 10)
    check("Sobrante devuelto sale del restringido",
          sv.saldo_articulo(s, PID, "CP-D", "CRUDO", "RESTRINGIDO") == 0)

print("\n=== 5. UN RECIBO PUEDE CRUZAR VARIAS OC ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN-MULTI-001",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        actor=ACT_PROV,
        lineas=[
            sv.LineaRecibo("CP-E", "E", 7, 7, ubicacion_hasta="UB-PROV-01"),
            sv.LineaRecibo("CP-F", "F", 9, 9, ubicacion_hasta="UB-PROV-01"),
        ])
    rid = r.id
    ids = {ln.articulo: ln.id for ln in r.lineas}
with session_scope() as s:
    res = sv.adjuntar_bin_y_match(
        s, recibo_id=rid, usuario="recibo@akt.com",
        lineas_oc={ids["CP-E"]: OCS["OC-005"], ids["CP-F"]: OCS["OC-006"]},
        actor=ACT_RECIBO)
    check("Match de dos líneas/dos OC cierra recibo",
          res["estado"] == "CERRADA" and len(res["lineas"]) == 2)

print("\n=== 6. DOCUMENTOS DUPLICADOS SE BLOQUEAN ===")
with session_scope() as s:
    esperar_error(
        "Misma referencia/tipo/proveedor/fecha no se duplica",
        lambda: sv.crear_recibo(
            s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN2686958",
            usuario="x", ubicacion_destino="UB-PROV-01",
            lineas=[sv.LineaRecibo("CP-A", cantidad_documento=1, cantidad_fisica=1)]),
        "duplicado")

print("\n=== 7. UBICACIONES RESTRINGIDAS Y PERMISOS ===")
with session_scope() as s:
    esperar_error(
        "No permite entrada DISPONIBLE a ubicación restringida",
        lambda: sv.mover_inventario(
            s, proveedor_id=PID, articulo="CP-C", cantidad=1, tipo="ENTRADA",
            ubicacion="UB-RESTR", estado="CRUDO", condicion="DISPONIBLE"),
        "restringida")
    esperar_error(
        "Proveedor no opera inventario de otro proveedor",
        lambda: sv.crear_recibo(
            s, proveedor_id=PID2, origen="REGISTRO", referencia="R-P2",
            usuario="p1", actor=ACT_PROV,
            lineas=[sv.LineaRecibo("CP-X", cantidad_documento=1, cantidad_fisica=1)]),
        "otro proveedor")

print("\n=== 8. BOM ESPECÍFICO POR PROVEEDOR ===")
with session_scope() as s:
    bom1 = sv.explosion_bom(s, "TR-100", PID)
    bom2 = sv.explosion_bom(s, "TR-100", PID2)
    check("Proveedor 1 usa CP-A/CP-B",
          {b.componente for b in bom1} == {"CP-A", "CP-B"})
    check("Proveedor 2 usa solo CP-X",
          {b.componente for b in bom2} == {"CP-X"})
    esperar_error(
        "Proveedor no puede programar MPS (solo Planeación)",
        lambda: sv.programar_mps(
            s, proveedor_id=PID, articulo="TR-100", cantidad=1,
            usuario="prov", actor=ACT_PROV),
        "rol")

print("\n=== 9. CONTEO, AVERÍA, PRODUCCIÓN Y DESPACHO ===")
# Completa CP-B hasta 80 para producción.
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="REGISTRO", referencia="REG-CPB-25",
        usuario="inventarios@akt.com", ubicacion_destino="UB-PROV-01",
        actor=ACT_INV,
        lineas=[sv.LineaRecibo("CP-B", "Calca", 25, 25, ubicacion_hasta="UB-PROV-01")])
    sv.confirmar_recibo_simple(s, r.id, "inventarios@akt.com", actor=ACT_INV)
    check("CP-B total = 80", sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 80)

with session_scope() as s:
    cp = sv.programar_conteo(
        s, proveedor_id=PID, articulo="CP-A", ubicacion="UB-PROV-01",
        prioridad=1, usuario="inventarios@akt.com", actor=ACT_INV)
    ce = sv.ejecutar_conteo(
        s, proveedor_id=PID, articulo="CP-A", cantidad_fisica=98,
        ubicacion="UB-PROV-01", programado_id=cp.id,
        usuario="proveedor@akt.com", actor=ACT_PROV)
    CE_ID = ce.id
    check("Conteo -2 entra a cola", ce.estado == "EN_COLA_INVENTARIOS" and ce.diferencia == -2)
with session_scope() as s:
    sv.ajustar_conteo(
        s, conteo_id=CE_ID, documento_ajuste="TD96", numero_ajuste="AJ-CNT-1",
        usuario="inventarios@akt.com", actor=ACT_INV)
    check("Conteo ajusta CP-A a 98",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 98)

with session_scope() as s:
    ev = sv.guardar_archivo(s, "dest.jpg", b"FOTO", "image/jpeg", "prov")
    av = sv.registrar_averia(
        s, proveedor_id=PID, articulo="CP-A", cantidad=3,
        motivo="MANIPULACION", momento="ALMACENAMIENTO",
        evidencia_id=ev.id, usuario="proveedor@akt.com", actor=ACT_PROV)
    AV_ID = av.id
    check("Avería reclasifica 3 a restringido",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "RESTRINGIDO") == 3)
with session_scope() as s:
    sv.ajustar_averia(
        s, averia_id=AV_ID, documento_ajuste="TD90", numero_ajuste="AJ-AV-1",
        usuario="inventarios@akt.com", actor=ACT_INV)
    check("Avería ajustada elimina restringido sin saldo negativo",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "RESTRINGIDO") == 0)
    check("Disponible CP-A permanece 95",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 95)

with session_scope() as s:
    maximo, _ = sv.maximo_producible(s, PID, "TR-100")
    check("Máximo producible = 47", maximo == 47, f"{maximo}")
    m = sv.programar_mps(
        s, proveedor_id=PID, articulo="TR-100", cantidad=40,
        usuario="planeacion@akt.com", ubicacion_destino="UB-PROC-01", actor=ACT_PLAN)
    MPS = m.id

with session_scope() as s:
    sv.ejecutar_produccion(
        s, mps_id=MPS, cantidad=25, usuario="proveedor@akt.com",
        ubicacion_origen="UB-PROV-01", actor=ACT_PROV)
    check("Produce 25 transformados",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 25)

with session_scope() as s:
    d = sv.crear_despacho(
        s, proveedor_id=PID, lote="LOTE-1", plan_ensamble="PLAN-1",
        usuario="proveedor@akt.com", ubicacion_origen="UB-PROC-01",
        lineas=[{"articulo": "TR-100", "cantidad": 20, "ubicacion": "UB-PROC-01"}],
        actor=ACT_PROV)
    DSP = d.id
with session_scope() as s:
    sv.confirmar_despacho(
        s, despacho_id=DSP, usuario="proveedor@akt.com", actor=ACT_PROV)
    check("Despacho deja 5 procesados",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 5)

print("\n=== 10. TRAZABILIDAD ===")
with session_scope() as s:
    trzs = [x[0] for x in s.query(Documento.trz).all()]
    check("Todos los TRZ son únicos", len(trzs) == len(set(trzs)))
    check("Se generaron documentos trazables", len(trzs) >= 15, f"{len(trzs)}")

print("\n" + "=" * 72)
print(f"RESULTADO: {len(OK)} correctas · {len(FAIL)} fallidas")
if FAIL:
    print("FALLAS:")
    for x in FAIL:
        print(" -", x)
    raise SystemExit(1)
print("TODAS LAS PRUEBAS CRÍTICAS PASARON")
