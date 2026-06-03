"""Pure AI cost model — no network, no DB, boot-safe.

Turns the token counts already recorded in ``ai_agent_runs`` into money so the
routing (gpt-4o -> gpt-4o-mini) and retrieval (fewer embedding calls) savings
become *visible* in DKK on the /admin/ai-cost cockpit. This module measures;
it never makes an OpenAI/network call and never raises out of its public
functions.

Design notes
------------
* Prices are constants below in USD per 1,000,000 tokens — the unit OpenAI
  publishes. ``estimate_cost`` divides by 1e6, so a 1M-token call costs exactly
  the headline number. The existing soft cost ceiling in ``ai_runtime`` uses
  per-1K rates (0.0025 / 0.01 for gpt-4o) — the same numbers, just *1000 here,
  so the two stay consistent.
* Cached input tokens are billed at a discount. OpenAI bills cached prompt
  tokens at ~50% of the normal input rate, so a token counted in
  ``cached_tokens`` is charged at ``input_rate * CACHED_INPUT_DISCOUNT`` instead
  of the full input rate. ``cached_tokens`` is treated as a SUBSET of
  ``input_tokens`` (it is in the OpenAI usage payload), so we bill the
  non-cached remainder at full rate and the cached part at the discount.
* DKK conversion is via the env ``AI_USD_DKK`` (default 7.0). Everything is
  reversible: set ``AI_COST_MODEL_ENABLED=0`` and the admin dashboard skips the
  cost block entirely (see ``cost_model_enabled``).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Mapping, Optional


# ---------------------------------------------------------------------------
# PRICE TABLE — USD per 1,000,000 tokens.
#
#   >>> UPDATE ME when OpenAI changes prices. <<<
#   Source: OpenAI API pricing page (https://openai.com/api/pricing/).
#   Last reviewed: 2026-06 — figures below are the standard (non-batch) tier.
#
# Keep keys as the exact model strings used in ai_runtime.py / app1/rag.py:
#   main_model()      -> "gpt-4o"
#   fast_model()      -> "gpt-4o-mini"
#   embedding_model() -> "text-embedding-3-small"
# Embedding models have no output tokens, so output is 0.0.
# ---------------------------------------------------------------------------
PRICE_TABLE_USD_PER_1M: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}

# Fraction of the normal input rate charged for cached prompt tokens (~50%).
# Override with AI_CACHED_INPUT_DISCOUNT if OpenAI changes the cached tier.
DEFAULT_CACHED_INPUT_DISCOUNT = 0.50

# Default USD->DKK rate; overridable via env AI_USD_DKK.
DEFAULT_USD_DKK = 7.0

_TOKENS_PER_UNIT = 1_000_000.0


def cost_model_enabled() -> bool:
    """Reversible master switch. Set AI_COST_MODEL_ENABLED=0 to disable.

    The admin dashboard checks this so cost figures can be turned off without a
    code change (restores prior behaviour exactly).
    """
    val = os.getenv("AI_COST_MODEL_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def usd_to_dkk_rate() -> float:
    """USD->DKK conversion rate from env AI_USD_DKK (default 7.0)."""
    try:
        rate = float(os.getenv("AI_USD_DKK", str(DEFAULT_USD_DKK)))
        if rate <= 0:
            return DEFAULT_USD_DKK
        return rate
    except (TypeError, ValueError):
        return DEFAULT_USD_DKK


def cached_input_discount() -> float:
    """Cached-input billing fraction from env AI_CACHED_INPUT_DISCOUNT.

    Clamped to [0, 1]; default 0.50 (cached tokens billed at half the input
    rate).
    """
    try:
        d = float(os.getenv("AI_CACHED_INPUT_DISCOUNT", str(DEFAULT_CACHED_INPUT_DISCOUNT)))
    except (TypeError, ValueError):
        return DEFAULT_CACHED_INPUT_DISCOUNT
    if d < 0.0:
        return 0.0
    if d > 1.0:
        return 1.0
    return d


def price_for(model: Optional[str]) -> Optional[Dict[str, float]]:
    """Return {'input', 'output'} USD-per-1M rates for a model, or None.

    Matches exact key first, then a normalised key (case/whitespace), so a
    stored model string like ' GPT-4o ' still resolves.
    """
    if not model:
        return None
    rates = PRICE_TABLE_USD_PER_1M.get(model)
    if rates is not None:
        return rates
    try:
        norm = str(model).strip().lower()
    except Exception:
        return None
    for key, val in PRICE_TABLE_USD_PER_1M.items():
        if key.lower() == norm:
            return val
    return None


def _coerce_int(val: Any) -> int:
    """Best-effort non-negative int; never raises."""
    try:
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def estimate_cost(
    model: Optional[str],
    input_tokens: Any,
    output_tokens: Any,
    cached_tokens: Any = 0,
) -> Dict[str, Any]:
    """Estimate the USD + DKK cost of a single call/run from token counts.

    ``cached_tokens`` is treated as a subset of ``input_tokens`` (as in the
    OpenAI usage payload): the cached portion is billed at the discounted input
    rate and the remaining input at full rate.

    Returns a dict::

        {"usd": float, "dkk": float, "model": str, "known": bool,
         "note": str | None}

    For an unknown model, cost is 0.0 and ``known`` is False with an explanatory
    note (so unpriced usage is visible rather than silently dropped). Never
    raises.
    """
    in_tok = _coerce_int(input_tokens)
    out_tok = _coerce_int(output_tokens)
    cached = _coerce_int(cached_tokens)
    # Cached can't exceed the input tokens it is a subset of.
    if cached > in_tok:
        cached = in_tok

    rates = price_for(model)
    if rates is None:
        return {
            "usd": 0.0,
            "dkk": 0.0,
            "model": model or "ukendt",
            "known": False,
            "note": f"Ukendt model '{model}' — ingen pris i tabellen (sat til 0).",
        }

    try:
        in_rate = float(rates.get("input", 0.0))
        out_rate = float(rates.get("output", 0.0))
        discount = cached_input_discount()

        non_cached_in = max(0, in_tok - cached)
        input_usd = (non_cached_in / _TOKENS_PER_UNIT) * in_rate
        cached_usd = (cached / _TOKENS_PER_UNIT) * in_rate * discount
        output_usd = (out_tok / _TOKENS_PER_UNIT) * out_rate
        usd = input_usd + cached_usd + output_usd
        dkk = usd * usd_to_dkk_rate()
    except Exception as exc:  # pragma: no cover - defensive, rates are floats
        return {
            "usd": 0.0,
            "dkk": 0.0,
            "model": model or "ukendt",
            "known": False,
            "note": f"Beregningsfejl: {exc}",
        }

    return {
        "usd": usd,
        "dkk": dkk,
        "model": model,
        "known": True,
        "note": None,
    }


def _row_get(row: Any, key: str) -> Any:
    """Read ``key`` from a dict-like row (DictCursor rows are Mappings)."""
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def summarize_runs(rows: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """Aggregate cost over a list of ai_agent_runs-shaped dicts.

    Each row is expected to expose ``model``, ``input_tokens``,
    ``output_tokens`` and ``cached_tokens`` (missing -> 0). Returns::

        {
          "total_usd": float,
          "total_dkk": float,
          "total_input_tokens": int,
          "total_output_tokens": int,
          "total_cached_tokens": int,
          "run_count": int,
          "by_model": {model: {"usd","dkk","input_tokens","output_tokens",
                                "cached_tokens","run_count","known"}},
          "unknown_models": [str, ...],   # models with no price entry
        }

    Never raises; a malformed row contributes 0 and is skipped.
    """
    result: Dict[str, Any] = {
        "total_usd": 0.0,
        "total_dkk": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_tokens": 0,
        "run_count": 0,
        "by_model": {},
        "unknown_models": [],
    }
    if not rows:
        return result

    unknown: set = set()
    by_model: Dict[str, Dict[str, Any]] = result["by_model"]

    for row in rows:
        try:
            model = _row_get(row, "model")
            in_tok = _coerce_int(_row_get(row, "input_tokens"))
            out_tok = _coerce_int(_row_get(row, "output_tokens"))
            cached = _coerce_int(_row_get(row, "cached_tokens"))

            est = estimate_cost(model, in_tok, out_tok, cached)

            key = model if model else "ukendt"
            bucket = by_model.get(key)
            if bucket is None:
                bucket = {
                    "usd": 0.0,
                    "dkk": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "run_count": 0,
                    "known": est["known"],
                }
                by_model[key] = bucket

            bucket["usd"] += est["usd"]
            bucket["dkk"] += est["dkk"]
            bucket["input_tokens"] += in_tok
            bucket["output_tokens"] += out_tok
            bucket["cached_tokens"] += cached
            bucket["run_count"] += 1

            result["total_usd"] += est["usd"]
            result["total_dkk"] += est["dkk"]
            result["total_input_tokens"] += in_tok
            result["total_output_tokens"] += out_tok
            result["total_cached_tokens"] += cached
            result["run_count"] += 1

            if not est["known"]:
                unknown.add(key)
        except Exception:  # pragma: no cover - per-row defensive skip
            continue

    result["unknown_models"] = sorted(unknown)
    return result


def project_monthly_dkk(period_dkk: Any, period_days: Any) -> float:
    """Project a full-month (30-day) DKK figure from a period's spend.

    ``period_dkk`` over ``period_days`` -> 30-day projection. Returns 0.0 for a
    non-positive or unusable period. Never raises.
    """
    try:
        spend = float(period_dkk)
        days = float(period_days)
    except (TypeError, ValueError):
        return 0.0
    if spend <= 0 or days <= 0:
        return 0.0
    return spend / days * 30.0
