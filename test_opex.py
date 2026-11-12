"""Offline checks, no network: uv run python test_opex.py"""
from datetime import date
from decimal import Decimal as D

from opex import docs, harness as h, memory


def test_validators():
    assert h.to_decimal("₱57,820.76") == D("57820.76") and h.to_decimal("(903.65)") == D("-903.65") and h.to_decimal("n/a") is None
    assert h.date_readings("05-07-2026") == {date(2026, 5, 7), date(2026, 7, 5)}  # two readings, the model picks one
    text = h.alnum("Period From 06-18-2026 CURRENT BILL 52,761.47 Account ID 5375557679-9")
    ok = {"value": "52761.47", "raw": "52,761.47"}
    assert h.check_cell("GRAND_TOTAL", ok, text) == (D("52761.47"), [])
    assert h.check_cell("GRAND_TOTAL", {"value": "52761.48", "raw": "52,761.47"}, text)[1][0][0]  # value differs from print: hard
    assert h.check_cell("GRAND_TOTAL", {"value": "99.00", "raw": "99.00"}, text)[1] == [(True, "printed text '99.00' was not found in the document text")]
    assert h.check_cell("GRAND_TOTAL", {"value": "99.00", "raw": "99.00"}, text, soft_evidence=True)[1][0][0] is False  # scans: soft
    assert h.check_cell("START_DATE", {"value": "2026-06-18", "raw": "06-18-2026"}, text) == (date(2026, 6, 18), [])
    assert h.check_cell("START_DATE", {"value": "2026-06-19", "raw": "06-18-2026"}, text)[1]
    assert h.check_cell("ACCOUNT_NO", {"value": "5375557679-9", "raw": "5375557679-9"}, text) == ("5375557679-9", [])
    assert h.check_cell("USAGE", {"value": None, "raw": None}, text) == (None, [])
    assert h.date_order(["06-05-2026", "07/23/2026"]) == "md" and h.date_order(["22-06-2026"]) == "dm"
    assert h.date_order(["06-05-2026", "07-05-2026"]) is None and h.date_order(["22-06-2026", "07/23/2026"]) is None  # no proof, or mixed
    doc = {"sheets": [{"text": "Period 06-05-2026 to 07-05-2026 due 07/23/2026", "has_text": True}]}
    cells = {n: {"value": None, "raw": None, "conf": 1} for n in h.EXTRACT}
    cells.update(START_DATE={"value": "2026-05-06", "raw": "06-05-2026", "conf": 0.9}, DUE_DATE={"value": "2026-07-23", "raw": "07/23/2026", "conf": 0.9})
    assert h.assess(doc, cells, [], {})[0]["START_DATE"]["value"] == date(2026, 6, 5)  # read day-first by the model, fixed in code
    assert h.show(h.check_cell("GRAND_TOTAL", {"value": "52761.470", "raw": "52,761.47"}, text)[0]) == "52761.47"  # printed precision wins


def test_cross_checks_and_formulas():
    vals = {"START_DATE": date(2026, 6, 18), "END_DATE": date(2026, 5, 19), "DUE_DATE": date(2026, 7, 2)}
    assert set(h.cross_checks(vals, [])[0]) == {"START_DATE", "END_DATE"}
    one_day = h.cross_checks({"START_DATE": date(2026, 5, 6), "END_DATE": date(2026, 5, 7)}, [])[0]  # 06-05 read as day-month
    assert one_day["START_DATE"][0][0] is False and h.cross_checks({"START_DATE": date(2026, 6, 5), "END_DATE": date(2026, 7, 5)}, []) == ({}, [])
    r = lambda i, n: {"doc_id": i, "file": "f", "values": {"INVOICE_NO": "A1", "ACCOUNT_NO": "9", "USAGE": "5" * n}}
    stub, full = r("stub", 0), r("full", 1)
    h.mark_duplicates([stub, full])
    assert stub["duplicate_of"] == "full" and "duplicate_of" not in full  # the most complete copy is kept
    fact = {"id": 7, "text": "usage is the reading difference times the multiplier",
            "expr": "abs((CURRENT_READING - PREV_READING) * MULTIPLIER - USAGE) <= 1"}  # fmt: skip
    good = {"CURRENT_READING": D("90586.1"), "PREV_READING": D("86485.6"), "MULTIPLIER": D(1), "USAGE": D("4100.5")}
    assert h.cross_checks(good, [fact]) == ({}, [])
    reasons, failed = h.cross_checks({**good, "USAGE": D(41)}, [fact])
    assert failed == [7] and set(reasons) == {"CURRENT_READING", "PREV_READING", "MULTIPLIER", "USAGE"}
    assert h.cross_checks({**good, "USAGE": None}, [fact]) == ({}, [])  # missing input: formula does not apply
    assert h.cross_checks(good, [{"id": 8, "text": "x", "expr": "__import__('os').system('true')"}]) == ({}, [])  # no builtins


