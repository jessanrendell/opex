"""Per-invoice extraction: fingerprint -> recall -> extract -> validate -> verify -> retry loop -> results."""
import collections
import io
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from PIL import Image

from . import docs, llm, memory

COLS = json.loads((Path(__file__).parent / "columns.json").read_text())
BY = {c["name"]: c for c in COLS}
EXTRACT = [c["name"] for c in COLS if c["source"] == "extract"]
MAX_RETRIES = 2
ACCEPT, RETRY_BELOW, BLANK_BELOW = 0.85, 0.7, 0.5  # accept / retry the column / blank the value
JEV_YES, JEV_NO = 0.85, 0.15  # Jev noul answers are decisive only outside this band
PAYEE = "- PAYEE_NAME (str): Issuer of the invoice: the supplier or utility name in the heading. Not: the billed customer, a payment bank or a collection agent."
RULES = """You read a utility invoice and fill columns. For each column return:
- value: the normalized value. Dates as YYYY-MM-DD. Numbers as plain decimals without thousands separators or units. null when the document does not show the column.
- raw: the value exactly as printed in the document (same digits, separators and date style). null when value is null.
- label: the caption printed for that value, exactly as printed. null if there is none.
- sheet: 0-based index of the sheet where the value is printed.
- conf: probability from 0 to 1 that the value (or its absence) is correct.
Never calculate a value; copy only what is printed. Do not use a near-match listed after "Not:"."""
ZOOM_Q = {
    "type": "noul",
    "instructions": "The unresolved columns will be read again. Should their page regions be enlarged first? "
    "Yes when a value is printed but is small, blurry or may be misread, unless MEMORY shows that enlarging does not help on invoices like this one. "
    "No when the unresolved columns have no printed value (they are probably not on the invoice), or when MEMORY shows that enlarging found nothing.",
}
ROUTE_Q = {
    "type": "noul",
    "instructions": "Does reading the billing fields from this document need careful multi-step reasoning or error recovery, "
    "for example a poor scan, handwriting, several meters or an unusual layout?",
}

# --- validators (pure code, no model) ---

NUM = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")
DATE_FORMATS = (
    "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%b %d,%Y", "%B %d,%Y", "%d %B %Y",
    "%m-%d-%Y", "%m/%d/%Y", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%m/%d/%y",
)  # fmt: skip


def alnum(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def to_decimal(s):
    """First number in the text as a Decimal ('₱57,820.76' -> 57820.76, '(903.65)' -> -903.65), else None."""
    s = str(s).strip()
    m = NUM.search(s)
    if not m:
        return None
    d = Decimal(m.group().replace(",", ""))
    return -d if "-" in s[: m.start()] or s.startswith("(") or s.endswith("-") else d


def date_readings(raw):
    """Every date the printed text can mean under the known formats (05-07-2026 has two readings)."""
    raw, out = re.sub(r"\s+", " ", str(raw).strip()), set()
    for f in DATE_FORMATS:
        try:
            out.add(datetime.strptime(raw, f).date())
        except ValueError:
            pass
    return out


NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.]\d{2,4}\s*$")


def date_order(raws):
    """'md' (month first) or 'dm' when printed numeric dates prove one order, else None.
    07/23/2026 can only be month first, so 06-05-2026 on the same invoice is 5 June."""
    orders = set()
    for raw in raws:
        m = NUMERIC_DATE.match(str(raw or ""))
        if m and max(int(m[1]), int(m[2])) > 12:
            orders.add("dm" if int(m[1]) > 12 else "md")
    return orders.pop() if len(orders) == 1 else None


