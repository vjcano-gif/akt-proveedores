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
            dets.append({
                "txt": txt,
                "cx": (min(xs)+max(xs))/2,
                "cy": (min(ys)+max(ys))/2,
                "h": max(1.0, max(ys)-min(ys)),
            })
        except Exception:
            continue

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

    columnas = sorted(
        [(k, v["cx"]) for k, v in headers.items()],
        key=lambda x: x[1])
    header_y = max(headers[k]["cy"] for k in headers)
    limites = [
        (columnas[i][1] + columnas[i+1][1]) / 2
        for i in range(len(columnas)-1)
    ]

    def columna_de_x(x):
        idx = 0
        while idx < len(limites) and x > limites[idx]:
            idx += 1
        return columnas[min(idx, len(columnas)-1)][0]

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
        celdas = {}
        for d in sorted(fila["items"], key=lambda x: x["cx"]):
            col = columna_de_x(d["cx"])
            celdas.setdefault(col, []).append(d["txt"])
        celdas = {k: " ".join(v).strip() for k, v in celdas.items()}

        codigo = re.sub(r"\s+", "", celdas.get("codigo", ""))
        if not re.fullmatch(r"[A-Z0-9._/-]{5,60}", codigo, re.I):
            continue
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
    """Extrae Código/Cantidad/Serial/Lote/DESDE/HASTA desde una imagen BIN."""
    try:
        img = _decode_image(data)
        res = _rapid_engine()(img)
        txts = _refinar_celdas_numericas(img, res)
        return _extraer_bin_columnas_resultado(res, txts)
    except Exception:
        return []

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


def _ocr_image(data: bytes) -> tuple[str, float, str, str]:
    """Devuelve texto, confianza, motor usado y diagnóstico."""
    diagnosticos = []
    try:
        img = _decode_image(data)
    except Exception as e:
        return "", 0.0, "OCR", f"Decodificación: {type(e).__name__}: {e}"

    # RapidOCR: prueba original y variantes, conservando el mejor resultado.
    mejor_texto, mejor_conf, mejor_var = "", 0.0, ""
    try:
        for nombre_var, variante in _preprocesar_para_ocr(img):
            texto, conf = _ocr_rapid(variante)
            if texto and (len(texto) > len(mejor_texto) or conf > mejor_conf + 0.08):
                mejor_texto, mejor_conf, mejor_var = texto, conf, nombre_var
            # Si ya hay una lectura razonable no triplica el tiempo de OCR.
            if len(mejor_texto) >= 30 and mejor_conf >= 0.55:
                break
        if mejor_texto:
            return mejor_texto, mejor_conf, f"RAPIDOCR/{mejor_var}", ""
        diagnosticos.append("RapidOCR no detectó texto.")
    except Exception as e:
        diagnosticos.append(f"RapidOCR: {type(e).__name__}: {e}")

    # Tesseract: fallback independiente de ONNX.
    try:
        # Usa primero la variante de contraste, que suele rendir mejor en documentos.
        variantes = _preprocesar_para_ocr(img)
        preferida = variantes[1][1] if len(variantes) > 1 else img
        texto, conf = _ocr_tesseract(preferida)
        if texto:
            return texto, conf, "TESSERACT", " | ".join(diagnosticos)
        diagnosticos.append("Tesseract no detectó texto.")
    except Exception as e:
        diagnosticos.append(f"Tesseract: {type(e).__name__}: {e}")

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



def inferir_origen(texto: str) -> str | None:
    """Sugiere el tipo documental a partir del texto OCR."""
    t = (texto or "").upper()
    if re.search(r"\bBIN(?:\s+A\s+BIN)?\b", t):
        return "BIN_A_BIN"
    if re.search(r"\b(FACTURA|FACTURACION|F[VE]-?\d)\b", t):
        return "FACTURA"
    return None


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
            "fuente": "BIN_ESPACIAL",
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
        prioridad = {"OCR_HEURISTICO": 0, "OCR": 0, "OCR+MAESTRO": 1,
                     "BIN_TABLA": 2, "BIN_ESPACIAL": 3}
        if qty_nueva > 0 and prioridad.get(fuente_nueva, 0) >= prioridad.get(fuente_actual, 0):
            actual["cantidad_documento"] = qty_nueva
            actual["cantidad_fisica"] = 0.0
            actual["fuente"] = fuente_nueva
            for campo in ("serial", "lote", "ubicacion_desde", "ubicacion_hasta"):
                if ln.get(campo):
                    actual[campo] = ln.get(campo)
        if ln.get("descripcion"):
            actual["descripcion"] = ln["descripcion"]

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
    if (mime or "").startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        out["bin_filas_espaciales"] = extraer_bin_columnas_imagen(data)
    else:
        out["bin_filas_espaciales"] = []
    out["metodo"] = metodo
    out["confianza_texto"] = round(cf, 3)
    out["diagnostico"] = diagnostico
    out["ocr_ok"] = bool(texto.strip())
    out["origen_sugerido"] = inferir_origen(texto)
    out["requiere_revision"] = cf < 0.85 or not out["lineas"]
    return out
