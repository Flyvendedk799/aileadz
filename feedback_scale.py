"""One feedback scale for every AI answer (N-3.3).

The chat thumbs write ``feedback_rating`` = +1 (godt svar) / -1 (dårligt svar) /
0 (ingen vurdering), on ``chatbot_interactions`` and in the AI analytics store.
Every report reads that scale through this module instead of assuming 1-5 stars:
the averages that dashboards show as "x/5" are the thumbs share mapped onto 1-5.
"""

UP = 1
DOWN = -1


def to_five(avg_rating):
    """Average of rated rows (-1..+1) -> the 1..5 scale dashboards display.
    None / no ratings -> 0 (nothing to show)."""
    if avg_rating is None:
        return 0
    try:
        avg = max(-1.0, min(1.0, float(avg_rating)))
    except (TypeError, ValueError):
        return 0
    return round(3 + 2 * avg, 1)


def approval_pct(up, down):
    up, down = int(up or 0), int(down or 0)
    return round(100 * up / (up + down)) if (up + down) else None
