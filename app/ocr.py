"""On-demand OCR of the captured screen, returned as structured JSON.

Reads a snapshot JPEG, OCRs it with Tesseract (via pytesseract.image_to_data), and returns the recognised
text grouped **line by line** and into **layout blocks (chunks) as they appear on screen** -- each with a
bounding box (in full-image pixel coords) and a confidence. CPU-only and local (no cloud).

pytesseract + Pillow are imported lazily so the rest of the app still runs if the OCR deps aren't installed;
`available()` reports whether OCR can run.
"""
import io

try:
    import pytesseract
    from PIL import Image, ImageOps
    _IMPORT_ERR = None
except Exception as e:                       # deps not installed
    pytesseract = None
    _IMPORT_ERR = str(e)


class OCRUnavailable(Exception):
    pass


def available():
    """True if the OCR deps AND the tesseract binary are usable."""
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_image(jpeg_bytes, lang="eng", psm=None, min_conf=0.0, region=None, scale=2.0, words=False):
    """OCR JPEG bytes -> dict {image, lang, text, lines[], blocks[]}. Blocking (run in an executor)."""
    if pytesseract is None:
        raise OCRUnavailable("OCR deps missing (%s); install tesseract-ocr + pip pytesseract Pillow" % _IMPORT_ERR)

    img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")      # grayscale
    full_w, full_h = img.size
    ox = oy = 0
    if region:
        x, y, w, h = region
        x = max(0, min(x, full_w)); y = max(0, min(y, full_h))
        w = max(1, min(w, full_w - x)); h = max(1, min(h, full_h - y))
        img = img.crop((x, y, x + w, y + h))
        ox, oy = x, y
    if scale and scale != 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    img = ImageOps.autocontrast(img)                          # boost contrast for screen text

    config = ("--psm %d" % int(psm)) if psm is not None else ""
    try:
        data = pytesseract.image_to_data(img, lang=lang, config=config,
                                         output_type=pytesseract.Output.DICT)
    except Exception as e:
        raise OCRUnavailable("tesseract failed: %s" % e)

    inv = (1.0 / scale) if (scale and scale != 1) else 1.0    # map boxes back to full-image pixels

    # Group words into lines keyed by Tesseract's (block, paragraph, line) -- this is reading order.
    grouped = {}
    order = []
    for i in range(len(data["text"])):
        txt = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not txt or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append({
            "text": txt,
            "bbox": [int(data["left"][i] * inv) + ox, int(data["top"][i] * inv) + oy,
                     int(data["width"][i] * inv), int(data["height"][i] * inv)],
            "conf": round(conf, 1),
        })

    lines = []
    for key in sorted(order):                                 # block, par, line order = top-to-bottom
        ws = grouped[key]
        conf = round(sum(w["conf"] for w in ws) / len(ws), 1)
        if conf < min_conf:
            continue
        x0 = min(w["bbox"][0] for w in ws)
        y0 = min(w["bbox"][1] for w in ws)
        x1 = max(w["bbox"][0] + w["bbox"][2] for w in ws)
        y1 = max(w["bbox"][1] + w["bbox"][3] for w in ws)
        line = {"text": " ".join(w["text"] for w in ws), "bbox": [x0, y0, x1 - x0, y1 - y0],
                "conf": conf, "block": key[0], "par": key[1], "line": key[2]}
        if words:
            line["words"] = ws
        lines.append(line)

    # Group lines into blocks (layout chunks: columns / panels stay separated).
    blocks = []
    by_block = {}
    for idx, ln in enumerate(lines):
        by_block.setdefault(ln["block"], []).append(idx)
    for bnum in sorted(by_block):
        idxs = by_block[bnum]
        bx0 = min(lines[i]["bbox"][0] for i in idxs)
        by0 = min(lines[i]["bbox"][1] for i in idxs)
        bx1 = max(lines[i]["bbox"][0] + lines[i]["bbox"][2] for i in idxs)
        by1 = max(lines[i]["bbox"][1] + lines[i]["bbox"][3] for i in idxs)
        blocks.append({"bbox": [bx0, by0, bx1 - bx0, by1 - by0],
                       "text": "\n".join(lines[i]["text"] for i in idxs), "lines": idxs})

    out = {"image": {"w": full_w, "h": full_h}, "lang": lang,
           "text": "\n".join(ln["text"] for ln in lines), "lines": lines, "blocks": blocks}
    if region:
        out["region"] = list(region)
    return out
