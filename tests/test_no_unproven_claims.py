"""Templates must not claim certifications the company does not hold.

The ISO 27001 claim stays out of every template until a certificate exists;
the allowlist is intentionally empty.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = set()
CLAIMS = ("iso 27001", "iso27001", "iso/iec 27001")


def test_no_template_claims_iso_27001():
    offenders = []
    for path in (ROOT / "templates").rglob("*.html"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(c in text for c in CLAIMS):
            offenders.append(rel)
    assert not offenders, f"Unproven certification claim in: {offenders}"
