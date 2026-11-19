"""An empty vendor overview and an empty CV studio say what to do first (and only when empty)."""
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parent.parent


def _block(path, start, end="{% endif %}"):
    text = (ROOT / path).read_text(encoding="utf-8")
    i = text.index(start)
    return text[i:text.index(end, text.index("</section>", i)) + len(end)]


def _render(block, **ctx):
    env = Environment(autoescape=True)
    env.filters["urlencode"] = lambda v: v
    return env.from_string(block).render(url_for=lambda name, **kw: "/" + name, **ctx)


def test_vendor_overview_guides_only_a_provider_with_nothing_yet():
    block = _block("templates/fm/vendor_dashboard.html", "{% if kpis and not kpis.course_count")
    empty = _render(block, kpis={"course_count": 0, "submission_count": 0, "awaiting_booking": 0})
    assert "Dit katalog er tomt endnu" in empty and "/vendor.vendor_submit" in empty and "/vendor.vendor_orders" in empty
    assert "tomt endnu" not in _render(block, kpis={"course_count": 3, "submission_count": 0, "awaiting_booking": 0})
    assert "tomt endnu" not in _render(block, kpis={"course_count": 0, "submission_count": 0, "awaiting_booking": 1})


def test_cv_studio_points_an_empty_cv_at_import():
    block = _block("templates/fm/my_cv.html", "{% if cv and not (stats.roles")
    empty = _render(block, cv={"name": "A"}, stats={})
    assert "data-cv-open" in empty and "Importér CV" in empty
    assert "tomt endnu" not in _render(block, cv={"name": "A"}, stats={"roles": 2})
    # The design gallery renders the page without data: no guidance there.
    assert "tomt endnu" not in _render(block, cv={}, stats={})
