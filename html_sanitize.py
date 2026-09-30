"""
Render-time HTML / CSS sanitising for user- or HR-authored content (S-1.9).

``sanitize_html`` is registered as the Jinja filter ``safe_html`` and replaces
the bare ``|safe`` on notification bodies and chat transcripts. It uses bleach
with a small formatting allowlist; if bleach is missing it escapes everything
(never returns raw input).

``safe_css`` is the Jinja filter ``safe_css`` for tenant custom CSS. It cannot
stop every CSS trick, but it removes the things that let CSS break out of the
``<style>`` element or load script/remote content.
"""

import re

from markupsafe import Markup, escape

try:  # bleach is pinned in requirements.txt, but never crash import without it
    import bleach as _bleach
except Exception:  # pragma: no cover
    _bleach = None

ALLOWED_TAGS = [
    "a", "b", "strong", "i", "em", "u", "br", "p", "span", "div",
    "ul", "ol", "li", "h2", "h3", "h4", "blockquote", "code", "pre",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "span": ["style"],
    "p": ["style"],
    "div": ["style"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto", "tel"]


def sanitize_html(value):
    """Return ``Markup`` that is safe to render inside an HTML body."""
    if value is None:
        return Markup("")
    text = value if isinstance(value, str) else str(value)
    if _bleach is None:
        return escape(text)
    try:
        cleaned = _bleach.clean(
            text,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRS,
            protocols=ALLOWED_PROTOCOLS,
            strip=True,
        )
        # bleach.linkify-style hardening: force rel on anchors.
        cleaned = re.sub(r"<a\b(?![^>]*\brel=)", '<a rel="noopener noreferrer"', cleaned)
        return Markup(cleaned)
    except Exception:  # pragma: no cover
        return escape(text)


_CSS_BAD = re.compile(
    r"(@import|expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|url\s*\(\s*['\"]?\s*(?!data:image/(png|jpeg|gif|webp);)[a-z]+:)",
    re.IGNORECASE,
)


def sanitize_css(value):
    """Return CSS text safe to place inside a ``<style>`` element."""
    if not value:
        return Markup("")
    css = str(value)
    # Break-out characters: no tags, no HTML comments, no CDATA.
    css = css.replace("<", "").replace(">", "").replace("\x00", "")
    css = _CSS_BAD.sub("/* blocked */", css)
    return Markup(css)


def register_html_filters(app):
    app.jinja_env.filters["safe_html"] = sanitize_html
    app.jinja_env.filters["safe_css"] = sanitize_css
