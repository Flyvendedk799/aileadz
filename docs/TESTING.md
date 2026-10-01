# Testing

~1,900 tests, all runnable **offline**: no MySQL, no OpenAI/Anthropic key, no network. A full run takes about one minute.

```bash
pip install -r requirements.txt pytest pytest-timeout
py -m pytest -q                      # whole suite (pytest.ini: testpaths=tests, timeout=120s)
py -m pytest tests/test_order_lifecycle.py -q          # one file
py -m pytest -q -k "approval and not email"            # by name
py -m ruff check .                   # lint gate (same as CI)
SANDBOX=1 AI_WARMUP_ON_IMPORT=0 py -c "from run import create_app; create_app()"   # boot smoke
```

`tests/conftest.py` sets the env for you (`SANDBOX=1`, `AI_WARMUP_ON_IMPORT=0`, `SCHEDULER_OPPORTUNISTIC=0`, `AI_MEMORY_BACKEND=sqlite`, `ENTERPRISE_TABLE_SYNC_SKIP=1`, `CATALOG_AUTO_EMBED=0`). If no MySQL answers on `MYSQL_HOST:MYSQL_PORT`, it makes `pymysql.connect` fail instantly so best-effort DB writes never hang a test. It also treats every session as an active membership and resets the confirm store between tests.

CI (`.github/workflows/ci.yml`) additionally runs against a real MySQL 8 service; only `tests/test_schema_baseline.py` needs it (builds every table from scratch and asserts `verify_schema` finds nothing missing). A handful of tests skip when the real Shopify export or MySQL is absent (`test_catalog_service.py`, `test_ask_sse_offline.py`).

## Which harness to use

| You are testing | Use | Example |
|---|---|---|
| A pure function / scorer / policy | plain `unittest.TestCase` / pytest, no fixtures | `test_ai_cost_model.py`, `test_eval_scorers.py` |
| A service that talks SQL (orders, notifications, credits, goals, skills) | `tests/sqlite_mysql.py` `SqliteMysql` — an in-memory SQLite that translates the MySQL constructs the code uses (`%s`, `NOW()`, `ON DUPLICATE KEY UPDATE`, ...) and **raises on anything it does not understand** | `test_order_lifecycle.py`, `test_credit_service.py` |
| Vendor / enterprise / settings flows through real routes | `tests/sqlite_platform.py` `make_app(db)` — real Flask app on top of `SqliteMysql` with extra vendor/webhook/token tables | `test_vendor_orders.py`, `test_settings_hub.py` |
| Route-level auth and "this request never touched the DB" | `tests/secapp.py` — boots the real `create_app()` once per process, `FakeMySQL` records every SQL statement; helpers `get_app`, `client_as`, `login`, `patch_mysql` | `test_security_s1.py` ... `s5` |
| A template rendered without the app | `tests/jinja_globals.py` `add_app_globals(env)` registers `can`, `has_endpoint`, `credit_chip`, `order_status_*` | `test_ai_sidebar.py` |
| AI runtime / tools | fake model clients (`_FakeClient` in `tests/test_ai_runtime.py`); never call a paid API | `test_ai_runtime.py`, `test_ai_tool_registry.py` |

If a statement is untranslatable, extend `SqliteMysql` (it raises loudly on purpose) rather than loosening the test.

## Conventions

- One behaviour per test; name the file after the module or feature (`test_<module>.py`).
- `test_security_s*.py` are regression tests for the security hardening (route table, decorators, hooks); keep them green when touching auth, uploads, SSRF, CSRF or tenant scoping.
- `test_prompt_tool_name_drift.py` fails if a prompt names a tool the selector cannot serve; `test_ai_tool_arg_shapes.py` pins the argument shapes models actually emit; `test_gdpr_table_coverage.py` fails when a per-user table created in `app1/user_profile_db.py` is not handled by `gdpr_service` (export + erase, or anonymise). Update the owning registry, not the test.
- No test may need network, a key, or wall-clock sleeps. Patch `time`/env, use the fakes above.
- New behaviour behind an env flag: test both the flag-on path and the fallback.

## AI quality (not part of pytest)

`ai_eval/` drives the real agent through `/app1/ask` against a golden set and scores groundedness, tool use and fluency; it needs a sandbox DB and an API key. Nightly via `.github/workflows/ai-eval-nightly.yml`. See [../ai_eval/README.md](../ai_eval/README.md). Scorer logic itself is unit-tested offline (`test_eval_scorers.py`, `test_tooler2_scorers.py`, `test_self_eval_scorer.py`).

## Local full-stack sandbox

For a real MySQL + browser session: [../sandbox/README.md](../sandbox/README.md).
