# AI Tooler 2 — Changelog

Branch: `ai-tooler-2`
Completed: 2026-06-16

This PR closes the "can read but barely act" gap in the Futurematch AI assistant.
It adds 11 new platform-control tools (HR + employee), a safe propose→confirm + audit
pipeline for every write tool, full tool-call UI feedback, memory dedup, and an
extended eval harness. All new behavior is behind env flags (master: `AI_TOOLER2`).

---

## Phase-by-phase summary

### P1 — ToolMeta scaffolding
- `ai_tool_registry.py`: extended `ToolMeta` with `confirm_required`, `audit_action`,
  `manager_only`, `progress_label`; surfaced in `tool_display_metadata()`
- `ai_runtime.py`: extended `build_tool_start_event` / `build_tool_call_event` with
  `progress_label`, `partial_failure`, `safe_error`; new `build_tool_progress_event`;
  `AI_TOOLER2` kill-switch; `AI_TOOL_TURN_TEMPERATURE`; token-estimator divisor tuning;
  cost accounting unified onto `ai_cost_model`

### P2 — Shared confirm + audit helper
- New `tool_confirm.py`: `needs_confirmation_payload()`, `manager_guard()`,
  `audit_chat_mutation()` — the canonical confirm/audit building block for every
  new write tool (≈30-line executors instead of ≈120)

### P3 — Runtime robustness (P3 batch A)
- `ai_runtime.py`: lower tool-turn temperature, unify cost model, token-estimate fix
- `app1/agent.py`: TTFB improvement — heavy company-context SQL moved behind first token

### P4 — Registry/router + grounding + payload trim (P3 batch B)
- `ai_tool_registry.py`: env-gated description trimming (`AI_TRIM_TOOL_DESCRIPTIONS`)
- `grounding.py`: Danish decompounding hook (`AI_GROUNDING_DECOMPOUND`)

### P5 — Safe HR ops tools (new)
Four new HR tools wired to dormant services:
| Tool | Service | Guard |
|---|---|---|
| `schedule_recurring_report` | scheduler + company_report_schedules | confirm + manager |
| `recheck_compliance` | compliance_service.recheck_company | confirm + manager |
| `generate_fresh_insights` | insights_engine.generate_company_insights | none (read) |
| `bulk_calendar_invites` | calendar_service.build_ics_feed | none (download) |

### P6 — High blast-radius HR writes (new)
Three new HR tools with strict guards:
| Tool | Service | Guard |
|---|---|---|
| `send_company_email` | email_service fan-out | confirm + manager + recipient-count preview |
| `send_deadline_reminders` | deadline_service.remind_company | confirm + manager |
| `create_order_for_employee` | order_service.create_order | confirm + manager + cross-tenant rejection |

### P7 — Employee-facing action tools (new)
Four new employee tools:
| Tool | Type | Guard |
|---|---|---|
| `save_course_for_later` | wishlist save | auth only (immediate) |
| `set_course_reminder` | reminder save | auth only (immediate) |
| `manage_my_order` | cancel own order | confirm + ownership-enforced |
| `request_manager_approval` | email manager | confirm + self-scoped |

### P8 — Generic confirm-card SSE round-trip
- `app1/confirm_store.py`: thread-safe pending-token store (TTL 10 min, session-bound);
  args held server-side — only opaque token reaches the browser
- `app1/agent.py` + `hr_agent.py`: any tool result with `needs_confirmation=True`
  emits a `confirm_card` SSE event; token stored in confirm_store
- `app1/__init__.py`: `POST /app1/confirm_tool_action` — consumes token, injects
  `confirm=True`, re-dispatches to correct executor, idempotent double-confirm

### P9 — UI feedback 1 (progress, confirm cards, partial-failure)
- `chat.js`:
  - 11 new TOOL_LABELS for Phase 5-7 tools
  - Progress bar inside running chips (pulse animation / filled from `percent`)
  - `partial_failure` amber chip + "delvis fejl" meta tag
  - Cache-age badge in chip when `cache_ttl` available
  - `updateToolProgress()` — updates in-flight bar from `tool_progress` SSE event
  - `renderConfirmCard()` — Bekræft/Afvis card → POST `/confirm_tool_action`
