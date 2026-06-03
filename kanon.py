"""
kanon.py — k-anonymity & consent-aware aggregation helpers (roadmap value-4,
Theme A/E).

Single-employee de-anonymisation is an EU/GDPR liability: a department or cohort
that contains only one (or a few) people lets an HR admin read an individual's
behaviour straight off an "aggregate" dashboard. These helpers enforce a minimum
cohort size **k** on the small-cohort BREAKDOWNS produced by the analytics
engines, so no displayed row ever represents fewer than ``k`` people.

Design contract
---------------
* Every helper is GUARDED and NEVER raises. On any unexpected input it returns
  the safest available value (usually the input unchanged, or an empty result),
  so a caller that wraps these in a ``try/except`` and one that does not both
  behave correctly. This module must never be able to crash an analytics call.
* The OVERALL / company-wide totals are already aggregated and safe — these
  helpers are only ever applied to the per-department / per-cohort breakdowns.
* Suppression can either DROP small rows or MERGE them into a single
  ``Anonymiseret`` bucket that sums their counts (so the column total is
  preserved while no single small group is individually profiled).

Danish UI note
--------------
``ANON_NOTE_DA`` is the canonical user-facing explanation; callers embed it in
the returned payload so the dashboard can show *why* a group is hidden.
"""

import os

# Default minimum cohort size. Env-overridable via AI_ANALYTICS_K (preferred) or
# ANALYTICS_K; falls back to the constant. Anything unparseable / < 1 falls back
# to the constant so a bad env var can never weaken anonymity below the default.
K_DEFAULT = 5

# Canonical Danish note for the UI. Formatted with the active k.
ANON_NOTE_DA = 'Grupper under k={k} er skjult af hensyn til anonymitet'

# Label used when small cohorts are MERGED into a single bucket instead of dropped.
DEFAULT_MERGE_LABEL = 'Anonymiseret (<k)'

# Placeholder shown for a single redacted cell.
REDACTED_PLACEHOLDER = '—'


def _resolve_k_default():
    """Resolve the configured default k from the environment, guarded.

    Honours ``AI_ANALYTICS_K`` first, then ``ANALYTICS_K``; any missing /
    non-integer / < 1 value falls back to the ``K_DEFAULT`` constant. Never
    raises and never returns a value weaker than 1.
    """
    for env_name in ('AI_ANALYTICS_K', 'ANALYTICS_K'):
        try:
            raw = os.environ.get(env_name)
            if raw is None or str(raw).strip() == '':
                continue
            val = int(str(raw).strip())
            if val >= 1:
                return val
        except Exception:
            continue
    return K_DEFAULT


# Resolve once at import time; if anything goes wrong, fall back to the constant.
try:
    K_DEFAULT = _resolve_k_default()
except Exception:
    K_DEFAULT = 5


def _coerce_k(k):
    """Coerce an arbitrary ``k`` to a safe positive int, defaulting to K_DEFAULT."""
    try:
        if k is None:
            return K_DEFAULT
        ki = int(k)
        return ki if ki >= 1 else K_DEFAULT
    except Exception:
        return K_DEFAULT


def _coerce_count(value):
    """Coerce a cohort-size value to an int, defaulting to 0 on bad input.

    A row whose count is missing / unparseable is treated as size 0, i.e. it is
    NOT safe and will be suppressed — fail closed, never expose."""
    try:
        if value is None:
            return 0
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        return int(float(str(value).strip()))
    except Exception:
        return 0


def anon_note(k=None):
    """Return the Danish UI note for the active k. Never raises."""
    try:
        return ANON_NOTE_DA.format(k=_coerce_k(k))
    except Exception:
        return ANON_NOTE_DA.replace('{k}', str(K_DEFAULT))


def is_cohort_safe(n, k=K_DEFAULT):
    """True iff a cohort of size ``n`` is large enough to display (n >= k).

    Guarded: bad input -> treated as unsafe (False), never raises.
    """
    try:
        return _coerce_count(n) >= _coerce_k(k)
    except Exception:
        return False


