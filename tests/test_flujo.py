"""
Prueba end-to-end del flujo: RECIBO -> ALMACENAMIENTO -> PRODUCCIÓN -> DESPACHO.
Valida saldos de inventario en cada paso.
Ejecutar:  python3 tests/test_flujo.py
"""
import os
import sys
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))

from core.db import init_db, session_scope                      # noqa: E402
from core.models import (Articulo, Bom, OrdenCompra, Proveedor,  # noqa: E402
                         Ubicacion, Novedad, Averia)
import core.services as sv                                      # noqa: E402

OK, FAIL = [], []


def check(nombre, cond, extra=""):
    (OK if cond else FAIL).append(nombre)
    print(("  ✓ " if cond else "  ✗ ") + nombre + (f"  [{extra}]" if extra else ""))


def esperar_error(nombre, fn, fragmento=""):
    try:
        fn()
        check(nombre, False, "no lanzó error")
    except sv.ReglaNegocio as e:
        ok = fragmento.lower() in str(e).lower() if fragmento else True
        check(nombre, ok, str(e)[:90])


print("\n=== PREPARACIÓN ===")
init_db()
with session_scope() as s:
    p = Proveedor(codigo="VDR0013714", nombre="Grupo DL Plus S.A.S",
                  tolerancia_averia_pct=1.0, activo=True)
    s.add(p); s.flush(); PID = p.id
    for c in ("UB-ORIGEN", "UB-PROV-01", "UB-PROC-01"):
        s.add(Ubicacion(codigo=c, proveedor_id=PID, activo=True))
    # Artículo transformado TR-100 = 2 x CP-A + 1 x CP-B
    for cod, desc, tipo in (("TR-100", "Carenaje Pintado CR4", "TRANSFORMADO"),
                            ("CP-A", "Carenaje Crudo CR4", "CRUDO"),
                            ("CP-B", "Calca 2D CR4", "CRUDO")):
        s.add(Articulo(codigo=cod, descripcion=desc, tipo=tipo))
    s.add(Bom(articulo_transformado="TR-100", componente="CP-A", cantidad=2,
              secuencia=10, proveedor_codigo="VDR0013714", activo=True))
    s.add(Bom(articulo_transformado="TR-100", componente="CP-B", cantidad=1,
              secuencia=20, proveedor_codigo="VDR0013714", activo=True))
    s.add(OrdenCompra(numero="OC-001", proveedor_id=PID, articulo="CP-A",
                      cantidad=100, estado="ABIERTA", fecha=dt.date.today()))
    s.add(OrdenCompra(numero="OC-002", proveedor_id=PID, articulo="CP-B",
                      cantidad=60, estado="ABIERTA", fecha=dt.date.today()))
    s.flush()
    OC1 = s.query(OrdenCompra).filter_by(numero="OC-001").one().id
    OC2 = s.query(OrdenCompra).filter_by(numero="OC-002").one().id
print("  proveedor, ubicaciones, artículos, BOM y 2 OC abiertas creados")

# ---------------------------------------------------------------- RECIBO
print("\n=== 1. RECIBO ===")
with session_scope() as s:
    r = sv.crear_recibo(
        s, proveedor_id=PID, origen="BIN_A_BIN", referencia="BIN2686958",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-A", "Carenaje Crudo CR4", 100, 100,
                               ubicacion_desde="UB-ORIGEN", ubicacion_hasta="UB-PROV-01")])
    check("BIN a BIN crea documento con ID de trazabilidad",
          r.documento.trz.startswith("TRZ-BIN-"), r.documento.trz)
    alertas = sv.validar_bin_a_bin(s, r, PID)
    check("Validación Desde/Hasta del proveedor logueado sin alertas", alertas == [])
    RB1 = r.id

with session_scope() as s:
    res = sv.adjuntar_bin_y_match(s, recibo_id=RB1, orden_compra_id=OC1,
                                  usuario="recibo@akt.com")
    check("Match exacto con OC ABIERTA deja el recibo CERRADA",
          res["estado"] == "CERRADA", str(res))
    check("La OC queda CERRADA al completarse",
          s.get(OrdenCompra, OC1).estado == "CERRADA")
    check("Ingresa 100 de CP-A a inventario CRUDO/DISPONIBLE",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 100)

