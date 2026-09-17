"""Utilidades de interfaz: cargue masivo, plantillas, tablas editables, estilos."""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from core.db import session_scope
from core.services import SEMAFORO_COLOR

COLORES_ESTADO = {
    "CERRADA": "#16a34a", "NOVEDAD": "#e02424", "BORRADOR": "#6b7280",
    "SELLADO": "#2563eb", "PENDIENTE_MATCH": "#f59e0b", "ABIERTA": "#f59e0b",
    "EN_COLA_INVENTARIOS": "#e02424", "PROGRAMADO": "#2563eb",
    "EN_PROCESO": "#f59e0b", "EJECUTADO": "#16a34a", "DESPACHADO": "#16a34a",
    "REGISTRADA": "#f59e0b", "AJUSTADO": "#16a34a", "REGISTRADO": "#16a34a",
}

CSS = """
<style>
  .block-container {padding-top: 2.2rem; max-width: 1500px;}
  .akt-header {background: linear-gradient(90deg,#0f172a 0%,#1e3a5f 100%);
     color:#fff; padding:14px 20px; border-radius:10px; margin-bottom:16px;}
  .akt-header h1 {margin:0; font-size:1.25rem; letter-spacing:.3px;}
  .akt-header p {margin:2px 0 0; font-size:.82rem; opacity:.85;}
  .pill {display:inline-block; padding:2px 10px; border-radius:999px;
     font-size:.72rem; font-weight:700; color:#fff;}
  .kpi {background:#fff; border:1px solid #e5e7eb; border-left:5px solid #1e3a5f;
     border-radius:10px; padding:12px 16px;}
  .kpi .v {font-size:1.55rem; font-weight:700; color:#0f172a; line-height:1.1;}
  .kpi .l {font-size:.74rem; color:#6b7280; text-transform:uppercase;
     letter-spacing:.5px; margin-bottom:2px;}
  .kpi .s {font-size:.72rem; color:#9ca3af;}
  div[data-testid="stMetricValue"] {font-size:1.4rem;}
</style>
"""


def aplicar_estilos():
    st.markdown(CSS, unsafe_allow_html=True)


def encabezado(titulo, subtitulo=""):
    st.markdown(
        f'<div class="akt-header"><h1>{titulo}</h1><p>{subtitulo}</p></div>',
        unsafe_allow_html=True)


def pill(texto, color=None):
    c = color or COLORES_ESTADO.get(str(texto).upper(), "#6b7280")
    return f'<span class="pill" style="background:{c}">{texto}</span>'


def kpi(col, label, valor, sub="", color="#1e3a5f"):
    col.markdown(
        f'<div class="kpi" style="border-left-color:{color}">'
        f'<div class="l">{label}</div><div class="v">{valor}</div>'
        f'<div class="s">{sub}</div></div>', unsafe_allow_html=True)


def color_semaforo(valor):
    return SEMAFORO_COLOR.get(str(valor), "#6b7280")


# ---------------------------------------------------------------- plantillas

PLANTILLAS = {
    "articulos": ["codigo", "descripcion", "familia", "desc_familia",
                  "grupo_articulo", "categoria", "color", "tipo", "unidad", "activo"],
    "bom": ["articulo_transformado", "desc_transformado", "componente",
            "desc_componente", "cantidad", "secuencia", "proveedor_codigo", "activo"],
    "ubicaciones": ["codigo", "un", "rol", "proveedor_codigo", "cerrada",
                    "inspeccion", "restringida", "activo"],
    "proveedores": ["codigo", "nombre", "nit", "tolerancia_averia_pct", "activo"],
    "ordenes_compra": ["numero", "proveedor_codigo", "articulo", "cantidad",
                       "estado", "fecha"],
    "recibo_lineas": ["articulo", "descripcion", "cantidad_documento",
                      "cantidad_fisica", "lote", "serial", "ubicacion_desde",
                      "ubicacion_hasta"],
    "despacho_lineas": ["articulo", "descripcion", "cantidad", "ubicacion"],
    "mps": ["articulo", "cantidad", "fecha_programada", "ubicacion_destino"],
    "conteos": ["articulo", "ubicacion", "fecha_programada", "prioridad"],
    "foto_erp": ["articulo", "ubicacion", "cantidad_erp"],
    "ddmrp": ["articulo", "consumo_promedio_dia", "lead_time_dias",
              "factor_variabilidad", "factor_lead_time", "moq"],
}

EJEMPLOS = {
    "articulos": [["7705946983051", "Carenaje Tras Der V2 CR4 Mp", "1631000003",
                   "Materia Prima terceros", "203002", "Ckd", "", "CRUDO", "UND", "TRUE"]],
    "bom": [["7700149031844", "Cubie Tras Der Gr/M INT CR4 TA", "7705946983051",
             "Carenaje Tras Der V2 CR4 Mp", "1", "40", "VDR0013714", "TRUE"]],
    "ubicaciones": [["TPROC-INTE-AKT", "MOTOS", "DESTINO", "VDR0013714",
                     "FALSE", "FALSE", "TRUE", "TRUE"]],
    "proveedores": [["VDR0013714", "Grupo DL Plus S.A.S", "900123456", "1.0", "TRUE"]],
    "ordenes_compra": [["OC-2026-0001", "VDR0013714", "7705946983051", "500",
                        "ABIERTA", "2026-09-16"]],
    "recibo_lineas": [["7705946983051", "Carenaje Tras Der V2 CR4 Mp", "179", "179",
                       "", "", "WSERE-PSER", "WSERE-WSER-VIPI-NTAR-TE"]],
    "despacho_lineas": [["7700149031844", "Cubie Tras Der Gr/M INT CR4 TA", "50",
                         "TPROC-INTE-AKT"]],
    "mps": [["7700149031844", "100", "2026-09-20", "TPROC-INTE-AKT"]],
    "conteos": [["7705946983051", "TPROC-INTE-AKT", "2026-09-20", "1"]],
    "foto_erp": [["7705946983051", "TPROC-INTE-AKT", "179"]],
    "ddmrp": [["7705946983051", "25", "15", "0.5", "0.5", "100"]],
}


