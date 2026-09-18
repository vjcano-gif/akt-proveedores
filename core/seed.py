"""Carga inicial de la base de datos con los datos reales del Excel del proyecto."""
from __future__ import annotations

import csv
import datetime as dt
import os
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


def _setting(nombre, default=None):
    try:
        import streamlit as st
        valor = st.secrets.get(nombre)
        if valor not in (None, ""):
            return valor
    except Exception:
        pass
    return os.environ.get(nombre, default)


def demo_mode() -> bool:
    """Demo explícito o ejecución sin DATABASE_URL (SQLite local)."""
    v = _setting("DEMO_MODE")
    if v is not None:
        return _b(v)
    return not bool(_setting("DATABASE_URL"))


def sembrar(force=False, con_demo=None) -> dict:
    """Idempotente. Producción nunca crea credenciales demo conocidas."""
    if con_demo is None:
        con_demo = demo_mode()
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
                                ubicacion_destino=(r.get("ubicacion_destino") or "").strip().upper() or None,
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
                pc = (r.get("proveedor_codigo") or "").strip()
                prov = s.query(Proveedor).filter_by(codigo=pc).first() if pc else None
                u = Ubicacion(codigo=cod, un=(r.get("un") or "MOTOS"),
                              rol=r.get("rol"), proveedor_id=prov.id if prov else None,
                              cerrada=_b(r.get("cerrada")),
                              inspeccion=_b(r.get("inspeccion")),
                              restringida=_b(r.get("restringida")), activo=True)
                s.add(u)
                if prov and not prov.ubicacion_destino and str(u.rol or "").upper() == "DESTINO":
                    prov.ubicacion_destino = cod
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
                pc = (r.get("proveedor_codigo") or "").strip()
                k = (at, cp, seq, pc)
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
                    proveedor_codigo=pc[:40],
                    proveedor_nombre=(r.get("proveedor_nombre") or "")[:200], activo=True))
                n += 1
                if len(objs) >= 3000:
                    s.bulk_save_objects(objs); s.flush(); objs = []
            if objs:
                s.bulk_save_objects(objs); s.flush()
            res["bom"] = n

        # ---------- usuario inicial / usuarios demo ----------
        if s.query(func.count(Usuario.id)).scalar() == 0:
            prov = (s.query(Proveedor).filter(Proveedor.nombre.ilike("%DL Plus%")).first()
                    or s.query(Proveedor).order_by(Proveedor.id).first())
            if con_demo:
                usuarios = [
                    ("proveedor@akt.com", "Proveedor de Transformación", "PROVEEDOR",
                     prov.id if prov else None),
                    ("recibo@akt.com", "Equipo Recibo AKT", "RECIBO_AKT", None),
                    ("inventarios@akt.com", "Equipo Inventarios", "INVENTARIOS", None),
                    ("planeacion@akt.com", "Planeación", "PLANEACION", None),
                ]
                for email, nombre, rol, pid in usuarios:
                    s.add(Usuario(email=email, nombre=nombre, rol=rol, proveedor_id=pid,
                                  password_hash=hash_password("akt2026"), activo=True))
                res["usuarios"] = len(usuarios)

                # Solo en demo se asignan ubicaciones automáticamente.
                if prov:
                    demo_ubs = s.query(Ubicacion).limit(12).all()
                    for u in demo_ubs:
                        u.proveedor_id = prov.id
                    if not prov.ubicacion_destino and demo_ubs:
                        destino = next(
                            (u for u in demo_ubs if str(u.rol or "").upper() == "DESTINO"),
                            demo_ubs[0])
                        prov.ubicacion_destino = destino.codigo
            else:
                email = str(_setting("BOOTSTRAP_ADMIN_EMAIL") or "").strip().lower()
                password = str(_setting("BOOTSTRAP_ADMIN_PASSWORD") or "")
                nombre = str(_setting("BOOTSTRAP_ADMIN_NAME") or "Administrador inicial").strip()
                if not email or not password or len(password) < 12:
                    raise RuntimeError(
                        "Base de producción sin usuarios. Configure BOOTSTRAP_ADMIN_EMAIL y "
                        "BOOTSTRAP_ADMIN_PASSWORD (mínimo 12 caracteres) en Streamlit Secrets.")
                s.add(Usuario(email=email, nombre=nombre, rol="INVENTARIOS",
                              proveedor_id=None, password_hash=hash_password(password),
                              activo=True))
                res["usuarios"] = 1
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
