"""Carga inicial de la base de datos con los datos reales del Excel del proyecto."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from sqlalchemy import func

from core.auth import hash_password
from core.db import init_db, session_scope
from core.models import (
    Articulo, Bom, OrdenCompra, Proveedor, Ubicacion, Usuario,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def _leer(nombre):
    p = DATA / nombre
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _b(v):
    return str(v).strip().lower() in ("true", "1", "y", "si", "sí", "yes")


def sembrar(force=False, con_demo=True, crear_usuarios_demo=True) -> dict:
    """Idempotente: solo carga lo que falta."""
    init_db()
    res = {}
    with session_scope() as s:
        # ---------- proveedores ----------
        if force or s.query(func.count(Proveedor.id)).scalar() == 0:
            n = 0
            for r in _leer("proveedores.csv"):
                cod = (r.get("codigo") or "").strip()
                if not cod or s.query(Proveedor).filter_by(codigo=cod).first():
                    continue
                s.add(Proveedor(codigo=cod, nombre=(r.get("nombre") or cod).strip(),
                                tolerancia_averia_pct=float(r.get("tolerancia_averia_pct") or 1.0),
                                activo=True))
                n += 1
            s.flush()
            res["proveedores"] = n

        # ---------- artículos ----------
        if force or s.query(func.count(Articulo.id)).scalar() == 0:
            existentes = {c for (c,) in s.query(Articulo.codigo).all()}
            objs, n = [], 0
            for r in _leer("articulos.csv"):
                cod = (r.get("codigo") or "").strip()
                if not cod or cod in existentes:
                    continue
                existentes.add(cod)
                objs.append(Articulo(
                    codigo=cod, descripcion=(r.get("descripcion") or cod)[:300],
                    familia=(r.get("familia") or "")[:60],
                    desc_familia=(r.get("desc_familia") or "")[:160],
                    grupo_articulo=(r.get("grupo_articulo") or "")[:60],
                    categoria=(r.get("categoria") or "")[:80],
                    color=(r.get("color") or "")[:80],
                    tipo=(r.get("tipo") or "CRUDO"), unidad="UND", activo=True))
                n += 1
                if len(objs) >= 2000:
                    s.bulk_save_objects(objs); s.flush(); objs = []
            if objs:
                s.bulk_save_objects(objs); s.flush()
            res["articulos"] = n

        # ---------- ubicaciones ----------
        if force or s.query(func.count(Ubicacion.id)).scalar() == 0:
            n = 0
            for r in _leer("ubicaciones.csv"):
                cod = (r.get("codigo") or "").strip().upper()
                if not cod or s.query(Ubicacion).filter_by(codigo=cod).first():
                    continue
                s.add(Ubicacion(codigo=cod, un=(r.get("un") or "MOTOS"),
                                rol=r.get("rol"), cerrada=_b(r.get("cerrada")),
                                inspeccion=_b(r.get("inspeccion")),
                                restringida=_b(r.get("restringida")), activo=True))
                n += 1
            s.flush()
            res["ubicaciones"] = n

        # ---------- BOM ----------
        if force or s.query(func.count(Bom.id)).scalar() == 0:
            vistos, objs, n = set(), [], 0
            for r in _leer("bom.csv"):
                at = (r.get("articulo_transformado") or "").strip()
                cp = (r.get("componente") or "").strip()
                if not at or not cp:
                    continue
                try:
                    seq = int(float(r.get("secuencia") or 0))
                except ValueError:
                    seq = 0
                k = (at, cp, seq)
                if k in vistos:
                    continue
                vistos.add(k)
                try:
                    cant = float(r.get("cantidad") or 1)
                except ValueError:
                    cant = 1.0
                objs.append(Bom(
                    articulo_transformado=at, desc_transformado=(r.get("desc_transformado") or "")[:300],
                    componente=cp, desc_componente=(r.get("desc_componente") or "")[:300],
                    cantidad=cant, secuencia=seq, fase=(r.get("fase") or "")[:20],
                    proveedor_codigo=(r.get("proveedor_codigo") or "")[:40],
                    proveedor_nombre=(r.get("proveedor_nombre") or "")[:200], activo=True))
                n += 1
                if len(objs) >= 3000:
                    s.bulk_save_objects(objs); s.flush(); objs = []
            if objs:
                s.bulk_save_objects(objs); s.flush()
            res["bom"] = n

        # ---------- usuarios demo (solo desarrollo local) ----------
        if crear_usuarios_demo and s.query(func.count(Usuario.id)).scalar() == 0:
            prov = (s.query(Proveedor).filter(Proveedor.nombre.ilike("%DL Plus%")).first()
                    or s.query(Proveedor).order_by(Proveedor.id).first())
            usuarios = [
                ("proveedor@akt.com", "Proveedor de Transformación", "PROVEEDOR",
                 prov.id if prov else None),
                ("recibo@akt.com", "Equipo Recibo AKT", "RECIBO_AKT", None),
                ("inventarios@akt.com", "Equipo Inventarios", "INVENTARIOS", None),
                ("planeacion@akt.com", "Planeación (Ochoa)", "PLANEACION", None),
            ]
            for email, nombre, rol, pid in usuarios:
                s.add(Usuario(email=email, nombre=nombre, rol=rol, proveedor_id=pid,
                              password_hash=hash_password("akt2026"), activo=True))
            s.flush()
            res["usuarios"] = len(usuarios)

            # Ubicaciones del proveedor demo, para validar Desde/Hasta del BIN a BIN
            if prov:
                for u in s.query(Ubicacion).limit(12).all():
                    u.proveedor_id = prov.id
                s.flush()

        # ---------- órdenes de compra demo ----------
        if con_demo and s.query(func.count(OrdenCompra.id)).scalar() == 0:
            prov = s.query(Proveedor).join(
                Usuario, Usuario.proveedor_id == Proveedor.id).first() \
                or s.query(Proveedor).first()
            if prov:
                arts = [b.componente for b in s.query(Bom).filter(
                    Bom.proveedor_codigo == prov.codigo).limit(6).all()] or \
                    [a.codigo for a in s.query(Articulo).limit(6).all()]
                n = 0
                for i, art in enumerate(dict.fromkeys(arts), start=1):
                    s.add(OrdenCompra(numero=f"OC-2026-{i:04d}", proveedor_id=prov.id,
                                      articulo=art, cantidad=100.0 * i,
                                      estado="ABIERTA", fecha=dt.date.today()))
                    n += 1
                s.flush()
                res["ordenes_compra"] = n
    return res


if __name__ == "__main__":
    print(sembrar())
