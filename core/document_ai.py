"""Extracción documental: PDF nativo + OCR local con doble motor.

Orden de lectura:
1) PDF con capa de texto -> PyMuPDF.
2) Imagen/PDF escaneado -> RapidOCR.
3) Si RapidOCR falla o no detecta texto -> Tesseract (fallback).

La extracción nunca afecta inventario directamente: siempre requiere confirmación humana.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from functools import lru_cache


@dataclass
class Campo:
    valor: str | float | None
    confianza: float
    fuente: str = ""


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
        and len(re.sub(r"\s+", "", d["txt"])) <= 8
        and bool(re.search(r"[0-9OBISZ]", d["txt"], re.I))
    ]

    for d in candidatos:
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
            for variante in (enlarged, contrast, otsu, adaptive):
                for psm in (7, 8):
                    raw = pytesseract.image_to_string(
                        variante,
                        config=(
                            f"--oem 3 --psm {psm} "
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
    """OCR principal conservando estructura espacial de tablas."""
    engine = _rapid_engine()
    res = engine(img)
    txts_refinados = _refinar_celdas_numericas(img, res)
    texto, scores = _texto_rapid_ordenado(res, txts_override=txts_refinados)
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
    """Extrae filas BIN usando geometría + relectura reforzada de cantidades."""
    try:
        img = _decode_image(data)
        res = _rapid_engine()(img)
        txts = _refinar_celdas_numericas(img, res)
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


def _ocr_image(data: bytes) -> tuple[str, float, str, str]:
    """Devuelve texto OCR escogiendo el motor que mejor conserva la tabla.

    En BIN fotografiados no se acepta RapidOCR solo por tener confianza alta:
    se compara contra Tesseract PSM 6 y gana el que recupere más filas completas
    Código/Cantidad/NONE/NONE. Esto evita códigos/cantidades plausibles pero
    erróneos generados por un OCR espacial.
    """
    diagnosticos = []
    try:
        img = _decode_image(data)
    except Exception as e:
        return "", 0.0, "OCR", f"Decodificación: {type(e).__name__}: {e}"

    rapid_texto, rapid_conf, rapid_var = "", 0.0, ""
    try:
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

    tess_texto, tess_conf = "", 0.0
    try:
        # Para tablas BIN el original suele preservar mejor el espaciado de
        # columnas que una binarización agresiva. PSM 6 reconstruye cada fila.
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

    # Si cualquiera de los motores reconoce una estructura BIN real, prima la
    # estructura sobre la confianza promedio. En empate, más filas completas;
    # después, confianza.
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
        "fecha": asdict(Campo(fecha, confianza_texto if fecha else 0.0, "texto")),
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

    es_imagen = bool(resultado.get("entrada_imagen"))
    metodo = str(resultado.get("metodo") or "")
    bin_score, bin_filas_texto = _score_bin_texto(resultado.get("texto", ""))

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
        key = str(row.get("articulo") or "").strip().upper()
        if key not in mapa_upper:
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

    for ln in propuestas + propuestas_bin + propuestas_espaciales:
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
        }
        if qty_nueva > 0 and prioridad.get(fuente_nueva, 0) >= prioridad.get(fuente_actual, 0):
            actual["cantidad_documento"] = qty_nueva
            actual["cantidad_fisica"] = 0.0
            actual["fuente"] = fuente_nueva
            for campo in ("serial", "lote", "ubicacion_desde", "ubicacion_hasta"):
                if ln.get(campo):
                    actual[campo] = ln.get(campo)
        if ln.get("descripcion"):
            actual["descripcion"] = ln["descripcion"]

    # Política anti-hallucination para BIN fotografiados:
    # si el texto estructurado recuperó varias filas válidas, la lista final
    # se limita a esos códigos exactos del maestro. Una caja espacial con un
    # código distinto (aunque casualmente exista en el maestro) no se añade.
    if es_imagen and bin_filas_texto >= 2 and propuestas_bin:
        codigos_texto_validos = {
            str(x.get("articulo") or "").strip().upper()
            for x in propuestas_bin
            if str(x.get("articulo") or "").strip()
        }
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
    texto, cf, metodo, diagnostico = extraer_texto(nombre, data, mime)
    out = estructurar(texto, cf)
    name = (nombre or "").lower()
    mime = mime or ""

    es_imagen = bool(
        mime.startswith("image/")
        or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
    )
    out["entrada_imagen"] = es_imagen

    if es_imagen:
        out["bin_filas_espaciales"] = extraer_bin_columnas_imagen(data)
    elif mime == "application/pdf" or name.endswith(".pdf"):
        # Para PDF nativo, conserva X/Y de cada palabra; no depende del texto plano.
        out["bin_filas_espaciales"] = extraer_bin_columnas_pdf(data)
    else:
        out["bin_filas_espaciales"] = []

    out.update(resumir_ubicaciones_bin(out["bin_filas_espaciales"]))
    out["metodo"] = metodo
    out["confianza_texto"] = round(cf, 3)
    out["diagnostico"] = diagnostico
    out["ocr_ok"] = bool(texto.strip())
    origen, origen_cf, origen_evidencia = clasificar_origen(
        texto, out["bin_filas_espaciales"])
    out["origen_sugerido"] = origen
    out["origen_confianza"] = round(float(origen_cf), 3)
    out["origen_evidencia"] = origen_evidencia
    out["requiere_revision"] = cf < 0.85 or not out["lineas"]
    return out