# Recibo con discrepancia -> NOVEDAD
with session_scope() as s:
    r2 = sv.crear_recibo(
        s, proveedor_id=PID, origen="FACTURA", referencia="FV-3-8619",
        usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
        lineas=[sv.LineaRecibo("CP-B", "Calca 2D CR4", 60, 55,
                               ubicacion_desde="UB-ORIGEN", ubicacion_hasta="UB-PROV-01")])
    RB2 = r2.id
    sv.sellar_recibo(s, RB2, "proveedor@akt.com")
    check("El proveedor sella el recibo del proveedor ORIGEN",
          s.get(type(r2), RB2).estado == "SELLADO")

with session_scope() as s:
    res = sv.adjuntar_bin_y_match(s, recibo_id=RB2, orden_compra_id=OC2,
                                  usuario="recibo@akt.com")
    check("Discrepancia de cantidad deja el recibo en NOVEDAD",
          res["estado"] == "NOVEDAD", f"esperado {res['esperado']} vs recibido {res['recibido']}")
    check("La novedad automática es FALTANTE de 5",
          s.get(Novedad, res["novedad_id"]).tipo == "FALTANTE"
          and s.get(Novedad, res["novedad_id"]).cantidad == 5)
    check("Ingresa lo realmente recibido (55 de CP-B)",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 55)
    NOV_ID = res["novedad_id"]

with session_scope() as s:
    esperar_error("Avería sin evidencia fotográfica es rechazada",
                  lambda: sv.registrar_novedad(s, proveedor_id=PID, articulo="CP-A",
                                               tipo="AVERIA", cantidad=1,
                                               usuario="x"), "evidencia")

with session_scope() as s:
    sv.ajustar_novedad(s, novedad_id=NOV_ID, documento_ajuste="TD90",
                       numero_ajuste="AJ-0001", usuario="inventarios@akt.com")
    check("Ajuste TD90 cierra la novedad",
          s.get(Novedad, NOV_ID).estado == "CERRADA")
    check("El faltante descuenta el inventario (55 -> 50)",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 50)

# Recibo adicional de CP-B para tener material suficiente
with session_scope() as s:
    r3 = sv.crear_recibo(s, proveedor_id=PID, origen="REGISTRO",
                         usuario="proveedor@akt.com", ubicacion_destino="UB-PROV-01",
                         lineas=[sv.LineaRecibo("CP-B", "Calca 2D CR4", 30, 30,
                                                ubicacion_hasta="UB-PROV-01")])
    sv.confirmar_recibo_simple(s, r3.id, "proveedor@akt.com")
    check("Registro manual suma inventario (50 + 30 = 80)",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 80)

# ------------------------------------------------------- ALMACENAMIENTO
print("\n=== 2. ALMACENAMIENTO ===")
with session_scope() as s:
    cp = sv.programar_conteo(s, proveedor_id=PID, articulo="CP-A",
                             ubicacion="UB-PROV-01", prioridad=1,
                             usuario="inventarios@akt.com")
    ce = sv.ejecutar_conteo(s, proveedor_id=PID, articulo="CP-A", cantidad_fisica=98,
                            ubicacion="UB-PROV-01", programado_id=cp.id,
                            usuario="proveedor@akt.com")
    check("Conteo con discrepancia genera cola de trabajo",
          ce.genera_cola and ce.estado == "EN_COLA_INVENTARIOS", f"dif={ce.diferencia}")
    check("La diferencia calculada es -2", ce.diferencia == -2)
    CE_ID = ce.id

with session_scope() as s:
    sv.ajustar_conteo(s, conteo_id=CE_ID, documento_ajuste="TD96",
                      numero_ajuste="AJ-0002", usuario="inventarios@akt.com")
    check("Ajuste del conteo deja el saldo en 98",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 98)
    cump = sv.cumplimiento_conteos(s, PID)
    check("Cumplimiento de conteos = 100%", cump["cumplimiento_pct"] == 100.0, str(cump))

