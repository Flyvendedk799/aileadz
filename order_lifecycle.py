"""The ONE order lifecycle (N-1.1): statuses, labels, transitions, who may move
an order, and the separate billing dimension.

Pure module: no Flask, no DB. Every label map in the app imports from here so a
status can never read "Afventer betaling" on one page and "Godkendt" on another.

Lifecycle (decided 2026-09-30)::

    pending_approval -> approved -> booked -> completed
            |              |           |
            +-> rejected   +-> cancelled (from any open state)

* ``booked`` is a real step for EVERY vendor: the seat is confirmed. Vendors
  without portal access are booked by HR or a platform admin on their behalf
  (the actor is recorded in ``order_status_history``).
* Billing is a *separate* dimension (``billing_status``): money moves
  off-platform, the platform only tracks it.
"""

from __future__ import annotations

# ── statuses ────────────────────────────────────────────────────────────────
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
BOOKED = "booked"
COMPLETED = "completed"
REJECTED = "rejected"
CANCELLED = "cancelled"

ORDER_STATUSES = (PENDING_APPROVAL, APPROVED, BOOKED, COMPLETED, REJECTED, CANCELLED)
OPEN_STATUSES = frozenset({PENDING_APPROVAL, APPROVED, BOOKED})
TERMINAL_STATUSES = frozenset({COMPLETED, REJECTED, CANCELLED})
# Statuses that hold a budget charge (a seat that will be paid for).
CHARGED_STATUSES = frozenset({APPROVED, BOOKED, COMPLETED})

# Statuses that older rows / older code paths still use. Reads normalise them so
# an unmigrated row renders correctly; schema_registry migrates the data once.
LEGACY_ALIASES = {
    "pending": APPROVED,        # the old (buggy) "approved" that showed "Afventer betaling"
    "processing": BOOKED,
    "confirmed": BOOKED,
    "invoiced": BOOKED,         # billing states that used to live in status
    "paid": BOOKED,
    "canceled": CANCELLED,
}

# ── labels (Danish) ─────────────────────────────────────────────────────────
STATUS_LABELS = {
    PENDING_APPROVAL: "Afventer godkendelse",
    APPROVED: "Godkendt – afventer booking",
    BOOKED: "Booket",
    COMPLETED: "Gennemført",
    REJECTED: "Afvist",
    CANCELLED: "Annulleret",
}

# Short labels for dense HR tables / dropdowns.
STATUS_LABELS_SHORT = {
    PENDING_APPROVAL: "Afventer godkendelse",
    APPROVED: "Godkendt",
    BOOKED: "Booket",
    COMPLETED: "Gennemført",
    REJECTED: "Afvist",
    CANCELLED: "Annulleret",
}

# One-line explanation shown to the learner under the status.
STATUS_HINTS = {
    PENDING_APPROVAL: "Din leder eller HR skal godkende bestillingen.",
    APPROVED: "Bestillingen er godkendt. Udbyderen bekræfter din plads.",
    BOOKED: "Din plads er bekræftet.",
    COMPLETED: "Kurset er gennemført.",
    REJECTED: "Bestillingen blev afvist.",
    CANCELLED: "Bestillingen er annulleret.",
}

# Learner-facing bucket for grouping/colouring the timeline.
LEARNER_BUCKETS = {
    PENDING_APPROVAL: "afventer_godkendelse",
    APPROVED: "godkendt",
    BOOKED: "booket",
    COMPLETED: "gennemfoert",
    REJECTED: "afvist",
    CANCELLED: "annulleret",
}

STATUS_TONES = {  # fm badge colours
    PENDING_APPROVAL: "amber",
    APPROVED: "teal",
    BOOKED: "indigo",
    COMPLETED: "green",
    REJECTED: "red",
    CANCELLED: "",
}

# ── transitions ─────────────────────────────────────────────────────────────
TRANSITIONS = {
    PENDING_APPROVAL: frozenset({APPROVED, REJECTED, CANCELLED}),
    APPROVED: frozenset({BOOKED, COMPLETED, CANCELLED}),
    BOOKED: frozenset({COMPLETED, CANCELLED}),
    COMPLETED: frozenset(),
    REJECTED: frozenset(),
    CANCELLED: frozenset(),
}

# Actor kinds: owner (the learner), manager (HR / company admin / department
# head of the same company), vendor (the order's own vendor), admin (platform),
# system (auto-approval policy, scheduled jobs).
ACTORS_FOR_TARGET = {
    APPROVED: frozenset({"manager", "admin", "system"}),
    REJECTED: frozenset({"manager", "admin"}),
    BOOKED: frozenset({"vendor", "manager", "admin"}),
    COMPLETED: frozenset({"owner", "manager", "vendor", "admin"}),
    CANCELLED: frozenset({"owner", "manager", "vendor", "admin"}),
}