def test_calc_columns():
    assert h.coverage(date(2026, 9, 15)) == "2026-09" and h.coverage(date(2026, 9, 16)) == "2026-10"
    assert h.coverage(date(2026, 12, 16)) == "2027-01"
    out = h.calc({"START_DATE": date(2026, 6, 18), "GRAND_TOTAL": D("52761.47"), "VAT_COST": D("4794.97"), "EWT_COST": D("903.65"), "USAGE": D("4100.5")})
    assert out["COVERAGE"][0] == "2026-07" and out["UTILITY"][0] == D("47966.50") and out["NET_AMT"][0] == D("51857.82")
    assert out["RATE"][0] == D("11.6977")  # fallback: UTILITY / USAGE, only when no tariff is printed
    out = h.calc({"GRAND_TOTAL": D(100), "VAT_COST": D(10), "RATE": D("9.5"), "USAGE": D(10)})
    assert "RATE" not in out and out["NET_AMT"] == (D(100), ["GRAND_TOTAL"], "EWT assumed 0", True) and out["COVERAGE"][0] is None
    assert h.calc({"GRAND_TOTAL": D(100)})["UTILITY"] == (None, ["GRAND_TOTAL", "VAT_COST"], "no VAT amount was read", True)  # no silent assumption
    # an empty cell is accepted when absence is more likely than not; a value needs the full bar
    assert h.bar({"value": None, "reasons": []}, h.ACCEPT) == 0.5 and h.bar({"value": D(1), "reasons": []}, h.ACCEPT) == 0.85


def test_join():
    assert docs.join(["invoice_start", "continuation", "supplementary", "invoice_start", "irrelevant", "continuation"]) == [
        ("invoice", [0, 1]), ("supplementary", [2]), ("invoice", [3, 5]), ("irrelevant", [4])]  # fmt: skip
    assert docs.join(["continuation", "unknown"]) == [("unknown", [0]), ("unknown", [1])]  # nothing open to continue


def test_fingerprint_match():
    memory.reset(":memory:")
    layout = ("Billing Invoice Account ID Period Pres Rdg PrevRdg Multiplier Total VAT Current Bill Generation Transmission "
              "System Loss Distribution Supply Metering Subsidy Universal Charge Lifeline Rate Schedule Premise Address")
    a = docs.tokens(f"Davao Light Power {layout} Globe Telecom 2026")
    b = docs.tokens(f"Davao Light Power {layout} Phil Tower 2027")
    sister = docs.tokens(f"Visayan Electric {layout} Phil Tower 2027")  # same layout, other vendor
    assert "2026" not in a and memory.jaccard(a, b) >= 0.6 and memory.jaccard(a, sister) >= 0.6
    tid = memory.save_template(a, "davao light power", {"USAGE": {"label": "Billed", "sheet": 0}}, ok=True)
    assert memory.match_template(b)["id"] == tid and memory.save_template(b, "", {}, ok=True) == tid
    assert memory.match_template(b)["ok_runs"] == 2 and memory.match_template(sister) is None
    assert memory.match_template(docs.tokens("Davao Light Power your electric bill customer account number")) is None
    assert memory.guess_vendor(docs.tokens("Davao Light and Power Company statement")) == "davao light power"