with session_scope() as s:
    ev = sv.guardar_archivo(s, "destruccion.jpg", b"FOTO-EVIDENCIA", "image/jpeg", "prov")
    av = sv.registrar_averia(s, proveedor_id=PID, articulo="CP-A", cantidad=3,
                             motivo="MANIPULACION", momento="ALMACENAMIENTO",
                             evidencia_id=ev.id, usuario="proveedor@akt.com")
    check("Avería con evidencia pasa producto a RESTRINGIDO",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "RESTRINGIDO") == 3)
    check("Y lo descuenta de DISPONIBLE (98 -> 95)",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 95)
    check("La avería entra a la cola de Inventarios",
          av.estado == "EN_COLA_INVENTARIOS")
    esperar_error("Motivo de avería inválido es rechazado",
                  lambda: sv.registrar_averia(s, proveedor_id=PID, articulo="CP-A",
                                              cantidad=1, motivo="OTRO",
                                              evidencia_id=ev.id), "motivo")

with session_scope() as s:
    tol = sv.tolerancia_averias(s, PID)
    check("Panel de tolerancia detecta exceso sobre el acuerdo comercial",
          bool(tol) and tol[0]["excede"] is True,
          f"{tol[0]['pct_averia']}% vs tol {tol[0]['tolerancia_pct']}%" if tol else "")

# ------------------------------------------------------------ PRODUCCIÓN
print("\n=== 3. PRODUCCIÓN / SUBCONTRATACIÓN ===")
with session_scope() as s:
    maximo, det = sv.maximo_producible(s, PID, "TR-100")
    # CP-A: 95 disp / 2 = 47 ; CP-B: 80 / 1 = 80  -> max 47
    check("Máximo producible según BOM e inventario = 47", maximo == 47, f"max={maximo}")
    esperar_error("No deja programar de más (48 > 47)",
                  lambda: sv.programar_mps(s, proveedor_id=PID, articulo="TR-100",
                                           cantidad=48, usuario="planeacion@akt.com"),
                  "máximo producible")
    esperar_error("No deja programar artículo sin BOM",
                  lambda: sv.programar_mps(s, proveedor_id=PID, articulo="CP-A",
                                           cantidad=1, usuario="planeacion@akt.com"),
                  "BOM")

with session_scope() as s:
    m = sv.programar_mps(s, proveedor_id=PID, articulo="TR-100", cantidad=40,
                         usuario="planeacion@akt.com", ubicacion_destino="UB-PROC-01")
    MPS_ID = m.id
    check("MPS programado por planeación", m.estado == "PROGRAMADO")

with session_scope() as s:
    maximo2, _ = sv.maximo_producible(s, PID, "TR-100")
    check("El MPS abierto compromete inventario (máx. libre baja a 7)",
          maximo2 == 7, f"max={maximo2}")

with session_scope() as s:
    esperar_error("No deja ejecutar más de lo programado",
                  lambda: sv.ejecutar_produccion(s, mps_id=MPS_ID, cantidad=41,
                                                 usuario="proveedor@akt.com"),
                  "pendiente")

with session_scope() as s:
    sv.ejecutar_produccion(s, mps_id=MPS_ID, cantidad=25, usuario="proveedor@akt.com",
                           ubicacion_origen="UB-PROV-01")
    check("Consume 50 de CP-A (95 - 25*2 = 45)",
          sv.saldo_articulo(s, PID, "CP-A", "CRUDO", "DISPONIBLE") == 45)
    check("Consume 25 de CP-B (80 - 25 = 55)",
          sv.saldo_articulo(s, PID, "CP-B", "CRUDO", "DISPONIBLE") == 55)
    check("Produce 25 de TR-100 en inventario PROCESADO",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 25)

with session_scope() as s:
    from core.models import ProgramaProduccion
    m = s.get(ProgramaProduccion, MPS_ID)
    check("El MPS queda EN_PROCESO con pendiente 15",
          m.estado == "EN_PROCESO" and m.pendiente == 15, f"{m.estado} pend={m.pendiente}")
    check("Cumplimiento del MPS = 62.5%", round(m.cumplimiento, 1) == 62.5)

