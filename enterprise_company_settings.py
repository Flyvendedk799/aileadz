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
        if ENTERPRISE_CONFIG['CDN_ENABLED']:
            self.s3_client = boto3.client('s3')

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
        
        # Check MIME type using python-magic
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
            
            if file_ext == 'svg':
                # Handle SVG files (no resizing needed)
                file_path = os.path.join(upload_dir, unique_filename)
                file.save(file_path)
                processed_assets['original'] = f"/static/brand_assets/{company_id}/{unique_filename}"
            else:
                # Process raster images
                image = Image.open(file)
                
                # Convert to RGB if necessary
                if image.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', image.size, (255, 255, 255))
                    if image.mode == 'P':
                        image = image.convert('RGBA')
                    background.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
                    image = background
                
                # Generate multiple sizes if configured
                sizes = ENTERPRISE_CONFIG['BRAND_ASSET_SIZES'].get(asset_type, [(image.width, image.height)])
                
                for width, height in sizes:
                    # Resize maintaining aspect ratio
                    resized_image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
                    
                    # Save optimized version
                    size_filename = f"{company_id}_{asset_type}_{width}x{height}_{uuid.uuid4().hex}.{file_ext}"
                    size_path = os.path.join(upload_dir, size_filename)
                    
                    # Optimize based on format
                    if file_ext in ['jpg', 'jpeg']:
                        resized_image.save(size_path, 'JPEG', quality=85, optimize=True)
                    elif file_ext == 'png':
                        resized_image.save(size_path, 'PNG', optimize=True)
                    elif file_ext == 'webp':
                        resized_image.save(size_path, 'WEBP', quality=85, optimize=True)
                    
                    processed_assets[f"{width}x{height}"] = f"/static/brand_assets/{company_id}/{size_filename}"
                
                # Save original
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
                'company_name': company_name,
                'company_legal_name': company_name,
                'company_description': '',
                'company_website': '',
                'company_industry': '',
                'company_size': 'medium',
                'primary_color': '#667eea',
                'secondary_color': '#764ba2',
                'accent_color': '#28a745',
                'success_color': '#48bb78',
                'warning_color': '#f6ad55',
                'danger_color': '#fc8181',
                'info_color': '#4299e1',
                'neutral_color': '#6b7280',
                'background_color': '#f8fafc',
                'surface_color': '#ffffff',
                'text_primary_color': '#1f2937',
                'text_secondary_color': '#6b7280',
                'border_color': '#e5e7eb',
                'font_family_primary': 'Inter, system-ui, sans-serif',
                'font_family_secondary': 'Inter, system-ui, sans-serif',
                'font_family_monospace': 'JetBrains Mono, monospace',
                'font_size_base': '16px',
                'font_weight_normal': '400',
                'font_weight_medium': '500',
                'font_weight_bold': '700',
                'line_height_base': '1.5',
                'letter_spacing': '0',
                'border_radius_sm': '4px',
                'border_radius_md': '8px',
                'border_radius_lg': '12px',
                'spacing_unit': '8px',
                'container_max_width': '1200px',
                'white_label_active': False,
                'default_language': 'en',
                'supported_languages': json.dumps(['en']),
                'dark_mode_enabled': True,
                'theme_switcher_enabled': True,
                'animations_enabled': True,
                'created_by': session.get('user_id')
            }
            
            # Insert default settings
            columns = ', '.join(default_settings.keys())
            placeholders = ', '.join(['%s'] * len(default_settings))
            
            cur.execute(f"""
                INSERT INTO company_settings ({columns})
                VALUES ({placeholders})
            """, list(default_settings.values()))
            
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
                update_values.extend([company_id, session.get('user_id')])
                
                cur.execute(f"""
                    UPDATE company_settings 
                    SET {', '.join(update_fields)}, updated_at = NOW(), updated_by = %s
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
                'text_primary_color': template['text_color'],
                'font_family_primary': template['font_family'],
                'font_size_base': template['font_size_base'],
                'border_radius_md': template['border_radius'],
                'spacing_unit': template['spacing_unit']
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
            
            # Allow platform admins and company admins/HR managers
            if user_type == 'platform_admin':
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
@require_enterprise_access()
def company_settings():
    """Enterprise company settings dashboard"""
    company = get_company_context()
    if not company:
        flash("Company information not found.", "danger")
        return redirect(url_for('dashboard.dashboard'))
    
    # Get current settings
    settings = settings_manager.get_company_settings(company['id'])
    
    # Get theme templates
    theme_templates = settings_manager.get_theme_templates()
    
    # Get settings history (last 50 changes)
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT csh.*, u.username as changed_by_username
            FROM company_settings_history csh
            LEFT JOIN users u ON csh.changed_by = u.id
            WHERE csh.company_id = %s
            ORDER BY csh.created_at DESC
            LIMIT 50
        """, (company['id'],))
        settings_history = cur.fetchall()
        cur.close()
    except Exception as e:
        current_app.logger.error(f"Failed to get settings history: {e}")
        settings_history = []
    
    return render_template('hr_dashboard/enterprise_company_settings.html',
                         company=company,
                         settings=settings,
                         theme_templates=theme_templates,
                         settings_history=settings_history)

@enterprise_settings_bp.route('/company-settings', methods=['POST'])
@require_enterprise_access()
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
        
        # Boolean settings
        boolean_fields = [
            'white_label_active', 'dark_mode_enabled', 'theme_switcher_enabled',
            'animations_enabled', 'sound_effects_enabled'
        ]
        
        for field in boolean_fields:
            settings_data[field] = field in request.form
        
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
                            len(file.read()), file.content_type
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
@require_enterprise_access()
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
@require_enterprise_access()
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
@require_enterprise_access()
def export_settings():
    """Export company settings as JSON"""
    company = get_company_context()
    if not company:
        return jsonify({'error': 'Company not found'}), 404
    
    settings = settings_manager.get_company
