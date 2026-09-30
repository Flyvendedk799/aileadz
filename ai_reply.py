"""Reply plumbing shared by the HR, vendor and embedded-widget assistants (N-5.3/4/6).

The employee chat parses ``<suggestions>[...]</suggestions>`` out of the model's
answer server-side and sends chips as their own SSE event. The other assistants
used to stream the raw tag to the browser. These helpers give them the same
behaviour: the visible text never contains the tag, and the chips arrive as a
``suggestions`` event.
"""
import json
import re

_TAG_OPEN = "<suggestions>"
_FULL = re.compile(r"<suggestions>\s*(\[.*?\])\s*</suggestions>", re.S)
_ANY = re.compile(r"\s*<suggestions>.*?(?:</suggestions>|$)\s*", re.S)


def extract_suggestions(text, limit=3, max_len=60):
    """Chips from the model's <suggestions> tag ([] when absent or malformed)."""
    m = _FULL.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [s.strip() for s in data if isinstance(s, str) and 0 < len(s.strip()) <= max_len][:limit]


def strip_suggestions(text):
    """The answer without the tag, including a half-streamed one."""
    return _ANY.sub("", text or "").strip()


class SuggestionFilter:
    """Streams text but never lets the ``<suggestions>`` tag reach the client.

    ``feed(token)`` returns the part that is safe to show now; text that could
    still turn out to be the start of the tag is held back until it is decided.
    ``flush()`` returns whatever was held (when the answer ended without a tag).
    """

    def __init__(self):
        self._held = ""
        self._in_tag = False

    def feed(self, token):
        if self._in_tag:
            return ""
        buf = self._held + (token or "")
        idx = buf.find(_TAG_OPEN)
        if idx != -1:
            self._in_tag = True
            self._held = ""
            return buf[:idx].rstrip() if buf[:idx].strip() else ""
        # Hold back a tail that might be the beginning of "<suggestions>".
        keep = 0
        for n in range(min(len(_TAG_OPEN) - 1, len(buf)), 0, -1):
            if _TAG_OPEN.startswith(buf[-n:]):
                keep = n
                break
        self._held = buf[len(buf) - keep:] if keep else ""
        return buf[:len(buf) - keep] if keep else buf

    def flush(self):
        out, self._held = ("" if self._in_tag else self._held), ""
        return out


def sse(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
