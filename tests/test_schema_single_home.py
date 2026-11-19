"""One table definition, in ``enterprise_tables.py`` / ``schema_registry.py``.

Older modules still create their own tables. This is a ratchet: the list below
may only shrink. A new ``CREATE TABLE`` anywhere else, or one more in a listed
file, fails; move the definition to ``schema_registry.py`` instead. When you
move one out of a listed file, lower its number here (or delete the line).
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOMES = {"enterprise_tables.py", "schema_registry.py"}
SKIP_DIRS = {".git", "__pycache__", "tests", "migrations", "sandbox", "scripts", "node_modules", ".venv", "venv"}

# file -> number of CREATE TABLE statements it is still allowed to hold (ROADMAP T1-10)
LEGACY = {
    "ai_runtime.py": 2,
    "app1/__init__.py": 1,
    "app1/confirm_store.py": 1,
    "app1/memory_store.py": 5,
    "app1/user_profile_db.py": 17,
    "email_service.py": 1,
    "enterprise_api/__init__.py": 2,
    "gdpr_service.py": 2,
}


def _counts():
    found = {}
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".py") or name in HOMES:
                continue
            path = os.path.join(root, name)
            n = len(re.findall(r"CREATE TABLE", open(path, encoding="utf-8", errors="ignore").read()))
            if n:
                found[os.path.relpath(path, ROOT).replace(os.sep, "/")] = n
    return found


def test_no_new_table_definition_outside_the_schema_homes():
    grown = {f: n for f, n in _counts().items() if n > LEGACY.get(f, 0)}
    assert grown == {}, "Define new tables in schema_registry.py, not here: %s" % grown


def test_the_legacy_list_is_kept_tight():
    counts = _counts()
    stale = {f: (allowed, counts.get(f, 0)) for f, allowed in LEGACY.items() if counts.get(f, 0) < allowed}
    assert stale == {}, "These files now hold fewer definitions; lower the number in LEGACY: %s" % stale
