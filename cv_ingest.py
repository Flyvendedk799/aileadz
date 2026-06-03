"""Multimodal CV / job-ad ingestion (Theme C: empty-profile cold-start).

Turns an uploaded CV (or pasted raw text) into a *proposed* Danish profile
that the user can review and selectively apply. Nothing here writes to the
database or mutates session state — the output is always a PROPOSAL.

Hard design rules:
  * Every public function is guarded and must NEVER raise. On any failure
    (missing optional dependency, unreadable file, OpenAI error, bad JSON)
    we degrade gracefully: extract_text returns ('', danish_hint) and
    parse_profile_from_text returns {}.
  * PDF parsing depends on pypdf / pdfplumber, which are import-guarded. When
    neither is installed we return a Danish hint asking the user to paste the
    text instead of crashing the route.
  * OpenAI access reuses ai_runtime._openai_client when importable; otherwise
    we fall back to a directly-guarded `openai` import. Both paths are wrapped.
"""
from __future__ import annotations

import io
import json
import os

# Cap how much text we ever feed the model — CVs are short, and this protects
# both latency and token spend. ~16k chars is comfortably more than any real CV.
_MAX_INPUT_CHARS = 16000

# Levels we ask the model to use and that downstream user_profile_db understands.
_ALLOWED_LEVELS = {"begynder", "mellem", "avanceret", "ekspert"}

# Extensions we can read as plain text without any optional dependency.
_TEXT_EXTS = {".txt", ".md", ".markdown", ".text", ".csv", ".rtf"}
_PDF_EXTS = {".pdf"}

# Danish hint shown when PDF parsing libraries are unavailable.
_PDF_DEP_HINT = (
    "Vi kunne ikke læse PDF-filen automatisk på serveren. "
    "Kopiér i stedet teksten fra dit CV og indsæt den i tekstfeltet nedenfor."
)
_UNSUPPORTED_HINT = (
    "Filtypen understøttes ikke til automatisk læsning. "
    "Indsæt i stedet teksten fra dit CV i tekstfeltet nedenfor."
)
_READ_ERROR_HINT = (
    "Vi kunne ikke læse filen. Prøv en anden fil, eller "
    "indsæt teksten fra dit CV direkte i tekstfeltet nedenfor."
)


def _filename_ext(file_storage) -> str:
    name = (getattr(file_storage, "filename", "") or "").lower().strip()
    _, _, ext = name.rpartition(".")
    return ("." + ext) if ext and "." in name else ""


def _decode_bytes(raw: bytes) -> str:
    """Best-effort decode of arbitrary uploaded bytes to text."""
    if not raw:
        return ""
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_pdf_text(raw: bytes) -> tuple[str, str]:
    """Return (text, hint). Import-guarded: missing deps -> ('', danish hint)."""
    # Prefer pdfplumber (better layout handling); fall back to pypdf.
    try:
        import pdfplumber  # type: ignore
    except Exception:
        pdfplumber = None  # noqa: N816
    if pdfplumber is not None:
        try:
            parts = []
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    try:
                        parts.append(page.extract_text() or "")
                    except Exception:
                        continue
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text, ""
        except Exception:
            pass  # fall through to pypdf

    try:
        import pypdf  # type: ignore
    except Exception:
        pypdf = None  # noqa: N816
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(io.BytesIO(raw))
            parts = []
            for page in getattr(reader, "pages", []):
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    continue
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text, ""
            # Reader worked but found no extractable text (e.g. scanned PDF).
            return "", (
                "PDF-filen ser ud til at være et scannet billede uden tekst. "
                "Indsæt i stedet teksten fra dit CV i tekstfeltet nedenfor."
            )
        except Exception:
            return "", _READ_ERROR_HINT

    # Neither dependency is installed.
    return "", _PDF_DEP_HINT


