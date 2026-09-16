# Guía de despliegue paso a paso

## Antes de empezar
Tenga a la mano: cuenta de GitHub, cuenta de Supabase y cuenta de Streamlit Cloud.

---

## Paso 1 — Base de datos en Supabase (5 min)

1. Entre a https://supabase.com y cree un proyecto nuevo.
   - **Name**: `akt-proveedores`
   - **Database Password**: genere una y **guárdela**, se necesita en el paso 3.
   - **Region**: la más cercana (por ejemplo `East US (North Virginia)`).
2. Espere a que el proyecto termine de aprovisionarse (~2 min).
3. Vaya a **Project Settings → Database → Connection string**.
4. Seleccione la pestaña **Session pooler** y copie la cadena. Se ve así:

   ```
   postgresql://postgres.abcdefghijklmnop:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```

5. Reemplace `[YOUR-PASSWORD]` por la contraseña del paso 1. Esa cadena completa
   es su `DATABASE_URL`.

> **Importante**: use *Session pooler*, no *Direct connection*. La conexión
> directa es IPv6 y Streamlit Cloud no la alcanza.

---

## Paso 2 — Repositorio en GitHub (3 min)

Desde la carpeta del proyecto:

```bash
git init
git add .
git commit -m "App proveedores de transformación AKT MOTOS"
git branch -M main
git remote add origin https://github.com/SU_USUARIO/akt-proveedores.git
git push -u origin main
```

Si el repo es nuevo, créelo primero en https://github.com/new (puede ser privado;
Streamlit Cloud lo lee con su permiso de GitHub).

---

## Paso 3 — Publicar en Streamlit Cloud (5 min)

1. Entre a https://share.streamlit.io e inicie sesión con GitHub.
2. **Create app → Deploy a public app from a repo**.
3. Complete:
   - **Repository**: `SU_USUARIO/akt-proveedores`
   - **Branch**: `main`
   - **Main file path**: `streamlit_app.py`
4. Abra **Advanced settings → Secrets** y pegue:

   ```toml
   DATABASE_URL = "postgresql://postgres.abcdefghijklmnop:SU_PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
   ```

5. **Deploy**. El primer arranque tarda ~1 minuto extra porque crea las 24 tablas
   y siembra 8.204 artículos y 37.843 líneas de BOM.

---

## Paso 4 — Primeros pasos dentro de la app

1. Entre con `inventarios@akt.com` / `akt2026`.
2. **Maestros → Usuarios**: cambie las contraseñas de los 4 usuarios demo y cree
   los usuarios reales. A cada usuario con rol `PROVEEDOR` asígnele su proveedor.
3. **Maestros → Ubicaciones**: asigne las ubicaciones a cada proveedor. Esto es
   lo que permite validar que las columnas *Desde* y *Hasta* del BIN a BIN
   correspondan al proveedor logueado.
4. **Maestros → Proveedores**: ajuste el margen de tolerancia de avería pactado
   en cada acuerdo comercial.
5. **Maestros → Órdenes de compra**: cargue las OC abiertas (tiene plantilla).
6. **Maestros → Datos de prueba**: si quiere ver la app con movimiento, genere el
   escenario de prueba; después use *Reiniciar movimientos* para dejarla limpia
   antes de operar de verdad.

---

## Mantenimiento

- **Actualizar la app**: haga `git push`; Streamlit Cloud redespliega solo.
- **Respaldo**: Supabase → Database → Backups (diario en el plan Free).
- **Ver los datos**: Supabase → Table Editor.
- **Reiniciar todo**: borre las tablas en Supabase y reinicie la app; se vuelven
  a crear y sembrar.

## Problemas comunes

| Síntoma | Causa | Solución |
|---|---|---|
| `could not translate host name` | Usó la conexión directa (IPv6) | Cambie a la cadena del **Session pooler** |
| `password authentication failed` | No reemplazó `[YOUR-PASSWORD]` | Ponga la contraseña real del proyecto |
| La app arranca vacía | No encontró los CSV | Verifique que la carpeta `data/` esté en el repo |
| Tarda en el primer arranque | Está sembrando el BOM | Normal, ~45 segundos, solo la primera vez |
