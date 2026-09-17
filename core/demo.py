"""Genera un escenario de demostración con el flujo completo, para probar la app."""
from __future__ import annotations

import datetime as dt
import random

from sqlalchemy import func

from core.db import session_scope
from core.models import (Bom, Inventario, OrdenCompra, Proveedor, Ubicacion, Usuario)
from core.services import (LineaRecibo, adjuntar_bin_y_match, confirmar_despacho,
                           crear_despacho, crear_recibo, ejecutar_conteo,
                           ejecutar_produccion, guardar_archivo, programar_conteo,
                           programar_mps, registrar_averia, sellar_recibo,
                           maximo_producible, explosion_bom)

FOTO = b"\x89PNG\r\n\x1a\n" + b"EVIDENCIA DEMO" * 10


def generar(proveedor_id=None, semilla=7) -> dict:
    random.seed(semilla)
    res = {}
    with session_scope() as s:
        if proveedor_id is None:
            u = s.query(Usuario).filter(Usuario.rol == "PROVEEDOR").first()
            proveedor_id = u.proveedor_id if u else None
        if not proveedor_id:
            proveedor_id = s.query(Proveedor.id).first()[0]
        prov = s.get(Proveedor, proveedor_id)

        ubis = [u.codigo for u in s.query(Ubicacion).filter(
            Ubicacion.proveedor_id == proveedor_id,
            Ubicacion.activo.is_(True), Ubicacion.cerrada.is_(False),
            Ubicacion.restringida.is_(False), Ubicacion.inspeccion.is_(False)
        ).limit(4).all()]
        if not ubis:
            ubis = [u.codigo for u in s.query(Ubicacion).limit(4).all()] or [""]
        ub_crudo, ub_proc = ubis[0], ubis[-1]

        # Artículos transformados con BOM de este proveedor
        trans = [r[0] for r in s.query(Bom.articulo_transformado).filter(
            Bom.proveedor_codigo == prov.codigo).distinct().limit(6).all()]
        if not trans:
            trans = [r[0] for r in s.query(Bom.articulo_transformado)
                     .distinct().limit(6).all()]
        componentes = {}
        for t in trans:
            componentes[t] = explosion_bom(s, t, proveedor_id)

        # 1) RECIBO de componentes (BIN a BIN)
        lineas, vistos = [], set()
        for t in trans:
            for b in componentes[t]:
                if b.componente in vistos:
                    continue
                vistos.add(b.componente)
                q = random.choice([120, 180, 240, 300, 360])
                lineas.append(LineaRecibo(b.componente, b.desc_componente or "", q, q,
                                          ubicacion_desde="MOTOS-ORIGEN",
                                          ubicacion_hasta=ub_crudo))
        r = crear_recibo(s, proveedor_id=proveedor_id, origen="BIN_A_BIN",
                         referencia=f"BIN-DEMO-{dt.datetime.utcnow():%H%M%S%f}", usuario="demo",
                         ubicacion_destino=ub_crudo, lineas=lineas[:25])
        mapping = {}
        for i, ln in enumerate(r.lineas, 1):
            oc_bin = OrdenCompra(
                numero=f"OC-DEMO-BIN-{r.id}-{i:02d}", proveedor_id=proveedor_id,
                articulo=ln.articulo, cantidad=ln.cantidad_fisica,
                estado="ABIERTA", fecha=dt.date.today())
            s.add(oc_bin); s.flush()
            mapping[ln.id] = oc_bin.id
        adjuntar_bin_y_match(
            s, recibo_id=r.id, lineas_oc=mapping, usuario="demo",
            referencia_bin=r.documento.referencia)
        res["recibo_bin"] = r.documento.trz

        # 2) RECIBO por FACTURA con discrepancia -> NOVEDAD
        comp = lineas[0].articulo
        oc = OrdenCompra(numero="OC-DEMO-001", proveedor_id=proveedor_id,
                         articulo=comp, cantidad=100, estado="ABIERTA",
                         fecha=dt.date.today())
        s.add(oc); s.flush()
        r2 = crear_recibo(s, proveedor_id=proveedor_id, origen="FACTURA",
                          referencia="FV-3-8619", usuario="demo",
                          ubicacion_destino=ub_crudo,
                          lineas=[LineaRecibo(comp, lineas[0].descripcion, 100, 94,
                                              ubicacion_hasta=ub_crudo)])
        sellar_recibo(s, r2.id, "demo")
        m = adjuntar_bin_y_match(s, recibo_id=r2.id, orden_compra_id=oc.id,
                                 usuario="demo", referencia_bin="BIN2686959")
        res["recibo_factura"] = f"{r2.documento.trz} ({m['estado']})"

        # 3) CONTEOS
        for c in lineas[:4]:
            cp = programar_conteo(s, proveedor_id=proveedor_id, articulo=c.articulo,
                                  ubicacion=ub_crudo, prioridad=random.choice([1, 2, 3]),
                                  usuario="demo")
            if random.random() < 0.75:
                base = c.cantidad_fisica
                ejecutar_conteo(s, proveedor_id=proveedor_id, articulo=c.articulo,
                                cantidad_fisica=base + random.choice([0, 0, -3, 2]),
                                ubicacion=ub_crudo, programado_id=cp.id, usuario="demo")
        res["conteos"] = 4

        # 4) AVERÍAS
        ev = guardar_archivo(s, "destruccion_demo.png", FOTO, "image/png", "demo")
        for c in lineas[:3]:
            registrar_averia(s, proveedor_id=proveedor_id, articulo=c.articulo,
                             cantidad=random.choice([2, 4, 6]),
                             motivo=random.choice(["ORIGEN", "MANIPULACION",
                                                   "PUESTA_A_PUNTO"]),
                             momento="ALMACENAMIENTO", evidencia_id=ev.id,
                             usuario="demo")
        res["averias"] = 3

        # 5) MPS + TRANSFORMACIÓN
        producidos = []
        for t in trans[:4]:
            maximo, _ = maximo_producible(s, proveedor_id, t)
            if maximo < 5:
                continue
            cant = max(5, int(maximo * 0.6))
            mps = programar_mps(s, proveedor_id=proveedor_id, articulo=t, cantidad=cant,
                                fecha_programada=dt.date.today(), usuario="demo",
                                ubicacion_destino=ub_proc)
            ejec = int(cant * random.choice([0.6, 0.8, 1.0]))
            if ejec > 0:
                ejecutar_produccion(s, mps_id=mps.id, cantidad=ejec, usuario="demo",
                                    ubicacion_origen=ub_crudo, ubicacion_destino=ub_proc)
                producidos.append((t, ejec))
        res["mps"] = len(producidos)

        # 6) DESPACHO
        if producidos:
            lns = [{"articulo": t, "cantidad": max(1, int(q * 0.5)), "ubicacion": ub_proc}
                   for t, q in producidos[:3]]
            d = crear_despacho(s, proveedor_id=proveedor_id, lineas=lns,
                               lote="LOTE-ENS-001",
                               plan_ensamble="Plan Ensamble Semana 38",
                               usuario="demo", ubicacion_origen=ub_proc)
            confirmar_despacho(s, despacho_id=d.id, usuario="demo")
            res["despacho"] = d.documento.trz
    return res


def hay_datos() -> bool:
    with session_scope() as s:
        return (s.query(func.count(Inventario.id)).scalar() or 0) > 0


if __name__ == "__main__":
    print(generar())
