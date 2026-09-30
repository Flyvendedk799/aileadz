"""
One password policy for every place a password is set (S-2.2): registration,
reset, invite/set-password, settings, vendor set-password.

Rules (Danish messages; the UI promises at least 10 characters):
  * 10-128 characters
  * not the username or e-mail (or its local part)
  * not one of the most common passwords
  * not a single repeated character or a trivial sequence
"""

MIN_LENGTH = 10
MAX_LENGTH = 128

POLICY_HINT = "Mindst 10 tegn. Undgå dit navn, din e-mail og almindelige kodeord."

_COMMON = frozenset(p.lower() for p in (
    "password", "password1", "password12", "password123", "passw0rd123", "qwertyuiop",
    "qwerty1234", "qwerty12345", "1234567890", "12345678910", "123456789012", "0123456789",
    "abcdefghij", "abcd123456", "iloveyou12", "letmein123", "welcome123", "welcome1234",
    "admin12345", "administrator", "changeme123", "changeme12", "test123456", "testtest12",
    "futurematch", "futurematch1", "futurematch123", "adgangskode", "adgangskode1",
    "adgangskode123", "kodeord123", "kodeord1234", "velkommen123", "sommer2024", "sommer2025",
    "vinter2024", "vinter2025", "danmark123", "kobenhavn1", "password!", "p@ssw0rd123",
    "monkey1234", "dragon1234", "football123", "baseball123", "trustno1234", "1q2w3e4r5t",
    "1qaz2wsx3e", "zaq12wsxcde", "qazwsxedc1", "passwordpassword",
))


def validate_password(password, username=None, email=None):
    """Return a list of Danish error strings (empty list == acceptable)."""
    errors = []
    pw = password if isinstance(password, str) else ""
    if len(pw) < MIN_LENGTH:
        errors.append("Adgangskoden skal være mindst %d tegn." % MIN_LENGTH)
    if len(pw) > MAX_LENGTH:
        errors.append("Adgangskoden må højst være %d tegn." % MAX_LENGTH)
    if errors:
        return errors

    low = pw.lower()
    if low in _COMMON:
        errors.append("Adgangskoden er for almindelig. Vælg en mere unik adgangskode.")
    if len(set(pw)) <= 2:
        errors.append("Adgangskoden er for ensformig. Brug flere forskellige tegn.")
    if _is_sequence(low):
        errors.append("Adgangskoden er en simpel talrække. Vælg en mere unik adgangskode.")

    for ident in (username, email, (email or "").split("@")[0] if email else None):
        ident = (ident or "").strip().lower()
        if len(ident) >= 4 and (ident in low):
            errors.append("Adgangskoden må ikke indeholde dit brugernavn eller din e-mail.")
            break
    return errors


def _is_sequence(s):
    if len(s) < 6:
        return False
    steps = {ord(b) - ord(a) for a, b in zip(s, s[1:])}
    return steps <= {1} or steps <= {-1}


def first_error(password, username=None, email=None):
    errs = validate_password(password, username, email)
    return errs[0] if errs else None
