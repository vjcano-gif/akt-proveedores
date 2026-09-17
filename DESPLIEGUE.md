# Despliegue seguro — AKT Proveedores

## 1. Repositorio

Use un repositorio **privado**. No suba `.streamlit/secrets.toml`, bases SQLite, credenciales, API keys ni exportaciones sensibles.

## 2. Supabase

Use exclusivamente el proyecto destinado a AKT Proveedores. Copie la conexión del **Session Pooler** y configure:

```toml
DATABASE_URL = "postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres"
```

Para una base existente puede ejecutar `sql/migrations/20260917_core_fixes.sql`. La app también ejecuta migraciones aditivas al iniciar.

> Verifique siempre el Project Ref antes de ejecutar DDL. No aplique esta migración en otro proyecto de Supabase.

## 3. Primer usuario productivo

No existen credenciales demo automáticas en PostgreSQL/Supabase.

```toml
BOOTSTRAP_ADMIN_EMAIL = "admin@empresa.com"
BOOTSTRAP_ADMIN_PASSWORD = "una-clave-segura-de-12-o-mas"
BOOTSTRAP_ADMIN_NAME = "Administrador Inventarios"
```

El bootstrap solo actúa si la tabla `usuarios` está vacía.

## 4. Reconocimiento PDF/imagen

```toml
GEMINI_API_KEY = "..."
GEMINI_MODEL = "gemini-2.5-flash-lite"
```

Sin clave, PDFs con texto pueden analizarse localmente; imágenes y escaneos quedan para revisión/captura manual. La IA propone, pero no confirma movimientos.

## 5. Supabase Storage

Cree un bucket privado, por ejemplo `documentos-akt`, y configure:

```toml
SUPABASE_URL = "https://<project-ref>.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "..."
SUPABASE_STORAGE_BUCKET = "documentos-akt"
```

La service-role key debe existir solo en Secrets del servidor.

## 6. Streamlit Community Cloud

Use el repositorio privado autorizado, branch `main` y main file `streamlit_app.py`. Añada los secretos anteriores en App settings → Secrets.

## 7. Antes de operar

1. Verifique proveedores.
2. Asigne ubicaciones a cada proveedor.
3. Confirme ubicaciones cerradas/restringidas/inspección.
4. Cargue OC abiertas.
5. Valide BOM por proveedor.
6. Cree usuarios y roles reales.
7. Pruebe BIN exacto, faltante, sobrante y documento con varias OC.
8. Compare kardex y saldos contra ERP.

## 8. CI

`.github/workflows/ci.yml` ejecuta:

```bash
python -m compileall -q core app_pages streamlit_app.py
python tests/test_flujo.py
```

en Python 3.12 y 3.14. El flujo recomendado es **rama → PR → CI verde → merge a main → despliegue**.

## 9. ERP/WMS

No active una sincronización automática hasta conocer el mecanismo oficial de intercambio de AKT. Mantenga por ahora los cargues controlados de OC, MPS y foto ERP.
