"""
Unified whitelabel / branding service.
Single read/write path for tenant visual identity.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import MySQLdb.cursors
from flask import current_app, session

DEFAULT_BRANDING = {
    'company_name': 'Futurematch',
    'company_slug': '',
    'company_logo': None,
    'logo_url': None,
    'primary_color': '#0b6b63',
    'secondary_color': '#2563eb',
    'accent_color': '#d97706',
    'background_color': '#f5f5f7',
    'text_color': '#1f2937',
    'font_family': 'Inter, sans-serif',
    'font_size_base': '14px',
    'border_radius': '8px',
    'tagline': '',
    'website': '',
    'support_email': '',
    'support_phone': '',
    'description': '',
    'favicon_url': None,
    'custom_css': '',
    'custom_js': '',
    'hide_platform_branding': False,
}

PLATFORM_NAME = 'Futurematch'


def _parse_features(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _coalesce(*values, default=None):
    for v in values:
        if v is not None and str(v).strip() != '':
            return v
    return default


def _row_to_branding(row: dict, slug: str = '') -> dict:
    if not row:
        return dict(DEFAULT_BRANDING)

    draft = row.get('branding_draft')
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except (TypeError, ValueError):
            draft = {}
    draft = draft or {}

    use_draft = row.get('branding_status') == 'draft' and draft
    src = draft if use_draft else row

    primary = _coalesce(
        src.get('primary_color'),
        row.get('cs_primary_color'),
        row.get('primary_color'),
        row.get('brand_primary_color'),
        default=DEFAULT_BRANDING['primary_color'],
    )
    secondary = _coalesce(
        src.get('secondary_color'),
        row.get('cs_secondary_color'),
        row.get('secondary_color'),
        row.get('brand_secondary_color'),
        default=DEFAULT_BRANDING['secondary_color'],
    )
    accent = _coalesce(
        src.get('accent_color'),
        row.get('cs_accent_color'),
        row.get('accent_color'),
        default=DEFAULT_BRANDING['accent_color'],
    )
    logo = _coalesce(
        src.get('logo_url'),
        row.get('asset_logo'),
        row.get('cs_logo_url'),
        row.get('company_logo'),
        row.get('logo_url'),
    )
    favicon = _coalesce(src.get('favicon_url'), row.get('cs_favicon_url'), row.get('asset_favicon'))

    return {
        'company_name': _coalesce(
            src.get('company_display_name'),
            row.get('company_display_name'),
            row.get('company_name'),
            default=PLATFORM_NAME,
        ),
        'company_slug': slug or row.get('company_slug') or '',
        'company_logo': logo,
        'logo_url': logo,
        'primary_color': primary,
        'secondary_color': secondary,
        'accent_color': accent,
        'background_color': _coalesce(
            src.get('background_color'), row.get('background_color'), default=DEFAULT_BRANDING['background_color']
        ),
        'text_color': _coalesce(src.get('text_color'), row.get('text_color'), default=DEFAULT_BRANDING['text_color']),
        'font_family': _coalesce(
            src.get('font_family'), row.get('font_family'), default=DEFAULT_BRANDING['font_family']
        ),
        'font_size_base': _coalesce(
            src.get('font_size_base'), row.get('font_size_base'), default=DEFAULT_BRANDING['font_size_base']
        ),
        'border_radius': _coalesce(
            src.get('border_radius'), row.get('border_radius'), default=DEFAULT_BRANDING['border_radius']
        ),
        'tagline': _coalesce(src.get('company_tagline'), row.get('company_tagline'), default=''),
        'website': _coalesce(src.get('company_website'), row.get('company_website'), default=''),
        'support_email': _coalesce(src.get('support_email'), row.get('support_email'), default=''),
        'support_phone': _coalesce(src.get('support_phone'), row.get('support_phone'), default=''),
        'description': _coalesce(src.get('company_description'), row.get('company_description'), default=''),
        'favicon_url': favicon,
        'custom_css': _coalesce(src.get('custom_css'), row.get('custom_css'), default='') or '',
        'custom_js': _coalesce(src.get('custom_js'), row.get('custom_js'), default='') or '',
        'hide_platform_branding': bool(
            src.get('hide_platform_branding') if 'hide_platform_branding' in src
            else row.get('hide_platform_branding')
        ),
        'language': _coalesce(src.get('language'), row.get('language'), default='da'),
        'enable_white_label': bool(row.get('enable_white_label')),
    }


def _fetch_branding_row(conn, company_id: int) -> Optional[dict]:
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        """
        SELECT c.id, c.company_name, c.company_slug, c.company_tagline,
               c.company_logo, c.logo_url, c.primary_color, c.brand_primary_color,
               c.secondary_color, c.brand_secondary_color, c.accent_color, c.font_family,
               c.features,
               cs.company_display_name, cs.company_description, cs.company_website,
               cs.support_email, cs.support_phone,
               cs.primary_color AS cs_primary_color,
               cs.secondary_color AS cs_secondary_color,
               cs.accent_color AS cs_accent_color,
               cs.background_color, cs.text_color, cs.font_family AS cs_font_family,
               cs.font_size_base, cs.border_radius, cs.logo_url AS cs_logo_url,
               cs.favicon_url AS cs_favicon_url, cs.custom_css, cs.custom_js,
               cs.enable_white_label, cs.hide_platform_branding,
               cs.language, cs.timezone, cs.branding_status, cs.branding_draft,
               (SELECT file_path FROM company_brand_assets
                WHERE company_id = c.id AND asset_type = 'company_logo_primary'
                  AND is_primary = TRUE LIMIT 1) AS asset_logo,
               (SELECT file_path FROM company_brand_assets
                WHERE company_id = c.id AND asset_type = 'company_favicon'
                  AND is_primary = TRUE LIMIT 1) AS asset_favicon
        FROM companies c
        LEFT JOIN company_settings cs ON cs.company_id = c.id
        WHERE c.id = %s
        """,
        (company_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row


def has_custom_branding_feature(company_id: int) -> bool:
    conn = current_app.mysql.connection
    if not conn:
        return False
    try:
        row = _fetch_branding_row(conn, company_id)
        if not row:
            return False
        features = _parse_features(row.get('features'))
        return bool(features.get('custom_branding'))
    except Exception as e:
        current_app.logger.error(f"has_custom_branding_feature: {e}")
        return False


def is_whitelabel_active(company_id: int, *, platform_override: bool = False) -> bool:
    if platform_override or session.get('role') == 'admin':
        conn = current_app.mysql.connection
        if conn:
            row = _fetch_branding_row(conn, company_id)
            if row and row.get('enable_white_label'):
                return True
    if not has_custom_branding_feature(company_id):
        return False
    conn = current_app.mysql.connection
    if not conn:
        return False
    try:
        row = _fetch_branding_row(conn, company_id)
        return bool(row and row.get('enable_white_label'))
    except Exception as e:
        current_app.logger.error(f"is_whitelabel_active: {e}")
        return False


def should_hide_platform_branding(company_id: int) -> bool:
    if not is_whitelabel_active(company_id):
        return False
    conn = current_app.mysql.connection
    if not conn:
        return False
    row = _fetch_branding_row(conn, company_id)
    return bool(row and row.get('hide_platform_branding'))


def get_branding(company_id: int) -> dict:
    conn = current_app.mysql.connection
    if not conn:
        return dict(DEFAULT_BRANDING)
    try:
        row = _fetch_branding_row(conn, company_id)
        if not row:
            return dict(DEFAULT_BRANDING)
        return _row_to_branding(row, row.get('company_slug') or '')
    except Exception as e:
        current_app.logger.error(f"get_branding: {e}")
        return dict(DEFAULT_BRANDING)


def get_branding_by_slug(slug: str) -> dict:
    conn = current_app.mysql.connection
    if not conn or not slug:
        return dict(DEFAULT_BRANDING)
    try:
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT id FROM companies WHERE company_slug = %s AND status = 'active'", (slug,))
        found = cur.fetchone()
        cur.close()
        if not found:
            return dict(DEFAULT_BRANDING)
        branding = get_branding(found['id'])
        branding['company_slug'] = slug
        return branding
    except Exception as e:
        current_app.logger.error(f"get_branding_by_slug: {e}")
        return dict(DEFAULT_BRANDING)


def get_template_context(company_id: Optional[int] = None, *, prelogin_slug: str = '') -> dict:
    """Return Flask template context keys: white_label_active, company_branding, platform_name."""
    cid = company_id or session.get('company_id')
    branding = dict(DEFAULT_BRANDING)

    if cid:
        branding = get_branding(cid)
        active = is_whitelabel_active(cid)
    elif prelogin_slug:
        branding = get_branding_by_slug(prelogin_slug)
        conn = current_app.mysql.connection
        if conn:
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT id FROM companies WHERE company_slug = %s", (prelogin_slug,))
            found = cur.fetchone()
            cur.close()
            active = is_whitelabel_active(found['id']) if found else False
        else:
            active = False
    else:
        active = False

    hide = branding.get('hide_platform_branding', False) if active else False
    platform_name = branding['company_name'] if active and not hide else (
        branding['company_name'] if active else PLATFORM_NAME
    )

    return {
        'white_label_active': active,
        'company_branding': branding,
        'platform_name': platform_name if active else PLATFORM_NAME,
        'hide_platform_branding': hide,
        'tenant_slug': prelogin_slug or branding.get('company_slug', ''),
    }


def sync_legacy_companies_columns(company_id: int, data: dict) -> None:
    """Dual-write branding fields to companies table during migration."""
    conn = current_app.mysql.connection
    if not conn:
        return
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE companies SET
            primary_color = %s, brand_primary_color = %s,
            secondary_color = %s, brand_secondary_color = %s,
            accent_color = %s, font_family = %s,
            logo_url = %s, company_logo = %s,
            company_tagline = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (
            data.get('primary_color'),
            data.get('primary_color'),
            data.get('secondary_color'),
            data.get('secondary_color'),
            data.get('accent_color'),
            data.get('font_family'),
            data.get('logo_url'),
            data.get('logo_url'),
            data.get('tagline') or data.get('company_tagline'),
            company_id,
        ),
    )
    conn.commit()
    cur.close()


def save_branding_settings(company_id: int, data: dict, *, as_draft: bool = False, user_id=None) -> bool:
    conn = current_app.mysql.connection
    if not conn:
        return False

    allowed = {
        'company_display_name', 'company_description', 'company_website',
        'support_email', 'support_phone', 'primary_color', 'secondary_color',
        'accent_color', 'background_color', 'text_color', 'font_family',
        'font_size_base', 'border_radius', 'logo_url', 'favicon_url',
        'custom_css', 'custom_js', 'enable_white_label', 'hide_platform_branding',
        'language', 'timezone', 'company_tagline',
    }
    filtered = {k: v for k, v in data.items() if k in allowed}

    try:
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT id, branding_draft, branding_status FROM company_settings WHERE company_id = %s", (company_id,))
        existing = cur.fetchone()

        if as_draft:
            draft_payload = json.dumps(filtered)
            if existing:
                cur.execute(
                    """
                    UPDATE company_settings
                    SET branding_draft = %s, branding_status = 'draft', updated_at = NOW()
                    WHERE company_id = %s
                    """,
                    (draft_payload, company_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO company_settings (company_id, branding_draft, branding_status)
                    VALUES (%s, %s, 'draft')
                    """,
                    (company_id, draft_payload),
                )
        else:
            cols = list(filtered.keys())
            if not cols:
                cur.close()
                return True
            if existing:
                set_clause = ', '.join(f"{c} = %s" for c in cols)
                cur.execute(
                    f"UPDATE company_settings SET {set_clause}, branding_status = 'live', branding_draft = NULL, updated_at = NOW() WHERE company_id = %s",
                    list(filtered.values()) + [company_id],
                )
            else:
                col_names = ', '.join(['company_id'] + cols)
                placeholders = ', '.join(['%s'] * (len(cols) + 1))
                cur.execute(
                    f"INSERT INTO company_settings ({col_names}, branding_status) VALUES ({placeholders}, 'live')",
                    [company_id] + list(filtered.values()),
                )
            sync_legacy_companies_columns(company_id, filtered)

        conn.commit()
        cur.close()
        return True
    except Exception as e:
        current_app.logger.error(f"save_branding_settings: {e}")
        return False


