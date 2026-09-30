"""Anonymous -> logged-in memory migration (N-5.8).

A visitor who chatted before signing in has a profile keyed by their browser token
(interests, searches, viewed courses, a short summary). On login those become the
user's own durable memories, so the assistant does not greet a new account as a
stranger, and the anonymous row is deleted so the data lives in exactly one place.
Never raises: login must not depend on it.
"""

import logging

logger = logging.getLogger(__name__)

MAX_ITEMS = 8


def _facts(profile):
    """(label, category, detail) triples from an anonymous profile."""
    out = []
    for interest in (profile.get("interests") or [])[:MAX_ITEMS]:
        if isinstance(interest, str) and interest.strip():
            out.append(("Interesse: " + interest.strip()[:150], "interesse", None))
    if profile.get("preferred_location"):
        out.append(("Foretrukken lokation", "praeference", str(profile["preferred_location"])))
    if profile.get("preferred_format"):
        out.append(("Foretrukket format", "praeference", str(profile["preferred_format"])))
    if profile.get("budget_range"):
        out.append(("Budget", "praeference", str(profile["budget_range"])))
    titles = [v.get("title") for v in (profile.get("last_viewed") or []) if isinstance(v, dict) and v.get("title")]
    if titles:
        out.append(("Har kigget på kurser", "kontekst", ", ".join(titles[:5])))
    if profile.get("conversation_summary"):
        out.append(("Tidligere samtale (som gæst)", "kontekst", str(profile["conversation_summary"])[:1000]))
    return out


def migrate(browser_token, username):
    """Move the anonymous profile into the user's memories. Returns the number of
    memories written (0 when there was nothing to move)."""
    if not browser_token or not username:
        return 0
    try:
        from app1 import memory_store, user_profile_db
        profile = memory_store.load_anonymous_profile(browser_token)
        if not profile:
            return 0
        written = 0
        for label, category, detail in _facts(profile):
            if user_profile_db.add_memory(username, label, category=category, detail=detail,
                                          source="anonymous", confidence=0.6):
                written += 1
        # Delete only after a successful copy, so a failure loses nothing.
        memory_store.erase_subject(browser_token=browser_token)
        return written
    except Exception as exc:
        logger.warning("anonymous memory migration skipped: %s", exc)
        return 0
