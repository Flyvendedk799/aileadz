"""Structural tests for Mind-Map v2 (`templates/fm/mind_map.html`).

The page is a DCLogic component: a `{% raw %}`-wrapped `x-dc` template whose
`{{ … }}` bindings are resolved against whatever `renderVals()` returns. Nothing
type-checks that pairing at runtime — a renamed key just renders as nothing, and
a bracketed `x-dc` mention above the real element silently hijacks the whole
page. These tests lock down the invariants that class of bug lives in, plus the
v2 navigation surface (keyboard, deep-link, focus mode, radar) and the one
cross-file contract the page has with the memories API.
"""
import os
import re

import jinja2
import pytest

TEMPLATES = "templates"
PAGE = os.path.join(TEMPLATES, "fm", "mind_map.html")
STR_Q = "\"'`"


@pytest.fixture(scope="module")
def src():
    with open(PAGE, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def parts(src):
    """(x-dc template body, DCLogic script body)."""
    tpl = re.search(r"<x-dc>(.*?)</x-dc>", src, re.S)
    logic = re.search(r'<script type="text/x-dc" data-dc-script>(.*?)</script>', src, re.S)
    assert tpl and logic, "mind_map.html must carry an x-dc template and a data-dc-script block"
    return tpl.group(1), logic.group(1)


# ── helpers ────────────────────────────────────────────────────────────────
def _skip_string(s, i):
    q, i, n = s[i], i + 1, len(s)
    while i < n and s[i] != q:
        i += 2 if s[i] == "\\" else 1
    return i + 1


def _skip_comment(s, i):
    if s.startswith("//", i):
        j = s.find("\n", i)
        return len(s) if j < 0 else j
    if s.startswith("/*", i):
        j = s.find("*/", i)
        return len(s) if j < 0 else j + 2
    return i


def rendervals_keys(logic):
    """Keys of the object literal returned by the outermost `return{` in renderVals()."""
    i = logic.index("{", logic.index("renderVals()")) + 1
    depth, n, start = 1, len(logic), None
    while i < n:
        j = _skip_comment(logic, i)
        if j != i:
            i = j
            continue
        c = logic[i]
        if c in STR_Q:
            i = _skip_string(logic, i)
            continue
        if depth == 1 and logic.startswith("return{", i):
            start = i + len("return")
            break
        depth += 1 if c in "{([" else -1 if c in "})]" else 0
        i += 1
    assert start is not None, "renderVals() must end in a top-level `return{ … }`"

    keys, depth, i = set(), 0, start
    while i < n:
        j = _skip_comment(logic, i)
        if j != i:
            i = j
            continue
        c = logic[i]
        if c in STR_Q:
            i = _skip_string(logic, i)
            continue
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and (c.isalpha() or c == "_"):
            word = re.match(r"\w+", logic[i:]).group(0)
            j = i + len(word)
            while j < n and logic[j] in " \t":
                j += 1
            if j < n and logic[j] in ":,":       # `key: value` or shorthand `key,`
                keys.add(word)
            i = j
            continue
        i += 1
    return keys


def template_bindings(tpl):
    loopvars = set(re.findall(r'<sc-for[^>]*\bas="([^"]+)"', tpl))
    literals = {"true", "false", "null", "undefined"}
    used = set()
    for expr in re.findall(r"\{\{(.*?)\}\}", tpl, re.S):
        head = re.split(r"[.\[\s(]", expr.strip(), 1)[0]
        if head and head not in literals and not head[0].isdigit():
            used.add(head)
    return used - loopvars


# ── tests ──────────────────────────────────────────────────────────────────
def test_page_renders_through_jinja():
    """The Jinja layer must survive rendering — `{% raw %}` has to fence every
    DC binding, or Jinja tries to resolve `{{ pTitle }}` itself and blows up."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES))
    env.globals.update(
        url_for=lambda ep, **kw: "/" + ep,
        session={},
        get_flashed_messages=lambda **kw: [],
        config={},
        request=type("R", (), {"endpoint": "futurematch.mind_map", "path": "/mind-map", "args": {}})(),
    )
    out = env.get_template("fm/mind_map.html").render()
    assert "<x-dc>" in out and "data-dc-script" in out
    assert "{{ pTitle }}" in out, "DC bindings must survive Jinja untouched"


def test_no_bracketed_x_dc_before_the_real_element(src):
    """support.js `parseDcText()` regex-matches the FIRST `x-dc` open tag in the
    raw page source. A bracketed mention anywhere above it — a CSS comment, a
    doc note — hijacks the template and dumps the page as plain text."""
    first = src.index("<x-dc")
    assert src.index("{% raw %}") < first, "the first bracketed x-dc tag must be the real element"
    assert src[first:first + 6] == "<x-dc>"


def test_every_template_binding_is_produced_by_render_vals(parts):
    """A renamed/dropped renderVals key renders as nothing — silently. This is
    the only thing standing between the two halves of the component."""
    tpl, logic = parts
    missing = sorted(template_bindings(tpl) - rendervals_keys(logic))
    assert not missing, "template binds keys renderVals() never returns: %s" % missing


def test_keyboard_navigation_is_wired(parts):
    """v2's headline claim is that the map is navigable without a mouse: the
    stage has to be focusable and actually receive key events."""
    tpl, logic = parts
    root = re.search(r'<div ref="\{\{ rootRef \}\}"[^>]*>', tpl).group(0)
    assert 'tabIndex="0"' in root, "the stage must be focusable"
    assert 'onKeyDown="{{ onKey }}"' in root, "the stage must handle key events"
    assert 'role="application"' in root and "aria-label=" in root

    handled = set(re.findall(r"case '([^']+)':", logic))
    for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                "Enter", "Escape", "Home", "/", "f", "n", "p", "?", " "):
        assert key in handled, "keyboard handler is missing %r" % key


def test_v2_navigation_and_immersion_hooks_are_present(parts):
    """Guards the features v2 is named for, so a refactor can't quietly drop one."""
    tpl, logic = parts
    for needle, what in [
        ("_layoutLabels", "screen-space label placement"),
        ("_updateProxLabels", None),                       # replaced by _layoutLabels
        ("_hashId", "deep-linkable selection"),
        ("_toggleFocus", "branch isolation"),
        ("_drawMini", "radar minimap"),
        ("_panelShift", "panel-aware camera framing"),
        ("_recolorEdges", "per-edge path highlight"),
        ("prefers-reduced-motion", "reduced-motion support"),
        ("visibilitychange", "hidden-tab render pause"),
    ]:
        if what is None:
            assert needle not in logic, "%s should no longer exist" % needle
        else:
            assert needle in logic, "missing %s (%s)" % (what, needle)
    for needle in ("data-mm-mini", "data-mm-live", "data-mm-search", "crumbs", "helpRows"):
        assert needle in tpl, "template is missing %r" % needle


def test_memory_composer_only_offers_categories_the_api_accepts(parts):
    """The add-memory composer POSTs `category` straight through; anything the
    server doesn't know is silently rewritten to 'andet'."""
    _, logic = parts
    block = logic[logic.index("_MEM_CATS"):logic.index("_HELP")]
    offered = set(re.findall(r"\{id:'([^']+)'", block))
    assert offered, "the composer must offer memory categories"

    with open(os.path.join("app1", "user_profile_db.py"), encoding="utf-8") as fh:
        server = fh.read()
    tup = re.search(r"_MEMORY_CATEGORIES\s*=\s*\(([^)]*)\)", server).group(1)
    accepted = set(re.findall(r"[\"']([a-z_]+)[\"']", tup))
    assert offered <= accepted, "composer offers categories the API rejects: %s" % sorted(offered - accepted)


def test_endpoints_the_page_calls_exist():
    with open("api.py", encoding="utf-8") as fh:
        api = fh.read()
    assert "@api_bp.route('/api/profile/mindmap')" in api
    assert "'/api/profile/memories', methods=['GET', 'POST', 'PUT', 'DELETE']" in api
