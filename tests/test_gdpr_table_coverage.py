"""GDPR schema-drift guard (AI quality patch, MC-02).

Every per-user table created by app1/user_profile_db.py MUST be handled by
gdpr_service — either exported AND erased (hard delete) or anonymised in
place. Before this patch the export/erase maps silently omitted
user_memories (the AI's free-form personality/life-context dossier — the
single most sensitive store) plus the 2026-06 profile tables
(user_certifications, user_languages, user_portfolio_links): export handed
the data subject an incomplete dossier and "retten til at blive glemt"
left the AI's notes about them in place.

This test parses the CREATE TABLE statements straight out of
app1/user_profile_db.py source text (read-only — no MySQLdb import, no DB)
and asserts membership in _EXPORT_QUERIES ∪ _DELETE_TABLES ∪
_ANONYMISE_TABLES, so the NEXT profile table cannot silently fall out of
compliance.

Pure string/constant inspection. Offline: no OPENAI_API_KEY, no MySQL.
"""
import os
import re
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import gdpr_service  # noqa: E402  (module-level is import-safe: json/logging only)

_PROFILE_DB_PATH = os.path.join(REPO_ROOT, "app1", "user_profile_db.py")

# The 2026-06 additions this patch closes the gap for. They are pure-profile
# (no financial/audit value) so they must be hard-deleted, not anonymised.
_NEW_PROFILE_TABLES = (
    "user_certifications",
    "user_languages",
    "user_portfolio_links",
    "user_memories",
    "user_active_sessions",
    "user_conversation_summaries",
    "user_knowledge",
)


def _profile_tables():
    """Table names from every CREATE TABLE in app1/user_profile_db.py."""
    with open(_PROFILE_DB_PATH, encoding="utf-8") as fh:
        source = fh.read()
    names = re.findall(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`?(\w+)`?", source, re.IGNORECASE
    )
    return sorted(set(names))


def _export_tables():
    return {table for table, _sql in gdpr_service._EXPORT_QUERIES}


def _delete_tables():
    return {table for table, _col in gdpr_service._DELETE_TABLES}


def _anonymise_tables():
    return {entry[0] for entry in gdpr_service._ANONYMISE_TABLES}


class TestGdprTableCoverage(unittest.TestCase):
    def test_parser_finds_the_known_schema(self):
        """Sanity: the regex actually sees the profile schema (guards against
        a refactor of user_profile_db.py silently emptying this test)."""
        tables = _profile_tables()
        self.assertGreaterEqual(
            len(tables), 12,
            f"Forventede mindst 12 CREATE TABLE i user_profile_db.py, fandt: {tables}",
        )
        self.assertIn("user_skills", tables)
        self.assertIn("user_memories", tables)

    def test_every_profile_table_is_exported(self):
        """GDPR art. 15/20: eksporten skal dække ALLE per-bruger-tabeller."""
        missing = sorted(set(_profile_tables()) - _export_tables())
        self.assertEqual(
            missing, [],
            "Tabeller oprettet i user_profile_db.py mangler i gdpr_service."
            f"_EXPORT_QUERIES: {missing}",
        )

    def test_every_profile_table_is_erased_or_anonymised(self):
        """GDPR art. 17: hver tabel skal enten hard-deletes eller anonymiseres."""
        covered = _delete_tables() | _anonymise_tables()
        missing = sorted(set(_profile_tables()) - covered)
        self.assertEqual(
            missing, [],
            "Tabeller oprettet i user_profile_db.py mangler i gdpr_service."
            f"_DELETE_TABLES/_ANONYMISE_TABLES: {missing}",
        )

    def test_new_profile_tables_are_hard_deleted(self):
        """user_memories + 2026-06-tabellerne er ren profil (ingen regnskabs-
        eller revisionsværdi) -> hard delete, ikke anonymisering."""
        deletes = _delete_tables()
        for table in _NEW_PROFILE_TABLES:
            self.assertIn(
                table, deletes,
                f"{table} skal hard-deletes ved GDPR-sletning",
            )

    def test_new_profile_tables_are_exported_keyed_on_username(self):
        """Eksport-SQL for de nye tabeller er scoped til præcis én bruger."""
        queries = dict(gdpr_service._EXPORT_QUERIES)
        for table in _NEW_PROFILE_TABLES:
            self.assertIn(table, queries)
            self.assertIn(
                "WHERE username=%s", queries[table],
                f"Eksport af {table} skal være keyed på username",
            )

    def test_delete_tables_use_username_column(self):
        """De nye delete-entries bruger `username` som WHERE-kolonne."""
        deletes = dict(gdpr_service._DELETE_TABLES)
        for table in _NEW_PROFILE_TABLES:
            self.assertEqual(
                deletes.get(table), "username",
                f"{table} skal slettes via WHERE username=%s",
            )

    def test_no_table_is_both_deleted_and_anonymised(self):
        """En tabel må ikke optræde i begge erase-planer (dobbeltbehandling
        ville give et misvisende slette-rapport-output)."""
        overlap = sorted(_delete_tables() & _anonymise_tables())
        self.assertEqual(overlap, [], f"Tabeller i begge planer: {overlap}")


