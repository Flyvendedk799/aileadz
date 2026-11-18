"""Company documents the assistants may answer from (policies, handbooks, guides).

Contract
--------
* A document belongs to ONE company. Every read and write takes ``company_id``
  and filters on it; there is no cross-company lookup and no global search.
* HR uploads a PDF or a plain-text/Markdown file. The text is extracted once,
  split into passages and stored (``company_documents`` / ``company_document_chunks``
  in ``schema_registry``). The original file is NOT kept: only the text.
* ``search`` ranks the company's own passages against a question with a small
  lexical scorer (term frequency x rarity, title bonus and
  shared-beginning matching for Danish inflections and compounds). No embedding call, so it
  works offline and costs nothing per upload; it is bounded by ``MAX_DOCUMENTS``
  and ``MAX_CHUNKS_SCANNED``.
* The text is the customer's, not ours: callers that hand passages to a model
  fence them with ``grounding.delimit_untrusted`` and name the document, so an
  answer can cite it and an instruction hidden in a document is read as data.

Callers pass a DictCursor, authorize the actor (``company.settings`` to manage,
any active member to search through the assistant), own the transaction and commit.
"""
from __future__ import annotations

import json
import math
import re
import uuid

MAX_BYTES = 5 * 1024 * 1024
MAX_DOCUMENTS = 40
MAX_CHARS = 400_000
CHUNK_CHARS = 1100
CHUNK_OVERLAP = 150
MAX_CHUNKS_SCANNED = 4000
ALLOWED_EXT = (".pdf", ".txt", ".md")

_WORD = re.compile(r"[a-zA-ZæøåÆØÅéÉüÜäÄöÖ0-9]{3,}")
_STOP = frozenset("""
    alle andre blev blive bliver den denne der deres det dette dig din disse dog efter eller
    for fra have hans har havde hende her hos hun hvad hvem hver hvis hvor hvordan ikke ind
    jeg kan kunne man mange med meget men mig min mod når ned nogle noget och også over på
    sig sin skal skulle som så til under var vil ville vor være været the and that this with
    """.split())


class DocumentError(ValueError):
    """A message that is safe to show to the person uploading (Danish)."""


# Danish inflects at the end of the word ("budget", "budgettet") and builds compounds
# at the front ("barsel" + "orlov", "hjemmearbejde" / "hjemmearbejdsdage"). Two words
# count as the same when they share a long enough beginning: no dictionary needed.
_PREFIX_MIN = 6


def tokens(text):
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text or "")) if w not in _STOP]


def same_word(a, b):
    if a == b:
        return True
    short = min(len(a), len(b))
    if short < _PREFIX_MIN:
        return False
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    return shared >= _PREFIX_MIN and shared >= 0.8 * short


def _matches(term, terms):
    return term in terms or any(same_word(term, t) for t in terms)


