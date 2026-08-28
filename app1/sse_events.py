"""Canonical SSE event vocabulary for the app1 AI chat stream.

Producers (app1/agent.py and friends) and the consumer (static/futurematch/
assets/chat.js) MUST agree on these `type` strings. Historically the event
names were string literals scattered across a 1000-line stream generator and a
hand-written JS dispatch, which let producer/consumer drift in silently (an
event whose `type` no renderer matches is dropped without a trace).

This module is the single source of truth for the NEW cross-surface events
introduced by the co-pilot upgrade (ui_action / comparison_card /
learning_path_card) plus the pre-existing ones, and a tiny `sse()` helper that
formats a `data: {...}\n\n` frame. It is intentionally dependency-free and
import-safe so it can be used from the agent loop, tests, and tooling without
pulling in Flask.
"""
import json

# ── Event type constants ──────────────────────────────────────────────────
# Streaming / lifecycle
PING = "ping"
META = "meta"
THINKING = "thinking"
CHUNK = "chunk"
DONE = "[DONE]"

# Tooling
TOOL_CALL = "tool_call"
TOOL_PROGRESS = "tool_progress"

# Catalog / recommendations
COURSE_CARDS = "course_cards"
PRODUCT = "product"
COMPARISON_CARD = "comparison_card"      # NEW: analytical course comparison
LEARNING_PATH_CARD = "learning_path_card"  # NEW: sequenced, grounded learning path

# Guidance
SUGGESTIONS = "suggestions"
NOTICE = "notice"
UI_ACTION = "ui_action"                  # NEW: cross-surface navigation directive

# Profile / memory
PROFILE_UPDATE = "profile_update"
PROFILE_CONFIRM_REQUEST = "profile_confirm_request"
UI_CARD = "ui_card"
MEMORY_USED = "memory_used"
MEMORY_SAVED = "memory_saved"
PROFILER_PROGRESS = "profiler_progress"

# Side-effect confirmation
CONFIRM_CARD = "confirm_card"

# CV + Mind-map inline cards
CV_SUMMARY_CARD = "cv_summary_card"    # structured CV snapshot + portal CTA
MINDMAP_CARD = "mindmap_card"          # mind-map stats + 3D globe link
SKILL_GAPS_CARD = "skill_gaps_card"    # per-learner current→target gaps (1-5 scale)

# Cross-silo learner cards (AI empowerment pass)
AGENDA_CARD = "agenda_card"            # one prioritised "what's on my plate" list
COMPLIANCE_CARD = "compliance_card"    # the learner's own mandatory-training status

# The full set the frontend is expected to handle (used by the drift test so a
# new producer event without a consumer branch fails loudly in CI).
KNOWN_EVENT_TYPES = frozenset({
    PING, META, THINKING, CHUNK,
    TOOL_CALL, TOOL_PROGRESS,
    COURSE_CARDS, PRODUCT, COMPARISON_CARD, LEARNING_PATH_CARD,
    SUGGESTIONS, NOTICE, UI_ACTION,
    PROFILE_UPDATE, PROFILE_CONFIRM_REQUEST, UI_CARD,
    MEMORY_USED, MEMORY_SAVED, PROFILER_PROGRESS,
    CONFIRM_CARD,
    CV_SUMMARY_CARD, MINDMAP_CARD, SKILL_GAPS_CARD,
    AGENDA_CARD, COMPLIANCE_CARD,
})

# Cross-surface action verbs accepted by the open_in_app tool and the chat.js
# ui_action handler. Kept here so the tool executor, the agent, and the tests
# all validate against one list.
UI_ACTIONS = frozenset({
    "view_product",     # open a catalog product page
    "open_compare",     # open the compare view for 2-4 handles
    "open_profile",     # open the user's profile (optionally a section)
    "open_mind_map",    # open the AI memory mind-map (3D globe)
    "open_cv_upload",   # open the interactive 3D CV upload portal
    "open_learning_path",  # open the saved learning-path view
    "open_catalog",     # open the catalog, optionally pre-filtered
    "start_order",      # begin enrolment for a product (does NOT place an order)
    "open_profiler",    # switch to the AI profiler
    "open_my_learning", # open the employee learning home (/min-laering)
    "open_goals",       # open the development-goals dashboard (/mine-maal)
    "open_timeline",    # open the personal deadline/approval timeline (/min-tidslinje)
})


# HR navigation destinations accepted by the hr_open_in_app tool. Keys are the
# canonical `active_hr_page` ids used by templates/fm/_hr_subnav.html, so the
# tool, the subnav and the page-context hint all speak one vocabulary. The
# endpoint is resolved server-side (url_for) with the literal path as fallback,
# so a renamed route breaks loudly in one place instead of shipping a dead link.
HR_DESTINATIONS = {
    "dashboard":          ("hr_dashboard.dashboard", "/hr", "Oversigt"),
    "team":               ("hr_dashboard.team_cockpit", "/hr/team", "Mit team"),
    "employees":          ("hr_dashboard.employee_progress", "/hr/employee-progress", "Medarbejdere"),
    "departments":        ("hr_dashboard.departments", "/hr/departments", "Afdelinger"),
    "approvals":          ("hr_dashboard.pending_approvals", "/hr/approvals", "Godkendelser"),
    "approval_policies":  ("hr_dashboard.approval_policies", "/hr/approval-policies", "Auto-godkendelse"),
    "budgets":            ("hr_dashboard.department_budgets", "/hr/budgets", "Budgetter"),
    "roi":                ("hr_dashboard.roi_dashboard", "/hr/roi", "ROI"),
    "funnel":             ("hr_dashboard.funnel_dashboard", "/hr/funnel", "Funnel"),
    "retention":          ("hr_dashboard.retention_dashboard", "/hr/retention", "Fastholdelse"),
    "learning_analytics": ("hr_dashboard.learning_analytics", "/hr/learning-analytics", "Læringsanalyse"),
    "benchmarking":       ("hr_dashboard.benchmarking_view", "/hr/benchmarking", "Benchmarking"),
    "skill_gaps":         ("hr_dashboard.skill_gaps_view", "/hr/skill-gaps", "Kompetencer"),
    "training_plan":      ("hr_ext.training_plan", "/hr/training-plan", "Træningsplan"),
    "learning_paths":     ("hr_dashboard.learning_paths", "/hr/learning-paths", "Læringsforløb"),
    "internal_courses":   ("hr_dashboard.internal_courses", "/hr/courses", "Interne kurser"),
    "compliance":         ("hr_dashboard.compliance_matrix", "/hr/compliance", "Compliance"),
    "suppliers":          ("hr_dashboard.supplier_management", "/hr/suppliers", "Leverandører"),
    "procurement":        ("hr_ext.procurement", "/hr/procurement", "Indkøb"),
    "engagement":         ("hr_ext.engagement", "/hr/engagement", "Engagement"),
    "ai_quality":         ("hr_ext.ai_quality", "/hr/ai-quality", "AI-kvalitet"),
    "reports":            ("hr_dashboard.reports", "/hr/reports", "Rapporter"),
}

# The HR advisor may also send the manager to a catalog surface (public routes,
# no endpoint indirection needed).
HR_UI_ACTIONS = frozenset(set(HR_DESTINATIONS) | {"view_product", "open_catalog"})


def sse(event_type, **payload):
    """Return a single SSE frame string: ``data: {json}\\n\\n``.

    The frame always carries ``type``. ``ensure_ascii=False`` so Danish text
    (æøå) streams as UTF-8 rather than \\uXXXX escapes. ``default=str`` keeps a
    stray non-serialisable value from breaking the whole turn.
    """
    payload["type"] = event_type
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"