def normalize_status(raw) -> str:
    """Map any stored/legacy status onto the canonical set."""
    s = (str(raw or "").strip().lower()) or APPROVED
    s = LEGACY_ALIASES.get(s, s)
    return s if s in ORDER_STATUSES else APPROVED


def status_label(raw, short: bool = False) -> str:
    s = normalize_status(raw)
    return (STATUS_LABELS_SHORT if short else STATUS_LABELS)[s]


def learner_bucket(raw) -> str:
    return LEARNER_BUCKETS[normalize_status(raw)]


def status_choices(short: bool = True):
    """(value, label) pairs for an HR status dropdown. Billing is NOT in here."""
    labels = STATUS_LABELS_SHORT if short else STATUS_LABELS
    return [(s, labels[s]) for s in ORDER_STATUSES]


def can_transition(old, new) -> bool:
    old_n, new_n = normalize_status(old), normalize_status(new)
    return new_n in TRANSITIONS.get(old_n, frozenset())


def allowed_targets(old, actors=None):
    """Targets reachable from ``old``; optionally filtered by actor kinds."""
    out = []
    for t in sorted(TRANSITIONS.get(normalize_status(old), frozenset())):
        if actors is None or ACTORS_FOR_TARGET.get(t, frozenset()) & set(actors):
            out.append(t)
    return out


def actor_may(actors, new) -> bool:
    return bool(ACTORS_FOR_TARGET.get(normalize_status(new), frozenset()) & set(actors or ()))


def check_transition(old, new, actors):
    """Return ``(ok, code, message_da)`` for a proposed move."""
    old_n, new_n = normalize_status(old), normalize_status(new)
    if old_n == new_n:
        return False, "no_change", "Ordren har allerede denne status."
    if not can_transition(old_n, new_n):
        return False, "bad_transition", (
            "Ordren kan ikke gå fra ‘%s’ til ‘%s’." % (STATUS_LABELS_SHORT[old_n], STATUS_LABELS_SHORT[new_n])
        )
    if not actor_may(actors, new_n):
        return False, "forbidden", "Du har ikke ret til at ændre ordren til denne status."
    return True, "ok", ""


# ── billing (separate dimension; money moves off-platform) ─────────────────
NOT_INVOICED = "not_invoiced"
INVOICED = "invoiced"
PAID = "paid"
CREDITED = "credited"
BILLING_STATUSES = (NOT_INVOICED, INVOICED, PAID, CREDITED)

BILLING_LABELS = {
    NOT_INVOICED: "Ikke faktureret",
    INVOICED: "Faktureret",
    PAID: "Betalt",
    CREDITED: "Krediteret",
}
BILLING_LEARNER_LABELS = {  # the learner only sees that billing happens elsewhere
    NOT_INVOICED: "Faktureres eksternt",
    INVOICED: "Faktureres eksternt",
    PAID: "Faktureres eksternt",
    CREDITED: "Faktureres eksternt",
}
BILLING_TONES = {NOT_INVOICED: "", INVOICED: "amber", PAID: "green", CREDITED: "indigo"}

BILLING_TRANSITIONS = {
    NOT_INVOICED: frozenset({INVOICED}),
    INVOICED: frozenset({PAID, CREDITED, NOT_INVOICED}),   # NOT_INVOICED = undo a mistake
    PAID: frozenset({CREDITED}),
    CREDITED: frozenset(),
}


def normalize_billing(raw) -> str:
    s = (str(raw or "").strip().lower()) or NOT_INVOICED
    return s if s in BILLING_STATUSES else NOT_INVOICED


def can_bill_transition(old, new) -> bool:
    return normalize_billing(new) in BILLING_TRANSITIONS.get(normalize_billing(old), frozenset())


def billing_label(raw) -> str:
    return BILLING_LABELS[normalize_billing(raw)]


def status_tone(raw) -> str:
    return STATUS_TONES[normalize_status(raw)]


def billing_tone(raw) -> str:
    return BILLING_TONES[normalize_billing(raw)]


def register_jinja(app) -> None:
    """Expose the label helpers to every template so no page keeps its own map."""
    app.jinja_env.globals.update(
        order_status_label=status_label,
        order_status_tone=status_tone,
        order_billing_label=billing_label,
        order_billing_tone=billing_tone,
        order_status_choices=status_choices,
    )
