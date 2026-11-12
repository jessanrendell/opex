"""Model clients: Gemini on Vertex, Jev on Mesh, and the cascade helper."""
import base64
import collections
import contextlib
import io
import json
import os
import random
import threading
import time
from pathlib import Path

import requests
from PIL import Image

FLASH, LITE, JEV = "gemini-3.8-flash", "gemini-3.5-flash-lite", "typesafe/jev-1.14"
MAX_SIDE = 1568  # Jev rejects images with a longer side
MIN_SIDE = 300  # Mesh rejected a 300x120 image ("Invalid image dimensions"); 300x300 passed
# USD per 1M tokens (input, output, cached input), checked 2026-11-12. Flash is from Google's price table and is an
# introductory rate: it doubles on 2027-01-01. Flash-Lite is from aggregators only. Jev is from Mesh's model list.
PRICES = {FLASH: (0.75, 3.75, 0.075), LITE: (0.30, 2.50, 0.03), JEV: (0.042, 0, 0.042)}

# "<model>.calls|in|out|cached|ms|retries". calls = attempts that got a reply; cached is the part of `in` billed at
# the cached rate; ms = time inside those calls (no slot wait, no backoff); retries = attempts that failed.
USAGE = collections.Counter()
LOG = []  # shadow log rows (ts, doc_id, point, jev_answer, jev_conf, llm_answer, used); memory.py stores them
_lock, _local = threading.Lock(), threading.local()


def _count(key, n=1):
    with _lock:
        USAGE[key] += n
        if getattr(_local, "usage", None) is not None:
            _local.usage[key] += n


@contextlib.contextmanager
def track():
    """Also count this thread's usage in a separate Counter (per-document tokens)."""
    _local.usage = collections.Counter()
    try:
        yield _local.usage
    finally:
        _local.usage = None


EVENTS = None  # open OUT_DIR/events.jsonl while a batch runs; the UI reads it


def emit(doc_id, step, **data):
    """Append one step to the batch's event log. Called when a step starts; a second call with the same step adds its result.
    cost = what this thread's invoice has cost so far. Does nothing when no batch is running."""
    if EVENTS is None:
        return
    u = getattr(_local, "usage", None)
    row = {"ts": time.time(), "doc_id": doc_id, "step": step, **({} if u is None else {"cost": round(sum(cost(u).values()), 6)}), **data}
    with _lock:
        EVENTS.write(json.dumps(row, default=str) + "\n")
        EVENTS.flush()


def _load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k, v.strip("'\""))


_load_env()
# Shadow mode: the LLM also answers when Jev is confident, so thresholds can be refit from the log.
# Costs one extra LLM call per Jev-decided point. Set OPEX_SHADOW=0 to turn it off.
SHADOW = os.environ.get("OPEX_SHADOW", "1") == "1"


# Calls in flight per service. With 6, a 77-page batch had no 429; with 10, Vertex and Mesh both answered 429
# and the backoff made the batch 6 times slower.
GEMINI_SLOTS, JEV_SLOTS = threading.Semaphore(6), threading.Semaphore(6)


def _retry(fn, key, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            _count(f"{key}.retries")  # a timed-out attempt may be billed but returns no usage: this count shows how many
            if i == tries - 1:
                raise
            print(f"  retry {i + 1}: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(2 * 2**i)  # 2, 4, 8, 16, 32 s


_client, _client_lock = None, threading.Lock()


def _gemini_client():
    global _client
    with _client_lock:  # threads start together; a second client would close the first one's connection
        if _client is None:
            from google import genai
            from google.oauth2 import service_account

            info = json.loads(base64.b64decode(os.environ["VERTEX_SA_JSON_B64"]))
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            _client = genai.Client(
                vertexai=True, credentials=creds, project=os.environ["VERTEX_PROJECT"], location=os.environ["VERTEX_LOCATION"],
                # Flash latency varied from 3 s to 256 s for the same request; cut the tail and retry instead
                http_options=genai.types.HttpOptions(timeout=75_000),
            )
    return _client


def gemini(model, parts, schema=None, thinking=None):
    """parts: str (text) or bytes (PNG). Returns parsed JSON when a JSON schema is given, else text."""
    from google.genai import types

    contents = [p if isinstance(p, str) else types.Part.from_bytes(data=p, mime_type="image/png") for p in parts]
    cfg = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=thinking or ("MINIMAL" if model == LITE else "LOW")),
        **({"response_mime_type": "application/json", "response_json_schema": schema} if schema else {}),
    )

    def call():
        client = _gemini_client()
        with GEMINI_SLOTS:
            t = time.perf_counter()
            r = client.models.generate_content(model=model, contents=contents, config=cfg)
            _count(f"{model}.ms", round((time.perf_counter() - t) * 1000))
        u = r.usage_metadata  # counted before parsing: a reply with bad JSON is billed too
        _count(f"{model}.calls")
        _count(f"{model}.in", u.prompt_token_count or 0)
        _count(f"{model}.cached", u.cached_content_token_count or 0)
        _count(f"{model}.out", (u.candidates_token_count or 0) + (u.thoughts_token_count or 0))
        return json.loads(r.text) if schema else r.text or ""  # bad JSON is retried; a blank page gives no text

    return _retry(call, model)


