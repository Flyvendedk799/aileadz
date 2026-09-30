"""
Branded transactional email helpers.
Uses Flask-Mail when configured; otherwise logs and returns HTML for debugging.
"""

from __future__ import annotations

import os
from typing import Optional

from flask import current_app, render_template_string


_TRUTHY = {'1', 'true', 'yes', 'on'}


def load_mail_config(app) -> dict:
    """Copy the ``MAIL_*`` environment into ``app.config`` (N-0.2).

    Flask-Mail reads its settings from ``app.config`` only, and nothing used to
    copy the env vars across, so every send silently fell through to
    "skipped_no_backend".  Safe to call repeatedly; returns the resolved,
    secret-free summary.
    """
    env = os.environ
    server = env.get('MAIL_SERVER') or env.get('SMTP_HOST') or env.get('SMTP_SERVER') or ''
    try:
        port = int(env.get('MAIL_PORT') or env.get('SMTP_PORT') or 587)
    except ValueError:
        port = 587
    use_ssl = (env.get('MAIL_USE_SSL') or '').lower() in _TRUTHY
    use_tls = (env.get('MAIL_USE_TLS') or ('0' if use_ssl else '1')).lower() in _TRUTHY
    username = env.get('MAIL_USERNAME') or env.get('SMTP_USER') or ''
    app.config.update({
        'MAIL_SERVER': server,
        'MAIL_PORT': port,
        'MAIL_USE_TLS': use_tls and not use_ssl,
        'MAIL_USE_SSL': use_ssl,
        'MAIL_USERNAME': username or None,
        'MAIL_PASSWORD': env.get('MAIL_PASSWORD') or env.get('SMTP_PASSWORD') or None,
        'MAIL_DEFAULT_SENDER': env.get('MAIL_DEFAULT_SENDER') or username or None,
        'MAIL_SUPPRESS_SEND': False,
    })
    return mail_status(app)


def mail_status(app=None) -> dict:
    """Honest mail readiness: which config is missing, never the secrets."""
    cfg = (app or current_app).config
    missing = []
    try:
        import flask_mail  # noqa: F401
    except Exception:
        missing.append('flask_mail (pip-pakke)')
    if not (cfg.get('MAIL_SERVER') or os.getenv('MAIL_SERVER')):
        missing.append('MAIL_SERVER')
    if not (cfg.get('MAIL_DEFAULT_SENDER') or os.getenv('MAIL_DEFAULT_SENDER')):
        missing.append('MAIL_DEFAULT_SENDER')
    return {
        'configured': not missing,
        'missing': missing,
        'server': cfg.get('MAIL_SERVER') or '',
        'port': cfg.get('MAIL_PORT'),
        'tls': bool(cfg.get('MAIL_USE_TLS')),
        'ssl': bool(cfg.get('MAIL_USE_SSL')),
        'has_credentials': bool(cfg.get('MAIL_USERNAME') and cfg.get('MAIL_PASSWORD')),
        'sender': cfg.get('MAIL_DEFAULT_SENDER') or '',
    }


def send_test_email(to_email: str) -> dict:
    """Send a real test mail and report the outcome (admin "send test email")."""
    status = mail_status()
    if not status['configured']:
        return {'ok': False, 'error': 'E-mail er ikke sat op endnu. Mangler: ' + ', '.join(status['missing'])}
    try:
        from flask_mail import Mail, Message
        Mail(current_app).send(Message(
            subject='Test fra Futurematch',
            recipients=[to_email],
            html='<p>Hej! Denne test-mail bekræfter, at Futurematch kan sende e-mail.</p>',
            sender=('Futurematch', _default_sender()),
        ))
        _record_email_attempt(to_email, 'test', 'sent')
        return {'ok': True}
    except Exception as e:  # SMTP auth/connect errors are the useful signal here
        _record_email_attempt(to_email, 'test', 'error', error=str(e))
        return {'ok': False, 'error': str(e)}


def _mail_configured() -> bool:
    return bool(os.getenv('MAIL_SERVER') or current_app.config.get('MAIL_SERVER'))


