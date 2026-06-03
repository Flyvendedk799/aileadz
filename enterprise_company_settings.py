"""
Enterprise Premium Company Settings & White Label Management
Advanced features for high-end SaaS solutions serving large enterprises
"""

import os
import json
import uuid
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional, Tuple
import MySQLdb.cursors
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from auth_decorators import require_company_role
try:
    import boto3
except ImportError:
    boto3 = None
try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None
try:
    import magic
except ImportError:
    magic = None
try:
    import cv2
except ImportError:
    cv2 = None
try:
    import numpy as np
except ImportError:
    np = None

enterprise_settings_bp = Blueprint('enterprise_settings', __name__)

# Enterprise Configuration
ENTERPRISE_CONFIG = {
    'MAX_FILE_SIZE': 50 * 1024 * 1024,  # 50MB for enterprise assets
    'ALLOWED_IMAGE_EXTENSIONS': {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico'},
    'ALLOWED_DOCUMENT_EXTENSIONS': {'pdf', 'docx', 'pptx'},
    'BRAND_ASSET_SIZES': {
        'logo_primary': [(300, 100), (600, 200), (1200, 400)],
        'logo_secondary': [(150, 50), (300, 100), (600, 200)],
        'favicon': [(16, 16), (32, 32), (64, 64)],
        'banner': [(1920, 400), (1200, 250), (800, 167)]
    },
    'CDN_ENABLED': True,
    'S3_BUCKET': 'enterprise-brand-assets',
    'CACHE_DURATION': 3600,  # 1 hour
    'AUDIT_RETENTION_DAYS': 365
}

class EnterpriseSettingsManager:
    """Advanced settings management with enterprise features"""
    
    def __init__(self):
        self.s3_client = None
        if ENTERPRISE_CONFIG['CDN_ENABLED'] and boto3 is not None:
            try:
                self.s3_client = boto3.client('s3')
            except Exception:
                self.s3_client = None

    def get_db(self):
        """Get database connection"""
        return current_app.mysql.connection
    
    def log_settings_change(self, company_id: int, field: str, old_value: str, new_value: str, reason: str = None):
        """Log settings changes for audit trail"""
        try:
            db = self.get_db()
            cur = db.cursor(MySQLdb.cursors.DictCursor)
            
            cur.execute("""
                INSERT INTO company_settings_history 
                (company_id, setting_field, old_value, new_value, changed_by, change_reason, ip_address, user_agent)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                company_id, field, old_value, new_value,
                session.get('user_id'), reason,
                request.remote_addr, request.headers.get('User-Agent', '')[:500]
            ))
            db.commit()
            cur.close()
        except Exception as e:
            current_app.logger.error(f"Failed to log settings change: {e}")
    
    def validate_brand_asset(self, file) -> Tuple[bool, str]:
        """Advanced file validation for brand assets"""
        if not file or not file.filename:
            return False, "No file provided"
        
        # Check file size
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)
        
        if file_size > ENTERPRISE_CONFIG['MAX_FILE_SIZE']:
            return False, f"File size exceeds {ENTERPRISE_CONFIG['MAX_FILE_SIZE'] // (1024*1024)}MB limit"
        
        # Check file extension
        filename = secure_filename(file.filename.lower())
        if not any(filename.endswith(f'.{ext}') for ext in ENTERPRISE_CONFIG['ALLOWED_IMAGE_EXTENSIONS']):
            return False, "Invalid file type. Allowed: " + ", ".join(ENTERPRISE_CONFIG['ALLOWED_IMAGE_EXTENSIONS'])
        
        # Check MIME type using python-magic when available
        if magic is not None:
            try:
                file_content = file.read(1024)
                file.seek(0)
                mime_type = magic.from_buffer(file_content, mime=True)
                
                allowed_mimes = ['image/jpeg', 'image/png', 'image/gif', 'image/svg+xml', 'image/webp', 'image/x-icon']
                if mime_type not in allowed_mimes:
                    return False, f"Invalid MIME type: {mime_type}"
            except Exception as e:
                current_app.logger.warning(f"MIME type check failed: {e}")
        
        return True, "Valid"
    
    def process_brand_asset(self, file, asset_type: str, company_id: int) -> Dict:
        """Process and optimize brand assets with multiple sizes"""
        try:
            # Generate unique filename
            file_ext = file.filename.rsplit('.', 1)[1].lower()
            unique_filename = f"{company_id}_{asset_type}_{uuid.uuid4().hex}.{file_ext}"
            
            # Create upload directory
            upload_dir = os.path.join(current_app.root_path, 'static', 'brand_assets', str(company_id))
            os.makedirs(upload_dir, exist_ok=True)
            
            processed_assets = {}
            
            if file_ext == 'svg' or Image is None:
                file_path = os.path.join(upload_dir, unique_filename)
                file.save(file_path)
                processed_assets['original'] = f"/static/brand_assets/{company_id}/{unique_filename}"
            else:
                image = Image.open(file)
                if image.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', image.size, (255, 255, 255))
                    if image.mode == 'P':
                        image = image.convert('RGBA')
                    background.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
                    image = background
                
                sizes = ENTERPRISE_CONFIG['BRAND_ASSET_SIZES'].get(asset_type, [(image.width, image.height)])
                
                for width, height in sizes:
                    resized_image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
                    size_filename = f"{company_id}_{asset_type}_{width}x{height}_{uuid.uuid4().hex}.{file_ext}"
                    size_path = os.path.join(upload_dir, size_filename)
                    
                    if file_ext in ['jpg', 'jpeg']:
                        resized_image.save(size_path, 'JPEG', quality=85, optimize=True)
                    elif file_ext == 'png':
                        resized_image.save(size_path, 'PNG', optimize=True)
                    elif file_ext == 'webp':
                        resized_image.save(size_path, 'WEBP', quality=85, optimize=True)
                    
                    processed_assets[f"{width}x{height}"] = f"/static/brand_assets/{company_id}/{size_filename}"
                
                original_path = os.path.join(upload_dir, unique_filename)
                image.save(original_path, quality=95, optimize=True)
                processed_assets['original'] = f"/static/brand_assets/{company_id}/{unique_filename}"
            
            # Upload to CDN if enabled
            if ENTERPRISE_CONFIG['CDN_ENABLED'] and self.s3_client:
                self.upload_to_cdn(processed_assets, company_id)
            
            return processed_assets
            
        except Exception as e:
            current_app.logger.error(f"Failed to process brand asset: {e}")
            raise
    
    def upload_to_cdn(self, assets: Dict, company_id: int):
        """Upload processed assets to CDN"""
        try:
            for size, local_path in assets.items():
                # Convert local path to actual file path
                file_path = os.path.join(current_app.root_path, local_path.lstrip('/'))
                
                # Generate CDN key
                cdn_key = f"brand_assets/{company_id}/{os.path.basename(local_path)}"
                
                # Upload to S3
                self.s3_client.upload_file(
                    file_path, 
                    ENTERPRISE_CONFIG['S3_BUCKET'], 
                    cdn_key,
                    ExtraArgs={
                        'ContentType': self.get_content_type(file_path),
                        'CacheControl': f'max-age={ENTERPRISE_CONFIG["CACHE_DURATION"]}'
                    }
                )
                
                # Update asset path to CDN URL
                assets[size] = f"https://{ENTERPRISE_CONFIG['S3_BUCKET']}.s3.amazonaws.com/{cdn_key}"
                
        except Exception as e:
            current_app.logger.error(f"CDN upload failed: {e}")
    
    def get_content_type(self, file_path: str) -> str:
        """Get content type for file"""
        ext = file_path.lower().split('.')[-1]
        content_types = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'gif': 'image/gif',
            'svg': 'image/svg+xml', 'webp': 'image/webp',
            'ico': 'image/x-icon'
        }
        return content_types.get(ext, 'application/octet-stream')
    
    def get_company_settings(self, company_id: int) -> Dict:
        """Get comprehensive company settings"""
        try:
            db = self.get_db()
            cur = db.cursor(MySQLdb.cursors.DictCursor)
            
            cur.execute("SELECT * FROM company_settings WHERE company_id = %s", (company_id,))
            settings = cur.fetchone()
            
            if not settings:
                # Create default settings
                settings = self.create_default_settings(company_id)
            
            # Get brand assets
            cur.execute("""
                SELECT asset_type, asset_name, file_path, dimensions, is_primary
                FROM company_brand_assets 
                WHERE company_id = %s AND is_primary = TRUE
            """, (company_id,))
            
            brand_assets = {}
            for asset in cur.fetchall():
                brand_assets[asset['asset_type']] = {
                    'name': asset['asset_name'],
                    'path': asset['file_path'],
                    'dimensions': asset['dimensions']
                }
            
            settings['brand_assets'] = brand_assets
            
            # Get custom code snippets
            cur.execute("""
                SELECT code_type, code_name, code_content, is_active
                FROM company_custom_code 
                WHERE company_id = %s AND is_active = TRUE
            """, (company_id,))
            
            custom_code = {}
            for code in cur.fetchall():
                if code['code_type'] not in custom_code:
                    custom_code[code['code_type']] = []
                custom_code[code['code_type']].append({
                    'name': code['code_name'],
                    'content': code['code_content']
                })
            
            settings['custom_code'] = custom_code
            
            cur.close()
            return settings
            
        except Exception as e:
            current_app.logger.error(f"Failed to get company settings: {e}")
            return self.create_default_settings(company_id)
    
    def create_default_settings(self, company_id: int) -> Dict:
        """Create default settings for a company"""
        try:
            db = self.get_db()
            cur = db.cursor(MySQLdb.cursors.DictCursor)
            
            # Get company info
            cur.execute("SELECT company_name FROM companies WHERE id = %s", (company_id,))
            company = cur.fetchone()
            company_name = company['company_name'] if company else 'Your Company'
            
            default_settings = {
                'company_id': company_id,
                'company_display_name': company_name,
                'company_description': '',
                'company_website': '',
                'primary_color': '#667eea',
                'secondary_color': '#764ba2',
                'accent_color': '#28a745',
                'background_color': '#f8fafc',
                'text_color': '#1f2937',
                'font_family': 'Inter, system-ui, sans-serif',
                'font_size_base': '16px',
                'border_radius': '8px',
                'spacing_unit': '8px',
                'enable_white_label': 0,
                'hide_platform_branding': 0,
                'language': 'da',
                'timezone': 'Europe/Copenhagen',
                'branding_status': 'live',
            }
            
            # Insert default settings using only existing columns
            cur.execute("""
                INSERT INTO company_settings (
                    company_id, company_display_name, company_description, company_website,
                    primary_color, secondary_color, accent_color, background_color, text_color,
                    font_family, font_size_base, border_radius, spacing_unit,
                    enable_white_label, hide_platform_branding, language, timezone, branding_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                company_id, company_name, '', '',
                default_settings['primary_color'], default_settings['secondary_color'],
                default_settings['accent_color'], default_settings['background_color'],
                default_settings['text_color'], default_settings['font_family'],
                default_settings['font_size_base'], default_settings['border_radius'],
                default_settings['spacing_unit'], 0, 0, 'da', 'Europe/Copenhagen', 'live',
            ))
            
            db.commit()
            cur.close()
            
            return default_settings
            
        except Exception as e:
            current_app.logger.error(f"Failed to create default settings: {e}")
            return {}
    
    def update_settings(self, company_id: int, settings_data: Dict) -> bool:
        """Update company settings with audit trail"""
        try:
            db = self.get_db()
            cur = db.cursor(MySQLdb.cursors.DictCursor)
            
            # Get current settings for audit
            current_settings = self.get_company_settings(company_id)
            
            # Prepare update data
            update_fields = []
            update_values = []
            
            for field, value in settings_data.items():
                if field in current_settings and current_settings[field] != value:
                    # Log the change
                    self.log_settings_change(
                        company_id, field, 
                        str(current_settings[field]), str(value),
                        f"Updated via settings panel"
                    )
                    
                    update_fields.append(f"{field} = %s")
                    update_values.append(value)
            
            if update_fields:
                update_values.append(company_id)
                cur.execute(f"""
                    UPDATE company_settings 
                    SET {', '.join(update_fields)}, updated_at = NOW()
                    WHERE company_id = %s
                """, update_values)
                
                db.commit()
            
            cur.close()
            return True
            
        except Exception as e:
            current_app.logger.error(f"Failed to update settings: {e}")
            return False
    
    def get_theme_templates(self, category: str = None) -> List[Dict]:
        """Get available theme templates"""
        try:
            db = self.get_db()
            cur = db.cursor(MySQLdb.cursors.DictCursor)
            
            query = "SELECT * FROM company_theme_templates WHERE is_active = TRUE"
            params = []
            
            if category:
                query += " AND template_category = %s"
                params.append(category)
            
            query += " ORDER BY template_category, template_name"
            
            cur.execute(query, params)
            templates = cur.fetchall()
            cur.close()
            
            return templates
            
        except Exception as e:
            current_app.logger.error(f"Failed to get theme templates: {e}")
            return []
    
    def apply_theme_template(self, company_id: int, template_id: int) -> bool:
        """Apply a theme template to company settings"""
        try:
            db = self.get_db()
            cur = db.cursor(MySQLdb.cursors.DictCursor)
            
            # Get template
            cur.execute("SELECT * FROM company_theme_templates WHERE id = %s", (template_id,))
            template = cur.fetchone()
            
            if not template:
                return False
            
            # Apply template settings
            template_settings = {
                'primary_color': template['primary_color'],
                'secondary_color': template['secondary_color'],
                'accent_color': template['accent_color'],
                'background_color': template['background_color'],
                'text_color': template['text_color'],
                'font_family': template['font_family'],
                'font_size_base': template['font_size_base'],
                'border_radius': template['border_radius'],
                'spacing_unit': template['spacing_unit'],
            }
            
            # Add custom CSS if available
            if template['template_css']:
                cur.execute("""
                    INSERT INTO company_custom_code 
                    (company_id, code_type, code_name, code_content, created_by)
                    VALUES (%s, 'css', %s, %s, %s)
                    ON DUPLICATE KEY UPDATE 
                    code_content = VALUES(code_content), updated_at = NOW()
                """, (
                    company_id, f"Theme: {template['template_name']}", 
                    template['template_css'], session.get('user_id')
                ))
            
            # Update settings
            success = self.update_settings(company_id, template_settings)
            
            if success:
                self.log_settings_change(
                    company_id, 'theme_template', 'custom', template['template_name'],
                    f"Applied theme template: {template['template_name']}"
                )
            
            db.commit()
            cur.close()
            
            return success
            
        except Exception as e:
            current_app.logger.error(f"Failed to apply theme template: {e}")
            return False

# Initialize settings manager
settings_manager = EnterpriseSettingsManager()

def require_enterprise_access():
    """Decorator for enterprise settings access"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                flash('Please log in to access enterprise settings.', 'danger')
                return redirect(url_for('auth.login'))
            
            user_type = session.get('user_type', 'regular')
            company_role = session.get('company_role', '')
            platform_role = session.get('role', '')
            
            if user_type == 'platform_admin' or platform_role == 'admin':
                return f(*args, **kwargs)
            
            if company_role in ['hr_manager', 'company_admin']:
                return f(*args, **kwargs)
            
            if user_type == 'company_user' and company_role in ['hr_manager', 'company_admin']:
                return f(*args, **kwargs)
            
            flash("You don't have permission to access enterprise settings.", "danger")
            return redirect(url_for('dashboard.dashboard'))
        
        return decorated_function
    return decorator

def get_company_context():
    """Get current user's company context"""
    if 'user' not in session:
        return None
    
    # For platform admin, use session company or default
    if session.get('user_type') == 'platform_admin':
        company_id = session.get('company_id', 1)
        company_name = session.get('company_name', 'Platform Admin')
        
        try:
            conn = current_app.mysql.connection
            if conn:
                cur = conn.cursor(MySQLdb.cursors.DictCursor)
                cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
                company = cur.fetchone()
                cur.close()
                if company:
                    return company
        except Exception as e:
            current_app.logger.error(f"Error getting company for admin: {e}")
        
        return {
            'id': company_id,
            'company_name': company_name,
            'company_slug': 'admin'
        }
    
    # For company users
    try:
        conn = current_app.mysql.connection
        if not conn:
            return None

        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT c.*, cu.role as user_role, cu.department
            FROM companies c
            JOIN company_users cu ON c.id = cu.company_id
            JOIN users u ON cu.user_id = u.id
            WHERE u.username = %s AND cu.status = 'active'
        """, (session['user'],))

        company = cur.fetchone()
        cur.close()
        return company
        
    except Exception as e:
        current_app.logger.error(f"Error getting company context: {e}")
        return None

@enterprise_settings_bp.route('/company-settings')
@require_company_role('company_admin', 'hr_manager')
def company_settings():
    """Redirect legacy enterprise settings to unified branding hub."""
    return redirect(url_for('companies.branding'))

@enterprise_settings_bp.route('/company-settings', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def update_company_settings():
    """Update enterprise company settings"""
    company = get_company_context()
    if not company:
        return jsonify({'success': False, 'message': 'Company not found'}), 404
    
    try:
        # Get form data
        settings_data = {}
        
        # Basic company information
        basic_fields = [
            'company_name', 'company_legal_name', 'company_description',
            'company_website', 'company_industry', 'company_size'
        ]
        
        for field in basic_fields:
            if field in request.form:
                settings_data[field] = request.form[field]
        
        # Color settings
        color_fields = [
            'primary_color', 'secondary_color', 'accent_color', 'success_color',
            'warning_color', 'danger_color', 'info_color', 'neutral_color',
            'background_color', 'surface_color', 'text_primary_color',
            'text_secondary_color', 'border_color'
        ]
        
        for field in color_fields:
            if field in request.form:
                settings_data[field] = request.form[field]
        
        # Typography settings
        typography_fields = [
            'font_family_primary', 'font_family_secondary', 'font_family_monospace',
            'font_size_base', 'font_weight_normal', 'font_weight_medium',
            'font_weight_bold', 'line_height_base', 'letter_spacing'
        ]
        
        for field in typography_fields:
            if field in request.form:
                settings_data[field] = request.form[field]
        
        # Layout settings
        layout_fields = [
            'border_radius_sm', 'border_radius_md', 'border_radius_lg',
            'spacing_unit', 'container_max_width'
        ]
        
        for field in layout_fields:
            if field in request.form:
                settings_data[field] = request.form[field]
        
        # Boolean settings — map white_label_active to enable_white_label
        if 'white_label_active' in request.form or 'enable_white_label' in request.form:
            settings_data['enable_white_label'] = (
                'white_label_active' in request.form or 'enable_white_label' in request.form
            )
        if 'hide_platform_branding' in request.form:
            settings_data['hide_platform_branding'] = True
        elif request.form.get('_hide_platform_branding_present'):
            settings_data['hide_platform_branding'] = False
        
        boolean_fields = ['dark_mode_enabled', 'theme_switcher_enabled', 'animations_enabled']
        for field in boolean_fields:
            if field in request.form:
                settings_data[field] = True
        
        # Custom code
        if 'custom_css' in request.form:
            settings_data['custom_css'] = request.form['custom_css']
        
        if 'custom_js' in request.form:
            settings_data['custom_js'] = request.form['custom_js']
        
        # Handle file uploads
        file_fields = [
            'company_logo_primary', 'company_logo_secondary',
            'company_logo_white', 'company_logo_dark', 'company_favicon'
        ]
        
        for field in file_fields:
            if field in request.files:
                file = request.files[field]
                if file and file.filename:
                    # Validate file
                    is_valid, message = settings_manager.validate_brand_asset(file)
                    if not is_valid:
                        return jsonify({'success': False, 'message': f'{field}: {message}'}), 400
                    
                    # Capture size before processing consumes the file stream.
                    try:
                        file.seek(0, 2)
                        _file_size = file.tell()
                        file.seek(0)
                    except Exception:
                        _file_size = 0

                    # Process file
                    try:
                        processed_assets = settings_manager.process_brand_asset(file, field, company['id'])
                        settings_data[field] = processed_assets.get('original', '')
                        
                        # Save to brand assets table
                        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                        
                        # Deactivate old primary asset
                        cur.execute("""
                            UPDATE company_brand_assets 
                            SET is_primary = FALSE 
                            WHERE company_id = %s AND asset_type = %s
                        """, (company['id'], field))
                        
                        # Insert new asset
                        cur.execute("""
                            INSERT INTO company_brand_assets 
                            (company_id, asset_type, asset_name, file_path, file_size, file_type, is_primary)
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                        """, (
                            company['id'], field, file.filename,
                            processed_assets.get('original', ''),
                            _file_size, file.content_type
                        ))
                        
                        current_app.mysql.connection.commit()
                        cur.close()

                    except Exception as e:
                        current_app.logger.error(f"Failed to process {field}: {e}")
                        return jsonify({'success': False, 'message': f'Failed to process {field}'}), 500
        
        # Update settings
        success = settings_manager.update_settings(company['id'], settings_data)
        
        if success:
            return jsonify({'success': True, 'message': 'Settings updated successfully'})
        else:
            return jsonify({'success': False, 'message': 'Failed to update settings'}), 500
    
    except Exception as e:
        current_app.logger.error(f"Failed to update company settings: {e}")
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

@enterprise_settings_bp.route('/apply-theme/<int:template_id>', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def apply_theme_template(template_id):
    """Apply a theme template"""
    company = get_company_context()
    if not company:
        return jsonify({'success': False, 'message': 'Company not found'}), 404
    
    success = settings_manager.apply_theme_template(company['id'], template_id)
    
    if success:
        return jsonify({'success': True, 'message': 'Theme applied successfully'})
    else:
        return jsonify({'success': False, 'message': 'Failed to apply theme'}), 500

@enterprise_settings_bp.route('/preview-theme', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def preview_theme():
    """Preview theme changes without saving"""
    try:
        theme_data = request.get_json()
        
        # Generate CSS variables for preview
        css_variables = f"""
        :root {{
            --primary-color: {theme_data.get('primary_color', '#667eea')};
            --secondary-color: {theme_data.get('secondary_color', '#764ba2')};
            --accent-color: {theme_data.get('accent_color', '#28a745')};
            --background-color: {theme_data.get('background_color', '#f8fafc')};
            --text-primary-color: {theme_data.get('text_primary_color', '#1f2937')};
            --font-family-primary: {theme_data.get('font_family_primary', 'Inter, sans-serif')};
            --border-radius-md: {theme_data.get('border_radius_md', '8px')};
        }}
        """
        
        return jsonify({'success': True, 'css': css_variables})
        
    except Exception as e:
        current_app.logger.error(f"Failed to preview theme: {e}")
        return jsonify({'success': False, 'message': 'Preview failed'}), 500

@enterprise_settings_bp.route('/export-settings')
@require_company_role('company_admin', 'hr_manager')
def export_settings():
    """Export company settings as JSON"""
    company = get_company_context()
    if not company:
        return jsonify({'error': 'Company not found'}), 404
    
    settings = settings_manager.get_company_settings(company['id'])
    export_data = {
        'company_id': company['id'],
        'company_slug': company.get('company_slug'),
        'company_name': company.get('company_name'),
        'exported_at': datetime.now().isoformat(),
        'settings': {k: v for k, v in settings.items() if k not in ('brand_assets', 'custom_code')},
    }
    return jsonify(export_data)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook management for HR / company admins
#
# Webhooks were previously API-only (enterprise_api), but the buyer is the HR
# admin sitting in the dashboard. These routes expose the company's own
# ``company_webhooks`` subscriptions with create / toggle / delete / send-test.
# Every query is scoped to the session company's id (no FKs in this schema).
# ─────────────────────────────────────────────────────────────────────────────

# The integration events a webhook may subscribe to. Kept in sync with the
# event names emitted by enterprise_api / event_bus. Each tuple is
# (event_name, Danish description) for the UI.
WEBHOOK_EVENT_CHOICES = [
    ('order.created', 'Ordre oprettet'),
    ('order.updated', 'Ordre opdateret'),
    ('employee.added', 'Medarbejder tilføjet'),
    ('employee.updated', 'Medarbejder opdateret'),
    ('course.completed', 'Kursus gennemført'),
    ('ping', 'Test / ping'),
]
_VALID_WEBHOOK_EVENTS = {name for name, _ in WEBHOOK_EVENT_CHOICES}


def _is_valid_webhook_url(url):
    """Validate a webhook URL: must be a non-empty http(s) URL.

    Prefers enterprise_api's SSRF guard (rejects private/loopback hosts) when
    importable; otherwise falls back to a basic scheme check so the form still
    rejects obviously bad input. Never raises into the caller.
    """
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if len(url) > 500:
        return False
    # Prefer the hardened SSRF guard from enterprise_api when available.
    try:
        from enterprise_api import _is_safe_webhook_url
        return bool(_is_safe_webhook_url(url))
    except Exception:
        pass
    # Fallback: scheme-only check (reject non-http(s)).
    lowered = url.lower()
    return lowered.startswith('http://') or lowered.startswith('https://')


def _parse_webhook_events(raw):
    """Normalise a company_webhooks.events JSON value into a list of names."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(e) for e in raw]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(e) for e in parsed]
            if isinstance(parsed, str):
                return [parsed]
        except Exception:
            # Legacy comma-separated storage.
            return [e.strip() for e in raw.split(',') if e.strip()]
    return []


def _load_company_webhooks(company_id):
    """Return the session company's webhook subscriptions (scoped by id)."""
    rows = []
    try:
        conn = current_app.mysql.connection
        if conn is None:
            return rows
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """SELECT id, name, url, events, is_active, total_deliveries,
                      successful_deliveries, failed_deliveries,
                      last_delivery_at, created_at
               FROM company_webhooks
               WHERE company_id = %s
               ORDER BY created_at DESC, id DESC""",
            (company_id,),
        )
        for r in cur.fetchall():
            r['events_list'] = _parse_webhook_events(r.get('events'))
            rows.append(r)
        cur.close()
    except Exception as e:
        current_app.logger.error(f"Failed to load company webhooks: {e}")
    return rows