def test_self_learn_activation():
    memory.reset(":memory:")
    scope = "vendor:davao light power"
    fid = memory.remember(scope, "GRAND_TOTAL", "alias", 'GRAND_TOTAL is printed under the caption "CURRENT BILL - JUNE 2026"', doc_id="d1")
    assert memory.recall([scope]) == []  # candidate: never injected
    assert memory.remember(scope, "GRAND_TOTAL", "alias", 'GRAND_TOTAL is printed under the caption "CURRENT BILL - JULY 2026"', doc_id="d2") == fid
    assert [f["id"] for f in memory.recall([scope])] == [fid]  # 2 agreeing documents: active
    assert [f["id"] for f in memory.recall([scope], query="GRAND_TOTAL")] == [fid] and memory.recall([scope], query="penalty") == []
    # a contradiction blocks both facts
    memory.remember(scope, "USAGE", "alias", 'USAGE is printed under the caption "Billed"', doc_id="d1")
    memory.remember(scope, "USAGE", "alias", 'USAGE is printed under the caption "Registered"', doc_id="d2")
    memory.remember(scope, "USAGE", "alias", 'USAGE is printed under the caption "Billed"', doc_id="d3")
    assert [f["column"] for f in memory.recall([scope])] == ["GRAND_TOTAL"]
    assert (memory.activation("formula", 2, 0), memory.activation("formula", 3, 0), memory.activation("alias", 5, 1)) == ("candidate", "active", "candidate")
    # a human fact hides self facts on the same scope and column; a missed self fact is retired
    hid = memory.remember(scope, "GRAND_TOTAL", "rule", "Use the current bill, not the total with arrears", confidence=1.0, source="human")
    assert [f["id"] for f in memory.recall([scope])] == [hid]
    memory.miss([fid])
    assert memory.q("SELECT status FROM facts WHERE id = ?", (fid,))[0]["status"] == "retired"


def test_usage_counts_failed_attempts_and_cached_tokens():
    import collections
    from types import SimpleNamespace as NS
    from unittest import mock

    from opex import llm

    meta = NS(prompt_token_count=1000, cached_content_token_count=400, candidates_token_count=10, thoughts_token_count=5)
    replies = iter([NS(text="not json", usage_metadata=meta), NS(text='{"a": 1}', usage_metadata=meta)])
    client = NS(models=NS(generate_content=lambda **_: next(replies)))
    with mock.patch.object(llm, "_gemini_client", lambda: client), mock.patch.object(llm.time, "sleep", lambda s: None), \
            mock.patch.object(llm, "USAGE", collections.Counter()):  # fmt: skip
        assert llm.gemini(llm.LITE, ["x"], {"type": "object"}) == {"a": 1}
        u = llm.USAGE
        # the reply with bad JSON was billed, so both attempts count
        assert [u[f"{llm.LITE}.{k}"] for k in ("calls", "in", "cached", "out", "retries")] == [2, 2000, 800, 30, 1]
        assert llm.cost() == {llm.LITE: 0.000459}  # (1200 * 0.30 + 800 * 0.03 + 30 * 2.50) / 1e6


