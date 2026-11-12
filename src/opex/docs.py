"""Files -> pages -> sheets -> documents (documents.json)."""
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
from PIL import Image

from . import llm

OCR_CACHE = Path(".cache/ocr")
PAGE_POOL = ThreadPoolExecutor(12)  # page-level model calls, shared by all files
SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}
TYPES = {
    "invoice_start": "the first page of an invoice or bill: it shows the bill's own number, dates, account and charges",
    "continuation": "a later page of the same invoice as the previous sheet: page 2, more charge lines, notes, back page",
    "supplementary": "an attachment that supports an invoice but is not an invoice: a photo of a meter, a printout of an image file or web page, a summary list, receipt, letter or tax form",
    "irrelevant": "a blank page, an advertisement, or content that is not about billing",
    "unknown": "cannot tell",
}
SPLIT_Q = {
    "type": "noul",
    "instructions": "Does this image show more than one separate paper sheet, for example two bills or two receipts scanned side by side?",
}
OCR_PROMPT = (
    "Transcribe all text in this document image as plain text. Keep the reading order. Keep each label on the same "
    "line as its value and each table row on one line. Copy numbers and dates exactly as printed. "
    "Output only the transcription."
)


def render(page):
    """Page -> PNG bytes at about 150 dpi, longest side <= 1568 px."""
    s = min(150 / 72, llm.MAX_SIDE / max(page.rect.width, page.rect.height))
    return page.get_pixmap(matrix=pymupdf.Matrix(s, s)).tobytes("png")


def load_pages(path, file_id, out):
    """One sheet per page. PyMuPDF opens PDFs and images alike."""
    sheets = []
    with pymupdf.open(path) as pdf:
        for i, page in enumerate(pdf):
            text = page.get_text()
            png = out / "pages" / f"{file_id}_p{i}.png"
            png.write_bytes(render(page))
            has_text = sum(c.isalpha() for c in text) >= 40
            sheets.append({"page": i, "box": None, "has_text": has_text, "png": str(png), "text": text if has_text else ""})
    return sheets


def ocr(png):
    """Flash-Lite transcription of an image-only sheet, cached by image hash."""
    f = OCR_CACHE / (hashlib.sha1(png).hexdigest() + ".txt")
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(llm.gemini(llm.LITE, [OCR_PROMPT, png]))
    else:
        llm._count("ocr.cache_hits")  # a warm cache makes a run cheaper and faster than a cold one
    return f.read_text()