with session_scope() as s:
    ev = sv.guardar_archivo(s, "dest2.jpg", b"FOTO2", "image/jpeg", "prov")
    sv.registrar_averia(s, proveedor_id=PID, articulo="TR-100", cantidad=2,
                        motivo="PUESTA_A_PUNTO", momento="PRODUCCION",
                        estado_inventario="PROCESADO", evidencia_id=ev.id,
                        usuario="proveedor@akt.com")
    check("Avería en producción sobre producto PROCESADO (25 -> 23)",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 23)

# -------------------------------------------------------------- DESPACHO
print("\n=== 4. DESPACHO ===")
with session_scope() as s:
    d = sv.crear_despacho(s, proveedor_id=PID, lote="LOTE-ENS-001",
                          plan_ensamble="Plan Ensamble CR4 Sem-38",
                          usuario="proveedor@akt.com", ubicacion_origen="UB-PROC-01",
                          lineas=[{"articulo": "TR-100", "cantidad": 20}])
    DSP = d.id
    check("Despacho creado con lote y plan de ensamble", d.estado == "BORRADOR")

with session_scope() as s:
    esperar_error("No deja despachar más de lo que hay",
                  lambda: sv.confirmar_despacho(
                      s, despacho_id=sv.crear_despacho(
                          s, proveedor_id=PID, lote="L-X", usuario="p",
                          lineas=[{"articulo": "TR-100", "cantidad": 999}]).id,
                      usuario="p"), "insuficiente")

with session_scope() as s:
    sv.confirmar_despacho(s, despacho_id=DSP, usuario="proveedor@akt.com")
    check("Despacho descuenta el inventario procesado (23 - 20 = 3)",
          sv.saldo_articulo(s, PID, "TR-100", "PROCESADO", "DISPONIBLE") == 3)

# ------------------------------------------------------ PANEL DE CONTROL
print("\n=== 5. PANEL DE CONTROL / TRAZABILIDAD ===")
with session_scope() as s:
    ind = sv.indicadores_inventario(s, PID)
    check("Indicadores calculan rotación, cobertura y semáforo",
          bool(ind) and all(k in ind[0] for k in
                            ("rotacion_anual", "cobertura_dias", "semaforo")),
          f"{len(ind)} artículos")
    docs = sv.antiguedad_documentos(s, PID)
    check("Todo documento tiene TRZ y antigüedad",
          bool(docs) and all(d["trz"].startswith("TRZ-") for d in docs),
          f"{len(docs)} documentos")
    cola = sv.cola_inventarios(s, PID)
    check("Cola de Inventarios lista averías pendientes",
          len(cola["averias"]) == 2, f"{len(cola['averias'])} averías")
    zonas = sv.calcular_zonas_ddmrp(10, 7)
    est, pct = sv.estado_ddmrp(30, zonas)
    check("DDMRP calcula zonas y semáforo", est == "ROJO", f"{est} {pct:.0f}%")

# Integridad del kardex vs saldos
with session_scope() as s:
    from core.models import Inventario, MovimientoInventario
    from sqlalchemy import func as F
    malas = 0
    for inv in s.query(Inventario).all():
        suma = s.query(F.coalesce(F.sum(MovimientoInventario.cantidad), 0.0)).filter(
            MovimientoInventario.proveedor_id == inv.proveedor_id,
            MovimientoInventario.articulo == inv.articulo,
            MovimientoInventario.ubicacion == inv.ubicacion,
            MovimientoInventario.estado == inv.estado,
            MovimientoInventario.condicion == inv.condicion).scalar()
        if abs(float(suma or 0) - float(inv.cantidad or 0)) > 1e-6:
            malas += 1
    check("El kardex cuadra con todos los saldos de inventario", malas == 0,
          f"{malas} descuadres")

print("\n" + "=" * 64)
print(f"RESULTADO: {len(OK)} pruebas OK, {len(FAIL)} fallidas")
if FAIL:
    for f in FAIL:
        print("  FALLÓ:", f)
    sys.exit(1)
print("Flujo end-to-end validado.")
