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


def render_branded_email(template_name: str, branding: dict, **context) -> str:
    """Render a branded HTML email body."""
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
    }
    body_tpl = templates.get(template_name, templates['welcome'])
    ctx = {
        'company_name': branding.get('company_name', 'Futurematch'),
        'logo_url': branding.get('logo_url') or branding.get('company_logo'),
        'primary_color': branding.get('primary_color', '#0f766e'),
        'background_color': branding.get('background_color', '#f8fafc'),
        'font_family': branding.get('font_family', 'Inter, sans-serif'),
        'support_email': branding.get('support_email', ''),
        **context,
    }
    return render_template_string(body_tpl, **ctx)


def send_branded_email(
    to_email: str,
    subject: str,
    template_name: str,
    branding: dict,
    *,
    reply_to: Optional[str] = None,
    **context,
) -> bool:
    """Send a branded email. Returns True on success."""
    html = render_branded_email(template_name, branding, **context)
    from_name = branding.get('company_name', 'Futurematch')
    reply = reply_to or branding.get('support_email') or os.getenv('MAIL_DEFAULT_SENDER', 'noreply@futurematch.dk')

    if not _mail_configured():
        current_app.logger.info(
            "Email (not sent — MAIL not configured): to=%s subject=%s from_name=%s",
            to_email, subject, from_name,
        )
        return False

    try:
        from flask_mail import Message, Mail
        mail = Mail(current_app)
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=html,
            sender=(from_name, current_app.config.get('MAIL_DEFAULT_SENDER')),
            reply_to=reply,
        )
        mail.send(msg)
        return True
    except Exception as e:
        current_app.logger.error(f"send_branded_email failed: {e}")
        return False