def publish_branding(company_id: int, user_id=None) -> bool:
    conn = current_app.mysql.connection
    if not conn:
        return False
    try:
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT branding_draft FROM company_settings WHERE company_id = %s", (company_id,))
        row = cur.fetchone()
        if not row or not row.get('branding_draft'):
            cur.close()
            return False
        draft = row['branding_draft']
        if isinstance(draft, str):
            draft = json.loads(draft)
        cur.close()
        ok = save_branding_settings(company_id, draft, as_draft=False, user_id=user_id)
        if ok:
            log_branding_change(company_id, 'publish', '', 'live', user_id, 'Published branding draft')
        return ok
    except Exception as e:
        current_app.logger.error(f"publish_branding: {e}")
        return False


def log_branding_change(company_id, field, old_value, new_value, user_id, reason='') -> None:
    conn = current_app.mysql.connection
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO company_settings_history
            (company_id, setting_field, old_value, new_value, changed_by, change_reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (company_id, field, str(old_value)[:500], str(new_value)[:500], user_id, reason),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        current_app.logger.error(f"log_branding_change: {e}")


def set_custom_branding_feature(company_id: int, enabled: bool) -> bool:
    conn = current_app.mysql.connection
    if not conn:
        return False
    try:
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT features FROM companies WHERE id = %s", (company_id,))
        row = cur.fetchone()
        features = _parse_features(row.get('features') if row else None)
        features['custom_branding'] = bool(enabled)
        cur.execute(
            "UPDATE companies SET features = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(features), company_id),
        )
        if enabled:
            cur.execute(
                """
                INSERT INTO company_settings (company_id, enable_white_label)
                VALUES (%s, 1)
                ON DUPLICATE KEY UPDATE enable_white_label = 1
                """,
                (company_id,),
            )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        current_app.logger.error(f"set_custom_branding_feature: {e}")
        return False


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE %s", (column_name,))
    return cur.fetchone() is not None


def ensure_branding_schema(app) -> None:
    """Add whitelabel columns to existing databases (safe to run repeatedly)."""
    column_migrations = [
        ('company_settings', 'branding_status', "VARCHAR(20) DEFAULT 'live'"),
        ('company_settings', 'branding_draft', 'JSON NULL'),
        ('company_settings', 'company_tagline', 'VARCHAR(255) NULL'),
        ('companies', 'company_tagline', 'VARCHAR(255) NULL'),
    ]
    log = __import__('logging').getLogger(__name__)

    def _run(conn) -> None:
        if not conn:
            return
        cur = conn.cursor()
        for table, column, definition in column_migrations:
            if _column_exists(cur, table, column):
                continue
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
            log.info("Added column %s.%s", table, column)
        conn.commit()
        cur.close()

    try:
        from flask import has_request_context
        if has_request_context():
            _run(app.mysql.connection)
        else:
            with app.test_request_context():
                _run(app.mysql.connection)
    except Exception as e:
        log.warning("ensure_branding_schema: %s", e)


def migrate_legacy_branding_data(app) -> None:
    """Copy companies inline branding into company_settings where missing."""
    with app.app_context():
        conn = app.mysql.connection
        if not conn:
            return
        try:
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(
                """
                SELECT c.id, c.primary_color, c.brand_primary_color,
                       c.secondary_color, c.brand_secondary_color,
                       c.accent_color, c.font_family, c.logo_url, c.company_logo
                FROM companies c
                LEFT JOIN company_settings cs ON cs.company_id = c.id
                WHERE cs.id IS NULL OR cs.primary_color IS NULL
                """
            )
            rows = cur.fetchall()
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO company_settings (company_id, primary_color, secondary_color,
                        accent_color, font_family, logo_url)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        primary_color = COALESCE(company_settings.primary_color, VALUES(primary_color)),
                        secondary_color = COALESCE(company_settings.secondary_color, VALUES(secondary_color)),
                        logo_url = COALESCE(company_settings.logo_url, VALUES(logo_url))
                    """,
                    (
                        row['id'],
                        row.get('primary_color') or row.get('brand_primary_color') or '#0b6b63',
                        row.get('secondary_color') or row.get('brand_secondary_color') or '#2563eb',
                        row.get('accent_color') or '#f59e0b',
                        row.get('font_family') or 'Inter',
                        row.get('logo_url') or row.get('company_logo') or '',
                    ),
                )
            conn.commit()
            cur.close()
        except Exception as e:
            logging = __import__('logging')
            logging.getLogger(__name__).warning("migrate_legacy_branding_data: %s", e)


def initials_avatar(name: str) -> str:
    if not name:
        return 'FM'
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()
