"""
Branded transactional email helpers.
Uses Flask-Mail when configured; otherwise logs and returns HTML for debugging.
"""

from __future__ import annotations

import os
from typing import Optional

from flask import current_app, render_template_string


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
    <p style="font-size:13px;color:#64748b;">Ordre: {{ order_id }}</p>
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
    """Idempotently create the email_log table. Best-effort; never raises."""
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
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
            INSERT INTO email_log (company_id, to_email, template, status, error, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            """,
            (
                company_id,
                (to_email or '')[:255],
                (template_name or '')[:64],
                (status or '')[:32],
                (error or None),
            ),
        )
        conn.commit()
        cur.close()
    except Exception as e:  # pragma: no cover - defensive
        try:
            current_app.logger.debug("email_log insert failed: %s", e)
        except Exception:
            pass


def send_branded_email(
    to_email: str,
    subject: str,
    template_name: str,
    branding: Optional[dict] = None,
    *,
    reply_to: Optional[str] = None,
    company_id=None,
    **context,
) -> bool:
    """Send a branded email. Returns True on success, False on no-op/failure.

    Best-effort by design: if no mail backend is configured this returns
    False quietly (logged at debug) and NEVER raises. Each attempt is recorded
    in email_log (guarded).
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
            error=f"render: {e}",
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
        )
        return True
    except Exception as e:
        try:
            current_app.logger.error(f"send_branded_email failed: {e}")
        except Exception:
            pass
        _record_email_attempt(
            to_email, template_name, 'error', company_id=company_id, error=str(e),
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
