"""Learning from human review: saved corrections -> memory facts."""
import json
import threading
from pathlib import Path

from . import harness, llm, memory

_lock = threading.Lock()  # review.json is read and written by server threads
BLANK = "(blank)"  # what a reviewer types when the correct value is empty
PROMPT = """A reviewer corrected one column of a utility invoice that was read by a model. Explain the mistake and write facts that prevent it on future invoices.

COLUMN: {col} - {desc}. Not: {reject}.
MODEL READ: value {extracted!r}, printed text {raw!r}, caption {label!r}
CORRECT VALUE: {correct}
REVIEWER NOTE: {note}
FACTS THE MODEL HAD:
{facts}

Write 1 to 3 short facts. Each fact has:
- scope: "template" (only this layout), "vendor" (every invoice from this supplier) or "global" (every invoice). Use the narrowest scope that is true.
- kind: one word such as alias, location, format, rule or formula.
- text: one sentence that a reader can apply without seeing this invoice. Name captions and positions. Do not include this invoice's own values.
- expr: null, or a Python expression over column names that is true on every correct extraction (numbers only: + - * /, abs, round, comparisons). Column names: {names}.
"""
SCHEMA = {
    "type": "object",
    "properties": {
        "why_wrong": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["template", "vendor", "global"]},
                    "kind": {"type": "string"},
                    "text": {"type": "string"},
                    "expr": {"type": ["string", "null"]},
                },
                "required": ["scope", "kind", "text", "expr"],
            },
        },
    },
    "required": ["why_wrong", "facts"],
}


def load(out):
    """-> (documents by id, results by id, saved reviews by id)."""
    out = Path(out)
    documents = {d["doc_id"]: d for d in json.loads((out / "documents.json").read_text())}
    results = {r["doc_id"]: r for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}
    return documents, results, load_reviews(out)


def load_reviews(out):
    path = Path(out) / "review.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_review(out, doc_id, rows, **extra):
    """Store one document's review. rows: {column: {"correct": text, "note": text}}; a column not in rows is accepted."""
    with _lock:
        path = Path(out) / "review.json"
        reviews = json.loads(path.read_text()) if path.exists() else {}
        reviews[doc_id] = {"rows": rows, "learned": False, **extra}
        path.write_text(json.dumps(reviews, indent=1))


def same(col, typed, correct):
    """Does a typed value equal the reviewer's text?"""
    if correct == BLANK:
        return typed is None
    kind = harness.BY[col]["type"]
    if kind == "decimal":
        return typed is not None and typed == harness.to_decimal(correct)
    if kind == "date":
        return typed is not None and (harness.show(typed) == correct or typed in harness.date_readings(correct))
    return harness.alnum(harness.show(typed)) == harness.alnum(correct)


def learn_doc(out, doc_id):
    """Turn one saved review into facts. Returns the facts written (list of dicts)."""
    documents, results, reviews = load(out)
    doc, r, rows = documents[doc_id], results[doc_id], reviews[doc_id]["rows"]
    corrected = {c: x for c, x in rows.items() if c in harness.EXTRACT and (x.get("correct") or x.get("note"))}
    used = memory.q(f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(r['facts_used']))})", r["facts_used"]) if r["facts_used"] else []
    memory.hit([f["id"] for f in used if f["column"] in harness.EXTRACT and f["column"] not in corrected])  # accepted rows are hits
    scope_of = {"global": "global", "vendor": f"vendor:{r['vendor']}" if r["vendor"] else "global",
                "template": f"template:{r['template']}" if r["template"] else "global"}  # fmt: skip
    text = harness.alnum(" ".join(s["text"] for s in doc["sheets"]))
    sheets = "\n\n".join(f"SHEET {i} TEXT\n{s['text'][:15000]}" for i, s in enumerate(doc["sheets"][:8]))
    images = [Path(s["png"]).read_bytes() for s in doc["sheets"][:8]]
    written = []
    for col, row in corrected.items():
        d, correct = r["detail"][col], (row.get("correct") or "").strip()
        memory.miss([f["id"] for f in used if f["column"] == col and f["source"] == "self"])  # self facts that did not prevent the error
        memory.drop_hint(r["template"], col)
        memory.mark_corrected(r["batch"], doc_id, col)  # the tool decisions made for this column did not work
        prompt = PROMPT.format(
            col=col, desc=harness.BY[col]["desc"], reject=harness.BY[col]["reject"], extracted=d["extracted"], raw=d["raw"],
            label=d["label"], correct=correct or "(not given, see the note)", note=row.get("note") or "(none)",
            facts=harness.fact_lines([f for f in used if f["column"] == col]) or "(none)", names=", ".join(harness.EXTRACT),
        )  # fmt: skip
        answer = llm.gemini(llm.FLASH, [prompt + "\n" + sheets] + images, SCHEMA)
        new = [{"id": 0, "source": "human", "column": col, **f} for f in answer["facts"][:3]]
        # regression guard: the cheap model must now read the corrected value with only the new facts
        confidence = 0.6
        if correct and new:
            cell = harness.extract(doc, [col], llm.LITE, new)[col]
            typed, _ = harness.check_cell(col, cell, text, True)
            confidence = 1.0 if same(col, typed, correct) else 0.6
        for f in new:
            fid = memory.remember(scope_of[f["scope"]], col, f["kind"], f["text"], f["expr"], confidence, "human", doc_id)
            written.append({"id": fid, "scope": scope_of[f["scope"]], "column": col, "kind": f["kind"], "text": f["text"],
                            "expr": f["expr"], "confidence": confidence, "why_wrong": answer["why_wrong"]})  # fmt: skip
    with _lock:
        path = Path(out) / "review.json"
        reviews = json.loads(path.read_text())
        reviews[doc_id].update(learned=True, facts=written)
        path.write_text(json.dumps(reviews, indent=1))
    return written


def learn_batch(out):
    """Learn from every saved review that was not learned yet."""
    _, _, reviews = load(out)
    for doc_id, rev in reviews.items():
        if not rev.get("learned"):
            facts = learn_doc(out, doc_id)
            print(f"{doc_id}: {len(facts)} facts")
            for f in facts:
                print(f"  [{f['scope']}] ({f['column']}, confidence {f['confidence']}) {f['text']}")
