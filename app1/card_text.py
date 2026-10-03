"""Keep the employee chat's answer text and its course cards from saying the same thing.

Contract: course cards (name, price, place, description, image) are rendered by the
browser from structured data. The model is told not to repeat them in prose, but it
sometimes writes a numbered list with a description and a pasted logo per course
anyway, which shows every course twice. ``tidy_answer`` runs on the finished answer
before it is streamed:

* markdown images are removed (cards carry the imagery; a pasted logo renders
  full width and pushes the real UI down);
* list items / paragraphs that merely restate a card are cut, and a
  ``CARDS_MARK`` is left where they were, so the browser can show the text before
  the mark, then the cards, then the text after it (the closing remark or question).

Pure text in, text out. No Flask, no network. A trailing ``<suggestions>`` tag is
carried through untouched.
"""
import re

CARDS_MARK = "<!--kort-->"

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SUGGESTIONS = re.compile(r"\s*<suggestions>.*$", re.S)
_ITEM = re.compile(r"^(\s*)(?:\d+[.)]|[-*•])\s+")
_MARKUP = re.compile(r"[*_`#>\[\]()]+")
_WORDS = re.compile(r"[^0-9a-zæøåé]+")


def _norm(text):
    return _WORDS.sub(" ", _MARKUP.sub(" ", (text or "").casefold())).strip()


def strip_images(text):
    """The text without markdown images (and the blank lines they leave behind)."""
    return re.sub(r"\n{3,}", "\n\n", _IMAGE.sub("", text or "")).strip()


def _indent(line):
    return len(line) - len(line.lstrip(" \t"))


def _item_end(lines, start):
    """Index after the list item that begins at ``start`` (continuation lines included)."""
    base = _indent(lines[start])
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            # a blank line only continues the item if the next text is indented under it
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _indent(lines[j]) > base and not _ITEM.match(lines[j]):
                i = j
                continue
            break
        if _ITEM.match(line) and _indent(line) <= base:
            break
        if i > start + 1 and not lines[i - 1].strip() and _indent(line) <= base:
            break
        i += 1
    return i


def _paragraph_end(lines, start):
    i = start
    while i < len(lines) and lines[i].strip():
        i += 1
    return i


def _mentions(block, titles):
    flat = _norm(" ".join(block))
    return any(t and t in flat for t in titles)


def _starts_with_title(first_line, titles):
    head = _norm(_ITEM.sub("", first_line))
    return any(t and head.startswith(t) for t in titles)


def tidy_answer(text, cards):
    """Return ``text`` ready to stream next to ``cards`` (a list of dicts with ``title``)."""
    text = text or ""
    tail_match = _SUGGESTIONS.search(text)
    tail = tail_match.group(0) if tail_match else ""
    body = strip_images(text[:tail_match.start()] if tail_match else text)
    titles = sorted({_norm(c.get("title")) for c in (cards or []) if isinstance(c, dict)} - {""}, key=len, reverse=True)
    titles = [t for t in titles if len(t) >= 4]
    if not titles or not body:
        return body + tail

    lines = body.split("\n")
    kept, removed_at, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        if _ITEM.match(line):
            end = _item_end(lines, i)
            if _mentions(lines[i:end], titles):
                removed_at = len(kept) if removed_at is None else removed_at
                i = end
                continue
        elif line.strip() and (i == 0 or not lines[i - 1].strip()) and _starts_with_title(line, titles):
            end = _paragraph_end(lines, i)
            if end - i > 1 or len(_norm(line)) > min(len(t) for t in titles) + 30:
                removed_at = len(kept) if removed_at is None else removed_at
                i = end
                continue
        kept.append(line)
        i += 1

    if removed_at is None:
        return body + tail
    kept.insert(removed_at, CARDS_MARK)
    tidy = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    tidy = re.sub(r"(?<!\n)\n?" + re.escape(CARDS_MARK) + r"\n?(?!\n)", "\n\n" + CARDS_MARK + "\n\n", tidy).strip()
    if len(_norm(tidy.replace(CARDS_MARK, ""))) < 15:
        return body + tail  # nothing worth keeping around the cards: leave the answer as written
    return tidy + tail
