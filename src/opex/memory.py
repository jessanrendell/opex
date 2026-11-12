"""Memory in SQLite: facts, templates, runs and the decision log."""
import json
import re
import sqlite3
import threading
import time

DB = "memory.db"
_lock, _con = threading.RLock(), None

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, scope TEXT, column TEXT, kind TEXT, text TEXT, expr TEXT,
  confidence REAL, source TEXT, status TEXT, hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0, docs TEXT DEFAULT '[]', created REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, column, kind);
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY, vendor TEXT, tokens TEXT, hints TEXT DEFAULT '{}',
  runs INTEGER DEFAULT 0, ok_runs INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs(doc_id TEXT, batch TEXT, template INTEGER, path TEXT, vals TEXT, confidence REAL, tokens INTEGER, seconds REAL);
CREATE TABLE IF NOT EXISTS log(ts REAL, doc_id TEXT, point TEXT, jev_answer TEXT, jev_conf REAL, llm_answer TEXT, used TEXT);
CREATE TABLE IF NOT EXISTS tools(ts REAL, batch TEXT, doc_id TEXT, template INTEGER, vendor TEXT, kind TEXT, tool TEXT, cols TEXT,
  jev INTEGER, jev_conf REAL, llm INTEGER, used TEXT, ran INTEGER, found INTEGER, fixed INTEGER, total INTEGER, cost REAL,
  corrected INTEGER DEFAULT 0);