def check_cell(name, cell, text, soft_evidence=False):
    """One extracted cell -> (typed value or None, [(hard, reason)]).
    text is alnum() of the document text. Evidence misses are soft when that text is a model transcription."""
    col, v, raw = BY[name], cell.get("value"), cell.get("raw")
    if v in (None, ""):
        return None, []
    if col["type"] == "enum":
        return (v, []) if v in col["options"] else (None, [(True, f"'{v}' is not one of {col['options']}")])
    reasons = []
    if col["type"] == "decimal":
        val, printed = to_decimal(v), to_decimal(raw or "")
        if val is None:
            return None, [(True, f"'{v}' is not a number")]
        if printed is None or abs(printed) != abs(val):
            reasons.append((True, f"value {v} does not match the printed text '{raw}'"))
        else:
            val = printed.copy_sign(val)  # keep the printed precision: 12.340 stays 12.340 whatever the model wrote
    elif col["type"] == "date":
        try:
            val = date.fromisoformat(str(v)[:10])
        except ValueError:
            return None, [(True, f"'{v}' is not a date")]
        readings = date_readings(raw or "")
        if readings and val not in readings:
            reasons.append((True, f"date {v} does not match the printed text '{raw}'"))
    else:
        val = str(v).strip()
        if alnum(val) not in alnum(raw or ""):
            reasons.append((True, f"value {v} does not match the printed text '{raw}'"))
    if not raw:
        reasons.append((True, "no printed text was given as evidence"))
    elif alnum(raw) not in text:
        reasons.append((not soft_evidence, f"printed text '{raw}' was not found in the document text"))
    return val, reasons


def cross_checks(vals, facts):
    """Checks across columns -> ({column: [(hard, reason)]}, [ids of formula facts that failed])."""
    out, failed = collections.defaultdict(list), []
    start, end, due = vals.get("START_DATE"), vals.get("END_DATE"), vals.get("DUE_DATE")
    if start and end and end < start:
        out["START_DATE"].append((True, "END_DATE is before START_DATE"))
        out["END_DATE"].append((True, "END_DATE is before START_DATE"))
    if start and end and end >= start and not 15 <= (end - start).days <= 62:
        why = f"the period is {(end - start).days} days long; a billing period is normally about one month"
        out["START_DATE"].append((False, why))
        out["END_DATE"].append((False, why))
    if start and due and due < start:
        out["DUE_DATE"].append((True, "DUE_DATE is before START_DATE"))
    numbers = {k: float(v) if isinstance(v, Decimal) else v for k, v in vals.items()}
    for f in facts:
        if not f.get("expr"):
            continue
        try:  # ponytail: restricted eval; use an AST whitelist if facts ever come from untrusted users
            ok = eval(f["expr"], {"__builtins__": {}, "abs": abs, "round": round}, dict(numbers))
        except Exception:
            continue  # a value the formula needs is missing
        if not ok:
            failed.append(f["id"])
            for name in EXTRACT:
                if re.search(rf"\b{name}\b", f["expr"]):
                    out[name].append((True, f"formula failed: {f['text']}"))
    return out, failed


def coverage(start):
    """Month of START_DATE; day 16 or later counts as the next month. Format YYYY-MM."""
    if start.day <= 15:
        return f"{start.year}-{start.month:02d}"
    return f"{start.year + (start.month == 12)}-{start.month % 12 + 1:02d}"


def calc(vals):
    """Calculated columns from typed values -> {name: (value, [input columns], note, needs a human check)}."""
    total, vat, ewt, usage = (vals.get(k) for k in ("GRAND_TOTAL", "VAT_COST", "EWT_COST", "USAGE"))
    utility = total - vat if total is not None and vat is not None else None
    out = {
        "COVERAGE": (coverage(vals["START_DATE"]) if vals.get("START_DATE") else None, ["START_DATE"], "", False),
        "UTILITY": (utility, ["GRAND_TOTAL", "VAT_COST"], *(("no VAT amount was read", True) if total is not None and vat is None else ("", False))),
        "NET_AMT": (
            total - abs(ewt or 0) if total is not None else None,
            ["GRAND_TOTAL"] + (["EWT_COST"] if ewt is not None else []),
            *(("EWT assumed 0", True) if total is not None and ewt is None else ("", False)),
        ),
    }
    if vals.get("RATE") is None and utility is not None and usage:
        rate = (utility / usage).quantize(Decimal("0.0001"))
        out["RATE"] = (rate, ["GRAND_TOTAL", "VAT_COST", "USAGE"], "calculated as UTILITY / USAGE", False)
    return out


def show(v):
    """Typed value -> output text."""
    return "" if v is None else format(v, "f") if isinstance(v, Decimal) else v.isoformat() if isinstance(v, date) else str(v)


