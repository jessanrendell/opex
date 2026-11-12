"""UI: a local web page over one batch, running or finished: steps per invoice, moving averages, review. Standard library only."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import harness, learn

PAGE = Path(__file__).parent / "ui.html"


def read_events(out, offset=0):
    """Steps logged by the run (llm.emit) from a byte offset -> (events, next offset).
    A half-written last line waits for the next call. A file shorter than the offset is a new run: read it from the start."""
    path = Path(out) / "events.jsonl"
    if not path.exists():
        return [], 0
    with open(path, "rb") as f:
        f.seek(offset if offset <= path.stat().st_size else 0)
        start, data = f.tell(), f.read()
    data = data[: data.rfind(b"\n") + 1]
    return [json.loads(line) for line in data.splitlines()], start + len(data)


def serve(out, port=8765):
    out = Path(out)
    cache = {"key": None, "documents": {}, "results": {}}

    def batch():
        """-> (documents by id, results by id), read again when the run rewrites the files.
        A file that does not exist yet is empty; a half-written file keeps the last good copy."""
        paths = [out / "documents.json", out / "results.jsonl"]
        key = [p.stat().st_mtime_ns if p.exists() else 0 for p in paths]
        if key != cache["key"]:
            try:
                documents = {d["doc_id"]: d for d in json.loads(paths[0].read_text())} if key[0] else {}
                results = {r["doc_id"]: r for r in map(json.loads, paths[1].read_text().splitlines())} if key[1] else {}
                cache.update(key=key, documents=documents, results=results)
            except ValueError:
                pass
        return cache["documents"], cache["results"]

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, default=str)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/events":
                n = query.get("offset", ["0"])[0]
                events, offset = read_events(out, int(n) if n.isdigit() else 0)
                return self.reply(200, {"events": events, "offset": offset})
            documents, results = batch()
            reviews = learn.load_reviews(out)
            if url.path == "/api/docs":
                return self.reply(200, [
                    {"doc_id": i, "done": i in results, **{k: r.get(k) for k in ("error", "duplicate_of", "empty", "cost", "seconds", "path")},
                     "flags": sum(bool(d["flag"]) for d in r.get("detail", {}).values()),
                     "reviewed": i in reviews, "learned": reviews.get(i, {}).get("learned", False)}
                    for i, doc in documents.items() if doc["type"] == "invoice" for r in [results.get(i, {})]])  # fmt: skip
            doc_id = query.get("id", [""])[0]
            if documents.get(doc_id, {}).get("type") != "invoice":  # images and details are looked up by document id, never by a path from the request
                return self.reply(404, {"error": "unknown document"})
            sheets = documents[doc_id]["sheets"]
            if url.path == "/api/doc":  # result is null until the run has written results.jsonl
                return self.reply(200, {"doc_id": doc_id, "file": documents[doc_id]["file"], "result": results.get(doc_id), "sheets": len(sheets),
                                        "review": reviews.get(doc_id), "columns": harness.COLS})  # fmt: skip
            n = query.get("sheet", ["0"])[0]
            if url.path == "/img" and n.isdigit() and int(n) < len(sheets):
                return self.reply(200, Path(sheets[int(n)]["png"]).read_bytes(), "image/png")
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            # JSON content type is required: a cross-site page cannot send it without a CORS preflight, which is never answered
            if urlparse(self.path).path != "/api/save" or self.headers.get("Content-Type") != "application/json":
                return self.reply(400, {"error": "POST /api/save with Content-Type application/json"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                return self.reply(413, {"error": "request too large"})
            results = batch()[1]
            try:
                body = json.loads(self.rfile.read(length))
                doc_id, rows = body["doc_id"], body.get("rows", {})
                assert doc_id in results and "detail" in results[doc_id] and isinstance(rows, dict)
                rows = {c: {k: str(x.get(k, ""))[:500] for k in ("correct", "note")} for c, x in rows.items() if c in harness.EXTRACT}
            except Exception:
                return self.reply(400, {"error": "bad request: need doc_id of an extracted invoice and rows {column: {correct, note}}"})
            learn.save_review(out, doc_id, rows)
            answer = {"saved": True}
            if body.get("learn"):
                try:
                    answer["facts"] = learn.learn_doc(out, doc_id)
                except Exception as e:
                    answer["learn_error"] = str(e)[:300]
            self.reply(200, answer)

        def log_message(self, *args):
            pass

    print(f"UI: http://127.0.0.1:{port}   (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
