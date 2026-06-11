#!/usr/bin/env python3
"""ai_eval.run_eval — golden-set Danish AI quality eval runner.

Boots the Futurematch Flask app exactly like ``sandbox/test_ai.py`` (no app code is
touched), drives every golden case through the REAL employee agent at ``/app1/ask``,
collects the streamed SSE events + telemetry, scores each interaction with
``ai_eval.scorers``, prints a per-case + aggregate scorecard, writes
``ai_eval/last_run.json``, and (optionally) gates against ``ai_eval/baseline.json``.

Standalone usage::

    SANDBOX=1 OPENAI_API_KEY=...  python3 ai_eval/run_eval.py
    SANDBOX=1 OPENAI_API_KEY=...  python3 ai_eval/run_eval.py --judge
    SANDBOX=1 OPENAI_API_KEY=...  python3 ai_eval/run_eval.py --gate
    SANDBOX=1 OPENAI_API_KEY=...  python3 ai_eval/run_eval.py --set-baseline

Flags:
    --judge          also run the gpt-4o-mini holistic judge (extra OpenAI cost)
    --gate           fail (exit 1) if any aggregate metric drops > threshold vs baseline
    --threshold T    gate threshold as a fraction (default 0.05 = 5 percentage points)
    --set-baseline   copy this run's aggregates to ai_eval/baseline.json and exit 0
    --only ID[,ID]   run only the listed case id(s)
    --no-warm        skip the RAG warmup (faster boot, first search may be slower)

Exit codes: 0 = ok / gate passed; 1 = gate regression; 2 = boot/setup error.
"""
from __future__ import annotations

import os
import sys
import json
import time
import pathlib
import argparse
from typing import Any, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

GOLDEN_PATH = HERE / "golden_set.json"
LAST_RUN_PATH = HERE / "last_run.json"
BASELINE_PATH = HERE / "baseline.json"
SANDBOX_DIR = ROOT / "sandbox"


# ─────────────────────────────────────────────────────────────────────────────
# env / boot (mirrors sandbox/test_ai.py)
# ─────────────────────────────────────────────────────────────────────────────