# --- model steps ---


def fact_lines(facts):
    return "\n".join(f"- [{f['source']}] ({f['column']}) {f['text']}" for f in facts)


def extract(doc, names, model, facts, hints=None, problems=None, crops=()):
    """One Gemini call that reads the named columns from all sheets -> {name: {value, raw, label, sheet, conf}}."""
    sheets = doc["sheets"][:8]
    lines = [PAYEE if n == "PAYEE_NAME" else f"- {n} ({BY[n]['type']}): {BY[n]['desc']}. Not: {BY[n]['reject']}."
             + (f" One of: {', '.join(BY[n]['options'])}." if BY[n]["type"] == "enum" else "") for n in names]  # fmt: skip
    prompt = [RULES, "COLUMNS\n" + "\n".join(lines)]
    if facts:
        prompt.append("MEMORY - facts learned from earlier invoices. Facts marked [human] come from a reviewer and win over your own reading.\n" + fact_lines(facts))
    if hints:
        prompt.append("TEMPLATE HINTS - where earlier invoices with this layout printed each column.\n"
                      + "\n".join(f"- {n}: caption '{h['label']}', sheet {h['sheet']}"
                                  + (f"; on another invoice the printed text {h['raw']!r} meant {h['value']}" if h.get("raw") else "")
                                  for n, h in hints.items() if n in names))  # fmt: skip
    if problems:
        prompt.append("PREVIOUS ATTEMPT - these values were rejected. Read these columns again with care.\n"
                      + "\n".join(f"- {n}: value {d['value']!r} (printed {d['raw']!r}): {'; '.join(d['reasons']) or 'low confidence'}" for n, d in problems.items()))  # fmt: skip
    for i, s in enumerate(sheets):
        kind = "text layer" if s["has_text"] else "machine transcription, may contain errors"
        prompt.append(f"SHEET {i} TEXT ({kind})\n{s['text'][:15000]}")
    prompt.append(f"The sheet images follow in order (sheet 0 first){', then enlarged crops of the hard regions' if crops else ''}.")
    cell = {
        "type": "object",
        "properties": {
            "value": {"type": ["string", "null"]},
            "raw": {"type": ["string", "null"]},
            "label": {"type": ["string", "null"]},
            "sheet": {"type": "integer"},
            "conf": {"type": "number"},
        },
        "required": ["value", "raw", "label", "sheet", "conf"],
    }
    schema = {"type": "object", "properties": {n: cell for n in names}, "required": list(names)}
    parts = ["\n\n".join(prompt)] + [Path(s["png"]).read_bytes() for s in sheets] + list(crops)
    out = llm.gemini(model, parts, schema)
    return {n: out.get(n) or {"value": None, "raw": None, "label": None, "sheet": 0, "conf": 0.0} for n in names}


def verify(doc, cells, names, text):
    """Jev guard: one call, one yes/no question per non-empty column -> {name: P(yes)}. Empty on a Mesh error."""
    qs = {
        n: {
            "type": "noul",
            "instructions": f"Is '{cells[n]['value'] if BY[n]['type'] == 'enum' else cells[n]['raw'] or cells[n]['value']}' the right value for this field of the invoice: "
            f"{BY[n]['desc']}? Answer no if it is instead: {BY[n]['reject']}.",
        }
        for n in names
        if cells[n].get("value") not in (None, "")
    }
    if not qs:
        return {}
    images = [Path(s["png"]).read_bytes() for s in doc["sheets"] if not s["has_text"]][:2]
    try:
        answers = llm.jev(("INVOICE TEXT\n" + text)[:12000], qs, images)
    except Exception as e:
        llm._count("jev.errors")
        print(f"  jev error at verify: {str(e)[:200]}")
        return {}
    for n, a in answers.items():  # shadow log: Jev's verdict next to Gemini's own confidence
        used = "jev" if a["noul"] >= JEV_YES or a["noul"] <= JEV_NO else "llm"
        llm.LOG.append((time.time(), doc["doc_id"], f"verify:{n}", json.dumps(a["noul"] >= 0.5), max(a["noul"], 1 - a["noul"]),
                        json.dumps(float(cells[n].get("conf") or 0) >= ACCEPT), used))  # fmt: skip
    return {n: a["noul"] for n, a in answers.items()}


