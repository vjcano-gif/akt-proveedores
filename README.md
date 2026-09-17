# AKT MOTOS — Proveedores de Transformación

Aplicación web para controlar el flujo **Recibo → Almacenamiento → Producción → Despacho** con inventario por proveedor, trazabilidad, novedades, conteos, transformación por BOM y despacho.

## Reglas críticas

- Un **BIN a BIN** o una **factura** no afecta inventario hasta completar el match por línea contra una OC abierta.
- Un recibo puede relacionar **varias líneas con varias OC**.
- Un faltante de recepción representa material que nunca entró: al cerrar el ajuste **no se descuenta por segunda vez**.
- Un sobrante entra **RESTRINGIDO** hasta que Inventarios decida **LIBERAR** o **DEVOLVER**.
- Factura y BIN se conservan como soportes separados.
- Se distingue **proveedor origen** de **proveedor transformador**.
- El BOM se resuelve por proveedor.
- Ubicaciones cerradas no se pueden operar; restringidas/de inspección no reciben producto DISPONIBLE.
- Los consecutivos `TRZ-*` usan un contador atómico por tipo/año.
- Rol y alcance del proveedor se verifican también en la capa de servicios.

## Reconocimiento de documentos

El módulo de recibo acepta PDF o imagen y propone los datos antes de guardar. PDFs con texto se analizan localmente con `pypdf`; imágenes o PDFs escaneados pueden analizarse con Gemini si se configura `GEMINI_API_KEY`. La extracción nunca confirma inventario ni OC automáticamente: el usuario revisa artículo, cantidad física, OC y ubicación.

Configuración opcional:

```toml
GEMINI_API_KEY = "..."
GEMINI_MODEL = "gemini-2.5-flash-lite"
```

## Evidencias

En producción se recomienda Supabase Storage. Si no está configurado o falla temporalmente, la app conserva un fallback binario en la base para no perder la evidencia.

```toml
SUPABASE_URL = "https://<project-ref>.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "..."
SUPABASE_STORAGE_BUCKET = "documentos-akt"
```

## Roles

| Rol | Operación |
|---|---|
| PROVEEDOR | Sus recibos, sellado, novedades, conteos, averías, transformación y despacho |
| RECIBO_AKT | Recibo y match por línea contra OC |
| INVENTARIOS | Conteos, ajustes TD90/TD96, maestros y control |
| PLANEACION | MPS y BOM |

## Arranque local

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Sin `DATABASE_URL` se usa SQLite local. Los cuatro usuarios con contraseña `akt2026` existen **solo en desarrollo local**.

## Producción

Configure como mínimo:

```toml
DATABASE_URL = "postgresql://..."
BOOTSTRAP_ADMIN_EMAIL = "admin@empresa.com"
BOOTSTRAP_ADMIN_PASSWORD = "una-clave-segura-de-12-o-mas"
BOOTSTRAP_ADMIN_NAME = "Administrador Inventarios"
```

En PostgreSQL/Supabase **no se crean usuarios demo**. El primer usuario INVENTARIOS se crea desde esos secretos solo si la tabla `usuarios` está vacía.

La app ejecuta creación de tablas nuevas y migraciones aditivas idempotentes. También existe la migración explícita `sql/migrations/20260917_core_fixes.sql`.

## Datos semilla

| Archivo | Contenido |
|---|---:|
| `data/articulos.csv` | 8.204 artículos |
| `data/bom.csv` | 37.843 relaciones BOM |
| `data/ubicaciones.csv` | 55 ubicaciones |
| `data/proveedores.csv` | 49 proveedores |

Antes de operar, asigne correctamente las ubicaciones a cada proveedor y cargue las OC abiertas.

## QA

```bash
python -m compileall -q core app_pages streamlit_app.py
python tests/test_flujo.py
```

GitHub Actions ejecuta compilación y pruebas en Python 3.12 y 3.14 en cada push y PR contra `main`. Las pruebas cubren recibo exacto, faltantes, sobrantes, múltiples OC, duplicados, ubicaciones, permisos, BOM por proveedor, conteos, averías, MPS, despacho y unicidad TRZ.

## Seguridad

El repositorio contiene maestros operativos y BOM. Para uso empresarial debe mantenerse **privado**. Streamlit Community Cloud puede desplegar desde repositorios privados cuando la cuenta de GitHub conectada tiene acceso.

## Integración ERP/WMS

La sincronización automática con el ERP/WMS de AKT requiere definir el mecanismo oficial disponible (API, archivos programados, SFTP, vista de base de datos u otro). Hasta entonces se mantienen cargues controlados de OC, MPS y foto ERP.