def extract_text(file_storage) -> tuple[str, str]:
    """Extract text from an uploaded CV file. Never raises.

    Returns a (text, hint) tuple:
      * text — the extracted text (may be '' on failure or empty file)
      * hint — a Danish, user-facing hint when extraction was not possible
               (empty string when extraction succeeded)

    Supported directly: .txt/.md (and similar plain-text extensions).
    Supported via guarded import: .pdf (pypdf / pdfplumber).
    Pasted raw text is handled by the caller, not here.
    """
    if file_storage is None:
        return "", ""
    try:
        ext = _filename_ext(file_storage)
        # Read the raw bytes once.
        raw = b""
        try:
            stream = getattr(file_storage, "stream", None)
            if stream is not None and hasattr(stream, "read"):
                try:
                    stream.seek(0)
                except Exception:
                    pass
                raw = stream.read() or b""
            elif hasattr(file_storage, "read"):
                raw = file_storage.read() or b""
        except Exception:
            return "", _READ_ERROR_HINT

        if not raw:
            return "", ""

        if ext in _PDF_EXTS:
            text, hint = _extract_pdf_text(raw)
            return (text or "")[: _MAX_INPUT_CHARS * 4], hint

        if ext in _TEXT_EXTS or ext == "":
            # Unknown/extension-less uploads: try to decode as text. If the
            # content looks like a PDF (magic bytes), route to the PDF path.
            if raw[:5] == b"%PDF-":
                text, hint = _extract_pdf_text(raw)
                return (text or "")[: _MAX_INPUT_CHARS * 4], hint
            return _decode_bytes(raw)[: _MAX_INPUT_CHARS * 4], ""

        # Any other extension: attempt a text decode as a last resort, but tell
        # the user this filetype is not officially supported if it looks binary.
        if raw[:5] == b"%PDF-":
            text, hint = _extract_pdf_text(raw)
            return (text or "")[: _MAX_INPUT_CHARS * 4], hint
        decoded = _decode_bytes(raw)
        if decoded and decoded.isprintable() is False and "\x00" in decoded:
            return "", _UNSUPPORTED_HINT
        return decoded[: _MAX_INPUT_CHARS * 4], ("" if decoded.strip() else _UNSUPPORTED_HINT)
    except Exception:
        return "", _READ_ERROR_HINT


def _get_openai_client():
    """Return an OpenAI client, reusing ai_runtime's when possible. Guarded."""
    # Prefer the app's shared, configured client.
    try:
        from ai_runtime import _openai_client  # type: ignore
        client = _openai_client()
        if client is not None:
            return client
    except Exception:
        pass
    # Fallback: construct one directly from a guarded openai import.
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def _model_name() -> str:
    """Pick a model, reusing ai_runtime's helpers when importable."""
    try:
        from ai_runtime import fast_model  # type: ignore
        m = fast_model()
        if m:
            return m
    except Exception:
        pass
    return os.getenv("AI_FAST_MODEL", "gpt-4o-mini")


_EXTRACTION_SYSTEM_PROMPT = (
    "Du er en præcis CV-analysator for en dansk lærings- og HR-platform. "
    "Du modtager rå tekst fra et CV eller et jobopslag og udtrækker en struktureret "
    "profil. Svar UDELUKKENDE med gyldig JSON (intet andet), på dansk, med dette skema:\n"
    "{\n"
    '  "summary": "kort dansk opsummering (maks 2-3 sætninger)",\n'
    '  "skills": [{"name": "kompetence", "level": "begynder|mellem|avanceret|ekspert"}],\n'
    '  "experience": [{"title": "stillingsbetegnelse", "company": "virksomhed", "years": "2019-2023 eller fx 4"}],\n'
    '  "education": [{"degree": "uddannelse/grad", "institution": "institution", "year": "2018"}]\n'
    "}\n"
    "Regler: Brug kun de fire kompetenceniveauer begynder, mellem, avanceret, ekspert. "
    "Oversæt felter til dansk hvor det er naturligt. Find på INTET — udelad felter du ikke kan udlede, "
    "og brug tomme strenge hvor en værdi mangler. Returnér tomme lister hvis intet kan udledes."
)


