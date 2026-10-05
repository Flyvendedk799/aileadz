"""Sales-led customer handover and evidence-based launch readiness.

A checkbox can document a setup review. It cannot manufacture a successful
booking, delivery, or completed learner journey.
"""

MANUAL_CHECKS = {
    "organisation": "Afdelinger og ledere er gennemgået",
    "approval_policy": "Godkendelsesregler er aftalt",
    "suppliers": "Leverandører, priser og kontaktveje er gennemgået",
    "support": "Medarbejderne ved, hvem de skal kontakte",
}


def readiness(cur, company_id):
    def count(sql):
        cur.execute(sql, (company_id,))
        row = cur.fetchone()
        return int((row or {}).get("n") or 0)

    cur.execute("SELECT check_key,note,confirmed_at FROM company_launch_checks WHERE company_id=%s", (company_id,))
    confirmations = {r["check_key"]: r for r in cur.fetchall() or []}
    members = count("SELECT COUNT(*) AS n FROM company_users WHERE company_id=%s AND status='active' AND role='employee'")
    missing = count(
        "SELECT COUNT(*) AS n FROM company_users WHERE company_id=%s AND status='active' AND role='employee' AND (department IS NULL OR department='' OR manager_user_id IS NULL)"
    )
    budgets = count("SELECT COUNT(*) AS n FROM department_budgets WHERE company_id=%s AND annual_budget>0")
    requests = count("SELECT COUNT(DISTINCT user_id) AS n FROM course_orders WHERE company_id=%s")
    booked = count("SELECT COUNT(*) AS n FROM course_orders WHERE company_id=%s AND booked_at IS NOT NULL")
    completed = count("SELECT COUNT(*) AS n FROM course_orders WHERE company_id=%s AND status='completed'")
    outcomes = count("SELECT COUNT(*) AS n FROM learning_outcome_reviews WHERE company_id=%s AND status='completed'")
    delivered = count("SELECT COUNT(*) AS n FROM mail_outbox WHERE company_id=%s AND state='sent'")
    delivery_issues = count("SELECT COUNT(*) AS n FROM mail_outbox WHERE company_id=%s AND state IN ('failed','uncertain')")
    stalled = count(
        "SELECT COUNT(*) AS n FROM course_orders WHERE company_id=%s AND status IN ('pending_approval','approved') AND created_at < DATE_SUB(NOW(),INTERVAL 2 DAY)"
    )
    checks = [
        {
            "key": "organisation",
            "label": "Medarbejdere, afdelinger og ledere",
            "done": members > 0 and missing == 0 and "organisation" in confirmations,
            "detail": "%s medarbejdere; %s mangler afdeling eller leder." % (members, missing),
            "url": "/companies/employees",
        },
        {
            "key": "budget",
            "label": "Budget og godkendelser",
            "done": budgets > 0 and "approval_policy" in confirmations,
            "detail": "%s budgetter er sat. Godkendelsesregler skal være gennemgået." % budgets,
            "url": "/hr/budgets",
        },
        {
            "key": "suppliers",
            "label": "Brugbart kursusudvalg og leverandøraftaler",
            "done": "suppliers" in confirmations,
            "detail": "Kontroller relevante kommende hold og en kontaktvej til hver udbyder.",
            "url": "/hr/suppliers",
        },
        {
            "key": "support",
            "label": "Kundeansvarlig og supportvej",
            "done": "support" in confirmations,
            "detail": "Aftal, hvem der ejer henvendelser og leverandøropfølgning.",
            "url": "/virksomhed/kundeforloeb",
        },
        {
            "key": "request",
            "label": "Første medarbejderanmodning",
            "done": requests > 0,
            "detail": "%s medarbejdere har bestilt et kursus." % requests,
            "url": "/hr/approvals",
        },
        {
            "key": "booking",
            "label": "Første godkendte og bekræftede booking",
            "done": booked > 0,
            "detail": "%s bookinger med registreret bekræftelse." % booked,
            "url": "/hr/",
        },
        {
            "key": "mail",
            "label": "Mailserveren har accepteret en meddelelse",
            "done": delivered > 0 and delivery_issues == 0,
            "detail": "%s afsendt; %s kræver handling. Kontroller også faktisk modtagelse hos kunden." % (delivered, delivery_issues),
            "url": "/hr/leveringer",
        },
        {
            "key": "outcome",
            "label": "Pilotens samlede læringsforløb er afsluttet",
            "done": completed > 0 and outcomes > 0,
            "detail": "%s bekræftede gennemførelser; %s vurderede kursusudbytter." % (completed, outcomes),
            "url": "/hr/roi",
        },
    ]
    return {
        "checks": checks,
        "complete": sum(c["done"] for c in checks),
        "total": len(checks),
        "members": members,
        "requesting_members": requests,
        "booked": booked,
        "completed": completed,
        "outcomes": outcomes,
        "stalled": stalled,
        "delivery_issues": delivery_issues,
        "confirmations": confirmations,
    }


def notify_account_team(cur, *, title, message, key, company_id=None):
    """Keep the account team's bell and durable delivery queue in the same commit."""
    from notification_service import notify_user
    from mail_delivery import enqueue
    import os

    cur.execute("SELECT id,username,email FROM users WHERE role='admin'")
    recipients = list(cur.fetchall() or [])
    emails = set()
    url = "/admin/kundeforloeb" + ("/%s" % company_id if company_id else "")
    for person in recipients:
        notify_user(
            cur,
            user_id=person["id"],
            username=person["username"],
            title=title,
            message=message,
            kind="customer_request",
            action_url=url,
            dedupe_key=key,
            dedupe_hours=None,
        )
        if person.get("email"):
            emails.add(person["email"])
    if company_id:
        cur.execute("SELECT account_email FROM customer_accounts WHERE company_id=%s", (company_id,))
        account = cur.fetchone() or {}
        if account.get("account_email"):
            emails.add(account["account_email"])
    fallback = os.getenv("SALES_EMAIL") or os.getenv("SUPPORT_EMAIL")
    if not emails and fallback:
        emails.add(fallback)
    from order_service import _app_base_url

    for email in emails:
        enqueue(
            email,
            title,
            "business_update",
            company_id=company_id,
            cursor=cur,
            dedupe_key=key,
            heading=title,
            message=message + "\n" + _app_base_url() + url,
        )


def notify_request_response(cur, company_id, request_id, state, note):
    from notification_service import notify_user
    from mail_delivery import enqueue
    from order_service import _app_base_url

    cur.execute(
        "SELECT r.user_id,u.username,u.email FROM customer_requests r JOIN users u ON u.id=r.user_id WHERE r.company_id=%s AND r.id=%s",
        (company_id, request_id),
    )
    person = cur.fetchone()
    if not person:
        return
    title = "Der er nyt om jeres henvendelse"
    url = "/virksomhed/kundeforloeb"
    key = "customer-response:%s:%s" % (request_id, state)
    notify_user(
        cur,
        user_id=person["user_id"],
        username=person["username"],
        company_id=company_id,
        title=title,
        message=note or "Henvendelsen er under behandling.",
        action_url=url,
        kind="customer_response",
        dedupe_key=key,
        dedupe_hours=1,
    )
    if person.get("email"):
        # Each substantive answer has its own event, even if the status is unchanged.
        import hashlib

        key += ":" + hashlib.sha256(note.encode()).hexdigest()[:16]
        enqueue(
            person["email"],
            title,
            "business_update",
            company_id=company_id,
            cursor=cur,
            dedupe_key=key,
            heading=title,
            message=(note or "Henvendelsen er under behandling.") + "\n" + _app_base_url() + url,
        )
