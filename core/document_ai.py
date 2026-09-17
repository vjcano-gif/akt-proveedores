"""Extracción documental local: PDF nativo + OCR de imágenes/PDF escaneado.

No envía documentos a servicios externos. RapidOCR/ONNX se usa localmente.
La extracción estructurada es heurística y siempre debe ser confirmada por usuario.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, asdict


@dataclass
class Campo:
    valor: str | float | None
    confianza: float
    fuente: str = ""


def _ocr_image(data: bytes) -> tuple[str, float]:
    try:
        from rapidocr import RapidOCR
        engine = RapidOCR()
        res = engine(data)
        txts = list(res.txts or [])
        scores = [float(x) for x in (res.scores or [])]
        return "\n".join(txts), (sum(scores) / len(scores) if scores else 0.0)
    except Exception:
        return "", 0.0


def extraer_texto(nombre: str, data: bytes, mime: str | None = None) -> tuple[str, float, str]:
    name = (nombre or "").lower()
    mime = mime or ""
    if name.endswith(".pdf") or mime == "application/pdf":
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        partes = []
        for p in doc:
            t = (p.get_text("text") or "").strip()
            if t:
                partes.append(t)
        texto = "\n".join(partes).strip()
        if len(texto) >= 40:
            return texto, 0.98, "PDF_TEXT"
        # PDF escaneado: renderizar cada página y OCR.
        ocr_partes, confs = [], []
        for p in doc:
            pix = p.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            t, cf = _ocr_image(pix.tobytes("png"))
            if t:
                ocr_partes.append(t)
                confs.append(cf)
        return "\n".join(ocr_partes), (sum(confs) / len(confs) if confs else 0.0), "OCR"
    if mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        t, cf = _ocr_image(data)
        return t, cf, "OCR"
    return "", 0.0, "NO_SOPORTADO"


def _primero(patrones, texto, flags=re.I):
    for p in patrones:
        m = re.search(p, texto, flags)
        if m:
            return m.group(1).strip()
    return None


def _num(s):
    if s is None:
        return None
    s = re.sub(r"[^0-9,.-]", "", str(s))
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def estructurar(texto: str, confianza_texto: float = 0.8) -> dict:
    """Extrae campos comunes sin inventar valores."""
    t = texto or ""
    ref = _primero([
        r"(?:factura|fv|documento|bin(?:\s*a\s*bin)?)\s*(?:no\.?|nro\.?|#|:)?\s*([A-Z0-9._/-]{4,})",
        r"\b(BIN[- ]?\d{4,})\b",
    ], t)
    oc = _primero([
        r"(?:orden\s+de\s+compra|oc|pedido)\s*(?:no\.?|nro\.?|#|:)?\s*([A-Z0-9._/-]{4,})"
    ], t)
    nit = _primero([r"(?:NIT|N\.I\.T\.)\s*[:#-]?\s*([0-9.-]{6,})"], t)
    fecha = _primero([
        r"(?:fecha|date)\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"\b(\d{4}-\d{2}-\d{2})\b",
    ], t)

    lineas = []
    # Heurística: código de artículo + descripción opcional + cantidad al final.
    for linea in t.splitlines():
        limpio = " ".join(linea.split())
        m = re.match(r"^([A-Z0-9][A-Z0-9._/-]{2,})\s+(.{0,100}?)\s+([0-9][0-9.,]*)$", limpio, re.I)
        if not m:
            continue
        cod, desc, cant = m.groups()
        n = _num(cant)
        if n is None:
            continue
        # Evita capturar NIT/fechas/documentos obvios como artículos.
        if cod.upper() in {"NIT", "TOTAL", "SUBTOTAL", "IVA", "FECHA"}:
            continue
        lineas.append({"articulo": cod.strip(), "descripcion": desc.strip(),
                       "cantidad_documento": n, "cantidad_fisica": n,
                       "confianza": round(min(confianza_texto, 0.9), 3)})

    return {
        "referencia": asdict(Campo(ref, confianza_texto if ref else 0.0, "texto")),
        "orden_compra": asdict(Campo(oc, confianza_texto if oc else 0.0, "texto")),
        "nit": asdict(Campo(nit, confianza_texto if nit else 0.0, "texto")),
        "fecha": asdict(Campo(fecha, confianza_texto if fecha else 0.0, "texto")),
        "lineas": lineas,
        "texto": t,
    }


def analizar_documento(nombre: str, data: bytes, mime: str | None = None) -> dict:
    texto, cf, metodo = extraer_texto(nombre, data, mime)
    out = estructurar(texto, cf)
    out["metodo"] = metodo
    out["confianza_texto"] = round(cf, 3)
    out["requiere_revision"] = cf < 0.85 or not out["lineas"]
    return out