def render_branded_email(template_name: str, branding: Optional[dict] = None, **context) -> str:
    """Render a branded HTML email body. Safe when branding is missing/None."""
    branding = branding or {}
    templates = {
        'welcome': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; color: #1f2937; background: {{ background_color }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:40px;margin-bottom:20px;">{% endif %}
    <h1 style="color: {{ primary_color }}; font-size: 22px;">Velkommen til {{ company_name }}</h1>
    <p>Hej {{ recipient_name }},</p>
    <p>Du er inviteret til {{ company_name }}s læringsplatform.</p>
    <p><a href="{{ login_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;">Log ind</a></p>
    <p style="font-size:12px;color:#64748b;">Har du spørgsmål? Kontakt {{ support_email or 'support' }}.</p>
  </div>
</body></html>
""",
        'password_reset': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">Nulstil adgangskode</h2>
    <p>Brug linket herunder for at nulstille din adgangskode hos {{ company_name }}.</p>
    <p><a href="{{ reset_url }}" style="color: {{ primary_color }};">Nulstil adgangskode</a></p>
  </div>
</body></html>
""",
        'order_confirmation': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">Ordrebekræftelse</h2>
    <p>Tak for din bestilling hos {{ company_name }}.</p>
    <p><strong>{{ product_title }}</strong></p>
    {% if status_line %}<p style="font-size:14px;">Status: <strong>{{ status_line }}</strong></p>{% endif %}
    {% if next_step %}<p style="font-size:14px;color:#334155;">{{ next_step }}</p>{% endif %}
    {% if order_url %}<p><a href="{{ order_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;">Se status</a></p>{% endif %}
    <p style="font-size:13px;color:#64748b;">Ordre: {{ order_id }}</p>
  </div>
</body></html>
""",
        'order_approval_needed': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">Kursusbestilling afventer godkendelse</h2>
    <p>En medarbejder har bestilt et kursus, der kræver din godkendelse.</p>
    <p><strong>{{ product_title }}</strong>{% if amount %} — {{ amount }} kr.{% endif %}</p>
    {% if requester %}<p style="font-size:13px;color:#64748b;">Bestilt af: {{ requester }}{% if department %} ({{ department }}){% endif %}</p>{% endif %}
    <p><a href="{{ approvals_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;">Gennemgå godkendelser</a></p>
  </div>
</body></html>
""",
        'order_approved': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">Din kursusbestilling er {{ decision or 'godkendt' }}</h2>
    <p><strong>{{ product_title }}</strong></p>
    <p>{{ message or 'Du kan nu komme i gang. Log ind for at se detaljerne.' }}</p>
    {% if order_url %}<p><a href="{{ order_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;">Se din bestilling</a></p>{% endif %}
    <p style="font-size:13px;color:#64748b;">Ordre: {{ order_id }}</p>
  </div>
</body></html>
""",
        'order_booked': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">Din plads er booket</h2>
    <p><strong>{{ product_title }}</strong></p>
    {% if variant_date %}<p style="font-size:14px;">Dato: {{ variant_date }}</p>{% endif %}
    {% if variant_location %}<p style="font-size:14px;">Sted: {{ variant_location }}</p>{% endif %}
    <p>Udbyderen har bekræftet din plads. Du kan tilføje kurset til din kalender fra bestillingen.</p>
    {% if order_url %}<p><a href="{{ order_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;">Se din bestilling</a></p>{% endif %}
    <p style="font-size:13px;color:#64748b;">Ordre: {{ order_id }}</p>
  </div>
</body></html>
""",
        'order_cancelled': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color:#b91c1c;">Bestilling annulleret</h2>
    <p><strong>{{ product_title }}</strong></p>
    {% if reason %}<p style="font-size:14px;">Årsag: {{ reason }}</p>{% endif %}
    {% if order_url %}<p><a href="{{ order_url }}" style="color: {{ primary_color }};">Se bestillingen</a></p>{% endif %}
    <p style="font-size:13px;color:#64748b;">Ordre: {{ order_id }}</p>
  </div>
</body></html>
""",
        'vendor_new_order': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    <h2 style="color: {{ primary_color }};">Ny bestilling afventer din bekræftelse</h2>
    <p>Hej {{ vendor_name or 'leverandør' }},</p>
    <p>En bestilling er godkendt og venter på, at du bekræfter pladsen.</p>
    <p><strong>{{ product_title }}</strong></p>
    {% if participant %}<p style="font-size:14px;">Deltager: {{ participant }}</p>{% endif %}
    {% if variant_date %}<p style="font-size:14px;">Dato: {{ variant_date }}</p>{% endif %}
    {% if variant_location %}<p style="font-size:14px;">Sted: {{ variant_location }}</p>{% endif %}
    {% if orders_url %}<p><a href="{{ orders_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;">Åbn bestillinger</a></p>{% endif %}
  </div>
</body></html>
""",
        'budget_overrun_alert': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #fee2e2;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color:#b91c1c;">Budgetadvarsel: {{ department }}</h2>
    <p>Afdelingen <strong>{{ department }}</strong> har overskredet sit årlige uddannelsesbudget.</p>
    <p style="font-size:14px;">Forbrugt: <strong>{{ spent }} kr.</strong> af {{ annual_budget }} kr.</p>
    <p style="font-size:13px;color:#64748b;">Udløst af ordre {{ order_id }}.</p>
  </div>
</body></html>
""",
        'vendor_invite': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; color:#1f2937; background: {{ background_color }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:40px;margin-bottom:20px;">{% endif %}
    <h1 style="color: {{ primary_color }}; font-size: 22px;">Velkommen som leverandør</h1>
    <p>Hej {{ vendor_name or 'leverandør' }},</p>
    <p>Du er blevet oprettet som leverandør på {{ company_name }}s kursusplatform. Sæt din adgangskode for at komme i gang med leverandørportalen.</p>
    <p><a href="{{ set_password_url }}" style="display:inline-block;background:{{ primary_color }};color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600;">Sæt din adgangskode</a></p>
    {% if expires_at %}<p style="font-size:12px;color:#64748b;">Linket udløber {{ expires_at }}.</p>{% endif %}
    <p style="font-size:12px;color:#64748b;">Virker knappen ikke, så kopiér dette link ind i din browser:<br><span style="word-break:break-all;">{{ set_password_url }}</span></p>
    <p style="font-size:12px;color:#64748b;">Har du spørgsmål? Kontakt {{ support_email or 'support' }}.</p>
  </div>
</body></html>
""",
        'compliance_recert_alert': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; color:#1f2937; background: {{ background_color }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #fee2e2;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color:#b91c1c;">Lovpligtig recertificering forfalden</h2>
    <p>Hej {{ recipient_name or 'leder' }},</p>
    <p>Et lovpligtigt compliance-krav hos {{ company_name }} er ikke længere overholdt og kræver recertificering.</p>
    <p style="font-size:15px;"><strong>{{ requirement_title }}</strong></p>
    <p style="font-size:14px;">{{ overdue_count }} medarbejder{{ 'e' if overdue_count != 1 else '' }} har en udløbet certificering ({{ scope }}).</p>
    <p style="font-size:13px;color:#64748b;">Log ind for at se hvem og planlæg recertificering, så kravet kommer tilbage i overholdelse.</p>
  </div>
</body></html>
""",
        'manager_weekly_digest': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; color:#1f2937; background: {{ background_color }}; padding: 24px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">Ugentligt lederoverblik</h2>
    <p>Hej {{ recipient_name or 'leder' }},</p>
    <p>Her er ugens overblik for {{ company_name }}:</p>
    <table style="width:100%;border-collapse:collapse;margin-top:8px;font-size:14px;">
      <tr><td style="padding:8px 0;border-bottom:1px solid #f1f5f9;">Bestillinger der afventer godkendelse</td>
          <td style="padding:8px 0;border-bottom:1px solid #f1f5f9;text-align:right;"><strong>{{ pending_approvals }}</strong></td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f1f5f9;">Budgetforbrug</td>
          <td style="padding:8px 0;border-bottom:1px solid #f1f5f9;text-align:right;"><strong>{{ budget_utilization }}</strong></td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f1f5f9;">Inaktive medarbejdere (7+ dage)</td>
          <td style="padding:8px 0;border-bottom:1px solid #f1f5f9;text-align:right;"><strong>{{ inactive_employees }}</strong></td></tr>
      <tr><td style="padding:8px 0;border-bottom:1px solid #f1f5f9;">Åbne kompetencegab</td>
          <td style="padding:8px 0;border-bottom:1px solid #f1f5f9;text-align:right;"><strong>{{ skill_gaps }}</strong></td></tr>
      <tr><td style="padding:8px 0;">Heraf kritiske kompetencegab</td>
          <td style="padding:8px 0;text-align:right;"><strong>{{ critical_skill_gaps }}</strong></td></tr>
    </table>
    <p style="font-size:13px;color:#64748b;margin-top:20px;">Log ind for at se detaljerne og handle på dem.</p>
  </div>
</body></html>
""",
        'announcement': """
<!DOCTYPE html>
<html><body style="font-family: {{ font_family }}; color:#1f2937; background: {{ background_color }}; padding: 24px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;border:1px solid #e2e8f0;">
    {% if logo_url %}<img src="{{ logo_url }}" alt="{{ company_name }}" style="height:36px;margin-bottom:16px;">{% endif %}
    <h2 style="color: {{ primary_color }};">{{ heading or subject }}</h2>
    <p>Hej {{ recipient_name or 'medarbejder' }},</p>
    <div style="font-size:15px;line-height:1.6;white-space:pre-line;">{{ message }}</div>
    <p style="font-size:13px;color:#64748b;margin-top:20px;">Sendt af {{ company_name }}.</p>
  </div>
</body></html>
""",
    }
    body_tpl = templates.get(template_name, templates['welcome'])
    ctx = {
        'company_name': branding.get('company_name', 'Futurematch'),
        'logo_url': branding.get('logo_url') or branding.get('company_logo'),
        'primary_color': branding.get('primary_color', '#0b6b63'),
        'background_color': branding.get('background_color', '#f8fafc'),
        'font_family': branding.get('font_family', 'Inter, sans-serif'),
        'support_email': branding.get('support_email', ''),
        **context,
    }
    return render_template_string(body_tpl, **ctx)