@enterprise_settings_bp.route('/webhooks')
@require_company_role('company_admin', 'hr_manager')
def webhooks_page():
    """List the session company's webhook subscriptions with management forms."""
    company = get_company_context()
    if not company:
        flash('Virksomhed ikke fundet.', 'danger')
        return redirect(url_for('dashboard.dashboard'))

    webhooks = _load_company_webhooks(company['id'])
    return render_template(
        'fm/webhooks.html',
        company_id=company['id'],
        company=company,
        webhooks=webhooks,
        event_choices=WEBHOOK_EVENT_CHOICES,
    )


@enterprise_settings_bp.route('/webhooks/create', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def webhooks_create():
    """Create a new webhook subscription for the session company."""
    company = get_company_context()
    if not company:
        flash('Virksomhed ikke fundet.', 'danger')
        return redirect(url_for('dashboard.dashboard'))

    company_id = company['id']
    name = (request.form.get('name') or '').strip()[:100]
    url = (request.form.get('url') or '').strip()
    is_active = 1 if request.form.get('is_active') else 0
    # Only accept events from the known catalog.
    selected = [e for e in request.form.getlist('events') if e in _VALID_WEBHOOK_EVENTS]

    if not _is_valid_webhook_url(url):
        flash('Ugyldig URL. Brug en offentlig http(s)-adresse.', 'danger')
        return redirect(url_for('enterprise_settings.webhooks_page'))

    if not selected:
        flash('Vælg mindst én hændelse, webhooken skal lytte på.', 'danger')
        return redirect(url_for('enterprise_settings.webhooks_page'))

    try:
        conn = current_app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """INSERT INTO company_webhooks
                   (company_id, name, url, events, is_active, created_by)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                company_id,
                name or url[:100],
                url[:500],
                json.dumps(selected),
                is_active,
                session.get('user_id'),
            ),
        )
        conn.commit()
        cur.close()
        flash('Webhook oprettet.', 'success')
    except Exception as e:
        current_app.logger.error(f"Failed to create webhook: {e}")
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        flash('Kunne ikke oprette webhook. Prøv igen.', 'danger')

    return redirect(url_for('enterprise_settings.webhooks_page'))


@enterprise_settings_bp.route('/webhooks/<int:webhook_id>/toggle', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def webhooks_toggle(webhook_id):
    """Activate / deactivate a webhook (scoped to the session company)."""
    company = get_company_context()
    if not company:
        flash('Virksomhed ikke fundet.', 'danger')
        return redirect(url_for('dashboard.dashboard'))

    company_id = company['id']
    try:
        conn = current_app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        # Flip is_active only for a row owned by this company.
        cur.execute(
            """UPDATE company_webhooks
               SET is_active = 1 - COALESCE(is_active, 0)
               WHERE id = %s AND company_id = %s""",
            (webhook_id, company_id),
        )
        affected = cur.rowcount
        conn.commit()
        cur.close()
        if affected:
            flash('Webhook-status opdateret.', 'success')
        else:
            flash('Webhook ikke fundet.', 'danger')
    except Exception as e:
        current_app.logger.error(f"Failed to toggle webhook {webhook_id}: {e}")
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        flash('Kunne ikke opdatere webhook.', 'danger')

    return redirect(url_for('enterprise_settings.webhooks_page'))


@enterprise_settings_bp.route('/webhooks/<int:webhook_id>/delete', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def webhooks_delete(webhook_id):
    """Delete a webhook subscription (scoped to the session company)."""
    company = get_company_context()
    if not company:
        flash('Virksomhed ikke fundet.', 'danger')
        return redirect(url_for('dashboard.dashboard'))

    company_id = company['id']
    try:
        conn = current_app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "DELETE FROM company_webhooks WHERE id = %s AND company_id = %s",
            (webhook_id, company_id),
        )
        affected = cur.rowcount
        conn.commit()
        cur.close()
        if affected:
            flash('Webhook slettet.', 'success')
        else:
            flash('Webhook ikke fundet.', 'danger')
    except Exception as e:
        current_app.logger.error(f"Failed to delete webhook {webhook_id}: {e}")
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        flash('Kunne ikke slette webhook.', 'danger')

    return redirect(url_for('enterprise_settings.webhooks_page'))


@enterprise_settings_bp.route('/webhooks/<int:webhook_id>/test', methods=['POST'])
@require_company_role('company_admin', 'hr_manager')
def webhooks_test(webhook_id):
    """Send a test 'ping' event for the session company via the event bus.

    The event bus fans the event out to all of the company's active webhook
    subscriptions, so this verifies the buyer's endpoints end-to-end.
    """
    company = get_company_context()
    if not company:
        flash('Virksomhed ikke fundet.', 'danger')
        return redirect(url_for('dashboard.dashboard'))

    company_id = company['id']

    # Confirm the webhook belongs to this company before emitting (scoping).
    target = None
    try:
        conn = current_app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT id, url FROM company_webhooks WHERE id = %s AND company_id = %s",
            (webhook_id, company_id),
        )
        target = cur.fetchone()
        cur.close()
    except Exception as e:
        current_app.logger.error(f"Failed to look up webhook {webhook_id}: {e}")

    if not target:
        flash('Webhook ikke fundet.', 'danger')
        return redirect(url_for('enterprise_settings.webhooks_page'))

    # Guarded import: never crash the page if event_bus is unavailable.
    try:
        from event_bus import emit_event
    except Exception as e:
        current_app.logger.error(f"event_bus unavailable for test ping: {e}")
        flash('Test kunne ikke sendes (event-bus utilgængelig).', 'danger')
        return redirect(url_for('enterprise_settings.webhooks_page'))

    try:
        emit_event(company_id, 'ping', {
            'message': 'Test-hændelse fra webhook-administrationen',
            'webhook_id': webhook_id,
            'company_id': company_id,
            'triggered_by': session.get('user_id'),
            'sent_at': datetime.now().isoformat(),
        })
        flash('Test-hændelse sendt. Den leveres til dine aktive webhooks.', 'success')
    except Exception as e:
        current_app.logger.error(f"Failed to emit test ping: {e}")
        flash('Test kunne ikke sendes. Prøv igen.', 'danger')

    return redirect(url_for('enterprise_settings.webhooks_page'))