def redact(value, n, k=K_DEFAULT):
    """Return ``value`` if a cohort of size ``n`` is safe, else the placeholder.

    For single-cell breakdowns (e.g. one department's average) where the value
    itself would expose a sub-k cohort. Never raises; on error it fails closed
    and redacts.
    """
    try:
        return value if is_cohort_safe(n, k) else REDACTED_PLACEHOLDER
    except Exception:
        return REDACTED_PLACEHOLDER


def suppress_small_groups(rows, count_key, k=K_DEFAULT, label_key=None,
                          merge_label=DEFAULT_MERGE_LABEL, merge=False):
    """Suppress or merge grouped aggregate rows whose cohort is below ``k``.

    Each item in ``rows`` is a grouped aggregate (a dict) that carries its cohort
    size under ``count_key`` (e.g. number of employees / distinct users in the
    group). Rows whose cohort is < ``k`` would let a single person be profiled,
    so they are removed from the visible breakdown.

    Two modes:

    * ``merge=False`` (default): small rows are DROPPED entirely.
    * ``merge=True``: small rows are FOLDED into a single ``Anonymiseret`` bucket
      whose ``count_key`` is the SUM of the dropped cohorts (so the column total
      stays correct), labelled via ``label_key``/``merge_label``. The merged
      bucket is only emitted when its summed cohort is itself >= ``k`` — i.e. we
      never reveal a merged bucket that still represents fewer than k people; in
      that case the residue is dropped too.

    Args:
        rows:        list of dicts (grouped aggregate rows). Non-list / falsy
                     input returns ``([], note(0))`` safely.
        count_key:   key on each row holding the cohort size.
        k:           minimum cohort size (defaults to env-resolved K_DEFAULT).
        label_key:   key under which to write the ``merge_label`` on the merged
                     bucket. If None, the label is written under ``'label'``.
        merge_label: Danish label for the merged bucket.
        merge:       merge into an ``Anonymiseret`` bucket instead of dropping.

    Returns:
        ``(out_rows, note)`` where ``out_rows`` is the filtered/merged list (a
        new list; the input is not mutated in place beyond the merged bucket it
        builds) and ``note`` is a dict::

            {'suppressed': <int rows removed/merged>,
             'merged': <int rows folded into the bucket>,  # 0 when merge=False
             'k': <active k>,
             'note_da': '<Danish UI note>'}

    Never raises. On any internal error the original ``rows`` are returned
    unchanged with a zeroed note so the analytics call still succeeds.
    """
    kk = _coerce_k(k)
    note = {
        'suppressed': 0,
        'merged': 0,
        'k': kk,
        'note_da': anon_note(kk),
    }

    try:
        if not rows or not isinstance(rows, (list, tuple)):
            return ([] if not rows else list(rows)), note

        kept = []
        small = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    # Unknown shape — keep as-is rather than silently dropping it;
                    # we can only reason about cohort size on dict rows.
                    kept.append(row)
                    continue
                n = _coerce_count(row.get(count_key))
                if n >= kk:
                    kept.append(row)
                else:
                    small.append(row)
            except Exception:
                # Per-row guard: a single bad row never poisons the whole call.
                kept.append(row)

        if not small:
            return kept, note

        note['suppressed'] = len(small)

        if merge:
            merged_count = 0
            for row in small:
                merged_count += _coerce_count(row.get(count_key))
            # Only surface the merged bucket if it is itself k-safe; otherwise
            # even the bucket would represent < k people — drop it entirely.
            if merged_count >= kk:
                bucket = {count_key: merged_count}
                target_label_key = label_key if label_key else 'label'
                bucket[target_label_key] = merge_label
                kept.append(bucket)
                note['merged'] = len(small)
            # else: bucket itself sub-k -> fully suppressed (already counted).

        return kept, note
    except Exception:
        # Total failure: fail OPEN on availability (return input) but the caller
        # still gets a coherent note. Anonymity is best-effort here; the engines
        # that call this also guard, and a crash would be worse for boot safety.
        try:
            return list(rows), note
        except Exception:
            return [], note