def split(sheet, file_id):
    """Image-only page -> one sheet per paper sheet shown on it.
    ponytail: text-layer pages are never split and crops are not rotated; add both if scans need it."""
    png = Path(sheet["png"]).read_bytes()
    yes_schema = {"type": "object", "properties": {"yes": {"type": "boolean"}}, "required": ["yes"]}
    ask = lambda: llm.gemini(llm.LITE, [SPLIT_Q["instructions"], png], yes_schema)["yes"]
    if not llm.decide("sheet_split", "A scanned page.", SPLIT_Q, ask, 0.85, [png], file_id):
        return [sheet]
    box = {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
    boxes = llm.gemini(
        llm.LITE,
        ["Return one box per separate paper sheet in this image, as [ymin, xmin, ymax, xmax] on a 0-1000 scale, in reading order.", png],
        {"type": "object", "properties": {"boxes": {"type": "array", "items": box}}, "required": ["boxes"]},
    )["boxes"]
    if len(boxes) < 2:
        return [sheet]
    im, parts = Image.open(io.BytesIO(png)), []
    for n, (y0, x0, y1, x1) in enumerate(boxes):
        p = sheet["png"].replace(".png", f"_s{n}.png")
        im.crop((x0 * im.width // 1000, y0 * im.height // 1000, x1 * im.width // 1000, y1 * im.height // 1000)).save(p)
        parts.append({**sheet, "box": [y0, x0, y1, x1], "png": p})
    return parts


def classify(sheet, prev, file_id):
    state = (
        "PREVIOUS SHEET (start):\n"
        + (prev["text"][:600] if prev else "(none: the current sheet is the first sheet of the file)")
        + "\n\nCURRENT SHEET (start):\n"
        + sheet["text"][:2500]
    )
    q = {"type": "choice", "instructions": "What is the current sheet?", "criteria": TYPES}
    schema = {"type": "object", "properties": {"type": {"type": "string", "enum": list(TYPES)}}, "required": ["type"]}
    ask = lambda: llm.gemini(llm.LITE, [f"Classify the current sheet. Options: {json.dumps(TYPES)}\n\n{state}"], schema)["type"]
    return llm.decide("sheet_type", state, q, ask, 0.85, doc_id=file_id)


def join(types):
    """Sheet types in file order -> [(document type, [sheet indexes])].
    invoice_start opens an invoice; continuation joins the open document (or is unknown when none is open);
    supplementary is its own document; irrelevant and unknown are listed alone and do not close the open document."""
    docs, open_doc = [], None
    for i, t in enumerate(types):
        if t == "continuation" and open_doc is not None:
            open_doc[1].append(i)
            continue
        t = {"continuation": "unknown", "invoice_start": "invoice"}.get(t, t)
        docs.append((t, [i]))
        if t in ("invoice", "supplementary"):
            open_doc = docs[-1]
    return docs


def tokens(text):
    """Label-like tokens for the template fingerprint: lowercase words, digits dropped."""
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def segment_file(path, file_id, out):
    def prepare(page):  # image-only pages: split into sheets, then transcribe
        if page["has_text"]:
            return [page]
        parts = split(page, file_id)
        for s in parts:
            s["text"] = ocr(Path(s["png"]).read_bytes())
        return parts

    sheets = [s for parts in PAGE_POOL.map(prepare, load_pages(path, file_id, out)) for s in parts]
    types = list(PAGE_POOL.map(lambda i: classify(sheets[i], sheets[i - 1] if i else None, file_id), range(len(sheets))))
    docs, last_invoice = [], None
    for n, (t, idx) in enumerate(join(types)):
        doc = {"doc_id": f"{file_id}-d{n}", "type": t, "file": str(path), "sheets": [sheets[i] for i in idx], "supports": []}
        if t == "invoice":
            last_invoice = doc["doc_id"]
        elif t == "supplementary" and last_invoice:
            doc["supports"] = [last_invoice]  # harness relinks by invoice number after extraction
        docs.append(doc)
    return docs


def segment(in_dir, out):
    """Segment every file under in_dir. Writes out/documents.json and returns the documents."""
    in_dir, out = Path(in_dir), Path(out)
    (out / "pages").mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in in_dir.rglob("*") if p.suffix.lower() in SUFFIXES)
    ids = {}
    for p in files:  # file id = file name; add a counter when two folders hold the same name
        base = re.sub(r"[^\w-]", "_", p.stem)
        ids[p] = base if base not in ids.values() else f"{base}_{len(ids)}"

    def one(p):  # one bad file must not stop the batch
        try:
            ds = segment_file(p, ids[p], out)
        except Exception as e:
            print(f"  segmentation failed for {p}: {str(e)[:200]}")
            ds = [{"doc_id": f"{ids[p]}-d0", "type": "error", "file": str(p), "sheets": [], "supports": [], "error": str(e)[:500]}]
        llm.emit("", "file", file=str(p), docs=[{"doc_id": d["doc_id"], "type": d["type"], "sheets": [
            {"page": s["page"], "has_text": s["has_text"], "split": bool(s["box"])} for s in d["sheets"]]} for d in ds])  # fmt: skip
        return ds

    with ThreadPoolExecutor(4) as pool:
        docs = [d for ds in pool.map(one, files) for d in ds]
    (out / "documents.json").write_text(json.dumps(docs, indent=1))
    return docs