def test_tool_outcomes():
    memory.reset(":memory:")
    base = {**dict.fromkeys(memory.TOOL_KEYS), "tool": "zoom", "batch": "b", "doc_id": "d", "kind": "scanned", "cols": '["RATE"]', "total": 2, "corrected": 0}
    row = lambda **k: {**base, **k}
    # the label comes from what happened, never from what a model picked
    assert memory.tool_label(row(ran=1, found=0, fixed=2)) is False  # zoom found nothing to enlarge
    assert memory.tool_label(row(ran=1, found=3, fixed=1)) is True and memory.tool_label(row(ran=1, found=3, fixed=0)) is None
    assert memory.tool_label(row(ran=0, fixed=2)) is False and memory.tool_label(row(ran=0, fixed=1)) is None
    assert memory.tool_label(row(ran=0, fixed=2, corrected=1)) is True  # a reviewer correction: what ran did not work
    # Jev earns trust at the lowest confidence where 20 scored answers are 90% right; the threshold is learned, not fixed
    rows = [row(ts=i, doc_id="e" if i < 3 else "d", ran=0, fixed=2, jev=0, jev_conf=0.7, llm=0, used="llm") for i in range(20)]
    memory.save_tools(rows[:19] + [row(ts=99, ran=0, fixed=2, jev=1, jev_conf=0.6)])  # a wrong answer at low confidence
    assert memory.jev_tau("zoom") is None  # 19 right of 20 at 0.6, but Flash-Lite is 19 of 19; only 19 answers at 0.65
    memory.save_tools(rows[19:])
    assert memory.jev_tau("zoom") == 0.65 and memory.tool_scores("zoom", 0.65) == {"jev": (20, 20), "llm": (20, 20)}
    assert memory.tool_record("zoom", None, "", "scanned") == "- All scanned invoices: without zoom 21 runs, the next read fixed 42 of 42 columns."
    # a reviewer correction flips the label of that invoice's decisions for that column, and Jev loses trust
    memory.mark_corrected("b", "e", "RAT")  # not a column of these rows
    assert memory.jev_tau("zoom") == 0.65
    memory.mark_corrected("b", "e", "RATE")
    assert memory.tool_scores("zoom", 0.65)["jev"] == (17, 20) and memory.jev_tau("zoom") is None


def test_decide_shadow_sample():
    from unittest import mock

    from opex import llm

    calls, q = [], {"type": "noul", "instructions": "?"}
    ask = lambda: calls.append(1) or True
    with mock.patch.object(llm, "jev", lambda *a: {"q": {"noul": 0.05}}), mock.patch.object(llm, "LOG", []), mock.patch.object(llm, "SHADOW", True):
        # (random roll, tau) -> answer, Flash-Lite calls so far, who decided
        for roll, tau, want in ((0.5, 0.85, (False, 0, "jev")), (0.05, 0.85, (False, 1, "jev")), (0.5, 2, (True, 2, "llm"))):
            info = {}
            with mock.patch.object(llm.random, "random", lambda: roll):
                answer = llm.decide("zoom", "state", q, ask, tau, shadow=0.1, info=info)
            assert (answer, len(calls), info["used"]) == want and info["jev"] is False and info["jev_conf"] == 0.95
        assert info["llm"] is True and [r[2] for r in llm.LOG] == ["zoom"] * 3


def test_event_log():
    import tempfile
    from pathlib import Path
    from unittest import mock

    from opex import llm, ui

    with tempfile.TemporaryDirectory() as out:
        assert ui.read_events(out) == ([], 0)  # no run yet
        llm.emit("d", "extract")  # no batch is running: nothing is written
        with open(Path(out) / "events.jsonl", "w") as f, mock.patch.object(llm, "EVENTS", f):
            llm.emit("d", "extract", model=llm.LITE)
            with llm.track() as used:
                used.update({f"{llm.LITE}.calls": 1, f"{llm.LITE}.in": 1_000_000})
                llm.emit("d", "verify")  # inside an invoice: the event carries what the invoice has cost so far
            f.write('{"ts": 1, "doc_id": "d", "st')  # a line the run is still writing
            f.flush()
            events, offset = ui.read_events(out)
            assert [(e["step"], e.get("cost")) for e in events] == [("extract", None), ("verify", 0.3)] and events[0]["model"] == llm.LITE
            assert ui.read_events(out, offset) == ([], offset)  # the half-written line waits
            f.write('ep": "done"}\n')
            f.flush()
            assert [e["step"] for e in ui.read_events(out, offset)[0]] == ["done"]
            assert len(ui.read_events(out, 10**6)[0]) == 3  # the file is shorter than the offset: a new run, read from the start


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