def _default_sender() -> str:
    """Resolve the configured default sender address (env wins, then config)."""
    return (
        os.getenv('MAIL_DEFAULT_SENDER')
        or current_app.config.get('MAIL_DEFAULT_SENDER')
        or ''
    )


def _ensure_email_log_table(conn) -> bool:
    """Idempotently create the email_log table. Best-effort; never raises.

    Also idempotently adds the nullable ``dedupe_key`` column on pre-existing
    installs (the ALTER is wrapped so it never breaks when the column is already
    present). ``dedupe_key`` is a caller-supplied per-event marker (e.g.
    ``order_approval_needed:<order_id>``) the recent-duplicate guard reads so the
    same alert is not re-sent within a short window.
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS email_log (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                to_email VARCHAR(255) NULL,
                template VARCHAR(64) NULL,
                status VARCHAR(32) NULL,
                error TEXT NULL,
                dedupe_key VARCHAR(255) NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        # Idempotent column add for installs that predate dedupe_key.
        try:
            cur.execute(
                "ALTER TABLE email_log ADD COLUMN dedupe_key VARCHAR(255) NULL"
            )
        except Exception:
            # Column already exists (or the DB rejected the harmless ALTER) — fine.
            pass
        conn.commit()
        cur.close()
        return True
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("email_log table ensure failed: %s", e)
        except Exception:
            pass
        return False


def _record_email_attempt(
    to_email: str,
    template_name: str,
    status: str,
    *,
    company_id=None,
    error: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> None:
    """Persist an email attempt into email_log. Fully guarded — never raises."""
    try:
        conn = getattr(current_app, 'mysql', None)
        conn = getattr(conn, 'connection', None) if conn is not None else None
        if conn is None:
            return
        if not _ensure_email_log_table(conn):
            return
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO email_log
                (company_id, to_email, template, status, error, dedupe_key, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                company_id,
                (to_email or '')[:255],
                (template_name or '')[:64],
                (status or '')[:32],
                (error or None),
                (dedupe_key or None) and str(dedupe_key)[:255],
            ),
        )
        conn.commit()
        cur.close()
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("email_log insert failed: %s", e)
        except Exception:
            pass


def email_recently_sent(dedupe_key: str, *, within_hours: int = 24,
                        company_id=None) -> bool:
    """True iff an email with this ``dedupe_key`` was logged within the window.

    Recent-duplicate guard for time-sensitive alerts (order-approval-needed,
    budget-overrun) so the same event is not re-emailed on every order while the
    condition persists. Counts ANY logged attempt for the key — including
    ``skipped_no_backend`` — so a configured-then-unconfigured flap does not
    cause a burst. Fully guarded: returns False on any error / missing DB (i.e.
    "not a known duplicate, go ahead and try").
    """
    if not dedupe_key:
        return False
    try:
        conn = getattr(current_app, 'mysql', None)
        conn = getattr(conn, 'connection', None) if conn is not None else None
        if conn is None:
            return False
        if not _ensure_email_log_table(conn):
            return False
        cur = conn.cursor()
        try:
            hours = int(within_hours)
        except (TypeError, ValueError):
            hours = 24
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM email_log
            WHERE dedupe_key = %s
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)
            """,
            (str(dedupe_key)[:255], hours),
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return False
        n = row.get('n') if isinstance(row, dict) else row[0]
        return bool(n and int(n) > 0)
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("email_recently_sent check failed: %s", e)
        except Exception:
            pass
        return False