# ---------------------------------------------------------------------------
# S-4.1  Whole-repo coverage: ANY table holding personal data must be registered
# ---------------------------------------------------------------------------
_PERSON_COLUMNS = re.compile(
    r"\b(username|user_id|user_email|email|full_name|user_name|phone|requester_user_id|"
    r"approver_user_id|employee_id|manager_user_id|ip_address|browser_token|session_id|to_email|account_id)\b",
    re.IGNORECASE,
)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".claude", "sandbox", "tests", "migrations"}


def _repo_tables_with_personal_columns():
    """{table: source file} for every CREATE TABLE in the code base that has at
    least one column that identifies a natural person."""
    found = {}
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            for m in re.finditer(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`?(\w+)`?\s*\(", text, re.IGNORECASE):
                body = text[m.end(): m.end() + 2500]
                end = re.search(r"\)\s*(ENGINE|;|\"\"\")", body)
                body = body[: end.start()] if end else body[:1500]
                if _PERSON_COLUMNS.search(body):
                    found.setdefault(m.group(1), os.path.relpath(path, REPO_ROOT))
    return found


def _erasable_tables():
    """Every table some code path in gdpr_service actually deletes/anonymises."""
    tables = _delete_tables() | _anonymise_tables()
    tables |= {gdpr_service._spec_table(spec) for spec in gdpr_service.EXTRA_SPECS}
    tables |= {"ai_sessions", "ai_analytics_events", "ai_debug_logs", "ai_latency_logs", "ai_anonymous_profiles"}  # AI store
    return tables


class TestGdprWholeRepoCoverage(unittest.TestCase):
    def test_scanner_sees_the_known_schema(self):
        found = _repo_tables_with_personal_columns()
        self.assertGreaterEqual(len(found), 40, sorted(found))
        for must in ("course_orders", "audit_log", "email_log", "employee_goals", "user_memories"):
            self.assertIn(must, found)

    def test_every_table_with_personal_data_has_a_disposition(self):
        """A NEW table with personal columns that is not registered in
        gdpr_service.COVERAGE (and therefore not exported/erased/retained on
        purpose) fails the build."""
        missing = {t: f for t, f in _repo_tables_with_personal_columns().items()
                   if t not in gdpr_service.COVERAGE}
        self.assertEqual(
            missing, {},
            "Tabeller med personoplysninger mangler i gdpr_service.COVERAGE "
            "(tilføj dem til eksport/sletning eller angiv en begrundet 'retain'): %s" % missing,
        )

    def test_retained_tables_carry_a_reason(self):
        for table, (disposition, note) in gdpr_service.COVERAGE.items():
            self.assertIn(disposition, ("delete", "anonymise", "pseudonymise", "retain"), table)
            if disposition == "retain":
                self.assertTrue(note.strip(), "%s er 'retain' uden begrundelse" % table)

    def test_delete_anonymise_pseudonymise_entries_are_really_implemented(self):
        erasable = _erasable_tables()
        unimplemented = sorted(
            t for t, (d, _n) in gdpr_service.COVERAGE.items()
            if d in ("delete", "anonymise", "pseudonymise") and t not in erasable)
        self.assertEqual(unimplemented, [], "COVERAGE lover sletning, men ingen kode udfører den: %s" % unimplemented)

    def test_audit_log_is_pseudonymised_never_deleted(self):
        self.assertEqual(gdpr_service.COVERAGE["audit_log"][0], "pseudonymise")
        self.assertNotIn("audit_log", _delete_tables())
        kinds = {s["kind"] for s in gdpr_service.EXTRA_SPECS if gdpr_service._spec_table(s) == "audit_log"}
        self.assertEqual(kinds, {"pseudonymise"})

    def test_sqlite_ai_store_is_not_called_orphaned_any_more(self):
        with open(os.path.join(REPO_ROOT, "gdpr_service.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("(SQLite, orphaned)", src)
        self.assertIn("_sqlite_ai_store_erase", src)

    def test_no_extra_spec_uses_an_unknown_subject_field(self):
        allowed = {"username", "user_id", "user_id_or_member", "email", "session_id"}
        for spec in gdpr_service.EXTRA_SPECS:
            for _col, field in spec["match"]:
                self.assertIn(field, allowed, spec["table"])


if __name__ == "__main__":
    unittest.main()
