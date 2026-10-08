# Documentation index

Read in this order when new to the repo: [../CLAUDE.md](../CLAUDE.md) -> [ARCHITECTURE.md](ARCHITECTURE.md) -> [DECISIONS.md](DECISIONS.md). Everything here describes the code **as it is on `main`**; finished plans and changelogs are deleted (use `git log` for history).

## Orientation

| Doc | Read it when |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | you need to find which module owns something; request lifecycle, auth, DB, jobs |
| [DECISIONS.md](DECISIONS.md) | you are about to change behaviour that a constraint protects (privacy floors, order rules, permissions, language) |
| [TESTING.md](TESTING.md) | you are writing or running tests; which harness to use |
| [ROADMAP.md](ROADMAP.md) | you want to know what is still open (verified against code) |
| [USER_ACTIONS.md](USER_ACTIONS.md) | something needs the owner's accounts, servers or a decision (env vars, rotations, one-time ops) |

## AI

| Doc | Covers |
|---|---|
| [ai-framework.md](ai-framework.md) | AI architecture: file map, context layers, tools, SSE events, memory, env flags |
| [AI_PROVIDER_TOGGLE.md](AI_PROVIDER_TOGGLE.md) | switching OpenAI <-> Claude, fallback rules, secrets, cost |
| [MIND_MAP_V2.md](MIND_MAP_V2.md) | the profile mind map (data model, API, UI behaviour) |
| [../ai_eval/README.md](../ai_eval/README.md) | golden-set quality eval harness and nightly gate |
| `../app1/help_kb/*.md` | Danish in-app help articles that feed the AI help knowledge base (`app1/help_kb.py`); they are product content, keep them true to the UI |

## Integrations

| Doc | Covers |
|---|---|
| [WHITELABEL.md](WHITELABEL.md) | tenant branding and white-label behaviour |
| [WEBHOOK_VERIFICATION.md](WEBHOOK_VERIFICATION.md) | how receivers verify outbound webhook signatures |

## Runbooks (operations)

| Runbook | Covers |
|---|---|
| [runbooks/LAUNCH_WORKFLOWS.md](runbooks/LAUNCH_WORKFLOWS.md) | learning/booking/customer handover, deployment and pilot acceptance |
| [runbooks/CHAT_DEBUG.md](runbooks/CHAT_DEBUG.md) | finding full captured conversations, errors and tool logs by a copied chat ID |
| [runbooks/DEPLOY.md](runbooks/DEPLOY.md) | ServerHoster deploy, environment variables, post-deploy checks |
| [runbooks/JOB_RUNNER.md](runbooks/JOB_RUNNER.md) | scheduler jobs, the worker service, opportunistic driver |
| [runbooks/EMAIL_SETUP.md](runbooks/EMAIL_SETUP.md) | SMTP configuration and test send |
| [runbooks/CATALOG_REBUILD.md](runbooks/CATALOG_REBUILD.md) | catalog source, Shopify sync, embeddings rebuild |
| [runbooks/SECRET_ROTATION.md](runbooks/SECRET_ROTATION.md) | rotating `SECRET_KEY`, API keys, SSO secrets |
| [runbooks/STATIC_AND_PERF.md](runbooks/STATIC_AND_PERF.md) | static caching, content-hash busting, performance indexes |
| [runbooks/GIT_HISTORY_PURGE.md](runbooks/GIT_HISTORY_PURGE.md) | removing leaked secrets from git history |

Local environments: [../sandbox/README.md](../sandbox/README.md) (Docker MySQL + seeded app), [../migrations/README.md](../migrations/README.md) (Alembic).

## Keeping docs healthy

- A doc that describes a finished plan is deleted, not archived. Durable "why" goes into `DECISIONS.md`; open work goes into `ROADMAP.md` only once verified open.
- Cite files and symbols, not line numbers. Grep every path you cite.
- English for engineering docs; Danish only for product text (help articles, UI strings).
- LF line endings, UTF-8 (no BOM, no UTF-16).
- Every doc must have a row in this index.