"""
TOOL_KEYS = "ts batch doc_id template vendor kind tool cols jev jev_conf llm used ran found fixed total cost".split()
MONTHS = "january february march april may june july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec"
VENDOR_NOISE = {"inc", "corp", "corporation", "company", "the", "and", "incorporated", "formerly"}


def q(sql, args=(), many=False):
    """Run one statement under a lock (worker threads share the connection). Returns rows as dicts."""
    global _con
    with _lock:
        if _con is None:
            _con = sqlite3.connect(DB, check_same_thread=False)
            _con.row_factory = sqlite3.Row
            _con.executescript(SCHEMA)
        cur = _con.executemany(sql, args) if many else _con.execute(sql, args)
        rows = [] if many else [dict(r) for r in cur.fetchall()]
        _con.commit()
        return rows


def reset(path):
    """Point memory at another database file (tests use ':memory:')."""
    global DB, _con
    DB, _con = path, None


# --- templates ---


def norm_vendor(name):
    words = [w for w in re.findall(r"[a-z]{3,}", (name or "").lower()) if w not in VENDOR_NOISE]
    return " ".join(words[:4])


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def match_template(tokens):
    """Best stored template of the same vendor with Jaccard >= 0.6 on label tokens, else None.
    Same vendor = the template's vendor name is printed on this sheet (sister utilities share one layout).
    ponytail: linear scan over all templates; index by vendor if there are thousands."""
    scored = [(jaccard(tokens, set(json.loads(t["tokens"]))), t) for t in q("SELECT * FROM templates") if set(t["vendor"].split()) <= tokens]
    score, best = max(scored, key=lambda x: x[0], default=(0, None))
    return best if score >= 0.6 else None


def guess_vendor(tokens):
    """A known vendor whose name words all appear on the first sheet. No model call."""
    hits = [t["vendor"] for t in q("SELECT DISTINCT vendor FROM templates WHERE vendor != ''") if set(t["vendor"].split()) <= tokens]
    return max(hits, key=len, default="")


def save_template(tokens, vendor, hints, ok):
    """Record one run on a template (creating it on first sight). Hints are merged only from ok runs."""
    t = match_template(tokens)
    if t is None:
        return q(
            "INSERT INTO templates(vendor, tokens, hints, runs, ok_runs) VALUES(?,?,?,1,?) RETURNING id",
            (vendor, json.dumps(sorted(tokens)), json.dumps(hints if ok else {}), int(ok)),
        )[0]["id"]
    merged = {**json.loads(t["hints"]), **(hints if ok else {})}
    q(
        "UPDATE templates SET runs = runs + 1, ok_runs = ok_runs + ?, hints = ?, vendor = ? WHERE id = ?",
        (int(ok), json.dumps(merged), t["vendor"] or vendor, t["id"]),
    )
    return t["id"]


# --- facts ---


def norm_text(text):
    """Text key used to see that two documents produced the same fact: lowercase letters only, no month names."""
    return " ".join(w for w in re.findall(r"[a-z]+", text.lower()) if w not in MONTHS.split())


def activation(kind, n_docs, misses):
    """Self-learned facts start as candidates (never injected). They become active after 2 agreeing documents
    (3 for formulas) with no contradiction."""
    return "active" if misses == 0 and n_docs >= (3 if kind == "formula" else 2) else "candidate"


def remember(scope, column, kind, text, expr=None, confidence=0.8, source="self", doc_id=None):
    """Store a fact. Human facts are active at once. Self facts follow the activation rule; a different
    self fact of the same kind for the same scope and column counts as a contradiction for both."""
    if source == "self":
        same_slot = q("SELECT * FROM facts WHERE source='self' AND scope=? AND column=? AND kind=? AND status!='retired'", (scope, column, kind))
        twin = next((f for f in same_slot if norm_text(f["text"]) == norm_text(text)), None)
        if twin:
            seen = sorted(set(json.loads(twin["docs"])) | {doc_id})
            q("UPDATE facts SET docs=?, status=? WHERE id=?", (json.dumps(seen), activation(kind, len(seen), twin["misses"]), twin["id"]))
            return twin["id"]
        miss([f["id"] for f in same_slot])
        status, misses = "candidate", int(bool(same_slot))
    else:
        status, misses = "active", 0
    fid = q(
        "INSERT INTO facts(scope, column, kind, text, expr, confidence, source, status, misses, docs, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (scope, column, kind, text, expr, confidence, source, status, misses, json.dumps([doc_id] if doc_id else []), time.time()),
    )[0]["id"]
    q("INSERT INTO facts_fts(rowid, text, column, kind) VALUES(?,?,?,?)", (fid, text, column, kind))
    return fid


def recall(scopes, query=None, limit=20):
    """Active facts for these scopes, best first. A query narrows by full-text match.
    A self fact never shows next to a human fact on the same scope and column."""
    rows = q(f"SELECT * FROM facts WHERE status='active' AND scope IN ({','.join('?' * len(scopes))})", scopes)
    if query:
        terms = " OR ".join(f'"{t}"' for t in re.findall(r"\w+", query))
        ids = {r["rowid"] for r in q("SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?", (terms,))} if terms else set()
        rows = [r for r in rows if r["id"] in ids]
    human = {(r["scope"], r["column"]) for r in rows if r["source"] == "human"}
    rows = [r for r in rows if r["source"] == "human" or (r["scope"], r["column"]) not in human]
    rows.sort(key=lambda r: -r["confidence"] * (r["hits"] + 1) / (r["misses"] + 1))
    return rows[:limit]


def hit(ids):
    q("UPDATE facts SET hits = hits + 1 WHERE id = ?", [(i,) for i in ids], many=True)


def miss(ids):
    """Count a miss. A self fact with more misses than hits is retired."""
    q("UPDATE facts SET misses = misses + 1 WHERE id = ?", [(i,) for i in ids], many=True)
    q("UPDATE facts SET status = 'retired' WHERE source = 'self' AND status = 'active' AND misses > hits")


# --- runs and decision log ---


def save_run(doc_id, batch, template, path, values, confidence, tokens, seconds):
    q("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", (doc_id, batch, template, path, json.dumps(values), confidence, tokens, seconds))


def save_log(rows):
    q("INSERT INTO log VALUES(?,?,?,?,?,?,?)", rows, many=True)


# --- tool outcomes: what happened after each "run this tool before reading again?" decision ---


def save_tools(rows):
    """rows: dicts with the TOOL_KEYS. jev and llm are each model's yes/no answer (None when it did not answer),
    ran says if the tool ran, found is what it produced, fixed of total is how many failing columns the next read cleared."""
    q(f"INSERT INTO tools({','.join(TOOL_KEYS)}) VALUES({','.join(':' + k for k in TOOL_KEYS)})", rows, many=True)


def tool_label(r):
    """Was running the tool the right answer? True or False, or None when the outcome does not tell.
    The label comes only from what happened, never from what a model picked."""
    if r["corrected"]:  # a reviewer corrected one of these columns: what ran did not work
        return not r["ran"]
    if r["ran"]:
        return False if not r["found"] else True if r["fixed"] else None
    return False if r["fixed"] == r["total"] else None


def tool_record(tool, template, vendor, kind):
    """Past outcomes of a tool as prompt text, most specific first: this layout, this supplier, all invoices of this kind."""
    lines = []
    for name, col, val in (("This layout", "template", template), ("This supplier", "vendor", vendor), (f"All {kind} invoices", "kind", kind)):
        if val in (None, ""):
            continue
        rows = q(f"SELECT ran, COUNT(*) n, SUM(found > 0) hit, SUM(fixed) fixed, SUM(total) total, SUM(corrected) bad "
                 f"FROM tools WHERE tool = ? AND {col} = ? GROUP BY ran ORDER BY ran DESC", (tool, val))  # fmt: skip
        parts = [(f"with {tool} {r['n']} runs, it found something in {r['hit']}" if r["ran"] else f"without {tool} {r['n']} runs")
                 + f", the next read fixed {r['fixed']} of {r['total']} columns" + (f", a reviewer later corrected {r['bad']}" if r["bad"] else "")
                 for r in rows]  # fmt: skip
        if parts:
            lines.append(f"- {name}: {'; '.join(parts)}.")
    return "\n".join(lines)


def tool_scores(tool, tau=0.85, last=50):
    """{model: (right, scored)} over each model's last scored answers. Jev counts only where it was decisive (confidence >= tau).
    ponytail: reads every row of the tool; add a LIMIT if the table grows past a few thousand rows."""
    rows = [(r, tool_label(r)) for r in q("SELECT * FROM tools WHERE tool = ? ORDER BY ts DESC", (tool,))]
    rows = [(r, label) for r, label in rows if label is not None]
    jev = [bool(r["jev"]) == label for r, label in rows if r["jev"] is not None and r["jev_conf"] >= tau][:last]
    lite = [bool(r["llm"]) == label for r, label in rows if r["llm"] is not None][:last]
    return {"jev": (sum(jev), len(jev)), "llm": (sum(lite), len(lite))}


def jev_tau(tool):
    """The lowest confidence at which Jev may decide alone, learned from outcomes, or None when it may not.
    At that confidence and above, Jev needs at least 20 scored answers, 90% of them right, and a rate not below Flash-Lite's.
    A fixed threshold does not work: Jev was right on 51 of 52 scored zoom answers and never more confident than 0.80."""
    for tau in (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95):
        (right, n), (lite_right, lite_n) = tool_scores(tool, tau).values()
        if n >= 20 and right / n >= 0.9 and (not lite_n or right / n >= lite_right / lite_n):
            return tau
    return None


def mark_corrected(batch, doc_id, column):
    """A reviewer corrected this column: the tool decisions made for it on this invoice did not work."""
    q("UPDATE tools SET corrected = 1 WHERE batch = ? AND doc_id = ? AND cols LIKE ?", (batch, doc_id, f'%"{column}"%'))


def drop_hint(template_id, column):
    """A reviewer corrected this column on this template: its stored caption hint is no longer trusted."""
    for t in q("SELECT hints FROM templates WHERE id = ?", (template_id,)):
        hints = json.loads(t["hints"])
        hints.pop(column, None)
        q("UPDATE templates SET hints = ? WHERE id = ?", (json.dumps(hints), template_id))
