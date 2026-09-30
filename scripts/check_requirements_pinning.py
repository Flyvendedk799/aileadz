#!/usr/bin/env python3
"""Dependency pinning review (S-5.3).

Every requirement must constrain BOTH ends (``>=x,<y`` or ``==x``) so a fresh
build cannot silently pick up a breaking major release, and a vulnerable range
is visible to pip-audit. Exit code 1 lists the offenders.

    python scripts/check_requirements_pinning.py requirements.txt
"""
import re
import sys


def offenders(text):
    bad = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        has_upper = bool(re.search(r"(<|==|~=)", line))
        has_lower = bool(re.search(r"(>=|==|~=|>)", line))
        if not (has_upper and has_lower):
            bad.append((n, line))
    return bad


def main(argv):
    path = argv[1] if len(argv) > 1 else "requirements.txt"
    with open(path, encoding="utf-8", errors="ignore") as fh:
        bad = offenders(fh.read())
    for n, line in bad:
        print("%s:%d: not bounded on both sides: %s" % (path, n, line))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