def _fit(png):
    """PNG -> JPEG bytes acceptable to Jev: longest side <= MAX_SIDE, shortest side >= MIN_SIDE (white padding).
    JPEG because Mesh estimates tokens from the upload size and rejects a 2 MB PNG scan as over the context window."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    s = min(1, MAX_SIDE / max(im.size))
    if s < 1:
        im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))))
    if min(im.size) < MIN_SIDE:
        canvas = Image.new("RGB", (max(im.width, MIN_SIDE), max(im.height, MIN_SIDE)), "white")
        canvas.paste(im, (0, 0))
        im = canvas
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80)
    return out.getvalue()


def jev(state, questions, images=()):
    """Ask Jev questions about a state. state: text. images: PNG bytes.
    questions: {key: {"type": "noul", "instructions"} | {"type": "choice", "instructions", "criteria": {option: meaning}}}.
    Returns {key: answer}: noul -> {"noul": p}; choice -> {"choice", "confidence", "probabilities"}."""
    if images:  # Mesh rejects `assets`; images go in `state` as image_url parts
        state = [{"type": "text", "text": state}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(_fit(i)).decode()}}
            for i in images
        ]

    def call():
        with JEV_SLOTS:
            r = requests.post(
                os.environ["MESH_BASE_URL"].rstrip("/") + "/evaluate",
                headers={"Authorization": "Bearer " + os.environ["MESH_API_KEY"]},
                json={"model": JEV, "state": state, "questions": questions},
                timeout=120,
            )
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()  # retried
        return r

    r = _retry(call, JEV)
    if not r.ok:
        raise RuntimeError(f"Mesh {r.status_code}: {r.text[:300]}")
    d = r.json()
    _count(f"{JEV}.calls")
    _count(f"{JEV}.ms", round(r.elapsed.total_seconds() * 1000))  # request sent -> reply headers; the body is small
    _count(f"{JEV}.in", d.get("usage", {}).get("input_tokens", 0))
    return d["answers"]


def decide(point, state, question, llm, tau, images=(), doc_id="", shadow=1.0, info=None):
    """Cascade for one decision point. Jev answers one noul or choice question; its answer is used when
    its confidence >= tau (noul: P >= tau or P <= 1 - tau). Otherwise, or on a Mesh error, `llm()` decides.
    Returns a bool for noul, an option name for choice. Every decision is appended to LOG.
    shadow: share of the Jev-decided calls that `llm()` also answers. info: a dict that receives both answers."""
    jev_ans = conf = None
    try:
        a = jev(state, {"q": question}, images)["q"]
        if question["type"] == "noul":
            jev_ans, conf = a["noul"] >= 0.5, max(a["noul"], 1 - a["noul"])
        else:
            jev_ans, conf = a["choice"], a["confidence"]
    except Exception as e:
        _count("jev.errors")
        print(f"  jev error at {point}: {str(e)[:200]}")
    use_jev = conf is not None and conf >= tau
    llm_ans = None
    if not use_jev:
        llm_ans = llm()
    elif SHADOW and random.random() < shadow:
        try:
            llm_ans = llm()
        except Exception:
            pass
    if info is not None:
        info.update(jev=jev_ans, jev_conf=conf, llm=llm_ans, used="jev" if use_jev else "llm")
    LOG.append((time.time(), doc_id, point, json.dumps(jev_ans), conf, json.dumps(llm_ans), "jev" if use_jev else "llm"))
    return jev_ans if use_jev else llm_ans


def cost(usage=None):
    """Estimated USD per model from a usage counter (default: the whole process) and PRICES."""
    u = USAGE if usage is None else usage
    return {m: round(((u[f"{m}.in"] - u[f"{m}.cached"]) * i + u[f"{m}.cached"] * c + u[f"{m}.out"] * o) / 1e6, 6)
            for m, (i, o, c) in PRICES.items() if u[f"{m}.calls"]}  # fmt: skip


if __name__ == "__main__":  # smoke test: uv run python -m opex.llm
    from PIL import ImageDraw

    im = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(im).text((10, 50), "INVOICE No 777   TOTAL 42.00", fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    png = buf.getvalue()
    schema = {"type": "object", "properties": {"invoice_no": {"type": ["string", "null"]}}, "required": ["invoice_no"]}
    for m in (FLASH, LITE):
        print(m, "text:", gemini(m, ["Reply with the single word: ok"]).strip())
        print(m, "json+image:", gemini(m, ["Read the invoice number from the image.", png], schema))
    print("jev noul+image:", jev("A scanned strip.", {"inv": {"type": "noul", "instructions": "Does the image show the word INVOICE?"}}, [png]))
    q = {"type": "choice", "instructions": "What kind of document is this?", "criteria": {"invoice": "a bill", "letter": "correspondence"}}
    print("jev choice:", jev("Invoice No 777. Total due 42.00.", {"kind": q}))
    print("decide:", decide("smoke", "Invoice No 777. Total due 42.00.", q, lambda: "invoice", 0.95), LOG[-1][3:])
    print(dict(USAGE), cost())