def bar(d, level):
    """Confidence a cell needs. A value needs the full level. An empty cell with no failed check only needs 0.5:
    the model says absence is more likely than not, and re-reading an absent column (core loss, penalty) finds nothing."""
    empty = d.get("value", d.get("extracted")) in (None, "") and not d["reasons"]
    return BLANK_BELOW if empty else level


def assess(doc, cells, facts, jev_p, known_dates=()):
    """All checks on the current cells -> ({name: detail}, [failed formula fact ids]).
    known_dates: printed dates from earlier invoices of the same template, as extra proof of the day/month order."""
    text = alnum(" ".join(s["text"] for s in doc["sheets"]))
    soft = not all(s["has_text"] for s in doc["sheets"])
    typed, reasons = {}, {}
    for n in EXTRACT:
        typed[n], reasons[n] = check_cell(n, cells[n], text, soft)
    dates = [n for n in EXTRACT if BY[n]["type"] == "date"]
    order = date_order([cells[n].get("raw") for n in dates] + list(known_dates))
    for n in dates:  # a date that can be read two ways follows the order proven by the other dates
        m = typed[n] and order and NUMERIC_DATE.match(str(cells[n].get("raw") or ""))
        if m and max(int(m[1]), int(m[2])) <= 12:
            month, day = (int(m[1]), int(m[2])) if order == "md" else (int(m[2]), int(m[1]))
            typed[n] = typed[n].replace(month=month, day=day)
    cross, failed = cross_checks(typed, facts)
    detail = {}
    for n in EXTRACT:
        rs, p = reasons[n] + cross.get(n, []), jev_p.get(n)
        conf = float(cells[n].get("conf") or 0)
        if p is not None and (p >= JEV_YES or p <= JEV_NO):
            conf = p
        if p is not None and p <= JEV_NO:
            rs.append((False, "the Jev guard rejected this value"))
        if any(not hard for hard, _ in rs):
            conf = min(conf, 0.6)
        if any(hard for hard, _ in rs):
            conf = 0.0
        detail[n] = {"value": typed[n], "raw": cells[n].get("raw"), "label": cells[n].get("label"), "sheet": cells[n].get("sheet"),
                     "conf": round(conf, 3), "reasons": [r for _, r in rs], "jev": p}  # fmt: skip
        detail[n]["retry"] = conf < bar(detail[n], RETRY_BELOW)
    return detail, failed


def route(doc, text):
    """Model for the first extraction of an unknown layout. Jev says 'easy' with P <= 0.15 -> Flash-Lite, else Flash."""
    llm.emit(doc["doc_id"], "route")
    try:
        p = llm.jev(("INVOICE TEXT (start)\n" + text)[:6000], {"q": ROUTE_Q})["q"]["noul"]
    except Exception:
        llm._count("jev.errors")
        p = None
    model = llm.LITE if p is not None and p <= JEV_NO else llm.FLASH
    llm.emit(doc["doc_id"], "route", model=model, p=p)
    conf = None if p is None else max(p, 1 - p)
    llm.LOG.append((time.time(), doc["doc_id"], "route", json.dumps(None if p is None else p >= 0.5), conf, "null",
                    "jev" if conf is not None and conf >= JEV_YES else "llm"))  # fmt: skip
    return model