- `chat.css`: all new styles bound to `--fm-*` tokens; dark-mode safe

### P10 — UI feedback 2 (error detail, retry, memory drill-down)
- `chat.js`:
  - Errored chips: expandable `safe_error` detail + "Prøv igen" retry button
  - `renderMemoryUsed`: category prefix + × delete button per chip → DELETE `/app1/memory/<id>`
- `app1/__init__.py`:
  - `GET /app1/memory` — list user's memories
  - `DELETE /app1/memory/<id>` — delete one memory (ownership enforced)

### P11 — Memory dedup/supersede
- `app1/tools.py`: `_normalize_memory_label()` + `_find_supersedable_memory()`
  near-duplicate detector (substring containment + 70% length ratio); applied in
  `_execute_remember_about_user()` before `add_memory()`; dedup failure falls through
  gracefully to normal insert

### P12 — Golden eval cases + scorers
- `ai_eval/golden_set.json`: 12 new Danish cases covering every new tool + role-gating
- `ai_eval/scorers.py`:
  - `confirmation_before_mutation()`: PASS when confirm_card emitted or text has
    confirmation marker; covers all Phase 5-7 mutation tools
  - `role_gating_correct()`: PASS when no manager-only tools appear in employee scope
  - Both wired into `METRIC_KEYS` + `score_case()`

---

## New tool inventory

### HR tools (11 new)
`schedule_recurring_report`, `recheck_compliance`, `generate_fresh_insights`,
`bulk_calendar_invites`, `send_company_email`, `send_deadline_reminders`,
`create_order_for_employee`

### Employee tools (4 new)
`save_course_for_later`, `set_course_reminder`, `manage_my_order`,
`request_manager_approval`

---

## Env-flag matrix

| Flag | Default | Phase | Controls |
|---|---|---|---|
| `AI_TOOLER2` | `off` | P1 | Master kill-switch for all new tools + UI |
| `AI_LIVE_TOOL_EVENTS` | `1` | existing | Live tool progress/start events |
| `AI_CAPTURE_FINAL` | `1` | existing | Final-answer capture (one completion per tool turn) |
| `AI_TOOL_TURN_TEMPERATURE` | `0.2` | P1/P3 | Temperature for tool-deciding turns |
| `AI_TOKEN_CHARS_PER_TOKEN` | `4.0` | P1/P3 | Token estimator divisor (Danish tuning) |
| `AI_TRIM_TOOL_DESCRIPTIONS` | `off` | P4 | Strip verbose tool descriptions to save tokens |
| `AI_GROUNDING_DECOMPOUND` | `off` | P4 | Danish compound-word decompounding in grounding |

To revert ALL AI Tooler 2 behavior: set `AI_TOOLER2=off`. Individual flags above control finer
behavior independently of the master switch.

---

## UI gaps closed

| Gap | Where |
|---|---|
| Side-effect tools showed no confirmation gate | P8/P9: confirm_card SSE + Bekræft/Afvis |
| Tool progress was opaque | P1/P9: build_tool_progress_event + progress bar |
| Partial-failure batches appeared as success | P1/P9: partial_failure chip + amber style |
| Errored tools gave no actionable detail | P10: expandable safe_error + "Prøv igen" |
| Memory store had no UI to view/delete entries | P10: × delete per chip + /memory routes |
| Near-duplicate memories grew unboundedly | P11: dedup/supersede before add_memory |

---

## Test coverage delta

| Test file | Phase | Tests |
|---|---|---|
| `test_tool_confirm.py` | P2 | 8 |
| `test_tooler2_registry_grounding.py` | P4 | 8 |
| `test_hr_platform_tools.py` | P5 | 19 |
| `test_hr_blast_radius_tools.py` | P6 | 12 |
| `test_employee_action_tools.py` | P7 | 10 |
| `test_ask_sse_offline.py` (extended) | P8 | +7 |
| `test_memory_routes.py` | P10 | 7 |
| `test_memory_dedup.py` | P11 | 12 |
| `test_tooler2_scorers.py` | P12 | 14 |
| **Total new** | | **97** |

Suite: **743 tests passing** under `SANDBOX=1` (no OPENAI_API_KEY, no MySQL).
