"""Product vocabulary: broad, but no word may mislead (docs/DECISIONS.md).

Synonyms are not policed. The one collision that is: the learner's order list is
"Mine bestillinger", never "tidslinje". The URL ``/min-tidslinje`` and the route
name stay, the icon class ``fa-timeline`` is not text, and a CV may still talk
about a coherent timeline of someone's career (``cv_service``). People may still
*say* "tidslinje", so the help search keeps it as a keyword (``keywords:`` lines).
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN = ("templates", "static/futurematch/assets", "app1", ".")
SKIP_DIRS = {".git", "__pycache__", "tests", "docs", "node_modules", "sandbox", "migrations", ".demo"}
ALLOWED = (
    "min-tidslinje", "min_tidslinje", "fa-timeline",
    "sammenhængende tidslinje",          # cv_service: the career timeline on a CV
    "slug: min-laering-og-tidslinje",    # help article id, part of its URL
    '"tidslinje", "min læring"',         # help_kb.py: words a person may type when asking for help
)


def _files():
    seen = set()
    for base in SCAN:
        top = os.path.join(ROOT, base)
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if base == "." and root != top:
                dirs[:] = []
                continue
            for name in files:
                if name.endswith((".py", ".html", ".js", ".md")):
                    path = os.path.join(root, name)
                    if path not in seen:
                        seen.add(path)
                        yield path


def test_the_order_list_is_never_called_tidslinje():
    offenders = []
    for path in _files():
        with open(path, encoding="utf-8", errors="ignore") as f:
            for number, line in enumerate(f, start=1):
                low = line.lower()
                if "tidslinje" not in low or low.startswith("keywords:"):
                    continue
                rest = low
                for ok in ALLOWED:
                    rest = rest.replace(ok, "")
                if re.search(r"tidslinje", rest):
                    offenders.append("%s:%d" % (os.path.relpath(path, ROOT), number))
    assert offenders == [], "Brug 'Mine bestillinger', ikke 'tidslinje': %s" % offenders
