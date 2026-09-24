"""Extracción documental: PDF nativo + OCR local con doble motor.

Orden de lectura:
1) PDF con capa de texto -> PyMuPDF.
2) Imagen/PDF escaneado -> RapidOCR.
3) Si RapidOCR falla o no detecta texto -> Tesseract (fallback).

La extracción nunca afecta inventario directamente: siempre requiere confirmación humana.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from functools import lru_cache


@dataclass
class Campo:
    valor: str | float | None
    confianza: float
    fuente: str = ""


def _openai_api_key() -> str:
    """Obtiene OPENAI_API_KEY desde entorno o Streamlit secrets."""
    key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    try:
        import streamlit as st
        key = str(st.secrets.get("OPENAI_API_KEY", "") or "").strip()
    except Exception:
        key = ""
    return key


def _imagen_api_optimizada(
        data: bytes, mime: str, max_side: int = 2048) -> tuple[bytes, str]:
    """Crea una copia de trabajo para Vision; el original NO se modifica.

    La API aplica sus propios límites de resolución en detalle high. Enviar
    una foto de 8-20 MP completa aumenta transferencia/base64 y memoria.
    Para lectura se reduce proporcionalmente a 2048 px de lado mayor,
    conservando el archivo original para trazabilidad y descarga.
    """
    if not data or not str(mime or "").startswith("image/"):
        return data, mime
    try:
        import cv2
        img = _decode_image(data)
        h, w = img.shape[:2]
        mayor = max(h, w)
        if mayor <= max_side:
            return data, mime
        escala = float(max_side) / float(mayor)
        nuevo = cv2.resize(
            img,
            (max(1, int(round(w * escala))), max(1, int(round(h * escala)))),
            interpolation=cv2.INTER_AREA,
        )
        ok, enc = cv2.imencode(
            ".jpg", nuevo, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if ok:
            return enc.tobytes(), "image/jpeg"
    except Exception:
        pass
    return data, mime


def _openai_document_ai(nombre: str, data: bytes, mime: str | None = None) -> dict | None:
    """Lee fotos de documentos con GPT-5 nano + Structured Outputs.

    OpenAI se usa como motor principal para fotografías. Si no hay API key,
    si el archivo es PDF, o si la API falla, el flujo continúa con los
    motores de respaldo existentes.
    """
    key = _openai_api_key()
    if not key or not data:
        return None

    import requests

    name = (nombre or "").lower()
    mime = (mime or "").strip().lower()
    if not mime:
        if name.endswith(".png"):
            mime = "image/png"
        elif name.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.endswith(".webp"):
            mime = "image/webp"
        elif name.endswith(".pdf"):
            mime = "application/pdf"

    # Por ahora OpenAI Vision se activa para imágenes; PDF conserva el lector
    # nativo/Mistral como respaldo porque los BIN PDF ya tienen parser X/Y.
    if not mime.startswith("image/"):
        return None

    data_api, mime_api = _imagen_api_optimizada(data, mime)
    encoded = base64.b64encode(data_api).decode("ascii")
    data_url = f"data:{mime_api};base64,{encoded}"
    model = str(
        os.getenv("OPENAI_VISION_MODEL")
        or "gpt-5-nano-2025-08-07"
    ).strip()

    prompt = (
        "Lee este documento de recibo con máxima precisión visual. "
        "NO inventes, NO completes y NO corrijas códigos ni cantidades. "
        "Si un carácter o número no es legible devuelve null en ese campo. "
        "Clasifica tipo_documento como BIN_A_BIN, FACTURA u OTRO. "
        "Si es BIN_A_BIN, extrae exactamente una fila por cada renglón visible "
        "de la tabla. El campo codigo debe contener exactamente el código impreso. "
        "El campo cantidad debe tomarse EXCLUSIVAMENTE de la columna Cantidad; "
        "nunca uses números de la descripción, serial, lote, DESDE o HASTA. "
        "Conserva Serial, Lote, DESDE y HASTA tal como aparecen. "
        "FECHAS: si es BIN_A_BIN pueden aparecer dos fechas distintas. "
        "fecha_transaccion debe ser EXCLUSIVAMENTE el valor rotulado "
        "«Fecha Transacción» (por ejemplo 2026-08-14-07.11.30); "
        "fecha_documento debe ser el valor rotulado simplemente «Fecha» "
        "(normalmente en el encabezado superior derecho). "
        "NO sustituyas Fecha Transacción por Fecha. Para el proceso de recibo "
        "la fecha prioritaria del BIN es Fecha Transacción. "
        "No omitas una fila legible y no agregues filas que no existan."
    )

    fila_schema = {
        "type": "object",
        "properties": {
            "proveedor": {"type": ["string", "null"]},
            "codigo": {"type": ["string", "null"]},
            "descripcion": {"type": ["string", "null"]},
            "cantidad": {"type": ["number", "null"]},
            "serial": {"type": ["string", "null"]},
            "lote": {"type": ["string", "null"]},
            "desde": {"type": ["string", "null"]},
            "hasta": {"type": ["string", "null"]},
        },
        "required": [
            "proveedor", "codigo", "descripcion", "cantidad",
            "serial", "lote", "desde", "hasta",
        ],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "tipo_documento": {
                "type": "string",
                "enum": ["BIN_A_BIN", "FACTURA", "OTRO"],
            },
            "referencia": {"type": ["string", "null"]},
            "fecha_transaccion": {"type": ["string", "null"]},
            "fecha_documento": {"type": ["string", "null"]},
            "filas": {"type": "array", "items": fila_schema},
        },
        "required": [
            "tipo_documento", "referencia",
            "fecha_transaccion", "fecha_documento", "filas"
        ],
        "additionalProperties": False,
    }

    payload = {
        "model": model,
        "store": False,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "high",
                },
            ],
        }],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "documento_recibo",
                "description": "Extracción exacta de BIN a BIN o factura.",
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": 4000,
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(5, 40),
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "texto": "",
            "filas": [],
            "fuente": "OPENAI_GPT5_NANO",
            "modelo": model,
        }

    output_text = ""
    for item in raw.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                output_text += str(part["text"])

    if not output_text.strip():
        return {
            "ok": False,
            "error": "OpenAI no devolvió salida estructurada.",
            "texto": "",
            "filas": [],
            "fuente": "OPENAI_GPT5_NANO",
            "modelo": model,
        }

    try:
        anot = json.loads(output_text)
    except Exception as e:
        return {
            "ok": False,
            "error": f"JSON inválido de OpenAI: {type(e).__name__}: {e}",
            "texto": output_text,
            "filas": [],
            "fuente": "OPENAI_GPT5_NANO",
            "modelo": model,
        }

    filas = []
    for row in anot.get("filas") or []:
        if not isinstance(row, dict):
            continue
        filas.append({
            "proveedor": str(row.get("proveedor") or "").strip(),
            "codigo": str(row.get("codigo") or "").strip(),
            "descripcion": str(row.get("descripcion") or "").strip(),
            "cantidad": _num(row.get("cantidad")),
            "serial": str(row.get("serial") or "").strip(),
            "lote": str(row.get("lote") or "").strip(),
            "desde": str(row.get("desde") or "").strip(),
            "hasta": str(row.get("hasta") or "").strip(),
        })

    usage = raw.get("usage") or {}
    return {
        "ok": True,
        # El JSON estructurado es también el texto auditable de esta lectura.
        "texto": output_text,
        "tipo_documento": str(
            anot.get("tipo_documento") or "").strip().upper(),
        "referencia": str(anot.get("referencia") or "").strip(),
        "fecha_transaccion": str(
            anot.get("fecha_transaccion") or "").strip(),
        "fecha_documento": str(
            anot.get("fecha_documento") or "").strip(),
        "fecha": str(
            anot.get("fecha_transaccion")
            or anot.get("fecha_documento")
            or ""
        ).strip(),
        "filas": filas,
        "modelo": str(raw.get("model") or model),
        "usage_info": usage,
        "fuente": "OPENAI_GPT5_NANO",
    }


def _mistral_api_key() -> str:
    """Obtiene la API key sin exponerla ni guardarla en el repositorio."""
    key = str(os.getenv("MISTRAL_API_KEY") or "").strip()
    if key:
        return key
    try:
        import streamlit as st
        key = str(st.secrets.get("MISTRAL_API_KEY", "") or "").strip()
    except Exception:
        key = ""
    return key


def _mistral_document_ai(nombre: str, data: bytes, mime: str | None = None) -> dict | None:
    """Extrae un documento con Mistral OCR/Document AI si hay API key.

    Devuelve texto OCR y una anotación JSON estricta. La IA NO se usa para
    inventar referencias: las referencias extraídas se validan posteriormente
    contra el maestro local de artículos antes de mostrarse al usuario.
    """
    key = _mistral_api_key()
    if not key or not data:
        return None

    import requests

    name = (nombre or "").lower()
    mime = (mime or "").strip().lower()
    if not mime:
        if name.endswith(".pdf"):
            mime = "application/pdf"
        elif name.endswith(".png"):
            mime = "image/png"
        else:
            mime = "image/jpeg"

    encoded = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"

    if mime == "application/pdf" or name.endswith(".pdf"):
        document = {
            "type": "document_url",
            "document_url": data_url,
        }
    else:
        document = {
            "type": "image_url",
            "image_url": data_url,
        }

    prompt = (
        "Extrae SOLO lo que está visualmente escrito en el documento. "
        "No completes, no corrijas y no inventes códigos, cantidades, "
        "ubicaciones ni proveedores. Si un valor no es legible usa null. "
        "Clasifica tipo_documento únicamente como BIN_A_BIN, FACTURA u OTRO. "
        "Para BIN_A_BIN extrae una fila por cada renglón visible de la tabla "
        "con proveedor, codigo, descripcion, cantidad, serial, lote, desde y hasta. "
        "El codigo debe conservar exactamente todos sus dígitos. "
        "La cantidad debe corresponder exclusivamente a la columna Cantidad, "
        "nunca a números incluidos dentro de la descripción ni de Desde/Hasta. "
        "FECHAS: en BIN_A_BIN extrae por separado fecha_transaccion del rótulo "
        "«Fecha Transacción» y fecha_documento del rótulo simple «Fecha». "
        "No confundas ambas; para recibo la prioritaria es fecha_transaccion. "
        "Devuelve JSON con las claves: tipo_documento, referencia, "
        "fecha_transaccion, fecha_documento, filas. "
        "filas debe ser una lista de objetos con proveedor, codigo, descripcion, "
        "cantidad, serial, lote, desde, hasta."
    )

    payload = {
        "model": "mistral-ocr-latest",
        "document": document,
        "table_format": "markdown",
        "include_blocks": True,
        "confidence_scores_granularity": "word",
        "document_annotation_format": {"type": "json_object"},
        "document_annotation_prompt": prompt,
    }

    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/ocr",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "texto": "",
            "filas": [],
        }

    paginas = raw.get("pages") or []
    texto = "\n".join(
        str(p.get("markdown") or "").strip()
        for p in paginas if str(p.get("markdown") or "").strip()
    ).strip()

    anot_raw = raw.get("document_annotation")
    anot = {}
    if isinstance(anot_raw, dict):
        anot = anot_raw
    elif isinstance(anot_raw, str) and anot_raw.strip():
        try:
            anot = json.loads(anot_raw)
        except Exception:
            anot = {}

    filas = []
    for row in anot.get("filas") or []:
        if not isinstance(row, dict):
            continue
        codigo = str(row.get("codigo") or "").strip()
        cantidad = _num(row.get("cantidad"))
        filas.append({
            "proveedor": str(row.get("proveedor") or "").strip(),
            "codigo": codigo,
            "descripcion": str(row.get("descripcion") or "").strip(),
            "cantidad": cantidad,
            "serial": str(row.get("serial") or "").strip(),
            "lote": str(row.get("lote") or "").strip(),
            "desde": str(row.get("desde") or "").strip(),
            "hasta": str(row.get("hasta") or "").strip(),
        })

    return {
        "ok": True,
        "texto": texto,
        "tipo_documento": str(anot.get("tipo_documento") or "").strip().upper(),
        "referencia": str(anot.get("referencia") or "").strip(),
        "fecha_transaccion": str(
            anot.get("fecha_transaccion") or "").strip(),
        "fecha_documento": str(
            anot.get("fecha_documento") or "").strip(),
        "fecha": str(
            anot.get("fecha_transaccion")
            or anot.get("fecha_documento")
            or ""
        ).strip(),
        "filas": filas,
        "modelo": str(raw.get("model") or "mistral-ocr-latest"),
        "usage_info": raw.get("usage_info") or {},
    }


def _decode_image(data: bytes):
    """Convierte bytes de PNG/JPG a ndarray BGR validado."""
    import cv2
    import numpy as np

    if not data:
        raise ValueError("El archivo de imagen está vacío.")
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or getattr(img, "size", 0) == 0:
        raise ValueError("No se pudo decodificar la imagen. Verifique que sea PNG o JPG válido.")
    return img


def _preprocesar_para_ocr(img):
    """Genera variantes útiles para documentos fotografiados o escaneados."""
    import cv2

    variantes = [("ORIGINAL", img)]
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Aumenta contraste local sin destruir texto tenue.
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        contrast = clahe.apply(gray)
        contrast_bgr = cv2.cvtColor(contrast, cv2.COLOR_GRAY2BGR)
        variantes.append(("CONTRASTE", contrast_bgr))

        # Binarización adaptativa, útil para fotos con iluminación desigual.
        bw = cv2.adaptiveThreshold(
            contrast, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 15)
        variantes.append(("BINARIO", cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)))
    except Exception:
        pass
    return variantes


@lru_cache(maxsize=1)
def _rapid_engine():
    from rapidocr import RapidOCR
    return RapidOCR()


def _texto_rapid_ordenado(res, txts_override=None) -> tuple[str, list[float]]:
    """Reconstruye filas de una tabla usando las coordenadas detectadas por RapidOCR.

    RapidOCR suele detectar cada celda por separado. Si solo concatenamos las
    detecciones con saltos de línea, se pierde la relación:
    artículo | descripción | cantidad. Aquí agrupamos cajas por coordenada Y y
    ordenamos cada fila de izquierda a derecha.
    """
    txts = list(txts_override if txts_override is not None
                else (getattr(res, "txts", None) or []))
    raw_scores = list(getattr(res, "scores", None) or [])
    scores = []
    for x in raw_scores:
        try:
            scores.append(float(x))
        except (TypeError, ValueError):
            scores.append(0.0)

    boxes = getattr(res, "boxes", None)
    if boxes is None or len(boxes) != len(txts):
        texto = "\n".join(str(t).strip() for t in txts if str(t).strip()).strip()
        return texto, scores

    detecciones = []
    for i, (txt, box) in enumerate(zip(txts, boxes)):
        txt = str(txt or "").strip()
        if not txt:
            continue
        try:
            pts = [[float(p[0]), float(p[1])] for p in box]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x = min(xs)
            cy = (min(ys) + max(ys)) / 2.0
            h = max(1.0, max(ys) - min(ys))
        except Exception:
            # Si una caja particular viene corrupta, conserva el texto.
            x, cy, h = 0.0, float(i) * 1000.0, 10.0
        detecciones.append({
            "texto": txt, "x": x, "cy": cy, "h": h,
            "score": scores[i] if i < len(scores) else 0.0,
        })

    if not detecciones:
        return "", scores

    detecciones.sort(key=lambda d: (d["cy"], d["x"]))
    filas = []
    for det in detecciones:
        mejor = None
        mejor_dist = None
        for fila in filas:
            dist = abs(det["cy"] - fila["cy"])
            tolerancia = max(10.0, 0.65 * max(det["h"], fila["h"]))
            if dist <= tolerancia and (mejor_dist is None or dist < mejor_dist):
                mejor, mejor_dist = fila, dist
        if mejor is None:
            filas.append({
                "cy": det["cy"], "h": det["h"], "items": [det],
            })
        else:
            mejor["items"].append(det)
            n = len(mejor["items"])
            mejor["cy"] = ((mejor["cy"] * (n - 1)) + det["cy"]) / n
            mejor["h"] = max(mejor["h"], det["h"])

    filas.sort(key=lambda f: f["cy"])
    lineas = []
    confs = []
    for fila in filas:
        items = sorted(fila["items"], key=lambda d: d["x"])
        linea = " ".join(d["texto"] for d in items if d["texto"]).strip()
        if linea:
            lineas.append(linea)
            confs.extend(d["score"] for d in items if d["score"] >= 0)

    return "\n".join(lineas).strip(), confs


def _refinar_celdas_numericas(img, res):
    """Relee celdas numéricas cortas con Tesseract ampliado."""
    txts = list(getattr(res, "txts", None) or [])
    boxes = getattr(res, "boxes", None)
    if boxes is None or len(boxes) != len(txts):
        return txts
    try:
        import cv2
        import pytesseract
    except Exception:
        return txts

    refinados = list(txts)
    h_img, w_img = img.shape[:2]
    for i, (txt, box) in enumerate(zip(txts, boxes)):
        raw = str(txt or "").strip()
        if not re.fullmatch(r"\d{1,6}", raw):
            continue
        try:
            pts = [[int(float(p[0])), int(float(p[1]))] for p in box]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            x1, x2 = max(0, min(xs)-8), min(w_img, max(xs)+8)
            y1, y2 = max(0, min(ys)-5), min(h_img, max(ys)+5)
            crop = img[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            scale = 5 if gray.shape[0] < 28 else 3
            enlarged = cv2.resize(gray, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_CUBIC)
            enlarged = cv2.copyMakeBorder(
                enlarged, 20, 20, 30, 30, cv2.BORDER_CONSTANT, value=255)
            _, bw = cv2.threshold(
                enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            reread = pytesseract.image_to_string(
                bw, config="--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789")
            reread = re.sub(r"\D", "", reread or "")
            if reread and len(reread) <= 6 and (len(reread) > len(raw) or reread == raw):
                refinados[i] = reread
        except Exception:
            continue
    return refinados


def _refinar_cantidades_bin(img, res, txts):
    """Relee exclusivamente la columna Cantidad de un BIN con varios votos OCR.

    RapidOCR puede ubicar correctamente una celda pero confundir dígitos de igual
    longitud (p.ej. 98 -> 38). El refinador genérico anterior no reemplazaba ese
    caso. Aquí localizamos la columna por los encabezados CANTIDAD/SERIAL y
    re-leemos solo esas celdas con Tesseract en varias variantes de alto aumento.
    """
    txts = list(txts or [])
    boxes = getattr(res, "boxes", None)
    if boxes is None or len(boxes) != len(txts):
        return txts

    try:
        import cv2
        import pytesseract
        from collections import Counter
    except Exception:
        return txts

    dets = []
    for i, (txt, box) in enumerate(zip(txts, boxes)):
        try:
            pts = [[float(p[0]), float(p[1])] for p in box]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            dets.append({
                "i": i,
                "txt": str(txt or "").strip(),
                "x0": min(xs), "x1": max(xs),
                "y0": min(ys), "y1": max(ys),
                "cx": (min(xs) + max(xs)) / 2.0,
                "cy": (min(ys) + max(ys)) / 2.0,
                "h": max(1.0, max(ys) - min(ys)),
                "w": max(1.0, max(xs) - min(xs)),
            })
        except Exception:
            continue

    headers = {}
    for d in dets:
        n = _norm_cabecera(d["txt"])
        if n == "CANTIDAD" and "cantidad" not in headers:
            headers["cantidad"] = d
        elif n == "SERIAL" and "serial" not in headers:
            headers["serial"] = d
        elif n == "LOTE" and "lote" not in headers:
            headers["lote"] = d

    if "cantidad" not in headers or "serial" not in headers:
        return txts

    x_qty = headers["cantidad"]["x0"]
    x_serial = headers["serial"]["x0"]
    header_y = max(d["cy"] for d in headers.values())
    ancho_col = max(20.0, x_serial - x_qty)
    h_img, w_img = img.shape[:2]
    scores = list(getattr(res, "scores", None) or [])

    candidatos = [
        d for d in dets
        if d["cy"] > header_y + 3
        and (x_qty - 0.45 * ancho_col) <= d["cx"] < (x_serial - 1.0)
        and 1 <= len(re.sub(r"\s+", "", d["txt"])) <= 8
    ]

    # Solo relanza Tesseract sobre celdas sospechosas. Las cantidades que
    # RapidOCR ya leyó como entero con confianza razonable se conservan; el
    # texto Tesseract de la fila completa sigue siendo la fuente autoritativa
    # cuando existe. Esto evita 8 procesos Tesseract por cada fila del BIN.
    sospechosos = []
    for d in candidatos:
        original = re.sub(r"\D", "", d["txt"])
        try:
            score_rapid = (
                float(scores[d["i"]]) if d["i"] < len(scores) else 0.0
            )
        except Exception:
            score_rapid = 0.0
        if not original or score_rapid < 0.72:
            sospechosos.append(d)

    for d in sospechosos:
        try:
            pad_x = max(10, int(d["w"] * 0.55))
            pad_y = max(7, int(d["h"] * 0.55))
            x1 = max(0, int(d["x0"]) - pad_x)
            x2 = min(w_img, int(d["x1"]) + pad_x)
            y1 = max(0, int(d["y0"]) - pad_y)
            y2 = min(h_img, int(d["y1"]) + pad_y)
            crop = img[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            escala = max(4, min(8, int(round(120 / max(12, gray.shape[0])))))
            enlarged = cv2.resize(
                gray, None, fx=escala, fy=escala,
                interpolation=cv2.INTER_CUBIC)
            enlarged = cv2.copyMakeBorder(
                enlarged, 28, 28, 45, 45,
                cv2.BORDER_CONSTANT, value=255)

            clahe = cv2.createCLAHE(clipLimit=2.3, tileGridSize=(8, 8))
            contrast = clahe.apply(enlarged)
            _, otsu = cv2.threshold(
                contrast, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            adaptive = cv2.adaptiveThreshold(
                contrast, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 11)

            lecturas = []
            # Dos lecturas son suficientes para rescatar una celda dañada
            # (p.ej. "uaz" -> 142) y reducen drásticamente la latencia frente
            # a las 8 combinaciones anteriores.
            for variante in (contrast, otsu):
                raw = pytesseract.image_to_string(
                    variante,
                    config=(
                        "--oem 3 --psm 7 "
                        "-c tessedit_char_whitelist=0123456789"
                    ),
                )
                val = re.sub(r"\D", "", raw or "")
                if 1 <= len(val) <= 6:
                    lecturas.append(val)

            if not lecturas:
                continue

            votos = Counter(lecturas)
            mejor, n_votos = votos.most_common(1)[0]
            original = re.sub(r"\D", "", d["txt"])
            try:
                score_rapid = float(scores[d["i"]]) if d["i"] < len(scores) else 0.0
            except Exception:
                score_rapid = 0.0

            # Reemplaza si hay consenso real entre preprocesamientos/PSM.
            # También permite rescatar una lectura cuando RapidOCR no produjo
            # un entero limpio o reportó baja confianza.
            if n_votos >= 2:
                txts[d["i"]] = mejor
            elif (not original or score_rapid < 0.72) and mejor:
                txts[d["i"]] = mejor
        except Exception:
            continue

    return txts


def _ocr_rapid(img) -> tuple[str, float]:
    """OCR principal conservando estructura espacial de tablas.

    No relanza Tesseract por cada número: esa validación se hace únicamente
    sobre celdas sospechosas de la columna Cantidad en el parser BIN.
    """
    engine = _rapid_engine()
    res = engine(img)
    texto, scores = _texto_rapid_ordenado(res)
    confianza = sum(scores) / len(scores) if scores else 0.0
    return texto, confianza


def _norm_cabecera(txt: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(txt or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z]", "", s.upper())


def _extraer_bin_columnas_resultado(res, txts=None) -> list[dict]:
    """Convierte detecciones RapidOCR en filas BIN usando geometría X/Y."""
    txts = list(txts if txts is not None else (getattr(res, "txts", None) or []))
    boxes = getattr(res, "boxes", None)
    if boxes is None or len(boxes) != len(txts):
        return []

    dets = []
    for txt, box in zip(txts, boxes):
        txt = str(txt or "").strip()
        if not txt:
            continue
        try:
            pts = [[float(p[0]), float(p[1])] for p in box]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            dets.append({
                "txt": txt,
                "x0": x0, "x1": x1,
                "y0": y0, "y1": y1,
                "cx": (x0+x1)/2,
                "cy": (y0+y1)/2,
                "w": max(1.0, x1-x0),
                "h": max(1.0, y1-y0),
            })
        except Exception:
            continue

    # Algunos ERP/PDF dibujan el mismo texto dos veces (por ejemplo para
    # simular negrilla) y PyMuPDF devuelve ambas capas. Se eliminan únicamente
    # detecciones del mismo texto que se superponen casi por completo. Esto no
    # elimina valores legítimos repetidos como "1 1 1", porque ocupan X distintas.
    depuradas = []
    for d in sorted(dets, key=lambda x: (x["cy"], x["x0"])):
        repetida = False
        txt_norm = " ".join(d["txt"].upper().split())
        for k in reversed(depuradas):
            if abs(d["cy"] - k["cy"]) > max(d["h"], k["h"]) * 1.2:
                break
            if txt_norm != " ".join(k["txt"].upper().split()):
                continue
            ix = max(0.0, min(d["x1"], k["x1"]) - max(d["x0"], k["x0"]))
            iy = max(0.0, min(d["y1"], k["y1"]) - max(d["y0"], k["y0"]))
            inter = ix * iy
            area_min = min(d["w"] * d["h"], k["w"] * k["h"])
            solape = inter / area_min if area_min > 0 else 0.0
            centros_casi_iguales = (
                abs(d["cx"] - k["cx"]) <= max(1.5, 0.12 * min(d["w"], k["w"]))
                and abs(d["cy"] - k["cy"]) <= max(1.0, 0.20 * min(d["h"], k["h"]))
            )
            if solape >= 0.72 or centros_casi_iguales:
                repetida = True
                break
        if not repetida:
            depuradas.append(d)
    dets = depuradas

    aliases = {
        "proveedor": {"PROVEEDOR"},
        "codigo": {"CODIGO"},
        "descripcion": {"DESCRIPCION"},
        "cantidad": {"CANTIDAD"},
        "serial": {"SERIAL"},
        "lote": {"LOTE"},
        "desde": {"DESDE"},
        "hasta": {"HASTA"},
    }
    headers = {}
    for d in dets:
        n = _norm_cabecera(d["txt"])
        for key, vals in aliases.items():
            if n in vals and key not in headers:
                headers[key] = d

    requeridas = {"codigo", "cantidad", "desde", "hasta"}
    if not requeridas.issubset(headers):
        return []

    # Usa el inicio X de cada encabezado como ancla de columna. Con texto
    # palabra-a-palabra (PDF nativo), el punto medio entre encabezados podía
    # mandar los últimos tokens de DESDE a HASTA. El ancla izquierda mantiene
    # cada palabra en su columna real hasta que comienza la siguiente.
    columnas = sorted(
        [(k, v["x0"]) for k, v in headers.items()],
        key=lambda x: x[1])
    header_y = max(headers[k]["cy"] for k in headers)

    def columna_de_det(d):
        # Se asigna a la última columna cuyo inicio está a la izquierda del
        # centro de la palabra/celda. Un pequeño margen absorbe desalineaciones.
        x = d["cx"] + max(1.0, 0.08 * d["w"])
        idx = 0
        for i, (_, inicio_x) in enumerate(columnas):
            if inicio_x <= x:
                idx = i
            else:
                break
        return columnas[idx][0]

    datos = [d for d in dets if d["cy"] > header_y + 4]
    datos.sort(key=lambda d: (d["cy"], d["cx"]))
    filas = []
    for d in datos:
        target = None
        for fila in filas:
            tol = max(9.0, 0.65 * max(d["h"], fila["h"]))
            if abs(d["cy"] - fila["cy"]) <= tol:
                target = fila
                break
        if target is None:
            target = {"cy": d["cy"], "h": d["h"], "items": []}
            filas.append(target)
        target["items"].append(d)

    out = []
    for fila in filas:
        items_fila = sorted(fila["items"], key=lambda x: x["cx"])
        celdas = {}
        for d in items_fila:
            col = columna_de_det(d)
            celdas.setdefault(col, []).append(d["txt"])
        celdas = {k: " ".join(v).strip() for k, v in celdas.items()}

        codigo = re.sub(r"\s+", "", celdas.get("codigo", ""))
        if not re.fullmatch(r"[A-Z0-9._/-]{5,60}", codigo, re.I):
            continue

        # Cantidad BIN: prioriza la relación semántica
        #   <CANTIDAD>  NONE  NONE
        # sobre la mera frontera geométrica de la columna. En fotos con
        # perspectiva, una cantidad puede quedar unos píxeles a la izquierda
        # del encabezado "Cantidad" y terminar clasificada como descripción.
        qty = None
        marcadores = {"NONE", "N/A", "NA", "N.A."}
        for j in range(len(items_fila) - 2):
            raw_qty = re.sub(r"[^0-9.,-]", "", str(items_fila[j]["txt"] or ""))
            s1 = str(items_fila[j + 1]["txt"] or "").strip().upper()
            s2 = str(items_fila[j + 2]["txt"] or "").strip().upper()
            if not raw_qty or s1 not in marcadores or s2 not in marcadores:
                continue
            val = _num(raw_qty)
            if val is None or val <= 0:
                continue
            # Debe caer después del inicio de Cantidad y antes de Serial,
            # con margen suficiente para inclinación/perspectiva.
            if items_fila[j]["cx"] < headers["cantidad"]["x0"] - 0.55 * max(
                    20.0, headers["serial"]["x0"] - headers["cantidad"]["x0"]):
                continue
            if items_fila[j]["cx"] >= headers["serial"]["x0"] + 4:
                continue
            qty = val
            break

        if qty is None or qty <= 0:
            qty = _num(celdas.get("cantidad"))
        if qty is None or qty <= 0:
            continue

        out.append({
            "articulo": codigo,
            "descripcion_ocr": celdas.get("descripcion", ""),
            "cantidad_documento": float(qty),
            "cantidad_fisica": 0.0,
            "serial": celdas.get("serial", ""),
            "lote": celdas.get("lote", ""),
            "ubicacion_desde": celdas.get("desde", "").strip().upper(),
            "ubicacion_hasta": celdas.get("hasta", "").strip().upper(),
            "proveedor_bin": celdas.get("proveedor", ""),
            "fuente": "BIN_ESPACIAL",
        })
    return out


def extraer_bin_columnas_imagen(data: bytes) -> list[dict]:
    """Extrae filas BIN reutilizando una sola pasada RapidOCR."""
    try:
        orientados, res = _rapid_resultado_bin(data)
        img = _decode_image(orientados)
        txts = list(getattr(res, "txts", None) or [])
        txts = _refinar_cantidades_bin(img, res, txts)
        return _extraer_bin_columnas_resultado(res, txts)
    except Exception:
        return []


def _pdf_word_obj(w):
    """Normaliza una palabra PyMuPDF a una estructura geométrica."""
    if len(w) < 5:
        return None
    x0, y0, x1, y1, txt = w[:5]
    txt = str(txt or "").strip()
    if not txt:
        return None
    x0, y0, x1, y1 = map(float, (x0, y0, x1, y1))
    return {
        "txt": txt,
        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
        "cx": (x0 + x1) / 2.0,
        "cy": (y0 + y1) / 2.0,
        "w": max(0.5, x1 - x0),
        "h": max(0.5, y1 - y0),
    }


def _dedupe_pdf_words(items: list[dict]) -> list[dict]:
    """Elimina capas duplicadas del PDF sin colapsar repeticiones reales.

    Algunos reportes ERP imprimen el mismo glifo/texto dos veces con un
    desplazamiento de 1-3 puntos. Las repeticiones reales (p.ej. 1 1 1)
    quedan separadas horizontalmente y por tanto se conservan.
    """
    out = []
    for d in sorted(items, key=lambda z: (z["cy"], z["x0"], z["x1"])):
        norm = " ".join(d["txt"].upper().split())
        dup = False
        for k in reversed(out):
            # Ya estamos en otra línea.
            if d["cy"] - k["cy"] > max(4.0, 0.8 * max(d["h"], k["h"])):
                break
            if norm != " ".join(k["txt"].upper().split()):
                continue

            dx = abs(d["cx"] - k["cx"])
            dy = abs(d["cy"] - k["cy"])
            ix = max(0.0, min(d["x1"], k["x1"]) - max(d["x0"], k["x0"]))
            iy = max(0.0, min(d["y1"], k["y1"]) - max(d["y0"], k["y0"]))
            inter = ix * iy
            amin = min(d["w"] * d["h"], k["w"] * k["h"])
            overlap = inter / amin if amin > 0 else 0.0

            # Umbral deliberadamente más amplio para caracteres estrechos como 1.
            if (
                (dx <= max(3.2, 0.45 * min(d["w"], k["w"]))
                 and dy <= max(2.4, 0.35 * min(d["h"], k["h"])))
                or overlap >= 0.42
            ):
                dup = True
                break
        if not dup:
            out.append(d)
    return out


def _agrupar_pdf_filas(items: list[dict], min_y: float) -> list[list[dict]]:
    """Agrupa palabras de la tabla en filas visuales por coordenada Y."""
    datos = [d for d in items if d["cy"] > min_y]
    datos.sort(key=lambda d: (d["cy"], d["x0"]))
    filas: list[dict] = []
    for d in datos:
        best = None
        best_dist = None
        for f in filas:
            dist = abs(d["cy"] - f["cy"])
            tol = max(2.8, 0.42 * max(d["h"], f["h"]))
            if dist <= tol and (best_dist is None or dist < best_dist):
                best, best_dist = f, dist
        if best is None:
            filas.append({"cy": d["cy"], "h": d["h"], "items": [d]})
        else:
            best["items"].append(d)
            n = len(best["items"])
            best["cy"] = ((best["cy"] * (n - 1)) + d["cy"]) / n
            best["h"] = max(best["h"], d["h"])
    filas.sort(key=lambda f: f["cy"])
    return [sorted(f["items"], key=lambda d: d["x0"]) for f in filas]


def _texto_celda_pdf(items: list[dict]) -> str:
    items = _dedupe_pdf_words(items)
    return " ".join(d["txt"] for d in sorted(items, key=lambda z: z["x0"])).strip()


def _chars_pdf_pagina(page) -> list[dict]:
    """Aplana get_text('rawdict') a una lista de caracteres con su bbox.

    Se usa solo para reconstruir columnas cuando dos celdas contiguas quedan
    pegadas en una misma palabra/space de PyMuPDF (ver _codigo_por_columna).
    """
    out = []
    try:
        raw = page.get_text("rawdict")
    except Exception:
        return out
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = str(ch.get("c") or "")
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = ch.get("bbox", (0, 0, 0, 0))
                    out.append({
                        "c": c,
                        "x0": float(x0), "x1": float(x1),
                        "cy": (float(y0) + float(y1)) / 2.0,
                    })
    return out


def _codigo_por_columna(chars: list[dict], word: dict, x_codigo: float) -> str:
    """Recupera el código de artículo cuando viene pegado al proveedor.

    Algunos ERP no dejan espacio entre el código del proveedor y el código
    de artículo cuando ambos caen en la misma celda visual (p.ej.
    "SANYANG IN-0017700149422819"). PyMuPDF entonces entrega esa celda como
    una sola palabra/token, contaminando el código real con el sufijo del
    proveedor ("IN-001"). La columna Código, sin embargo, SIEMPRE empieza en
    la misma X que su encabezado ("Código"): se reconstruye el código
    tomando, por posición de carácter, solo lo que cae a la derecha de esa
    X, en vez de adivinar por longitud o forma del texto.
    """
    if word["x0"] >= x_codigo - 1.0:
        # La palabra ya empieza en la columna Código: no hay contaminación.
        return re.sub(r"\s+", "", word["txt"])
    tol_y = max(2.0, word["h"] * 0.6)
    seleccion = [
        ch for ch in chars
        if abs(ch["cy"] - word["cy"]) <= tol_y
        and ch["x0"] >= x_codigo - 0.5
        and ch["x0"] < word["x1"] + 0.5
    ]
    if not seleccion:
        return re.sub(r"\s+", "", word["txt"])
    seleccion.sort(key=lambda z: z["x0"])
    return "".join(ch["c"] for ch in seleccion)


def extraer_bin_columnas_pdf(data: bytes) -> list[dict]:
    """Extrae BIN nativo por regiones geométricas de columna.

    A diferencia del parser genérico palabra-a-palabra, aquí los encabezados
    definen regiones amplias. Esto soporta cantidades alineadas a la derecha y
    PDFs con capas de texto duplicadas.
    """
    import fitz

    if not data:
        return []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return []

    aliases = {
        "proveedor": {"PROVEEDOR"},
        "codigo": {"CODIGO"},
        "descripcion": {"DESCRIPCION"},
        "cantidad": {"CANTIDAD"},
        "serial": {"SERIAL"},
        "lote": {"LOTE"},
        "desde": {"DESDE"},
        "hasta": {"HASTA"},
    }

    out = []
    for page in doc:
        try:
            raw_words = page.get_text("words", sort=True) or []
        except Exception:
            continue

        words_raw = [_pdf_word_obj(w) for w in raw_words]
        words_raw = [w for w in words_raw if w]
        words = _dedupe_pdf_words(words_raw)
        if not words:
            continue

        headers = {}
        for w in words:
            n = _norm_cabecera(w["txt"])
            for key, vals in aliases.items():
                if n in vals and key not in headers:
                    headers[key] = w

        required = {"codigo", "descripcion", "cantidad", "serial", "lote", "desde", "hasta"}
        if not required.issubset(headers):
            continue

        # Las regiones se anclan en el inicio de los encabezados, pero se
        # expanden hacia la izquierda para tolerar valores centrados/derecha.
        x_codigo = headers["codigo"]["x0"]
        x_desc = headers["descripcion"]["x0"]
        x_qty = headers["cantidad"]["x0"]
        x_serial = headers["serial"]["x0"]
        x_lote = headers["lote"]["x0"]
        x_desde = headers["desde"]["x0"]
        x_hasta = headers["hasta"]["x0"]

        # Límites escogidos para no depender del ancho del encabezado.
        # Cantidad recibe margen izquierdo amplio porque suele estar alineada
        # a la derecha; DESDE/HASTA usan el inicio del encabezado siguiente.
        bounds = {
            "proveedor": (0.0, x_codigo - 6.0),
            "codigo": (x_codigo - 18.0, x_desc - 8.0),
            "descripcion": (x_desc - 8.0, x_qty - 28.0),
            "cantidad": (x_qty - 32.0, x_serial - 8.0),
            "serial": (x_serial - 8.0, x_lote - 8.0),
            "lote": (x_lote - 8.0, x_desde - 16.0),
            "desde": (x_desde - 16.0, x_hasta - 16.0),
            "hasta": (x_hasta - 16.0, float(page.rect.width) + 1.0),
        }

        header_y = max(headers[k]["cy"] for k in required)

        # Para PDF, cada CÓDIGO es el ancla vertical de una fila. Esto evita
        # depender de que Cantidad/Serial/Desde/Hasta tengan exactamente el
        # mismo Y que Código/Descripción; varios ERP desplazan celdas unos
        # puntos y el agrupador por baseline perdía cantidades desde la fila 2.
        chars_pagina = _chars_pdf_pagina(page)
        x_codigo_hdr = headers["codigo"]["x0"]

        code_left, code_right = bounds["codigo"]
        anchors = []
        for w in words:
            if w["cy"] <= header_y + 2.0:
                continue
            if not (code_left <= w["cx"] < code_right):
                continue
            codigo_cand = _codigo_por_columna(chars_pagina, w, x_codigo_hdr)
            if not re.fullmatch(r"[A-Z0-9._/-]{5,60}", codigo_cand, re.I):
                continue
            if not re.search(r"\d", codigo_cand):
                continue
            anchors.append({**w, "codigo": codigo_cand})

        # Dedup adicional de códigos por capas PDF.
        anchors.sort(key=lambda a: (a["cy"], a["x0"]))
        anchors_unicos = []
        for a in anchors:
            if anchors_unicos:
                prev = anchors_unicos[-1]
                if (a["codigo"].upper() == prev["codigo"].upper()
                        and abs(a["cy"] - prev["cy"]) <= max(5.0, a["h"] * 0.7)):
                    continue
            anchors_unicos.append(a)
        anchors = anchors_unicos

        if not anchors:
            continue

        # Espaciado típico entre renglones, usado para cerrar la primera/última
        # banda sin absorber encabezados o pie de página.
        diffs = [
            anchors[i+1]["cy"] - anchors[i]["cy"]
            for i in range(len(anchors)-1)
            if anchors[i+1]["cy"] - anchors[i]["cy"] > 2.0
        ]
        if diffs:
            diffs_ord = sorted(diffs)
            paso = diffs_ord[len(diffs_ord)//2]
        else:
            paso = max(12.0, anchors[0]["h"] * 1.8)

        # Prelectura independiente de la columna CANTIDAD.
        #
        # IMPORTANTE: usa words_raw y NO la lista global deduplicada. Dos filas
        # consecutivas pueden tener exactamente la misma cantidad (p.ej. 288 y
        # 288) y, si sus baselines vienen desplazados, una deduplicación global
        # puede confundirlas como una sola capa repetida.
        qty_y_min = max(header_y + 2.0, anchors[0]["cy"] - paso * 0.95)
        qty_y_max = min(float(page.rect.height), anchors[-1]["cy"] + paso * 0.95)
        qty_candidates_raw = [
            w for w in words_raw
            if qty_y_min <= w["cy"] <= qty_y_max
            and (x_qty - 90.0) <= w["cx"] < (x_serial + 10.0)
            and re.fullmatch(r"\d+(?:[.,]\d+)?", str(w["txt"] or "").strip())
        ]

        # Cada candidato se asigna PRIMERO a la fila cuyo código está más cerca
        # verticalmente. Solo después se eliminan las capas duplicadas dentro de
        # esa fila. Así dos cantidades iguales de filas distintas nunca se
        # colapsan entre sí.
        qty_por_fila = [[] for _ in anchors]
        for w in qty_candidates_raw:
            distancias = [abs(w["cy"] - a["cy"]) for a in anchors]
            if not distancias:
                continue
            j = min(range(len(distancias)), key=lambda k: distancias[k])
            if distancias[j] <= paso * 0.62:
                qty_por_fila[j].append(w)

        qty_por_indice = [None] * len(anchors)
        for j, items_raw in enumerate(qty_por_fila):
            if not items_raw:
                continue
            items = _dedupe_pdf_words(items_raw)
            toks = [
                re.sub(r"[^0-9,.-]", "", str(w["txt"] or ""))
                for w in sorted(items, key=lambda z: z["x0"])
            ]
            toks = [t for t in toks if t]
            if not toks:
                continue

            val = None
            if len(set(toks)) == 1:
                val = _num(toks[0])
            else:
                # Si el PDF partió 288 en "2" + "88", recompone la celda.
                unido = "".join(
                    t.replace(".", "").replace(",", "") for t in toks)
                if unido.isdigit() and len(unido) <= 9:
                    val = _num(unido)
                if val is None or val <= 0:
                    vals = [_num(t) for t in toks]
                    vals = [v for v in vals if v is not None and v > 0]
                    if vals:
                        val = max(vals)

            if val is not None and val > 0:
                qty_por_indice[j] = float(val)

        # Respaldo ordinal: si alguna fila quedó vacía pero el PDF sí contiene
        # exactamente una cantidad lógica por código, alinea por orden visual.
        if any(v is None for v in qty_por_indice):
            qty_dedup_por_fila = []
            for j, items_raw in enumerate(qty_por_fila):
                items = _dedupe_pdf_words(items_raw)
                vals = []
                for w in items:
                    v = _num(w["txt"])
                    if v is not None and v > 0:
                        vals.append(float(v))
                if vals:
                    qty_dedup_por_fila.append((j, vals[0]))
            for j, v in qty_dedup_por_fila:
                if qty_por_indice[j] is None:
                    qty_por_indice[j] = v

        for i, anchor in enumerate(anchors):
            if i == 0:
                top = max(header_y + 2.0, anchor["cy"] - paso * 0.52)
            else:
                top = (anchors[i-1]["cy"] + anchor["cy"]) / 2.0

            if i + 1 < len(anchors):
                bottom = (anchor["cy"] + anchors[i+1]["cy"]) / 2.0
            else:
                bottom = min(float(page.rect.height), anchor["cy"] + paso * 0.58)

            # Banda completa de la referencia; tolera desplazamientos verticales
            # entre las diferentes celdas de la misma fila.
            row = [w for w in words if top <= w["cy"] < bottom]

            cells = {}
            for key, (left, right) in bounds.items():
                cell_words = [w for w in row if left <= w["cx"] < right]
                cells[key] = _texto_celda_pdf(cell_words)

            # Descarta anclas espurias (p.ej. "Cantidad Total 812.00" en el
            # pie de página) que caen dentro del rango X de la columna
            # Código pero no son una fila real: una fila BIN legítima
            # siempre trae descripción y/o serial/lote.
            if not cells.get("descripcion") and not cells.get("serial") and not cells.get("lote"):
                continue

            codigo = anchor["codigo"]

            qty_words = [
                w for w in row
                if bounds["cantidad"][0] <= w["cx"] < bounds["cantidad"][1]
                and re.search(r"\d", w["txt"])
            ]
            qty_words = _dedupe_pdf_words(qty_words)
            qty_tokens = [
                re.sub(r"[^0-9,.-]", "", w["txt"])
                for w in sorted(qty_words, key=lambda z: z["x0"])
            ]
            qty_tokens = [q for q in qty_tokens if q]

            qty = qty_por_indice[i]
            if (qty is None or qty <= 0) and qty_tokens:
                # Capas duplicadas pueden producir 156,156. Si todos los tokens
                # son iguales se toma una sola vez. Si el PDF partió un número
                # en glifos/tokens (1 + 56), se recompone por orden X.
                if len(set(qty_tokens)) == 1:
                    qty = _num(qty_tokens[0])
                else:
                    concatenado = "".join(q.replace(".", "").replace(",", "")
                                         for q in qty_tokens)
                    if concatenado.isdigit() and len(concatenado) <= 9:
                        qty = _num(concatenado)
                    if qty is None or qty <= 0:
                        candidatos = [
                            (_num(q), q) for q in qty_tokens
                            if _num(q) is not None and _num(q) > 0
                        ]
                        if candidatos:
                            qty = max(v for v, _ in candidatos)

            if qty is None or qty <= 0:
                # Rescate semántico para reportes BIN de ancho fijo: la cantidad
                # aparece inmediatamente antes de SERIAL y LOTE. En algunos PDFs
                # PyMuPDF desplaza horizontalmente la cantidad de las filas 2+
                # fuera del rango calculado por el encabezado, aunque visualmente
                # esté en la columna correcta.
                #
                # Ejemplo real:
                #   ... Base Sillin RX Mp 156 NONE NONE WSERE ...
                ordenados = sorted(_dedupe_pdf_words(row), key=lambda z: z["x0"])
                marcadores_vacios = {"NONE", "N/A", "NA", "N.A.", "-"}
                for j in range(0, max(0, len(ordenados) - 2)):
                    raw_qty = str(ordenados[j]["txt"] or "").strip()
                    s1 = str(ordenados[j + 1]["txt"] or "").strip().upper()
                    s2 = str(ordenados[j + 2]["txt"] or "").strip().upper()
                    if not re.fullmatch(r"\d+(?:[.,]\d+)?", raw_qty):
                        continue
                    if s1 not in marcadores_vacios or s2 not in marcadores_vacios:
                        continue
                    val = _num(raw_qty)
                    if val is None or val <= 0:
                        continue
                    # Debe quedar después del código y antes de DESDE para no
                    # confundir los dígitos de las ubicaciones con la cantidad.
                    if ordenados[j]["cx"] <= anchor["cx"]:
                        continue
                    if ordenados[j]["cx"] >= x_desde:
                        continue
                    qty = val
                    break

            if qty is None or qty <= 0:
                # Último rescate geométrico: acepta una franja algo más amplia.
                # El candidato más cercano al encabezado Cantidad gana.
                candidatos = []
                for w in row:
                    if not re.fullmatch(r"\d+(?:[.,]\d+)?", w["txt"]):
                        continue
                    if (x_qty - 75.0) <= w["cx"] < (x_serial + 8.0):
                        val = _num(w["txt"])
                        if val and val > 0:
                            candidatos.append((abs(w["cx"] - x_qty), val))
                if candidatos:
                    candidatos.sort(key=lambda z: z[0])
                    qty = candidatos[0][1]

            if qty is None or qty <= 0:
                # Conserva la referencia para que la interfaz pueda señalar qué
                # cantidad falta en vez de perder por completo la fila.
                qty = 0.0

            out.append({
                "articulo": codigo,
                "descripcion_ocr": cells.get("descripcion", ""),
                "cantidad_documento": float(qty),
                "cantidad_fisica": 0.0,
                "serial": cells.get("serial", ""),
                "lote": cells.get("lote", ""),
                "ubicacion_desde": cells.get("desde", "").strip().upper(),
                "ubicacion_hasta": cells.get("hasta", "").strip().upper(),
                "proveedor_bin": cells.get("proveedor", ""),
                "fuente": "BIN_PDF_CODIGO",
            })

    return out

def resumir_ubicaciones_bin(filas: list[dict]) -> dict:
    """Resume DESDE/HASTA a nivel de documento.

    En el proceso real todas las referencias del mismo BIN comparten el mismo
    DESDE y HASTA. Las filas se conservan solo como evidencia de OCR, pero la
    regla de negocio se valida una sola vez por documento.
    """
    def _vals(campo):
        vals = []
        for fila in filas or []:
            v = " ".join(str(fila.get(campo) or "").strip().upper().split())
            if v and v not in vals:
                vals.append(v)
        return vals

    desde = _vals("ubicacion_desde")
    hasta = _vals("ubicacion_hasta")
    return {
        "bin_ubicacion_desde": desde[0] if len(desde) == 1 else "",
        "bin_ubicacion_hasta": hasta[0] if len(hasta) == 1 else "",
        "bin_desde_valores": desde,
        "bin_hasta_valores": hasta,
        "bin_desde_consistente": len(desde) <= 1,
        "bin_hasta_consistente": len(hasta) <= 1,
    }

def _ocr_tesseract(img) -> tuple[str, float]:
    """Fallback gratuito mediante el binario Tesseract del servidor."""
    import cv2
    import pytesseract
    from pytesseract import Output

    disponibles = set(pytesseract.get_languages(config=""))
    if "spa" in disponibles and "eng" in disponibles:
        lang = "spa+eng"
    elif "spa" in disponibles:
        lang = "spa"
    elif "eng" in disponibles:
        lang = "eng"
    else:
        lang = None

    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    kwargs = {
        "config": "--oem 3 --psm 6",
        "output_type": Output.DICT,
    }
    if lang:
        kwargs["lang"] = lang
    datos = pytesseract.image_to_data(rgb, **kwargs)

    lineas = {}
    confs = []
    textos = datos.get("text", [])
    confidencias = datos.get("conf", [])
    bloques = datos.get("block_num", [0] * len(textos))
    parrafos = datos.get("par_num", [0] * len(textos))
    numeros_linea = datos.get("line_num", list(range(len(textos))))

    for txt, cf, bloque, parrafo, numero_linea in zip(
            textos, confidencias, bloques, parrafos, numeros_linea):
        txt = str(txt or "").strip()
        if not txt:
            continue
        try:
            score = float(cf)
        except (TypeError, ValueError):
            score = -1
        if score < 0:
            continue
        clave = (bloque, parrafo, numero_linea)
        lineas.setdefault(clave, []).append(txt)
        confs.append(score / 100.0)

    texto = "\n".join(" ".join(tokens) for tokens in lineas.values()).strip()
    confianza = sum(confs) / len(confs) if confs else 0.0
    return texto, confianza


def _score_bin_texto(texto: str) -> tuple[int, int]:
    """Puntúa qué tan fielmente un OCR conserva la estructura de un BIN.

    Para fotografías de BIN importa más recuperar filas completas
    Código -> Cantidad -> NONE -> NONE que una confianza OCR promedio alta.
    """
    t = str(texto or "")
    u = t.upper()
    score = 0
    if re.search(r"\bMOVIMIENTO\s+BIN\s+A\s+BIN\b", u):
        score += 20
    if all(x in u for x in ("CODIGO", "CANTIDAD", "SERIAL", "LOTE")):
        score += 10
    if "DESDE" in u and "HASTA" in u:
        score += 6

    filas = 0
    for raw in t.splitlines():
        linea = " ".join(raw.split())
        if not linea:
            continue
        # Código largo + cantidad seguida de Serial/Lote vacíos.
        if re.search(
            r"\b[A-Z0-9._/-]*\d{8,16}[A-Z0-9._/-]*\b"
            r".*?\s([0-9]{1,7}(?:[.,][0-9]+)?)\s+"
            r"(?:NONE|N/?A)\s+(?:NONE|N/?A)(?:\s+|$)",
            linea, re.I,
        ):
            filas += 1
    score += filas * 8
    return score, filas


def _img_reducida_ocr(img, max_side=1500):
    """Copia reducida usada solo para decidir orientación rápidamente."""
    try:
        import cv2
        h, w = img.shape[:2]
        mayor = max(h, w)
        if mayor <= max_side:
            return img
        s = float(max_side) / float(mayor)
        return cv2.resize(
            img,
            (max(1, int(round(w * s))), max(1, int(round(h * s)))),
            interpolation=cv2.INTER_AREA,
        )
    except Exception:
        return img


@lru_cache(maxsize=8)
def _orientar_bin_bytes(data: bytes) -> tuple[bytes, int, str, float]:
    """Corrige fotos BIN laterales sin ejecutar Tesseract full-res 4 veces.

    La orientación se decide sobre una copia reducida. Una vez elegido el giro,
    Tesseract se ejecuta una sola vez sobre la imagen orientada a resolución
    completa para conservar precisión.
    """
    import cv2

    img = _decode_image(data)
    mini = _img_reducida_ocr(img, 1500)

    candidatos = [(0, mini)]
    h, w = mini.shape[:2]
    candidatos.extend([
        (90, cv2.rotate(mini, cv2.ROTATE_90_COUNTERCLOCKWISE)),
        (-90, cv2.rotate(mini, cv2.ROTATE_90_CLOCKWISE)),
    ])
    if w >= h:
        candidatos.append((180, cv2.rotate(mini, cv2.ROTATE_180)))

    evaluados = []
    for grados, candidato in candidatos:
        try:
            texto, conf = _ocr_tesseract(candidato)
        except Exception:
            texto, conf = "", 0.0
        score, filas = _score_bin_texto(texto)
        evaluados.append((score, filas, float(conf or 0.0), grados))
        if grados == 0 and filas >= 2 and score >= 36:
            break

    mejores_bin = [x for x in evaluados if x[1] >= 2 and x[0] >= 36]
    if mejores_bin:
        _, _, _, grados = max(
            mejores_bin, key=lambda x: (x[0], x[1], x[2]))
    else:
        con_senal = [x for x in evaluados if x[0] >= 20]
        grados = (
            max(con_senal, key=lambda x: (x[0], x[1], x[2]))[3]
            if con_senal else 0
        )

    if grados == 90:
        elegido = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    elif grados == -90:
        elegido = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif grados == 180:
        elegido = cv2.rotate(img, cv2.ROTATE_180)
    else:
        elegido = img

    try:
        texto_final, conf_final = _ocr_tesseract(elegido)
    except Exception:
        texto_final, conf_final = "", 0.0

    ok, encoded = cv2.imencode(".png", elegido)
    if not ok:
        return data, 0, texto_final, conf_final
    return encoded.tobytes(), int(grados), texto_final, float(conf_final or 0.0)


@lru_cache(maxsize=6)
def _rapid_resultado_bin(data: bytes):
    """RapidOCR único y reutilizable para un BIN fotografiado."""
    orientados, _, _, _ = _orientar_bin_bytes(data)
    img = _decode_image(orientados)
    return orientados, _rapid_engine()(img)


def _ocr_image(data: bytes) -> tuple[str, float, str, str]:
    """Devuelve texto OCR escogiendo el motor que mejor conserva la tabla.

    En BIN fotografiados no se acepta RapidOCR solo por tener confianza alta:
    se compara contra Tesseract PSM 6 y gana el que recupere más filas completas
    Código/Cantidad/NONE/NONE. Esto evita códigos/cantidades plausibles pero
    erróneos generados por un OCR espacial.
    """
    diagnosticos = []
    try:
        orientados, grados, tess_pre_texto, tess_pre_conf = _orientar_bin_bytes(data)
        img = _decode_image(orientados)
        if grados:
            diagnosticos.append(
                f"Orientación automática BIN: {grados:+d}°")
    except Exception as e:
        try:
            img = _decode_image(data)
            grados, tess_pre_texto, tess_pre_conf = 0, "", 0.0
        except Exception:
            return "", 0.0, "OCR", f"Decodificación: {type(e).__name__}: {e}"

    rapid_texto, rapid_conf, rapid_var = "", 0.0, ""
    try:
        pre_score, _ = _score_bin_texto(tess_pre_texto)
        if pre_score >= 20:
            _, res_rapid = _rapid_resultado_bin(data)
            rapid_texto, scores = _texto_rapid_ordenado(res_rapid)
            rapid_conf = (
                sum(float(x) for x in scores) / len(scores)
                if scores else 0.0
            )
            rapid_var = "ORIGINAL"
        else:
            for nombre_var, variante in _preprocesar_para_ocr(img):
                texto, conf = _ocr_rapid(variante)
                if texto and (
                        len(texto) > len(rapid_texto)
                        or conf > rapid_conf + 0.08):
                    rapid_texto, rapid_conf, rapid_var = texto, conf, nombre_var
                if len(rapid_texto) >= 30 and rapid_conf >= 0.55:
                    break
        if not rapid_texto:
            diagnosticos.append("RapidOCR no detectó texto.")
    except Exception as e:
        diagnosticos.append(f"RapidOCR: {type(e).__name__}: {e}")

    tess_texto, tess_conf = tess_pre_texto, float(tess_pre_conf or 0.0)
    try:
        # La selección de orientación ya ejecutó Tesseract sobre la imagen
        # elegida. Solo relee si aquella etapa no produjo texto.
        if not tess_texto:
            tess_texto, tess_conf = _ocr_tesseract(img)
        if not tess_texto:
            variantes = _preprocesar_para_ocr(img)
            preferida = variantes[1][1] if len(variantes) > 1 else img
            tess_texto, tess_conf = _ocr_tesseract(preferida)
        if not tess_texto:
            diagnosticos.append("Tesseract no detectó texto.")
    except Exception as e:
        diagnosticos.append(f"Tesseract: {type(e).__name__}: {e}")

    rapid_score, rapid_filas = _score_bin_texto(rapid_texto)
    tess_score, tess_filas = _score_bin_texto(tess_texto)

    # Para el formato estándar "MOVIMIENTO BIN A BIN" fotografiado,
    # Tesseract PSM 6 es la fuente AUTORITATIVA de códigos y cantidades cuando
    # logra reconstruir al menos 2 filas completas Código/Cantidad/NONE/NONE.
    #
    # Motivo: RapidOCR puede mantener una confianza muy alta aunque confunda
    # dígitos de la misma forma (98->38, 120->170) o fragmente un código. Ese
    # error es especialmente peligroso porque parece un dato válido. Por tanto,
    # no se desempata por confianza cuando Tesseract ya reconstruyó la tabla.
    if tess_texto and tess_filas >= 2 and tess_score >= 36:
        return (
            tess_texto, tess_conf, "TESSERACT/BIN_ESTRUCTURADO",
            " | ".join(diagnosticos),
        )

    # Si Tesseract no alcanzó una tabla utilizable, RapidOCR queda como
    # respaldo estructurado. Nunca se combinan sus cantidades con las de una
    # tabla Tesseract ya validada.
    if rapid_texto and rapid_filas >= 2 and rapid_score >= 36:
        return (
            rapid_texto, rapid_conf,
            f"RAPIDOCR/{rapid_var}/BIN_ESTRUCTURADO",
            " | ".join(diagnosticos),
        )

    # Para otros documentos sí conserva el criterio general.
    if max(rapid_score, tess_score) >= 20:
        if (tess_score, tess_filas, tess_conf) > (
                rapid_score, rapid_filas, rapid_conf):
            return (
                tess_texto, tess_conf, "TESSERACT/BIN_ESTRUCTURADO",
                " | ".join(diagnosticos),
            )
        if rapid_texto:
            return (
                rapid_texto, rapid_conf, f"RAPIDOCR/{rapid_var}/BIN_ESTRUCTURADO",
                " | ".join(diagnosticos),
            )

    if rapid_texto:
        return rapid_texto, rapid_conf, f"RAPIDOCR/{rapid_var}", " | ".join(diagnosticos)
    if tess_texto:
        return tess_texto, tess_conf, "TESSERACT", " | ".join(diagnosticos)
    return "", 0.0, "OCR_FALLIDO", " | ".join(diagnosticos)


def extraer_texto(nombre: str, data: bytes, mime: str | None = None) -> tuple[str, float, str, str]:
    name = (nombre or "").lower()
    mime = mime or ""

    if name.endswith(".pdf") or mime == "application/pdf":
        import fitz

        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            return "", 0.0, "PDF_ERROR", f"PyMuPDF: {type(e).__name__}: {e}"

        partes = []
        for p in doc:
            t = (p.get_text("text") or "").strip()
            if t:
                partes.append(t)
        texto = "\n".join(partes).strip()
        if len(texto) >= 40:
            return texto, 0.98, "PDF_TEXT", ""

        # PDF escaneado: renderizar página por página y aplicar doble OCR.
        ocr_partes, confs, motores, diags = [], [], [], []
        for p in doc:
            pix = p.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False)
            t, cf, motor, diag = _ocr_image(pix.tobytes("png"))
            if t:
                ocr_partes.append(t)
                confs.append(cf)
                motores.append(motor)
            if diag:
                diags.append(diag)
        metodo = "+".join(sorted(set(motores))) if motores else "OCR_FALLIDO"
        return (
            "\n".join(ocr_partes),
            (sum(confs) / len(confs) if confs else 0.0),
            metodo,
            " | ".join(diags),
        )

    if mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        return _ocr_image(data)

    return "", 0.0, "NO_SOPORTADO", "Tipo de archivo no soportado para lectura automática."


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


def _extraer_fecha_transaccion(texto: str) -> str | None:
    """Extrae la fecha operativa del rótulo 'Fecha Transacción'.

    En MOVIMIENTO BIN A BIN esta fecha es distinta de la 'Fecha' de emisión
    que aparece en el encabezado. Para recibo debe prevalecer la transacción.
    Admite ejemplos como:
      Fecha Transacción: 2026-08-14-07.11.30
      Fecha Transaccion: 14/08/2026 07:11:30
    Devuelve únicamente la parte calendario.
    """
    t = str(texto or "")
    patrones = [
        r"fecha\s+transacci[oó]n\s*[:#-]?\s*"
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})"
        r"(?:[- T]\d{1,2}[.:]\d{1,2}(?:[.:]\d{1,2})?)?",
        r"fecha\s+transacci[oó]n\s*[:#-]?\s*"
        r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
        r"(?:[ T-]\d{1,2}[.:]\d{1,2}(?:[.:]\d{1,2})?)?",
    ]
    for patron in patrones:
        m = re.search(patron, t, re.I)
        if m:
            return m.group(1).strip()
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
    fecha_transaccion = _extraer_fecha_transaccion(t)
    fecha_documento = _primero([
        r"(?:^|\n)\s*(?:fecha|date)\s*[:#-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"(?:^|\n)\s*(?:fecha|date)\s*[:#-]?\s*"
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    ], t)
    # En BIN a BIN, Fecha Transacción es la fecha operativa que debe viajar
    # al recibo. La fecha simple del encabezado solo es respaldo.
    fecha = fecha_transaccion or fecha_documento
    # DESDE/HASTA de un BIN son columnas tabulares; no se extraen aquí
    # porque tomar "la palabra siguiente" al encabezado produce falsos positivos.
    ubicacion_origen = _primero([
        r"(?:ubicaci[oó]n\s+origen)\s*[:#-]?\s*([A-Z0-9._/-]{2,40})",
    ], t)
    ubicacion_destino = _primero([
        r"(?:ubicaci[oó]n\s+destino)\s*[:#-]?\s*([A-Z0-9._/-]{2,40})",
    ], t)
    reproceso = bool(re.search(r"\b(REPROCESO|GARANT[IÍ]A|RETRABAJO)\b", t, re.I))

    lineas = []
    # Acepta líneas OCR con espacios variables. La cantidad debe quedar al final.
    for linea in t.splitlines():
        limpio = " ".join(linea.split())
        m = re.match(
            r"^([A-Z0-9][A-Z0-9._/-]{2,})\s+(.{0,100}?)\s+([0-9][0-9.,]*)$",
            limpio, re.I)
        if not m:
            continue
        cod, desc, cant = m.groups()
        n = _num(cant)
        if n is None:
            continue
        if cod.upper() in {"NIT", "TOTAL", "SUBTOTAL", "IVA", "FECHA", "FACTURA", "OC"}:
            continue
        lineas.append({
            "articulo": cod.strip(),
            "descripcion": desc.strip(),
            "cantidad_documento": n,
            "cantidad_fisica": 0.0,
            "confianza": round(min(confianza_texto, 0.9), 3),
        })

    return {
        "referencia": asdict(Campo(ref, confianza_texto if ref else 0.0, "texto")),
        "orden_compra": asdict(Campo(oc, confianza_texto if oc else 0.0, "texto")),
        "nit": asdict(Campo(nit, confianza_texto if nit else 0.0, "texto")),
        "fecha": asdict(Campo(
            fecha,
            confianza_texto if fecha else 0.0,
            "Fecha Transacción" if fecha_transaccion else "Fecha documento",
        )),
        "fecha_transaccion": asdict(Campo(
            fecha_transaccion,
            confianza_texto if fecha_transaccion else 0.0,
            "Fecha Transacción",
        )),
        "fecha_documento_origen": asdict(Campo(
            fecha_documento,
            confianza_texto if fecha_documento else 0.0,
            "Fecha documento",
        )),
        "ubicacion_origen": asdict(Campo(
            ubicacion_origen, confianza_texto if ubicacion_origen else 0.0, "texto")),
        "ubicacion_destino": asdict(Campo(
            ubicacion_destino, confianza_texto if ubicacion_destino else 0.0, "texto")),
        "es_reproceso_sugerido": reproceso,
        "lineas": lineas,
        "texto": t,
    }



def clasificar_origen(texto: str, bin_filas: list[dict] | None = None) -> tuple[str | None, float, str]:
    """Clasifica el documento como BIN A BIN o FACTURA con evidencia explícita.

    El estándar BIN de AKT se reconoce por cualquiera de estas señales fuertes:
    - título "MOVIMIENTO BIN A BIN" / "BIN A BIN";
    - "Id de Bin" más las columnas tabulares DESDE/HASTA;
    - parser espacial BIN exitoso (Código/Cantidad/Serial/Lote/Desde/Hasta).

    Para facturas se conserva la detección por FACTURA/FV/FE. Si no hay evidencia
    suficiente, devuelve None para que el usuario pueda escoger manualmente.
    """
    t = " ".join((texto or "").upper().split())
    filas = list(bin_filas or [])

    if re.search(r"\bMOVIMIENTO\s+BIN\s+A\s+BIN\b", t):
        return "BIN_A_BIN", 0.99, "Título MOVIMIENTO BIN A BIN"
    if re.search(r"\bBIN\s+A\s+BIN\b", t):
        return "BIN_A_BIN", 0.98, "Texto BIN A BIN"
    if (re.search(r"\bID\s+DE\s+BIN\b", t)
            and "DESDE" in t and "HASTA" in t
            and "CANTIDAD" in t):
        return "BIN_A_BIN", 0.97, "Estructura estándar Id de Bin + tabla"
    if filas:
        return "BIN_A_BIN", 0.96, "Estructura tabular BIN reconocida"

    if re.search(r"\bFACTURA(?:CION)?\b", t):
        return "FACTURA", 0.98, "Texto FACTURA"
    if re.search(r"\bF[VE][-\s]?[A-Z0-9._/-]{2,}\b", t):
        return "FACTURA", 0.94, "Número de factura FV/FE"

    return None, 0.0, ""


def inferir_origen(texto: str, bin_filas: list[dict] | None = None) -> str | None:
    """Compatibilidad: devuelve solo el tipo documental sugerido."""
    origen, _, _ = clasificar_origen(texto, bin_filas)
    return origen


def _cantidad_probable(resto: str):
    """Busca una cantidad probable en el resto de una línea de artículo."""
    if not resto:
        return None

    # Prioridad alta: valores explícitamente asociados a cantidad/unidades.
    m = re.search(
        r"(?:CANT(?:IDAD)?|CTD|QTY|UNID(?:ADES)?|UND)\s*[:#-]?\s*"
        r"([0-9]+(?:[.,][0-9]+)?)",
        resto, re.I)
    if m:
        return _num(m.group(1))

    # Fallback: números aislados; favorece enteros razonables y evita valores monetarios.
    candidatos = []
    for m in re.finditer(r"(?<![A-Z0-9])([0-9]+(?:[.,][0-9]+)?)(?![A-Z0-9])", resto, re.I):
        raw = m.group(1)
        val = _num(raw)
        if val is None or val <= 0:
            continue
        contexto = resto[max(0, m.start()-4):m.end()+4]
        score = 0
        if val <= 10000:
            score += 2
        if float(val).is_integer():
            score += 1
        if "$" in contexto:
            score -= 4
        if any(x in raw for x in (".", ",")) and val > 1000:
            score -= 2
        # Ante empate favorece el número más a la derecha: en tablas de recibo
        # la cantidad suele estar en la última columna.
        candidatos.append((score, m.start(), val))

    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][2]


def lineas_desde_catalogo(texto: str, catalogo: dict[str, str],
                          confianza_texto: float = 0.8) -> list[dict]:
    """Detecta todas las referencias del maestro y su cantidad probable.

    También recompone códigos numéricos cuando el OCR inserta espacios entre
    dígitos, algo frecuente en etiquetas y tablas fotografiadas.
    """
    if not texto or not catalogo:
        return []

    mapa = {
        str(k).strip().upper(): (str(k).strip(), v or "")
        for k, v in catalogo.items() if str(k).strip()
    }
    encontradas = {}

    for linea in texto.splitlines():
        limpio = " ".join(linea.split())
        if not limpio:
            continue
        upper = limpio.upper()

        # Variante compacta para referencias numéricas partidas por espacios.
        compacto_digitos = re.sub(r"(?<=\d)\s+(?=\d)", "", upper)

        candidatos_linea = set()
        tokens = re.findall(r"[A-Z0-9][A-Z0-9._/-]{2,}", upper)
        for token in tokens:
            if token in mapa:
                candidatos_linea.add(token)

        # Busca referencias del maestro que no hayan quedado como token completo.
        # Se limita a códigos presentes literalmente/compactados para evitar fuzzy
        # matching peligroso sobre inventario.
        for key in mapa:
            if key in candidatos_linea:
                continue
            if key in upper or (key.isdigit() and key in compacto_digitos):
                candidatos_linea.add(key)

        for key in candidatos_linea:
            codigo, descripcion = mapa[key]

            # Busca contexto posterior al código. Si el OCR separó el código
            # numérico, usa la fila completa; los números embebidos en letras
            # (ej. 200DS) no cuentan como cantidad por la regex de _cantidad_probable.
            pos = upper.find(key)
            resto = limpio[pos + len(key):] if pos >= 0 else limpio
            cantidad = _cantidad_probable(resto)

            actual = encontradas.get(codigo)
            propuesta = {
                "articulo": codigo,
                "descripcion": descripcion,
                "cantidad_documento": float(cantidad or 0),
                # Cantidad física comienza en 0: la recepción real la confirma
                # el usuario con "recibo completo" o modo discrepancias.
                "cantidad_fisica": 0.0,
                "confianza": round(min(confianza_texto, 0.92), 3),
                "fuente": "OCR+MAESTRO",
            }
            if actual is None or (
                    not float(actual.get("cantidad_documento") or 0) and cantidad):
                encontradas[codigo] = propuesta

    return list(encontradas.values())

def lineas_bin_desde_texto(texto: str, catalogo: dict[str, str],
                           confianza_texto: float = 0.8) -> list[dict]:
    """Parser estricto para filas BIN: Proveedor Código Descripción Cantidad Serial."""
    if not texto or not catalogo:
        return []
    mapa = {str(k).strip().upper(): (str(k).strip(), v or "")
            for k, v in catalogo.items() if str(k).strip()}
    out = {}
    for raw_line in texto.splitlines():
        linea = " ".join(raw_line.split())
        if not linea:
            continue
        upper = linea.upper()
        codigo_key, pos = None, -1
        for key in mapa:
            p = upper.find(key)
            if p >= 0 and (codigo_key is None or p < pos):
                codigo_key, pos = key, p
        if not codigo_key:
            continue
        codigo, descripcion = mapa[codigo_key]
        despues = linea[pos + len(codigo_key):].strip()
        # Formato BIN habitual:
        # Descripción | Cantidad | Serial | Lote | Desde | Hasta
        m = re.search(
            r"\s([0-9]{1,9}(?:[.,][0-9]+)?)\s+"
            r"(?:NONE|N/?A)\s+(?:NONE|N/?A)(?:\s+|$)",
            despues, re.I)
        if not m:
            m = re.search(
                r"\s([0-9]{1,9}(?:[.,][0-9]+)?)\s+"
                r"(?:NONE|N/?A|[A-Z][A-Z0-9._/-]{1,40})\s*$",
                despues, re.I)
        if not m:
            m = re.search(r"\s([0-9]{1,9}(?:[.,][0-9]+)?)\s*$", despues)
        qty = _num(m.group(1)) if m else None
        if qty is None or qty <= 0:
            continue
        out[codigo] = {
            "articulo": codigo,
            "descripcion": descripcion,
            "cantidad_documento": float(qty),
            "cantidad_fisica": 0.0,
            "confianza": round(min(confianza_texto, 0.96), 3),
            "fuente": "BIN_TABLA",
        }
    return list(out.values())


def _tokens_desc_bin(s: str) -> list[str]:
    """Tokens útiles para comparar descripciones OCR contra el maestro."""
    toks = re.findall(r"[A-Z0-9]+", str(s or "").upper())
    return [t for t in toks if len(t) >= 2 and t not in {
        "NONE", "WSERE", "SANYANG", "CHONGQING", "GUANGDONG"
    }]


def _sim_desc_bin(descripcion: str, linea: str) -> float:
    base = _tokens_desc_bin(descripcion)
    if not base:
        return 0.0
    fila = set(_tokens_desc_bin(linea))
    return sum(1 for t in base if t in fila) / max(1, len(base))


def _sim_codigo_bin(codigo: str, streams: list[str]) -> float:
    """Similitud robusta ante 1-2 dígitos omitidos/agregados por OCR."""
    code = re.sub(r"\D", "", str(codigo or ""))
    if not code:
        return 0.0
    best = 0.0
    for raw in streams:
        d = re.sub(r"\D", "", str(raw or ""))
        if not d:
            continue
        if code in d:
            return 1.0
        best = max(best, SequenceMatcher(None, d, code).ratio())
        # El proveedor puede venir pegado delante del artículo. Se prueban
        # ventanas cercanas al largo real del código para no penalizar ese ruido.
        for L in range(max(8, len(code) - 2), min(len(d), len(code) + 2) + 1):
            for i in range(0, len(d) - L + 1):
                best = max(
                    best,
                    SequenceMatcher(None, d[i:i + L], code).ratio()
                )
    return float(best)


def _cantidad_bin_antes_none(linea: str) -> float | None:
    """Toma el último número aislado antes del primer NONE de una fila BIN."""
    upper = str(linea or "").upper()
    pos_none = upper.find("NONE")
    if pos_none < 0:
        return None
    previo = upper[:pos_none]
    candidatos = re.findall(
        r"(?<![A-Z0-9])([0-9]{1,7}(?:[.,][0-9]+)?)(?![A-Z0-9])",
        previo,
    )
    for raw in reversed(candidatos):
        val = _num(raw)
        if val is not None and val > 0:
            return float(val)
    return None


def lineas_bin_desde_texto_maestro(
        texto: str,
        catalogo: dict[str, str],
        confianza_texto: float = 0.8) -> list[dict]:
    """Recupera filas BIN dañadas por OCR usando dos señales independientes.

    Solo acepta una referencia del maestro si:
    1) el patrón numérico del código es muy parecido (>= 0.90), Y
    2) la descripción del maestro también coincide con la fila (>= 0.60), Y
    3) la mejor opción tiene margen suficiente frente a la segunda.

    Esto cubre fotos reales donde:
      7700149604673 -> 700149604673
      7700149604161 -> 700149604161
      7700149604710 -> 700149604710
    sin permitir un fuzzy-match libre sobre inventario.
    """
    if not texto or not catalogo:
        return []

    mapa = {
        str(k).strip().upper(): (str(k).strip(), str(v or ""))
        for k, v in catalogo.items()
        if str(k).strip()
    }
    out = {}

    for raw_line in str(texto).splitlines():
        linea = " ".join(str(raw_line or "").split())
        if not linea:
            continue
        upper = linea.upper()

        # Una fila BIN real normalmente conserva al menos un NONE aun con ruido.
        if "NONE" not in upper:
            continue

        tokens = re.findall(r"[A-Z0-9./-]{8,}", upper)
        streams = []
        for tok in tokens:
            dig = re.sub(r"\D", "", tok)
            if 10 <= len(dig) <= 20:
                streams.append(dig)
        if not streams:
            continue

        candidatos = []
        for key, (codigo, descripcion) in mapa.items():
            if not key.isdigit():
                continue
            sc_code = _sim_codigo_bin(key, streams)
            if sc_code < 0.90:
                continue
            sc_desc = _sim_desc_bin(descripcion, linea)
            if sc_desc < 0.60:
                continue
            score = (0.72 * sc_code) + (0.28 * sc_desc)
            candidatos.append((score, sc_code, sc_desc, key, codigo, descripcion))

        if not candidatos:
            continue

        candidatos.sort(reverse=True)
        mejor = candidatos[0]
        segundo = candidatos[1] if len(candidatos) > 1 else None
        margen = mejor[0] - (segundo[0] if segundo else 0.0)

        # Exacto dentro del stream: basta margen pequeño. Si hubo un dígito
        # omitido/agregado, se exige margen claro para no asignar otro artículo.
        exacto = mejor[1] >= 0.999
        if (exacto and margen < 0.035) or (not exacto and margen < 0.06):
            continue

        _, sc_code, sc_desc, key, codigo, descripcion = mejor
        qty = _cantidad_bin_antes_none(linea)

        propuesta = {
            "articulo": codigo,
            "descripcion": descripcion,
            "cantidad_documento": float(qty or 0),
            "cantidad_fisica": 0.0,
            "confianza": round(
                min(0.99, max(float(confianza_texto), mejor[0])), 3),
            "fuente": "BIN_MAESTRO_VALIDADO",
            "codigo_score": round(float(sc_code), 3),
            "descripcion_score": round(float(sc_desc), 3),
        }
        actual = out.get(key)
        if actual is None or (
            not float(actual.get("cantidad_documento") or 0)
            and float(propuesta.get("cantidad_documento") or 0) > 0
        ):
            out[key] = propuesta

    return list(out.values())


def completar_con_catalogo(resultado: dict, catalogo: dict[str, str]) -> dict:
    """Combina OCR con maestro priorizando parsers estructurados sobre heurísticos."""
    if not resultado:
        return resultado
    cf = float(resultado.get("confianza_texto") or 0.0)
    mapa_upper = {str(k).upper(): (str(k), v or "") for k, v in catalogo.items()}
    existentes = {}

    for ln in resultado.get("lineas", []) or []:
        cod = str(ln.get("articulo") or "").strip()
        if not cod:
            continue
        key = cod.upper()
        if key in mapa_upper:
            oficial_cod, oficial_desc = mapa_upper[key]
            ln["articulo"] = oficial_cod
            if oficial_desc:
                ln["descripcion"] = oficial_desc
            ln.setdefault("fuente", "OCR_HEURISTICO")
            existentes[key] = ln

    propuestas = lineas_desde_catalogo(resultado.get("texto", ""), catalogo, cf)
    propuestas_bin = lineas_bin_desde_texto(resultado.get("texto", ""), catalogo, cf)
    propuestas_bin_maestro = lineas_bin_desde_texto_maestro(
        resultado.get("texto", ""), catalogo, cf)

    # El parser estricto manda. El parser validado contra maestro solo completa
    # referencias que el estricto perdió por errores de OCR en 1-2 dígitos.
    por_codigo_bin = {
        str(x.get("articulo") or "").strip().upper(): x
        for x in propuestas_bin
        if str(x.get("articulo") or "").strip()
    }
    for x in propuestas_bin_maestro:
        key = str(x.get("articulo") or "").strip().upper()
        if not key:
            continue
        actual = por_codigo_bin.get(key)
        if actual is None:
            propuestas_bin.append(x)
            por_codigo_bin[key] = x
        elif (
            not float(actual.get("cantidad_documento") or 0)
            and float(x.get("cantidad_documento") or 0) > 0
        ):
            idx_actual = propuestas_bin.index(actual)
            propuestas_bin[idx_actual] = x
            por_codigo_bin[key] = x
    # Códigos que aparecen LITERALMENTE en el OCR, aunque la cantidad de esa
    # fila no se haya podido leer (caso real: 142 leído como "uaz"). Este set
    # sirve únicamente para autorizar un rescate geométrico de la cantidad;
    # nunca se toma la cantidad heurística de propuestas para un BIN.
    codigos_texto_presentes = {
        str(x.get("articulo") or "").strip().upper()
        for x in propuestas
        if str(x.get("articulo") or "").strip()
    }

    propuestas_ia = []
    for row in resultado.get("lineas_ia", []) or []:
        key = str(row.get("codigo") or "").strip().upper()
        if key not in mapa_upper:
            continue
        qty = _num(row.get("cantidad"))
        if qty is None or qty <= 0:
            continue
        oficial_cod, oficial_desc = mapa_upper[key]
        fuente_ia = str(
            resultado.get("ia_fuente") or "DOCUMENT_AI"
        ).strip() or "DOCUMENT_AI"
        propuestas_ia.append({
            "articulo": oficial_cod,
            "descripcion": oficial_desc or str(row.get("descripcion") or ""),
            "cantidad_documento": float(qty),
            "cantidad_fisica": 0.0,
            "serial": str(row.get("serial") or ""),
            "lote": str(row.get("lote") or ""),
            "ubicacion_desde": str(row.get("desde") or ""),
            "ubicacion_hasta": str(row.get("hasta") or ""),
            "confianza": 0.99,
            "fuente": fuente_ia,
        })

    es_imagen = bool(resultado.get("entrada_imagen"))
    metodo = str(resultado.get("metodo") or "")
    bin_score, bin_filas_texto = _score_bin_texto(resultado.get("texto", ""))

    es_bin_imagen = bool(
        es_imagen and (
            resultado.get("origen_sugerido") == "BIN_A_BIN"
            or "BIN_ESTRUCTURADO" in metodo
            or bin_score >= 20
        )
    )

    # En una fotografía BIN eliminamos por completo las líneas heurísticas que
    # nacieron antes de validar la tabla. Es preferible mostrar una fila como
    # "no leída" a inventar un código o una cantidad plausible pero incorrecta.
    # A partir de aquí solo vuelven a entrar filas con:
    #   código EXACTO del maestro + cantidad seguida de NONE/NONE.
    if es_bin_imagen or propuestas_ia:
        resultado["lineas"] = []
        existentes = {}

    # En una fotografía BIN, las líneas de texto estructuradas son el camino
    # más seguro porque el código se acepta únicamente si existe EXACTAMENTE en
    # el maestro y la cantidad debe aparecer antes de NONE/NONE. La geometría
    # RapidOCR queda como respaldo y no puede pisar una cantidad validada.
    if es_imagen and propuestas_bin and bin_score >= 20:
        for ln in propuestas_bin:
            ln["fuente"] = "BIN_TABLA_IMAGEN_VALIDADA"
            ln["confianza"] = round(min(max(cf, 0.90), 0.99), 3)

    propuestas_espaciales = []
    for row in resultado.get("bin_filas_espaciales", []) or []:
        raw_key = re.sub(
            r"\s+", "", str(row.get("articulo") or "").strip().upper())
        key = raw_key

        # En fotos reales Proveedor y Código pueden quedar unidos en una sola
        # detección OCR, igual que ocurría en los PDF diagnosticados por Claude:
        #   SANYANGIN-0017700149604819
        # El rescate NO es fuzzy: solo se permite si un único código EXACTO del
        # maestro aparece literalmente dentro del token contaminado.
        if key not in mapa_upper:
            coincidencias = [k for k in mapa_upper if k and k in raw_key]
            if len(coincidencias) == 1:
                key = coincidencias[0]
            else:
                continue

        oficial_cod, oficial_desc = mapa_upper[key]
        propuestas_espaciales.append({
            "articulo": oficial_cod,
            "descripcion": oficial_desc or row.get("descripcion_ocr", ""),
            "cantidad_documento": float(row.get("cantidad_documento") or 0),
            "cantidad_fisica": 0.0,
            "serial": row.get("serial", ""),
            "lote": row.get("lote", ""),
            "ubicacion_desde": row.get("ubicacion_desde", ""),
            "ubicacion_hasta": row.get("ubicacion_hasta", ""),
            "confianza": round(min(cf, 0.99), 3),
            "fuente": row.get("fuente", "BIN_ESPACIAL"),
        })

    if propuestas_ia:
        # Cuando Document AI respondió, esta es la única fuente autorizada para
        # código y cantidad. Todo código se valida EXACTAMENTE contra el maestro
        # local antes de llegar aquí.
        fuentes_merge = propuestas_ia
        resultado["diagnostico_bin"] = (
            f"{resultado.get('ia_fuente') or 'Document AI'} validó "
            f"{len(propuestas_ia)} fila(s) contra el maestro."
        )
    elif es_bin_imagen:
        # Un rescate espacial solo puede entrar si el MISMO código apareció
        # literalmente en el OCR y existe exactamente en el maestro. Así
        # recuperamos cantidades que el OCR textual deformó (p.ej. 142 -> uaz)
        # sin permitir que la geometría invente otra referencia válida.
        espaciales_validadas = [
            x for x in propuestas_espaciales
            if str(x.get("articulo") or "").strip().upper()
            in codigos_texto_presentes
        ]

        if propuestas_bin:
            fuentes_merge = propuestas_bin + espaciales_validadas
            rescatadas = len({
                str(x.get("articulo") or "").strip().upper()
                for x in espaciales_validadas
            } - {
                str(x.get("articulo") or "").strip().upper()
                for x in propuestas_bin
            })
            if rescatadas:
                resultado["diagnostico_bin"] = (
                    f"Se rescataron {rescatadas} fila(s) cuya referencia sí "
                    "aparecía exactamente en el documento, pero cuya cantidad "
                    "requirió lectura geométrica reforzada."
                )
        elif len(codigos_texto_presentes) >= 2 and espaciales_validadas:
            # Si el patrón NONE/NONE se deterioró por la foto, todavía puede
            # recuperarse la tabla cuando hay coincidencia triple:
            # código literal en OCR + código exacto en maestro + cantidad en
            # la columna geométrica Cantidad.
            fuentes_merge = espaciales_validadas
            resultado["diagnostico_bin"] = (
                "La foto no conservó el patrón textual completo, pero las "
                "filas se recuperaron por coincidencia exacta de código en OCR "
                "+ maestro y por posición de la columna Cantidad. Revise antes "
                "de confirmar la recepción."
            )
        else:
            fuentes_merge = []
            resultado["diagnostico_bin"] = (
                "No se pudieron validar filas BIN con seguridad. No se "
                "autocompletaron códigos ni cantidades para evitar datos "
                "incorrectos."
            )
    else:
        fuentes_merge = propuestas + propuestas_bin + propuestas_espaciales

    for ln in fuentes_merge:
        key = ln["articulo"].upper()
        actual = existentes.get(key)
        if actual is None:
            resultado.setdefault("lineas", []).append(ln)
            existentes[key] = ln
            continue
        qty_nueva = float(ln.get("cantidad_documento") or 0)
        fuente_nueva = str(ln.get("fuente") or "")
        fuente_actual = str(actual.get("fuente") or "")
        prioridad = {
            "OCR_HEURISTICO": 0,
            "OCR": 0,
            "OCR+MAESTRO": 1,
            "BIN_ESPACIAL": 2,
            "BIN_TABLA": 3,
            "BIN_PDF_ESPACIAL": 4,
            "BIN_PDF_CLIP": 5,
            "BIN_PDF_CODIGO": 6,
            "BIN_TABLA_IMAGEN_VALIDADA": 8,
            "BIN_MAESTRO_VALIDADO": 9,
            "MISTRAL_DOCUMENT_AI": 20,
            "OPENAI_GPT5_NANO": 20,
            "DOCUMENT_AI": 20,
        }
        qty_actual = float(actual.get("cantidad_documento") or 0)
        if qty_nueva > 0 and (
            qty_actual <= 0
            or prioridad.get(fuente_nueva, 0) >= prioridad.get(fuente_actual, 0)
        ):
            # Una fila validada con cantidad 0 significa "código reconocido,
            # cantidad no leída". En ese caso una cantidad positiva de una
            # fuente espacial ya validada para EL MISMO código debe poder
            # completarla aunque su prioridad nominal sea menor.
            actual["cantidad_documento"] = qty_nueva
            actual["cantidad_fisica"] = 0.0
            actual["fuente"] = fuente_nueva

        # Serial/lote/DESDE/HASTA son metadatos complementarios y pueden venir
        # mejor de la geometría aun cuando la cantidad válida provenga del texto.
        # Se completan solo si faltan; nunca degradan una cantidad ya validada.
        for campo in ("serial", "lote", "ubicacion_desde", "ubicacion_hasta"):
            if ln.get(campo) and not actual.get(campo):
                actual[campo] = ln.get(campo)

        if ln.get("descripcion"):
            actual["descripcion"] = ln["descripcion"]

    # Política anti-hallucination para BIN fotografiados:
    # si el texto estructurado recuperó varias filas válidas, la lista final
    # se limita a esos códigos exactos del maestro. Una caja espacial con un
    # código distinto (aunque casualmente exista en el maestro) no se añade.
    if es_bin_imagen and (propuestas_bin or codigos_texto_presentes):
        codigos_texto_validos = {
            str(x.get("articulo") or "").strip().upper()
            for x in propuestas_bin
            if str(x.get("articulo") or "").strip()
        } | codigos_texto_presentes

        resultado["lineas"] = [
            x for x in (resultado.get("lineas") or [])
            if str(x.get("articulo") or "").strip().upper()
            in codigos_texto_validos
        ]
        existentes = {
            str(x.get("articulo") or "").strip().upper(): x
            for x in resultado["lineas"]
        }
        resultado["lineas_descartadas_no_validadas"] = max(
            0,
            len(propuestas_espaciales) - len([
                x for x in propuestas_espaciales
                if str(x.get("articulo") or "").strip().upper()
                in codigos_texto_validos
            ]),
        )

    depuradas, vistos = [], set()
    for ln in resultado.get("lineas", []) or []:
        key = str(ln.get("articulo") or "").strip().upper()
        if not key or key in vistos:
            continue
        if key in mapa_upper:
            depuradas.append(existentes.get(key, ln))
            vistos.add(key)
    resultado["lineas"] = depuradas

    resultado["requiere_revision"] = (
        cf < 0.85 or not resultado.get("lineas")
        or any(float(x.get("cantidad_documento") or 0) <= 0
               for x in resultado.get("lineas", []))
    )
    return resultado

def analizar_documento(nombre: str, data: bytes, mime: str | None = None) -> dict:
    name = (nombre or "").lower()
    mime = mime or ""

    # Motor principal: GPT-5 nano Vision cuando OPENAI_API_KEY existe.
    # Si no está configurado o falla, intenta Mistral (si tiene clave) y luego
    # cae automáticamente al OCR local existente.
    ia = _openai_document_ai(nombre, data, mime)
    if not (ia and ia.get("ok") and (ia.get("texto") or ia.get("filas"))):
        ia_mistral = _mistral_document_ai(nombre, data, mime)
        if ia_mistral and ia_mistral.get("ok"):
            ia = ia_mistral
    ia_ok = bool(ia and ia.get("ok") and (ia.get("texto") or ia.get("filas")))

    if ia_ok:
        texto = str(ia.get("texto") or "")
        cf = 0.97
        fuente_ia = str(ia.get("fuente") or "MISTRAL_DOCUMENT_AI")
        metodo = f"{fuente_ia}/{ia.get('modelo') or 'document-ai'}"
        diagnostico = ""
        out = estructurar(texto, cf)
        out["entrada_ia"] = True
        out["ia_fuente"] = fuente_ia
        out["ia_modelo"] = ia.get("modelo")
        out["ia_usage_info"] = ia.get("usage_info") or {}
        out["lineas_ia"] = list(ia.get("filas") or [])

        if ia.get("referencia"):
            out["referencia"] = asdict(Campo(
                ia["referencia"], 0.99, "MISTRAL_DOCUMENT_AI"))
        if ia.get("fecha"):
            out["fecha"] = asdict(Campo(
                ia["fecha"], 0.98, "MISTRAL_DOCUMENT_AI"))

        filas_ia_bin = []
        for row in ia.get("filas") or []:
            codigo = str(row.get("codigo") or "").strip()
            qty = _num(row.get("cantidad"))
            if not codigo:
                continue
            filas_ia_bin.append({
                "articulo": codigo,
                "descripcion_ocr": str(row.get("descripcion") or "").strip(),
                "cantidad_documento": float(qty or 0),
                "cantidad_fisica": 0.0,
                "serial": str(row.get("serial") or "").strip(),
                "lote": str(row.get("lote") or "").strip(),
                "ubicacion_desde": str(row.get("desde") or "").strip(),
                "ubicacion_hasta": str(row.get("hasta") or "").strip(),
                "proveedor_bin": str(row.get("proveedor") or "").strip(),
                "fuente": fuente_ia,
            })
        out["bin_filas_espaciales"] = filas_ia_bin
    else:
        texto, cf, metodo, diagnostico = extraer_texto(nombre, data, mime)
        out = estructurar(texto, cf)
        out["entrada_ia"] = False
        out["ia_error"] = (ia or {}).get("error") if isinstance(ia, dict) else ""

        es_imagen_local = bool(
            mime.startswith("image/")
            or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        )
        if es_imagen_local:
            out["bin_filas_espaciales"] = extraer_bin_columnas_imagen(data)
        elif mime == "application/pdf" or name.endswith(".pdf"):
            out["bin_filas_espaciales"] = extraer_bin_columnas_pdf(data)
        else:
            out["bin_filas_espaciales"] = []

    es_imagen = bool(
        mime.startswith("image/")
        or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
    )
    out["entrada_imagen"] = es_imagen

    out.update(resumir_ubicaciones_bin(out.get("bin_filas_espaciales") or []))
    out["metodo"] = metodo
    out["confianza_texto"] = round(float(cf), 3)
    out["diagnostico"] = diagnostico
    out["ocr_ok"] = bool(texto.strip() or out.get("lineas_ia"))

    tipo_ia = str((ia or {}).get("tipo_documento") or "").strip().upper() if ia_ok else ""
    if tipo_ia == "BIN_A_BIN":
        origen, origen_cf, origen_evidencia = (
            "BIN_A_BIN", 0.995, str(ia.get("fuente") or "Document AI"))
    elif tipo_ia == "FACTURA":
        origen, origen_cf, origen_evidencia = (
            "FACTURA", 0.995, str(ia.get("fuente") or "Document AI"))
    else:
        origen, origen_cf, origen_evidencia = clasificar_origen(
            texto, out.get("bin_filas_espaciales") or [])

    out["origen_sugerido"] = origen
    out["origen_confianza"] = round(float(origen_cf), 3)
    out["origen_evidencia"] = origen_evidencia
    out["requiere_revision"] = (
        float(cf) < 0.85
        or not (out.get("lineas") or out.get("lineas_ia"))
    )
    return out