# Templates that respect users.email_notifications. Everything else (order
# confirmation/decision, password reset, invites, welcome) is transactional.
NON_TRANSACTIONAL_TEMPLATES = frozenset({
    'manager_weekly_digest', 'compliance_recert_alert', 'announcement',
    'budget_overrun_alert', 'order_approval_needed', 'scheduled_report',
})


def recipient_opted_out(to_email: str) -> bool:
    """True iff a platform user with this address switched email notifications
    off. Fully guarded: any error means "not opted out"."""
    try:
        conn = getattr(current_app, 'mysql', None)
        conn = getattr(conn, 'connection', None) if conn is not None else None
        if conn is None or not to_email:
            return False
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT email_notifications FROM users WHERE email = %s LIMIT 1",
                (to_email,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if not row:
            return False
        val = row.get('email_notifications') if isinstance(row, dict) else row[0]
        return val is not None and int(val) == 0
    except Exception:
        return False


def send_branded_email(
    to_email: str,
    subject: str,
    template_name: str,
    branding: Optional[dict] = None,
    *,
    reply_to: Optional[str] = None,
    company_id=None,
    dedupe_key: Optional[str] = None,
    **context,
) -> bool:
    """Send a branded email. Returns True on success, False on no-op/failure.

    Best-effort by design: if no mail backend is configured this returns
    False quietly (logged at debug) and NEVER raises. Each attempt is recorded
    in email_log (guarded). An optional ``dedupe_key`` is stamped on the log row
    so callers can use ``email_recently_sent`` to avoid re-sending the same
    time-sensitive alert within a window.
    """
    branding = branding or {}

    # No recipient -> nothing to do.
    if not to_email:
        try:
            current_app.logger.debug("send_branded_email: no recipient, skipping")
        except Exception:
            pass
        return False

    # Render is safe even when branding is empty (render_branded_email
    # applies defaults for every key).
    try:
        html = render_branded_email(template_name, branding, **context)
    except Exception as e:
        try:
            current_app.logger.debug("send_branded_email: render failed: %s", e)
        except Exception:
            pass
        _record_email_attempt(
            to_email, template_name, 'error', company_id=company_id,
            error=f"render: {e}", dedupe_key=dedupe_key,
        )
        return False

    # Honour the per-user "email notifications" preference for non-transactional
    # mail (digests, alerts, announcements). Order/account/invite mail is always
    # sent (N-3.2).
    if template_name in NON_TRANSACTIONAL_TEMPLATES and recipient_opted_out(to_email):
        _record_email_attempt(
            to_email, template_name, 'skipped_opt_out', company_id=company_id,
            dedupe_key=dedupe_key,
        )
        return False

    from_name = branding.get('company_name') or 'Futurematch'
    default_sender = _default_sender()
    reply = reply_to or branding.get('support_email') or default_sender or None

    # Ops-gate: need both a server and a default sender to actually send.
    if not _mail_configured() or not default_sender:
        current_app.logger.debug(
            "Email (not sent — MAIL not configured): to=%s subject=%s from_name=%s",
            to_email, subject, from_name,
        )
        _record_email_attempt(
            to_email, template_name, 'skipped_no_backend', company_id=company_id,
            dedupe_key=dedupe_key,
        )
        return False

    try:
        from flask_mail import Message, Mail
        mail = Mail(current_app)
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=html,
            sender=(from_name, default_sender),
            reply_to=reply,
        )
        mail.send(msg)
        _record_email_attempt(
            to_email, template_name, 'sent', company_id=company_id,
            dedupe_key=dedupe_key,
        )
        return True
    except Exception as e:
        try:
            current_app.logger.error(f"send_branded_email failed: {e}")
        except Exception:
            pass
        _record_email_attempt(
            to_email, template_name, 'error', company_id=company_id, error=str(e),
            dedupe_key=dedupe_key,
        )
        return False