def _coerce_level(value) -> str:
    v = (str(value or "")).strip().lower()
    if v in _ALLOWED_LEVELS:
        return v
    # Map a few common synonyms / English terms onto our scale.
    mapping = {
        "beginner": "begynder", "novice": "begynder", "grundlæggende": "begynder",
        "intermediate": "mellem", "middel": "mellem", "øvet": "mellem",
        "advanced": "avanceret", "erfaren": "avanceret",
        "expert": "ekspert", "specialist": "ekspert",
    }
    return mapping.get(v, "mellem")


def _clean_str(value, max_len: int = 255) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s[:max_len]


def _normalise_profile(data) -> dict:
    """Coerce arbitrary model JSON into our strict proposal shape. Guarded."""
    if not isinstance(data, dict):
        return {}
    out = {"summary": "", "skills": [], "experience": [], "education": []}
    try:
        out["summary"] = _clean_str(data.get("summary"), 600)
    except Exception:
        out["summary"] = ""

    try:
        for s in (data.get("skills") or [])[:60]:
            if isinstance(s, dict):
                name = _clean_str(s.get("name") or s.get("skill") or s.get("navn"))
                level = _coerce_level(s.get("level") or s.get("niveau"))
            else:
                name, level = _clean_str(s), "mellem"
            if name:
                out["skills"].append({"name": name, "level": level})
    except Exception:
        pass

    try:
        for e in (data.get("experience") or [])[:40]:
            if not isinstance(e, dict):
                continue
            title = _clean_str(e.get("title") or e.get("stilling") or e.get("titel"))
            company = _clean_str(e.get("company") or e.get("virksomhed") or e.get("firma"))
            years = _clean_str(e.get("years") or e.get("år") or e.get("period") or e.get("periode"), 60)
            if title or company:
                out["experience"].append({"title": title, "company": company, "years": years})
    except Exception:
        pass

    try:
        for ed in (data.get("education") or [])[:40]:
            if not isinstance(ed, dict):
                continue
            degree = _clean_str(ed.get("degree") or ed.get("uddannelse") or ed.get("grad"))
            institution = _clean_str(ed.get("institution") or ed.get("skole") or ed.get("uddannelsessted"))
            year = _clean_str(ed.get("year") or ed.get("år") or ed.get("year_completed"), 20)
            if degree or institution:
                out["education"].append(
                    {"degree": degree, "institution": institution, "year": year}
                )
    except Exception:
        pass

    return out


def parse_profile_from_text(text: str) -> dict:
    """Extract a structured Danish profile proposal from raw CV text.

    Never raises. Returns {} on any failure (no client, OpenAI error, bad JSON,
    empty input). On success returns a dict with keys:
        summary (str), skills (list), experience (list), education (list)

    The result is a PROPOSAL for the user to review — it is never auto-applied.
    """
    try:
        if not text or not str(text).strip():
            return {}
        snippet = str(text).strip()[: _MAX_INPUT_CHARS]

        client = _get_openai_client()
        if client is None:
            return {}

        model = _model_name()
        user_msg = (
            "Udtræk en struktureret profil fra følgende CV/jobtekst. "
            "Svar kun med JSON efter det aftalte skema.\n\n---\n" + snippet
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=1500,
            )
            raw = ""
            try:
                raw = resp.choices[0].message.content or ""
            except Exception:
                raw = ""
        except Exception:
            # Retry once without response_format in case the model/endpoint
            # does not support JSON mode.
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=1500,
                )
                raw = (resp.choices[0].message.content or "") if resp else ""
            except Exception:
                return {}

        if not raw:
            return {}
        parsed = _safe_load_json(raw)
        if parsed is None:
            return {}
        return _normalise_profile(parsed)
    except Exception:
        return {}


def _safe_load_json(raw: str):
    """Parse JSON from a model response, tolerating code fences / stray text."""
    if not raw:
        return None
    s = raw.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Strip markdown code fences if present.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        try:
            return json.loads(s.strip())
        except Exception:
            pass
    # Last resort: grab the outermost {...} block.
    try:
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start : end + 1])
    except Exception:
        pass
    return None