def _load_env(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and os.environ.get(k) in (None, ""):
            os.environ[k] = v


def boot_app():
    """Load sandbox env, import run, build the app. Returns (app, run_module)."""
    _load_env(SANDBOX_DIR / ".env.sandbox")
    _load_env(ROOT / ".env")
    os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
    os.environ.setdefault("SANDBOX", "1")
    os.chdir(ROOT)

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set (export it or add it to sandbox/.env.sandbox).", file=sys.stderr)
        sys.exit(2)

    import run  # noqa: E402  (must come after env is loaded)
    app = run.create_app()
    app.config.update(TESTING=True)
    return app, run


def warm(app) -> None:
    with app.app_context():
        try:
            from ai_context import warm_ai_subsystems
            warm_ai_subsystems()
        except Exception as e:
            print(f"  (warmup skipped: {e})")


# ─────────────────────────────────────────────────────────────────────────────
# SSE collection (mirrors sandbox/test_ai.py, extended for cards/tools/telemetry)
# ─────────────────────────────────────────────────────────────────────────────

def parse_sse(body: str) -> List[Dict[str, Any]]:
    events = []
    for part in body.split("\n\n"):
        line = next((l for l in part.split("\n") if l.startswith("data: ")), None)
        if not line:
            continue
        raw = line[6:].strip()
        if raw == "[DONE]":
            continue
        try:
            events.append(json.loads(raw))
        except Exception:
            pass
    return events


def fresh_client(app, user: str = "test"):
    c = app.test_client()
    c.post("/login", data={"username": user, "password": "test"})
    return c


def ask(client, query: str) -> Dict[str, Any]:
    """POST one turn to /app1/ask and decode the SSE stream into a structured result."""
    r = client.post("/app1/ask", json={"query": query})
    body = r.get_data(as_text=True)
    events = parse_sse(body)

    text = ""
    err = None
    cards: List[Dict[str, Any]] = []
    types: Dict[str, int] = {}
    for e in events:
        t = e.get("type")
        types[t] = types.get(t, 0) + 1
        if t == "chunk":
            text += e.get("content", "")
        elif t == "error":
            err = e.get("content")
        elif t == "course_cards":
            for item in e.get("items", []) or []:
                if isinstance(item, dict):
                    cards.append(item)
    return {
        "http": r.status_code,
        "events": events,
        "types": types,
        "text": text.strip(),
        "error": err,
        "cards": cards,
    }


def read_session_telemetry(app, session_id: str) -> Tuple[Optional[List[str]], Optional[int], Optional[Dict[str, int]], List[str]]:
    """Pull tool names (debug_logs), latency + tokens (ai_agent_runs) and raw tool-result
    JSON for this session. Best-effort: returns (tools, latency_ms, tokens, tool_result_jsons).
    All elements may be None / [] if telemetry tables are unavailable."""
    tools: Optional[List[str]] = None
    latency: Optional[int] = None
    tokens: Optional[Dict[str, int]] = None
    tool_jsons: List[str] = []

    # Tool names + raw tool-result evidence from the agent's own debug log.
    # NOTE: debug_logs lives in the SQLite ai_memory.db (app1/memory_store.py),
    # NOT MySQL — querying MySQL here always returned nothing (table absent),
    # which made the eval false-report tool:F for every non-card tool. Read the
    # real source — and do it OUTSIDE the MySQL block so a missing MySQLdb
    # never wipes the tool/evidence telemetry too.
    try:
        import app1.memory_store as _mem
        entries = _mem.get_debug_logs_for_session(session_id) or []
        names = []
        for e in entries:
            step = e.get("step")
            d = e.get("data")
            if step == "tool_call":
                if isinstance(d, dict) and d.get("tool"):
                    names.append(d["tool"])
            elif step == "tool_result":
                # Raw tool-result payload → grounding evidence (tool_jsons).
                if isinstance(d, (dict, list)):
                    try:
                        tool_jsons.append(json.dumps(d, ensure_ascii=False, default=str))
                    except Exception:
                        tool_jsons.append(str(d))
                elif isinstance(d, str) and d.strip():
                    tool_jsons.append(d)
        tools = list(dict.fromkeys(names)) or None  # de-dup, keep order
    except Exception:
        tools = None

    try:
        with app.app_context():
            import MySQLdb
            conn = app.mysql.connection
            cur = conn.cursor(MySQLdb.cursors.DictCursor)

            # Latency + tokens from ai_agent_runs (the agentic-loop telemetry).
            try:
                cur.execute(
                    "SELECT latency_ms, input_tokens, output_tokens FROM ai_agent_runs "
                    "WHERE session_id=%s ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
                row = cur.fetchone()
                if row:
                    latency = int(row.get("latency_ms") or 0) or None
                    it = int(row.get("input_tokens") or 0)
                    ot = int(row.get("output_tokens") or 0)
                    if it or ot:
                        tokens = {"input": it, "output": ot, "total": it + ot}
            except Exception:
                pass

            cur.close()
    except Exception:
        pass
    return tools, latency, tokens, tool_jsons


# ─────────────────────────────────────────────────────────────────────────────
# Drive one golden case (single- or multi-turn) and collect the scored turn
# ─────────────────────────────────────────────────────────────────────────────

def run_case(app, case: Dict[str, Any]) -> Dict[str, Any]:
    """Run a case end-to-end. The SCORED turn is the last turn (or the only query).
    Returns a 'collected' dict consumed by scorers.score_case."""
    client = fresh_client(app)
    # Recover the session_id the app assigned (cookie 'session' is opaque; instead we
    # read it back from the last ai_agent_runs row, but we need a stable key). The app
    # stores session_id in the Flask session; we can fetch it via the cookie jar.
    turns = case.get("turns") or [{"query": case["query"]}]

    wall_start = time.time()
    last = None
    for i, turn in enumerate(turns):
        last = ask(client, turn["query"])
    wall_ms = int((time.time() - wall_start) * 1000)

    session_id = _session_id_from_client(client)
    tools, latency, tokens, tool_jsons = (None, None, None, [])
    if session_id:
        tools, latency, tokens, tool_jsons = read_session_telemetry(app, session_id)

    collected = {
        "events": last["events"],
        "text": last["text"],
        "cards": last["cards"],
        "tools": tools if tools is not None else _tools_from_event_types(last),
        # Grounding evidence = the tool-result telemetry (if any) PLUS the
        # course_cards actually streamed to the user (which carry the concrete
        # titles/prices/vendors the answer must be grounded in). The cards are
        # always available from the SSE stream even when ai_tool_runs telemetry
        # is absent, so the chain-of-custody check always has an evidence base.
        "tool_results": list(tool_jsons) + [
            json.dumps(c, ensure_ascii=False, default=str) for c in (last["cards"] or [])
        ],
        "error": last["error"],
        "http": last["http"],
        "types": last["types"],
        "latency_ms": latency if latency is not None else wall_ms,
        "latency_source": "telemetry" if latency is not None else "wall_clock",
        "tokens": tokens,
        "session_id": session_id,
    }
    return collected


def _session_id_from_client(client) -> Optional[str]:
    """Best-effort: the app keeps session_id inside the Flask session. We surface it
    by re-reading the server-side session through a tiny app context using the cookie.
    Falls back to None — telemetry lookups then degrade to wall-clock latency."""
    try:
        # Flask test client stores cookies; the signed 'session' cookie holds session_id.
        # We decode it via the app's secret using flask's session interface.
        app = client.application
        cookie = None
        # werkzeug>=2.3 exposes cookie jar via client._cookies or get_cookie
        try:
            sc = client.get_cookie("session")
            cookie = sc.value if sc else None
        except Exception:
            cookie = None
        if not cookie:
            return None
        from itsdangerous import URLSafeTimedSerializer
        from flask.sessions import TaggedJSONSerializer
        secret = app.secret_key
        if not secret:
            return None
        s = URLSafeTimedSerializer(
            secret, salt="cookie-session",
            serializer=TaggedJSONSerializer(),
            signer_kwargs={"key_derivation": "hmac", "digest_method": __import__("hashlib").sha1},
        )
        data = s.loads(cookie)
        return data.get("session_id")
    except Exception:
        return None


def _tools_from_event_types(last) -> List[str]:
    """When telemetry is unavailable, infer tool activity from emitted event types so
    'no_tool' / 'tool_any_of' still get a usable signal."""
    out = []
    types = last.get("types") or {}
    if types.get("course_cards") or types.get("product"):
        out.append("search_courses")  # a catalog tool clearly fired
    if types.get("profile_confirm_request") or types.get("ui_card"):
        out.append("update_user_profile")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation + scorecard
# ─────────────────────────────────────────────────────────────────────────────

from ai_eval import scorers as S  # noqa: E402


def _pct(num: int, den: int) -> Optional[float]:
    if den == 0:
        return None
    return round(100.0 * num / den, 1)


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * p
    f = int(k)
    c = min(f + 1, len(vs) - 1)
    if f == c:
        return round(vs[f], 1)
    return round(vs[f] + (vs[c] - vs[f]) * (k - f), 1)


def aggregate(per_case: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build aggregate metrics across all cases."""
    metric_num = {k: 0 for k in S.METRIC_KEYS}
    metric_den = {k: 0 for k in S.METRIC_KEYS}
    judge_scores: List[float] = []
    latencies: List[float] = []
    retrieval_precisions: List[float] = []
    total_tokens = 0
    cases_with_tokens = 0
    passed = 0

    for pc in per_case:
        scored = pc["scored"]
        if scored.get("_passed"):
            passed += 1
        for k in S.METRIC_KEYS:
            r = scored.get(k) or {}
            if k == "judge":
                if r.get("applies") and r.get("score") is not None:
                    judge_scores.append(r["score"])
                continue
            if r.get("applies"):
                metric_den[k] += 1
                if r.get("score") == S.PASS:
                    metric_num[k] += 1
                if k == "retrieval" and isinstance(r.get("precision"), (int, float)):
                    retrieval_precisions.append(float(r["precision"]))
        lat = pc["collected"].get("latency_ms")
        if isinstance(lat, (int, float)):
            latencies.append(float(lat))
        tok = pc["collected"].get("tokens")
        if tok and tok.get("total"):
            total_tokens += tok["total"]
            cases_with_tokens += 1

    metrics = {
        "tool_selection_pct": _pct(metric_num["tool_selection"], metric_den["tool_selection"]),
        "refusal_pct": _pct(metric_num["refusal"], metric_den["refusal"]),
        "retrieval_pct": _pct(metric_num["retrieval"], metric_den["retrieval"]),
        # Mean per-case card precision (matched/len(cards)) across applicable
        # retrieval cases — finer-grained than the binary retrieval_pct.
        "retrieval_precision_pct": (
            round(100.0 * sum(retrieval_precisions) / len(retrieval_precisions), 1)
            if retrieval_precisions else None
        ),
        "grounding_pct": _pct(metric_num["grounding"], metric_den["grounding"]),
        "profile_event_pct": _pct(metric_num["profile_event"], metric_den["profile_event"]),
        "order_confirmation_pct": _pct(metric_num["order_confirmation"], metric_den["order_confirmation"]),
        "overall_pass_pct": _pct(passed, len(per_case)),
    }
    if judge_scores:
        metrics["judge_avg"] = round(100.0 * sum(judge_scores) / len(judge_scores), 1)

    return {
        "metrics": metrics,
        "counts": {k: {"pass": metric_num[k], "applicable": metric_den[k]} for k in S.METRIC_KEYS if k != "judge"},
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "avg_tokens": round(total_tokens / cases_with_tokens, 0) if cases_with_tokens else None,
        "cases_total": len(per_case),
        "cases_passed": passed,
    }


_METRIC_LABELS = [
    ("tool_selection_pct", "Tool selection"),
    ("refusal_pct", "Refusal/redirect"),
    ("retrieval_pct", "Retrieval relevance"),
    ("retrieval_precision_pct", "Retrieval precision"),
    ("grounding_pct", "Grounding"),
    ("profile_event_pct", "Profile events"),
    ("order_confirmation_pct", "Order confirmation"),
    ("overall_pass_pct", "OVERALL pass"),
]


def print_scorecard(per_case: List[Dict[str, Any]], agg: Dict[str, Any], used_judge: bool) -> None:
    print("\n" + "=" * 78)
    print("PER-CASE SCORECARD")
    print("=" * 78)
    for pc in per_case:
        case = pc["case"]
        scored = pc["scored"]
        flag = "PASS" if scored.get("_passed") else "FAIL"
        marks = []
        for k in ("tool_selection", "refusal", "retrieval", "grounding", "profile_event", "order_confirmation"):
            r = scored.get(k) or {}
            if not r.get("applies"):
                marks.append(f"{_short(k)}:-")
            else:
                marks.append(f"{_short(k)}:{'P' if r.get('score') == S.PASS else 'F'}")
        if used_judge:
            jr = scored.get("judge") or {}
            if jr.get("applies") and jr.get("score") is not None:
                marks.append(f"jdg:{int(round(jr['score'] * 10))}/10")
        lat = pc["collected"].get("latency_ms")
        lat_src = pc["collected"].get("latency_source", "")
        print(f"[{flag}] {case['id']:<32} {' '.join(marks)}  ({lat}ms {lat_src})")
        # surface failing detail lines
        if not scored.get("_passed"):
            for k in ("tool_selection", "refusal", "retrieval", "grounding", "profile_event", "order_confirmation"):
                r = scored.get(k) or {}
                if r.get("applies") and r.get("score") != S.PASS:
                    print(f"        ! {k}: {r.get('detail')}")
            if scored.get("collected_error") or pc["collected"].get("error"):
                print(f"        ! transport error: {pc['collected'].get('error')}")

    print("\n" + "=" * 78)
    print("AGGREGATE SCORECARD")
    print("=" * 78)
    m = agg["metrics"]
    for key, label in _METRIC_LABELS:
        val = m.get(key)
        cnt = agg["counts"].get(key.replace("_pct", ""), {})
        suffix = ""
        if cnt:
            suffix = f"  ({cnt['pass']}/{cnt['applicable']})"
        print(f"  {label:<22} {('n/a' if val is None else str(val) + '%'):>8}{suffix}")
    if used_judge and m.get("judge_avg") is not None:
        print(f"  {'LLM judge (avg)':<22} {str(m['judge_avg']) + '%':>8}")
    print(f"  {'Latency p50 / p95':<22} {str(agg['latency_p50_ms']) + ' / ' + str(agg['latency_p95_ms']) + ' ms':>8}")
    if agg.get("avg_tokens") is not None:
        print(f"  {'Avg tokens / case':<22} {str(int(agg['avg_tokens'])):>8}")
    print(f"\n  RESULT: {agg['cases_passed']}/{agg['cases_total']} cases passed")


def _short(metric_key: str) -> str:
    return {
        "tool_selection": "tool", "refusal": "refu", "retrieval": "retr",
        "grounding": "grnd", "profile_event": "prof", "order_confirmation": "ordr",
    }.get(metric_key, metric_key[:4])


# ─────────────────────────────────────────────────────────────────────────────
# Regression gate
# ─────────────────────────────────────────────────────────────────────────────

# Metrics that gate (higher = better). Latency is reported but not gated by default.
_GATED_METRICS = (
    "tool_selection_pct", "refusal_pct", "retrieval_pct", "retrieval_precision_pct",
    "grounding_pct", "profile_event_pct", "order_confirmation_pct", "overall_pass_pct",
)


def run_gate(current: Dict[str, Any], threshold_pct: float) -> Tuple[bool, List[str]]:
    """Compare current aggregate metrics against ai_eval/baseline.json.
    Returns (ok, messages). A metric that drops by more than threshold (percentage
    points) fails the gate. Missing baseline → gate passes with a note."""
    msgs = []
    if not BASELINE_PATH.exists():
        return True, [f"no baseline at {BASELINE_PATH.name}; gate skipped (use --set-baseline to create one)"]
    try:
        base = json.loads(BASELINE_PATH.read_text())
    except Exception as e:
        return True, [f"baseline unreadable ({e}); gate skipped"]

    base_m = base.get("metrics", base)
    cur_m = current["metrics"]
    ok = True
    for key in _GATED_METRICS:
        b = base_m.get(key)
        c = cur_m.get(key)
        if b is None or c is None:
            continue
        drop = b - c
        if drop > threshold_pct:
            ok = False
            msgs.append(f"REGRESSION {key}: {b}% -> {c}%  (drop {round(drop, 1)} > {threshold_pct} pp)")
        elif drop > 0:
            msgs.append(f"minor dip {key}: {b}% -> {c}% (within {threshold_pct} pp)")
        elif c > b:
            msgs.append(f"improved {key}: {b}% -> {c}%")
    if ok and not any("REGRESSION" in m for m in msgs):
        msgs.insert(0, "gate PASSED — no metric dropped beyond threshold")
    return ok, msgs


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def load_golden() -> List[Dict[str, Any]]:
    data = json.loads(GOLDEN_PATH.read_text())
    return data["cases"]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Golden-set Danish AI eval for the Futurematch employee agent.")
    ap.add_argument("--judge", action="store_true", help="also run the gpt-4o-mini holistic judge")
    ap.add_argument("--gate", action="store_true", help="fail (exit 1) on regression vs baseline.json")
    ap.add_argument("--threshold", type=float, default=5.0, help="gate threshold in percentage points (default 5.0)")
    ap.add_argument("--set-baseline", action="store_true", help="write this run's aggregates to baseline.json and exit")
    ap.add_argument("--only", type=str, default="", help="comma-separated case ids to run")
    ap.add_argument("--no-warm", action="store_true", help="skip RAG warmup")
    args = ap.parse_args(argv)

    cases = load_golden()
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in cases if c["id"] in wanted]
        if not cases:
            print(f"No matching case ids for --only={args.only}", file=sys.stderr)
            return 2

    app, _run = boot_app()
    if not args.no_warm:
        print("Warming AI subsystems (RAG)…")
        warm(app)

    print(f"\nRunning {len(cases)} golden case(s) through the real agent"
          + (" with LLM judge" if args.judge else "") + "…\n")

    per_case: List[Dict[str, Any]] = []
    for case in cases:
        t0 = time.time()
        try:
            collected = run_case(app, case)
        except Exception as e:
            print(f"[ERROR] {case['id']}: {e}")
            collected = {
                "events": [], "text": "", "cards": [], "tools": [], "tool_results": [],
                "error": str(e), "http": 500, "types": {},
                "latency_ms": int((time.time() - t0) * 1000), "latency_source": "wall_clock", "tokens": None,
            }
        try:
            scored = S.score_case(collected, case.get("expect", {}), use_judge=args.judge, case=case)
        except Exception as e:
            scored = {"_passed": False, "_error": f"scorer crash: {e}"}
        per_case.append({"case": case, "collected": collected, "scored": scored})
        flag = "PASS" if scored.get("_passed") else "FAIL"
        print(f"  [{flag}] {case['id']}  ({collected.get('latency_ms')}ms)")

    agg = aggregate(per_case)
    print_scorecard(per_case, agg, used_judge=args.judge)

    # Persist the run.
    run_record = {
        "version": 1,
        "timestamp": int(time.time()),
        "judge": bool(args.judge),
        "metrics": agg["metrics"],
        "latency_p50_ms": agg["latency_p50_ms"],
        "latency_p95_ms": agg["latency_p95_ms"],
        "avg_tokens": agg["avg_tokens"],
        "cases_total": agg["cases_total"],
        "cases_passed": agg["cases_passed"],
        "cases": [
            {
                "id": pc["case"]["id"],
                "intent": pc["case"].get("intent"),
                "passed": pc["scored"].get("_passed"),
                "latency_ms": pc["collected"].get("latency_ms"),
                "latency_source": pc["collected"].get("latency_source"),
                "tools": pc["collected"].get("tools"),
                "n_cards": len(pc["collected"].get("cards") or []),
                "scores": {
                    k: {"score": (pc["scored"].get(k) or {}).get("score"),
                        "applies": (pc["scored"].get(k) or {}).get("applies"),
                        "detail": (pc["scored"].get(k) or {}).get("detail")}
                    for k in S.METRIC_KEYS
                },
            }
            for pc in per_case
        ],
    }
    LAST_RUN_PATH.write_text(json.dumps(run_record, ensure_ascii=False, indent=2))
    print(f"\nWrote {LAST_RUN_PATH}")

    if args.set_baseline:
        BASELINE_PATH.write_text(json.dumps({
            "version": 1,
            "timestamp": run_record["timestamp"],
            "metrics": agg["metrics"],
            "latency_p50_ms": agg["latency_p50_ms"],
            "latency_p95_ms": agg["latency_p95_ms"],
        }, ensure_ascii=False, indent=2))
        print(f"Wrote baseline -> {BASELINE_PATH}")
        return 0

    if args.gate:
        ok, msgs = run_gate(agg, args.threshold)
        print("\n" + "=" * 78)
        print("REGRESSION GATE")
        print("=" * 78)
        for m in msgs:
            print(f"  {m}")
        if not ok:
            print("\nGATE FAILED")
            return 1
        print("\nGATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