def extract_text(filename, data):
    """Plain text from an upload. Raises ``DocumentError`` with a Danish reason."""
    import upload_guard
    name = (filename or "").strip()
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in ALLOWED_EXT:
        raise DocumentError("Upload en PDF eller en tekstfil (.txt eller .md).")
    if not data:
        raise DocumentError("Filen er tom.")
    if len(data) > MAX_BYTES:
        raise DocumentError("Filen er for stor (maks %d MB)." % (MAX_BYTES // (1024 * 1024)))
    if not upload_guard.looks_like(data, ext if ext == ".pdf" else ".txt"):
        raise DocumentError("Filens indhold passer ikke til filtypen.")
    if ext == ".pdf":
        from cv_ingest import _extract_pdf_text
        text, hint = _extract_pdf_text(data)
        if not (text or "").strip():
            raise DocumentError(hint or "Vi kunne ikke læse tekst i PDF-filen. Er det en scannet kopi?")
    else:
        text = data.decode("utf-8", errors="replace")
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n").replace("\x00", "")).strip()
    if len(text) < 40:
        raise DocumentError("Dokumentet indeholder for lidt tekst til at kunne bruges.")
    return text[:MAX_CHARS]


def split_passages(text):
    """Passages of about ``CHUNK_CHARS``, cut at paragraph or sentence ends, with a small overlap."""
    out, start, n = [], 0, len(text)
    while start < n:
        end = min(n, start + CHUNK_CHARS)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > CHUNK_CHARS // 2:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def list_documents(cur, company_id):
    cur.execute("SELECT id,title,filename,char_count,chunk_count,uploaded_by,created_at FROM company_documents "
                "WHERE company_id=%s ORDER BY created_at DESC, title", (company_id,))
    return list(cur.fetchall())


def add_document(cur, company_id, actor_user_id, title, filename, data):
    """Store one document for this company. Returns its id."""
    text = extract_text(filename, data)
    title = (title or "").strip() or re.sub(r"\.[A-Za-z0-9]+$", "", (filename or "Dokument")).replace("_", " ").strip()
    cur.execute("SELECT COUNT(*) AS n FROM company_documents WHERE company_id=%s", (company_id,))
    if int((cur.fetchone() or {}).get("n") or 0) >= MAX_DOCUMENTS:
        raise DocumentError("I har nået grænsen på %d dokumenter. Fjern et, før I uploader et nyt." % MAX_DOCUMENTS)
    passages = split_passages(text)
    doc_id = str(uuid.uuid4())
    cur.execute("INSERT INTO company_documents (id,company_id,title,filename,char_count,chunk_count,uploaded_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (doc_id, company_id, title[:255], (filename or "")[:255], len(text), len(passages), actor_user_id))
    for position, passage in enumerate(passages):
        cur.execute("INSERT INTO company_document_chunks (document_id,company_id,position,content,terms_json) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (doc_id, company_id, position, passage, json.dumps(sorted(set(tokens(passage))), ensure_ascii=False)))
    return doc_id


def remove_document(cur, company_id, document_id):
    """Delete a document and its passages. Returns True when it existed in THIS company."""
    cur.execute("SELECT id FROM company_documents WHERE company_id=%s AND id=%s", (company_id, document_id))
    if not cur.fetchone():
        return False
    cur.execute("DELETE FROM company_document_chunks WHERE company_id=%s AND document_id=%s", (company_id, document_id))
    cur.execute("DELETE FROM company_documents WHERE company_id=%s AND id=%s", (company_id, document_id))
    return True


def has_documents(cur, company_id):
    cur.execute("SELECT 1 AS x FROM company_documents WHERE company_id=%s LIMIT 1", (company_id,))
    return bool(cur.fetchone())


def search(cur, company_id, query, limit=4):
    """The company's own passages that best match ``query``:
    ``[{document_id, title, position, content, score}]``, best first, at most ``limit``."""
    wanted = list(dict.fromkeys(tokens(query)))
    if not company_id or not wanted:
        return []
    cur.execute("SELECT c.document_id,c.position,c.content,c.terms_json,d.title FROM company_document_chunks c "
                "JOIN company_documents d ON d.id=c.document_id AND d.company_id=c.company_id "
                "WHERE c.company_id=%s ORDER BY c.document_id,c.position LIMIT %s", (company_id, MAX_CHUNKS_SCANNED))
    rows = list(cur.fetchall())
    if not rows:
        return []
    term_sets = []
    for row in rows:
        try:
            term_sets.append(set(json.loads(row.get("terms_json") or "[]")))
        except ValueError:
            term_sets.append(set(tokens(row.get("content"))))
    total = len(rows)
    found = [[t for t in wanted if _matches(t, terms)] for terms in term_sets]
    rarity = {t: math.log(1 + total / (1 + sum(1 for hits in found if t in hits))) for t in wanted}
    scored = []
    for row, hits in zip(rows, found):
        if not hits:
            continue
        low = row["content"].lower()
        score = sum((1 + math.log(1 + low.count(t))) * rarity[t] for t in hits)
        score += 0.5 * sum(rarity[t] for t in wanted if t in (row.get("title") or "").lower())
        score = max(score, 0.01)
        score *= len(hits) / len(wanted)          # a passage covering the whole question beats one strong word
        scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["document_id"], pair[1]["position"]))
    return [{"document_id": r["document_id"], "title": r["title"], "position": r["position"],
             "content": r["content"], "score": round(s, 3)} for s, r in scored[:max(1, int(limit))]]
