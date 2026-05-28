"""
Global White-Label Integration System
Provides company branding context to all templates across the application
"""

from flask import request, session

from branding_service import get_template_context


def get_company_branding_context():
    """Get company branding context for white-label functionality."""
    company_id = session.get('company_id')
    if company_id and session.get('user'):
        is_company_user = (
            session.get('user_type') == 'company_user'
            or session.get('company_role')
            or session.get('company_id')
        )
        if is_company_user:
            return get_template_context(company_id)

    prelogin_slug = ''
    if request.view_args:
        prelogin_slug = request.view_args.get('slug') or request.view_args.get('company_slug') or ''
    prelogin_slug = prelogin_slug or request.args.get('tenant') or ''

    if prelogin_slug:
        return get_template_context(prelogin_slug=prelogin_slug)

    return get_template_context(None)


def register_white_label_context_processor(app):
    """Register the white-label context processor with the Flask app."""

    @app.context_processor
    def inject_white_label_context():
        return get_company_branding_context()