def plantilla_csv(nombre: str) -> bytes:
    cols = PLANTILLAS[nombre]
    df = pd.DataFrame(EJEMPLOS.get(nombre, []), columns=cols)
    if df.empty:
        df = pd.DataFrame(columns=cols)
    return df.to_csv(index=False).encode("utf-8-sig")


def plantilla_xlsx(nombre: str) -> bytes:
    cols = PLANTILLAS[nombre]
    df = pd.DataFrame(EJEMPLOS.get(nombre, []), columns=cols)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="plantilla")
    return buf.getvalue()


def boton_plantilla(nombre: str, etiqueta="Descargar plantilla de ejemplo", key=None):
    c1, c2 = st.columns(2)
    c1.download_button(f"{etiqueta} (CSV)", plantilla_csv(nombre),
                       file_name=f"plantilla_{nombre}.csv", mime="text/csv",
                       key=f"csv_{key or nombre}", use_container_width=True)
    c2.download_button(f"{etiqueta} (Excel)", plantilla_xlsx(nombre),
                       file_name=f"plantilla_{nombre}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key=f"xls_{key or nombre}", use_container_width=True)


def leer_archivo_tabular(archivo) -> pd.DataFrame | None:
    if archivo is None:
        return None
    nombre = archivo.name.lower()
    try:
        if nombre.endswith((".xlsx", ".xls")):
            return pd.read_excel(archivo, dtype=str)
        for sep in (",", ";", "\t"):
            archivo.seek(0)
            df = pd.read_csv(archivo, dtype=str, sep=sep)
            if df.shape[1] > 1 or sep == "\t":
                return df
        archivo.seek(0)
        return pd.read_csv(archivo, dtype=str)
    except Exception as e:
        st.error(f"No se pudo leer el archivo: {e}")
        return None


def exportar_excel(dfs: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for hoja, df in dfs.items():
            df.to_excel(w, index=False, sheet_name=hoja[:31])
    return buf.getvalue()


# ------------------------------------------------------------------ cachés

@st.cache_data(ttl=300, show_spinner=False)
def catalogo_articulos(limite=5000) -> pd.DataFrame:
    from core.models import Articulo
    with session_scope() as s:
        rows = s.query(Articulo.codigo, Articulo.descripcion, Articulo.tipo).filter(
            Articulo.activo.is_(True)).order_by(Articulo.codigo).limit(limite).all()
    return pd.DataFrame(rows, columns=["codigo", "descripcion", "tipo"])


@st.cache_data(ttl=300, show_spinner=False)
def catalogo_ubicaciones(proveedor_id=None, incluir_restringidas=False) -> list[str]:
    from core.models import Ubicacion
    with session_scope() as s:
        q = s.query(Ubicacion.codigo).filter(
            Ubicacion.activo.is_(True),
            Ubicacion.cerrada.is_(False))
        if not incluir_restringidas:
            q = q.filter(Ubicacion.restringida.is_(False))
        if proveedor_id:
            q = q.filter((Ubicacion.proveedor_id == proveedor_id)
                         | (Ubicacion.proveedor_id.is_(None)))
        return [c for (c,) in q.order_by(Ubicacion.codigo).all()]


@st.cache_data(ttl=300, show_spinner=False)
def catalogo_proveedores() -> pd.DataFrame:
    from core.models import Proveedor
    with session_scope() as s:
        rows = s.query(Proveedor.id, Proveedor.codigo, Proveedor.nombre).filter(
            Proveedor.activo.is_(True)).order_by(Proveedor.nombre).all()
    return pd.DataFrame(rows, columns=["id", "codigo", "nombre"])


def limpiar_cache():
    catalogo_articulos.clear()
    catalogo_ubicaciones.clear()
    catalogo_proveedores.clear()


def selector_proveedor(user, label="Proveedor", key=None):
    """Un PROVEEDOR queda fijo en el suyo; los demás roles eligen."""
    if user["rol"] == "PROVEEDOR":
        st.caption(f"{label}: **{user['proveedor_nombre']}** ({user['proveedor_codigo']})")
        return user["proveedor_id"]
    df = catalogo_proveedores()
    if df.empty:
        st.warning("No hay proveedores activos.")
        return None
    opciones = {f"{r.nombre} ({r.codigo})": int(r.id) for r in df.itertuples()}
    sel = st.selectbox(label, list(opciones), key=key)
    return opciones[sel]


def ok(msg):
    st.success(msg, icon="✅")


def err(msg):
    st.error(msg, icon="⚠️")
