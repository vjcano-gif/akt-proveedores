"""Smoke test de importación: no conecta a producción ni modifica datos."""
import importlib

MODULES = [
    "core.models",
    "core.db",
    "core.services",
    "core.ui",
    "core.auth",
    "core.document_ai",
    "app_pages.inicio",
    "app_pages.recibo",
    "app_pages.novedades",
    "app_pages.almacenamiento",
    "app_pages.averias",
    "app_pages.produccion",
    "app_pages.despacho",
    "app_pages.inventario",
    "app_pages.ddmrp",
    "app_pages.maestros",
]

fallos = []
for nombre in MODULES:
    try:
        importlib.import_module(nombre)
        print(f"OK  {nombre}")
    except Exception as exc:
        fallos.append((nombre, type(exc).__name__, str(exc)))
        print(f"FAIL {nombre}: {type(exc).__name__}: {exc}")

if fallos:
    detalle = "\n".join(f"{m}: {t}: {e}" for m, t, e in fallos)
    raise SystemExit("Falló el smoke test de imports:\n" + detalle)

print(f"Smoke test OK: {len(MODULES)} módulos.")
