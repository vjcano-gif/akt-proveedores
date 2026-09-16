# AKT MOTOS — App para Proveedores de Transformación

Aplicación web para gestionar el flujo de materiales con los proveedores de
transformación: **Recibo → Almacenamiento → Producción → Despacho**, con lógica
de WMS opcional, trazabilidad documental y panel de control de inventario.

Construida a mínimo costo sobre **Streamlit + Supabase + GitHub** (las tres con
plan gratuito suficiente para arrancar).

---

## Qué hace

| Etapa | Funcionalidad |
|---|---|
| **Recibo** | BIN a BIN (materia prima cruda desde AKT MOTOS), Factura (producto desde otro proveedor ORIGEN con sello del proveedor de transformación), Registro manual con PDF o foto. Match automático contra **OC ABIERTA**: si la cantidad coincide el documento queda **CERRADA**, si no queda en **NOVEDAD** y abre la novedad sola. Tipificación de **reprocesos** (garantías). |
| **Novedades** | Faltantes, sobrantes y averías. Derivan producto **DISPONIBLE** o **RESTRINGIDO**. La avería exige evidencia fotográfica de la destrucción y genera cola de trabajo para Inventarios, que monta el ajuste **TD90 / TD96**. |
| **Almacenamiento** | Agenda de conteos cíclicos programados por Inventarios, medición de **cumplimiento programado vs. ejecutado**, conteos aleatorios, carga de la **foto del inventario del ERP** para compararla contra la app y priorizar conteos. Las discrepancias generan cola de trabajo. |
| **Averías** | Reporte con artículo, cantidad y evidencia. Tres momentos (Recibo, Almacenamiento, Producción) y cuatro motivos (**Origen, Manipulación, Puesta a punto, AKT**). Panel de **tolerancia por acuerdo comercial** de cada proveedor. |
| **Producción** | MPS: Planeación sugiere qué transformar. El sistema **valida existencias por BOM y no deja programar de más** (descuenta lo ya comprometido). La ejecución consume los componentes del inventario **crudo** y produce el artículo en inventario **procesado**. |
| **Despacho** | Plan de ensamble y lote, descarga del inventario procesado con soporte adjunto. |
| **Inventario** | Posición, rotación, cobertura y semáforo por colores. Kardex completo. Trazabilidad y antigüedad de todos los documentos. |
| **Maestros** | Artículos, BOM, ubicaciones, proveedores, órdenes de compra y usuarios. Todas las tablas se consultan, editan y **cargan masivamente con plantilla de ejemplo descargable** (CSV y Excel). Un artículo con saldo no se puede deshabilitar. |
| **DDMRP (fase 2)** | Parametrización de topes y zonas (roja, amarilla, verde) con alertas por colores sobre el inventario real. |

### Trazabilidad
Cada documento recibe un consecutivo del tipo `TRZ-BIN-2026-000123`, con su
antigüedad en días y rangos (0-7, 8-30, 31-60, 61-90, 90+).

### Roles
| Rol | Alcance |
|---|---|
| **PROVEEDOR** | Solo ve y opera **sus** documentos e inventario. Registra recibos, sella facturas, reporta novedades y averías, ejecuta conteos, ejecuta la transformación y despacha. |
| **RECIBO_AKT** | Adjunta el BIN a BIN y hace el match con la orden de compra. Administra órdenes de compra. |
| **INVENTARIOS** | Programa conteos, carga la foto del ERP, monta los ajustes TD90/TD96 y administra todos los maestros. |
| **PLANEACION** | Programa el MPS y administra artículos y BOM. |

---

## Puesta en marcha

### 1. Local (para probar)

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Sin configuración usa SQLite en `data/akt.db` y siembra sola los datos del
proyecto. Usuarios de demostración (contraseña `akt2026`):

| Correo | Rol |
|---|---|
| `proveedor@akt.com` | Proveedor |
| `recibo@akt.com` | Recibo AKT |
| `inventarios@akt.com` | Inventarios |
| `planeacion@akt.com` | Planeación |

> **Cambie estas contraseñas** en Maestros → Usuarios antes de usarla de verdad.

### 2. Supabase (base de datos)

1. Cree un proyecto en [supabase.com](https://supabase.com) (plan Free).
2. **Project Settings → Database → Connection string → Session pooler**.
   Copie la cadena y reemplace `[YOUR-PASSWORD]` por la contraseña del proyecto.
   Use el *pooler* (puerto 5432 o 6543), no la conexión directa: Streamlit Cloud
   no tiene IPv6.
3. Opcional: ejecute `sql/schema_supabase.sql` en **SQL Editor**. Si no lo hace,
   la app crea las tablas sola en el primer arranque.

### 3. GitHub

```bash
git init
git add .
git commit -m "App proveedores de transformación AKT"
git branch -M main
git remote add origin https://github.com/USUARIO/akt-proveedores.git
git push -u origin main
```

`.gitignore` ya excluye `secrets.toml` y la base local: las credenciales nunca
se suben.

### 4. Streamlit Community Cloud

1. [share.streamlit.io](https://share.streamlit.io) → **New app** → elija el repo.
2. *Main file path*: `streamlit_app.py`
3. **Advanced settings → Secrets**, pegue:

```toml
DATABASE_URL = "postgresql://postgres.xxxx:SU_PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
```

4. Deploy. El primer arranque crea las tablas y siembra artículos, BOM,
   ubicaciones y proveedores (unos 45 segundos).

---

## Cargar los datos reales

Los CSV de `data/` salieron del Excel del proyecto y se siembran solos:

| Archivo | Contenido |
|---|---|
| `articulos.csv` | 8.204 artículos (1.092 transformados, 7.112 crudos) |
| `bom.csv` | 37.843 relaciones artículo transformado → componente |
| `ubicaciones.csv` | 55 ubicaciones con marca de restringida / inspección / cerrada |
| `proveedores.csv` | 49 proveedores |

Para actualizarlos después use el **cargue masivo** de cada maestro, que trae
plantilla de ejemplo descargable.

---

## Estructura

```
streamlit_app.py        Login, navegación por rol
core/
  models.py             24 tablas (SQLAlchemy)
  db.py                 Conexión SQLite / Supabase
  auth.py               Autenticación y permisos por rol
  services.py           Reglas de negocio del flujo
  ui.py                 Estilos, plantillas, cargue masivo
  seed.py               Siembra de datos del Excel
  demo.py               Escenario de prueba
app_pages/              Una pantalla por etapa del flujo
sql/schema_supabase.sql DDL de PostgreSQL
tests/test_flujo.py     42 pruebas del flujo end-to-end
data/                   CSV semilla
```

## Pruebas

```bash
python3 tests/test_flujo.py
```

Recorre recibo → novedad → ajuste → conteo → avería → MPS → transformación →
despacho y verifica los saldos en cada paso, incluida la cuadratura entre el
kardex y el inventario.

---

## Costo

| Servicio | Plan | Costo |
|---|---|---|
| GitHub | Free | $0 |
| Supabase | Free (500 MB, 2 proyectos) | $0 |
| Streamlit Community Cloud | Free (app pública) | $0 |

Si más adelante necesita la app privada o más base de datos: Streamlit Cloud no
ofrece apps privadas en el plan gratuito y Supabase Pro cuesta USD 25/mes.
Alternativa privada económica: desplegar en un contenedor propio (Render, Fly.io)
por unos USD 5-7/mes.