def _resolve_branding(company_id) -> dict:
    """Best-effort branding lookup for a company. Never raises; returns {} on miss."""
    if not company_id:
        return {}
    try:
        from branding_service import get_branding
        return get_branding(company_id) or {}
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("_resolve_branding failed: %s", e)
        except Exception:
            pass
        return {}


def send_order_confirmation(order: dict, *, branding: Optional[dict] = None,
                            company_id=None) -> bool:
    """Best-effort order-confirmation email. Guarded; never raises.

    Accepts the in-memory order dict used by order_handler (keys: order_id,
    product{title,...}, user{email,...}). Resolves branding from the company
    when not supplied.
    """
    try:
        order = order or {}
        user = order.get('user') or {}
        product = order.get('product') or {}
        to_email = user.get('email') or ''
        if not to_email:
            return False

        if branding is None:
            branding = _resolve_branding(company_id)

        company_name = (branding or {}).get('company_name') or 'Futurematch'
        return send_branded_email(
            to_email,
            f"Ordrebekræftelse – {company_name}",
            'order_confirmation',
            branding or {},
            company_id=company_id,
            company_name=company_name,
            recipient_name=user.get('name', ''),
            product_title=product.get('title', ''),
            order_id=order.get('order_id', ''),
        )
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("send_order_confirmation failed: %s", e)
        except Exception:
            pass
        return False


def send_employee_welcome(company: Optional[dict], employee: dict, *,
                          login_url: str = '', branding: Optional[dict] = None) -> bool:
    """Best-effort welcome/invite email to a newly added employee. Never raises.

    `company` may be the company row dict (with an 'id'); `employee` carries
    at least 'email' and optionally 'name'/'username'.
    """
    try:
        company = company or {}
        employee = employee or {}
        to_email = employee.get('email') or ''
        if not to_email:
            return False

        company_id = company.get('id') or company.get('company_id')
        if branding is None:
            branding = _resolve_branding(company_id)

        company_name = (
            (branding or {}).get('company_name')
            or company.get('company_name')
            or 'Futurematch'
        )
        recipient_name = (
            employee.get('name')
            or employee.get('full_name')
            or employee.get('username')
            or ''
        )
        return send_branded_email(
            to_email,
            f"Velkommen til {company_name}",
            'welcome',
            branding or {},
            company_id=company_id,
            company_name=company_name,
            recipient_name=recipient_name,
            login_url=login_url or os.getenv('APP_BASE_URL', ''),
        )
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("send_employee_welcome failed: %s", e)
        except Exception:
            pass
        return False