def zoom(doc, names):
    """2x crops of the page regions that show the named columns; Flash-Lite finds the boxes -> [PNG bytes].
    ponytail: crops come from the 1568 px page image; render the PDF clip at a higher dpi if small print stays unreadable."""
    box = {"type": ["array", "null"], "items": {"type": "integer"}}
    schema = {"type": "object", "properties": {n: box for n in names}, "required": list(names)}
    ask = ("For each field return the box [ymin, xmin, ymax, xmax] on a 0-1000 scale around its caption and value in this image, "
           "or null when the field is not on this page.\n" + "\n".join(f"- {n}: {BY[n]['desc']}" for n in names))  # fmt: skip
    crops = []
    for sheet in doc["sheets"][:2]:
        png = Path(sheet["png"]).read_bytes()
        im = Image.open(io.BytesIO(png))
        for b in llm.gemini(llm.LITE, [ask, png], schema).values():
            if not b or len(b) != 4:
                continue
            y0, x0, y1, x1 = b
            crop = im.crop((max(0, x0 - 60) * im.width // 1000, max(0, y0 - 30) * im.height // 1000,
                            min(1000, x1 + 60) * im.width // 1000, min(1000, y1 + 30) * im.height // 1000))  # fmt: skip
            if crop.width > 8 and crop.height > 8:
                buf = io.BytesIO()
                crop.resize((crop.width * 2, crop.height * 2)).save(buf, "PNG")
                crops.append(buf.getvalue())
    return crops[:6]


def want_zoom(doc, about, past, detail, failing, history):
    """Cascade decision before a retry: enlarge the regions of the failing columns first? -> (bool, who answered what).
    Jev and Flash-Lite read the same state: what is known about this invoice (about) and how zoom worked on invoices
    like it (past). Jev decides alone only at a confidence where its outcome score is good (memory.jev_tau).
    State follows the paper's template."""
    resolved = "; ".join(f"{n}={show(d['value'])}" for n, d in detail.items() if n not in failing)
    unresolved = "; ".join(f"{n}={detail[n]['raw']!r} ({', '.join(detail[n]['reasons']) or 'low confidence'})" for n in failing)
    state = (
        f"TASK\nExtract these invoice columns: {', '.join(EXTRACT)}\n\nCONTEXT\n{about}\n\n"
        f"MEMORY - what happened on earlier invoices when the page regions were enlarged, and when they were not\n{past or '(no earlier results)'}\n\n"
        "HISTORY\n" + "\n".join(h[:400] for h in history[-8:]) + f"\n\nCURRENT OBSERVATION\n{resolved[:800]}\nUNRESOLVED: {unresolved[:1200]}"
    )[:12000]
    images = [Path(s["png"]).read_bytes() for s in doc["sheets"] if not s["has_text"]][:2]
    schema = {"type": "object", "properties": {"zoom": {"type": "boolean"}}, "required": ["zoom"]}
    ask = lambda: llm.gemini(llm.LITE, [f"{ZOOM_Q['instructions']}\n\n{state}"] + images, schema)["zoom"]
    who = {}
    tau = memory.jev_tau("zoom")
    if tau is None or random.random() < 0.1:  # 1 time in 10 Flash-Lite decides anyway and its pick runs: an answer that
        tau = 2  # is only logged has no outcome, and Jev's "no" could never be proven wrong. 2 = Jev only answers in the background
    return llm.decide("zoom", state, ZOOM_Q, ask, tau, images, doc["doc_id"], shadow=0, info=who), who


# --- one invoice ---


def process(doc, batch=""):
    t0 = time.perf_counter()
    with llm.track() as used:
        text = "\n\n".join(s["text"] for s in doc["sheets"])
        toks = docs.tokens(doc["sheets"][0]["text"])
        tpl = memory.match_template(toks)
        vendor = tpl["vendor"] if tpl else memory.guess_vendor(toks)
        scopes = ["global"] + ([f"vendor:{vendor}"] if vendor else []) + ([f"template:{tpl['id']}"] if tpl else [])
        facts = memory.recall(scopes)
        fast = bool(tpl and tpl["ok_runs"] >= 2)  # known layout: one Flash-Lite call with hints
        kind = "text-layer" if all(s["has_text"] for s in doc["sheets"]) else "scanned"
        ev = lambda step, **data: llm.emit(doc["doc_id"], step, **data)  # the UI's step log
        seen = lambda: ev("assess", cols={n: {k: d[k] for k in ("value", "raw", "conf", "reasons", "retry")} for n, d in detail.items()})
        told = lambda fs: [{k: f[k] for k in ("source", "column", "text")} for f in fs]
        ev("fingerprint", template=tpl and tpl["id"], vendor=vendor, runs=tpl and tpl["runs"], ok_runs=tpl and tpl["ok_runs"],
           path="fast" if fast else "explore", kind=kind)  # fmt: skip
        ev("recall", facts=told(facts))
        model = llm.LITE if fast else route(doc, text)
        hints = json.loads(tpl["hints"]) if tpl else {}
        known_dates = [h.get("raw") for n, h in hints.items() if BY[n]["type"] == "date"]
        ev("extract", model=model, cols=EXTRACT, hints=fast)
        cells = extract(doc, EXTRACT + ["PAYEE_NAME"], model, facts, hints=hints if fast else None)
        ev("verify")
        jev_p = verify(doc, cells, EXTRACT, text)
        detail, bad_facts = assess(doc, cells, facts, jev_p, known_dates)
        seen()
        history, retries, tools = [f"extract(all columns, {model})"], 0, []
        about = (f"Known layout: seen {tpl['runs']} times before, {tpl['ok_runs']} of them read without flags." if tpl else "New layout, not seen before.") \
            + f" Supplier: {vendor or 'unknown'}. Document: {kind}."  # fmt: skip
        past = memory.tool_record("zoom", tpl and tpl["id"], vendor, kind)
        while retries < MAX_RETRIES:  # stop is decided in code, never by a model
            failing = [n for n in EXTRACT if detail[n]["retry"]]
            if not failing:
                break
            spent, known = sum(llm.cost(used).values()), {f["id"] for f in facts}
            more = [f for f in memory.recall(scopes, query=" ".join(failing)) if f["id"] not in known]  # free lookup: no model picks it
            if more:
                facts = facts + more
                history.append(f"recall({', '.join(failing)}) -> {len(more)} new facts")
                ev("recall", facts=told(more))
            ev("zoom_decision")
            zoomed, who = want_zoom(doc, about, past, detail, failing, history)
            ev("zoom_decision", zoom=zoomed, **who)
            if zoomed:
                ev("zoom", cols=failing[:4])
            crops = zoom(doc, failing[:4]) if zoomed else []
            if zoomed:
                history.append(f"zoom({', '.join(failing)}) -> {len(crops)} crops")
            ev("extract", model=llm.FLASH, cols=failing, crops=len(crops))
            new = extract(doc, failing, llm.FLASH, facts, problems={n: detail[n] for n in failing}, crops=crops)
            cells.update(new)
            for n in failing:
                jev_p.pop(n, None)
            ev("verify")
            jev_p.update(verify(doc, new, failing, text))
            detail, bad_facts = assess(doc, cells, facts, jev_p, known_dates)
            seen()
            retries += 1
            left = sum(detail[n]["retry"] for n in failing)
            history.append(f"extract({', '.join(failing)}) -> {left} of {len(failing)} still unresolved")
            # the outcome of this decision; run() stores it and later decisions read it back through memory.tool_record
            tools.append({"ts": time.time(), "tool": "zoom", "kind": kind, "cols": failing, **who, "ran": zoomed, "found": len(crops),
                          "fixed": len(failing) - left, "total": len(failing), "cost": round(sum(llm.cost(used).values()) - spent, 6)})  # fmt: skip

        vals = {n: detail[n]["value"] if detail[n]["conf"] >= BLANK_BELOW else None for n in EXTRACT}
        for name, (v, inputs, note, check) in calc(vals).items():
            detail[name] = {"value": v, "raw": None, "label": None, "sheet": None, "reasons": [note] if note else [], "jev": None,
                            "conf": min([detail[i]["conf"] for i in inputs] + [0.6] * check)}  # fmt: skip
        ev("calc", cols={n: {"value": detail[n]["value"], "reasons": detail[n]["reasons"]} for n in calc(vals)})
        values = {}
        for c in COLS:
            d = detail.get(c["name"])
            if d is None:  # reserved or application-owned column
                values[c["name"]] = ""
                continue
            d["flag"] = "" if d["conf"] >= bar(d, ACCEPT) else "check"
            d["extracted"] = show(d.pop("value"))  # what the model read, kept for review
            values[c["name"]] = d["extracted"] if d["conf"] >= BLANK_BELOW else ""
    tokens = sum(used[f"{m}.{k}"] for m in (llm.FLASH, llm.LITE) for k in ("in", "out"))
    return {"doc_id": doc["doc_id"], "file": doc["file"], "batch": batch, "path": "fast" if fast else "explore", "model": model,
            "template": tpl and tpl["id"], "vendor": vendor, "payee": cells["PAYEE_NAME"].get("value"), "retries": retries,
            "steps": history, "tools": tools, "tokens": tokens, "usage": dict(used), "cost": round(sum(llm.cost(used).values()), 6), "seconds": round(time.perf_counter() - t0, 1), "facts_used": [f["id"] for f in facts],
            "bad_facts": bad_facts, "empty": not any(values[n] for n in EXTRACT if BY[n]["type"] != "enum"),
            "values": values, "detail": detail}  # fmt: skip


# --- one batch ---


def mark_duplicates(results):
    """Scans often show two copies of one bill, plus tear-off stubs. Invoices of one file with the same invoice and
    account number are one invoice: the most complete copy is kept, the others point to it."""
    groups = collections.defaultdict(list)
    for r in results:
        v = r.get("values", {})
        if v.get("INVOICE_NO") and v.get("ACCOUNT_NO"):
            groups[r["file"], alnum(v["INVOICE_NO"]), alnum(v["ACCOUNT_NO"])].append(r)
    for group in groups.values():
        keep = max(group, key=lambda r: sum(bool(x) for x in r["values"].values()))
        for r in group:
            if r is not keep:
                r["duplicate_of"] = keep["doc_id"]


def relink(documents, results):
    """Supplementary documents support the invoices of the same file whose invoice number appears in their text."""
    numbers = {r["doc_id"]: (r["file"], alnum(r["values"]["INVOICE_NO"])) for r in results if r.get("values", {}).get("INVOICE_NO")}
    for d in documents:
        if d["type"] == "supplementary":
            text = alnum(" ".join(s["text"] for s in d["sheets"]))
            hits = [i for i, (f, no) in numbers.items() if f == d["file"] and len(no) >= 5 and no in text]
            d["supports"] = hits or d["supports"]


def self_learn(doc, r):
    """No-review learning: captions of accepted columns become vendor facts. memory.remember keeps them as
    candidates until 2 documents agree with no contradiction. Jev gates each write against the invoice text."""
    if not r["vendor"]:
        return
    cands = {n: d["label"] for n in EXTRACT
             if (d := r["detail"][n])["label"] and d["extracted"] and d["conf"] >= ACCEPT and not d["reasons"]
             and BY[n]["type"] != "enum" and memory.norm_text(d["label"])}  # fmt: skip
    if not cands:
        return
    qs = {n: {"type": "noul", "instructions": f'Is "{label}" a fixed field label printed on this invoice form, as opposed to a value '
              "such as a number, a date, a name or an address?"} for n, label in cands.items()}  # fmt: skip
    try:
        answers = llm.jev(("INVOICE TEXT\n" + "\n\n".join(s["text"] for s in doc["sheets"]))[:12000], qs)
    except Exception:
        llm._count("jev.errors")
        return
    for n, label in cands.items():
        p = answers[n]["noul"]
        llm.LOG.append((time.time(), r["doc_id"], "memory_write", json.dumps(p >= 0.5), max(p, 1 - p), "null", "jev" if p >= JEV_YES else "llm"))
        if p >= JEV_YES:
            memory.remember(f"vendor:{r['vendor']}", n, "alias", f'{n} is printed under the caption "{label}"', source="self", doc_id=r["doc_id"])
    llm.emit(r["doc_id"], "memory", captions={n: label for n, label in cands.items() if answers[n]["noul"] >= JEV_YES})


def run(in_dir, out):
    """Segment, extract every invoice, write results.jsonl and usage.json, then update memory."""
    out, t0 = Path(out), time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    llm.EVENTS = open(out / "events.jsonl", "w")  # step log for `opex ui`, live and after the run
    llm.emit("", "run_start", batch=out.name)
    documents = docs.segment(in_dir, out)
    seg, seg_seconds = llm.USAGE.copy(), round(time.perf_counter() - t0)  # per-invoice numbers do not include this part
    invoices = [d for d in documents if d["type"] == "invoice"]
    llm.emit("", "segmented", seconds=seg_seconds, cost=round(sum(llm.cost(seg).values()), 6), invoices=len(invoices))

    def safe(doc):  # one bad invoice must not stop the batch
        try:
            r = process(doc, out.name)
        except Exception as e:
            print(f"  extraction failed for {doc['doc_id']}: {str(e)[:200]}")
            r = {"doc_id": doc["doc_id"], "file": doc["file"], "error": str(e)[:500]}
        llm.emit(doc["doc_id"], "done", **{k: r.get(k) for k in ("path", "model", "retries", "empty", "cost", "seconds", "error")},
                 flags=[n for n, d in r.get("detail", {}).items() if d["flag"]])  # fmt: skip
        return r

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(safe, invoices))
    mark_duplicates(results)
    relink(documents, results)
    (out / "documents.json").write_text(json.dumps(documents, indent=1))
    for doc, r in zip(invoices, results):  # memory updates run in order, in this thread
        if "error" in r or r["empty"]:  # nothing was read (for example a photo printout): not a layout worth remembering
            continue
        ok = not any(r["detail"][n]["flag"] for n in EXTRACT)
        r["vendor"] = r["vendor"] or memory.norm_vendor(r["payee"])
        hints = {n: {"label": d["label"], "sheet": d["sheet"], "raw": d["raw"], "value": d["extracted"]}
                 for n in EXTRACT if (d := r["detail"][n])["label"] and d["extracted"] and not d["flag"]}
        r["template"] = memory.save_template(docs.tokens(doc["sheets"][0]["text"]), r["vendor"], hints, ok)
        conf = [r["detail"][n]["conf"] for n in EXTRACT]
        memory.save_run(r["doc_id"], out.name, r["template"], r["path"], r["values"], sum(conf) / len(conf), r["tokens"], r["seconds"])
        memory.miss(r["bad_facts"])
        llm.emit(r["doc_id"], "memory", template=r["template"], ok=ok, duplicate_of=r.get("duplicate_of"))
        if "duplicate_of" not in r:
            self_learn(doc, r)
    memory.save_log(llm.LOG)
    llm.LOG.clear()
    decisions = [{**t, "batch": out.name, "doc_id": r["doc_id"], "template": r["template"], "vendor": r["vendor"], "cols": json.dumps(t["cols"])}
                 for r in results for t in r.get("tools", [])]  # fmt: skip
    memory.save_tools(decisions)
    (out / "results.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in results))
    good = [r for r in results if "error" not in r]
    flags = collections.Counter(n for r in good for n, d in r["detail"].items() if d["flag"])
    summary = {
        "files": len({d["file"] for d in documents}), "documents": dict(collections.Counter(d["type"] for d in documents)),
        "errors": len(results) - len(good), "duplicates": sum("duplicate_of" in r for r in good), "empty": sum(r["empty"] for r in good),
        "paths": dict(collections.Counter(r["path"] for r in good)), "retries": sum(r["retries"] for r in good),
        "invoices_without_flags_on_extracted_columns": sum(not r["empty"] and not any(r["detail"][n]["flag"] for n in EXTRACT) for r in good),
        "flags_per_column": dict(flags.most_common()), "seconds": round(time.perf_counter() - t0),
        "tools": {"zoom_decisions": len(decisions), "decided_by": dict(collections.Counter(t["used"] for t in decisions)),
                  "zoom_ran": sum(t["ran"] for t in decisions), "zoom_found_nothing": sum(t["ran"] and not t["found"] for t in decisions),
                  "columns_fixed": [sum(t["fixed"] for t in decisions), sum(t["total"] for t in decisions)],
                  "jev_tau_next_run": memory.jev_tau("zoom"), "right_of_scored": memory.tool_scores("zoom", memory.jev_tau("zoom") or 0.6)},
        "usage": dict(llm.USAGE), "cost_usd_estimate": llm.cost(), "shadow": llm.SHADOW,
        "segment": {"seconds": seg_seconds, "usage": dict(seg), "cost_usd_estimate": llm.cost(seg)},
    }  # fmt: skip
    (out / "usage.json").write_text(json.dumps(summary, indent=1))
    llm.emit("", "finished", summary=summary)
    llm.EVENTS.close()
    llm.EVENTS = None
    print(json.dumps(summary, indent=1))
    return results
