"""Reconocimiento documental de PDF/imágenes con costo mínimo.

1) PDFs con texto: extracción local con pypdf.
2) PDF escaneado / imagen: Gemini multimodal si GEMINI_API_KEY está configurada.
3) Sin IA: heurística local y revisión humana obligatoria.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import urllib.request


def _setting(name: str, default=None):
    try:
        import streamlit as st
        value = st.secrets.get(name)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.environ.get(name, default)


def ai_configured() -> bool:
    return bool(_setting("GEMINI_API_KEY"))


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception:
        return ""


def _clean_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _gemini_extract(data: bytes, mime: str) -> dict:
    key = _setting("GEMINI_API_KEY")
    model = _setting("GEMINI_MODEL", "gemini-2.5-flash-lite")
    if not key:
        raise RuntimeError("GEMINI_API_KEY no configurada.")
    prompt = """Analiza este documento logístico de AKT MOTOS. Devuelve SOLO JSON válido.
No inventes datos. Si no ves un valor, usa null. Para cada dato incluye confianza 0..1.
Estructura:
{
 "tipo_documento":"BIN_A_BIN|FACTURA|REGISTRO|DESCONOCIDO",
 "referencia":null,
 "fecha":"YYYY-MM-DD"|null,
 "proveedor_origen":{"nombre":null,"nit":null,"confianza":0},
 "ordenes_compra":[],
 "lineas":[
   {"articulo":null,"descripcion":null,"cantidad_documento":null,
    "cantidad_fisica":null,"ubicacion_desde":null,"ubicacion_hasta":null,
    "orden_compra":null,"confianza":0}
 ],
 "confianza_global":0,
 "observaciones":[]
}
Prioriza códigos de artículo, cantidades, número de factura/BIN, OC y ubicaciones.
Diferencia claramente cantidad documental de cantidad física; si el documento solo contiene
una cantidad, colócala en cantidad_documento y deja cantidad_fisica null."""
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}}
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    text = payload["candidates"][0]["content"]["parts"][0]["text"]
    return _clean_json(text)


def _heuristic(text: str) -> dict:
    compact = re.sub(r"[ \t]+", " ", text or "")
    ref = None
    for pat in (
        r"\b(BIN[-\s]?[A-Z0-9]{4,})\b",
        r"\b(?:FACTURA|FACT|FV)\s*[:#-]?\s*([A-Z0-9.-]{4,})\b",
        r"\b(OC[-\s]?[A-Z0-9]{4,})\b",
    ):
        mm = re.search(pat, compact, re.I)
        if mm:
            ref = mm.group(1)
            break
    nitm = re.search(r"\bNIT\s*[:#-]?\s*([0-9.\-]{7,20})", compact, re.I)
    datem = re.search(r"\b(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)\b", compact)
    fecha = None
    if datem:
        try:
            fecha = dt.date(int(datem.group(1)), int(datem.group(2)), int(datem.group(3))).isoformat()
        except ValueError:
            pass
    ocs = sorted(set(re.findall(r"\bOC[-\s]?[A-Z0-9]{4,}\b", compact, re.I)))
    lineas = []
    # Heurística conservadora: códigos largos + último número de la línea como cantidad.
    for raw in (text or "").splitlines():
        code = re.search(r"\b([A-Z0-9][A-Z0-9._-]{5,24})\b", raw, re.I)
        nums = re.findall(r"(?<![A-Z0-9])([0-9]+(?:[.,][0-9]+)?)(?![A-Z0-9])", raw, re.I)
        if code and nums:
            cod = code.group(1)
            if cod.upper().startswith(("NIT", "FACTURA", "FECHA")):
                continue
            try:
                qty = float(nums[-1].replace(",", "."))
            except ValueError:
                continue
            lineas.append({
                "articulo": cod, "descripcion": raw.strip()[:300],
                "cantidad_documento": qty, "cantidad_fisica": None,
                "ubicacion_desde": None, "ubicacion_hasta": None,
                "orden_compra": ocs[0] if len(ocs) == 1 else None,
                "confianza": 0.45,
            })
    return {
        "tipo_documento": "BIN_A_BIN" if ref and str(ref).upper().startswith("BIN") else "DESCONOCIDO",
        "referencia": ref, "fecha": fecha,
        "proveedor_origen": {"nombre": None, "nit": nitm.group(1) if nitm else None,
                             "confianza": 0.55 if nitm else 0},
        "ordenes_compra": ocs, "lineas": lineas[:200],
        "confianza_global": 0.55 if ref else 0.35,
        "observaciones": ["Extracción heurística local: requiere validación humana."],
    }


def analizar_documento(nombre: str, data: bytes, mime: str | None = None) -> dict:
    mime = mime or ("application/pdf" if str(nombre).lower().endswith(".pdf")
                    else "application/octet-stream")
    text = _pdf_text(data) if mime == "application/pdf" else ""
    # Un PDF con suficiente texto se puede intentar interpretar localmente primero.
    local = _heuristic(text) if text else None
    use_ai = ai_configured() and (not text or not local or local["confianza_global"] < 0.75)
    if use_ai:
        try:
            out = _gemini_extract(data, mime)
            out["fuente"] = "GEMINI"
            conf = float(out.get("confianza_global") or 0)
            out["requiere_revision"] = conf < 0.85
            return out
        except Exception as exc:
            if local:
                local.setdefault("observaciones", []).append(f"IA no disponible: {exc}")
            else:
                return {"tipo_documento": "DESCONOCIDO", "referencia": None, "fecha": None,
                        "proveedor_origen": {}, "ordenes_compra": [], "lineas": [],
                        "confianza_global": 0, "fuente": "ERROR", "requiere_revision": True,
                        "observaciones": [f"No fue posible reconocer el documento: {exc}"]}
    out = local or {"tipo_documento": "DESCONOCIDO", "referencia": None, "fecha": None,
                    "proveedor_origen": {}, "ordenes_compra": [], "lineas": [],
                    "confianza_global": 0, "observaciones": []}
    out["fuente"] = "PDF_TEXTO" if text else "SIN_MOTOR_OCR"
    out["requiere_revision"] = True
    if not text and not ai_configured():
        out["observaciones"].append(
            "Imagen/PDF escaneado: configure GEMINI_API_KEY para reconocimiento multimodal.")
    return out
