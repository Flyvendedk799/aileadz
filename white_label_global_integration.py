"""
Global White-Label Integration System
Provides company branding context to all templates across the application
"""

from flask import session, current_app
import MySQLdb.cursors

def get_company_branding_context():
    """Get company branding context for white-label functionality"""
    if 'user' not in session:
        return {}
    
    # Only apply white-label for company users
    if session.get('user_type') != 'company_user':
        return {}
    
    company_id = session.get('company_id')
    if not company_id:
        return {}
    
    conn = current_app.mysql.connection
    if not conn:
        return {}

    try:
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        
        # Get company information from the correct tables
        cur.execute("""
            SELECT c.*, 
                   c.company_name,
                   c.company_logo,
                   c.logo_url,
                   c.company_tagline,
                   COALESCE(c.primary_color, c.brand_primary_color) as primary_color,
                   COALESCE(c.secondary_color, c.brand_secondary_color) as secondary_color,
                   c.accent_color,
                   c.font_family,
                   cs.company_display_name, 
                   cs.company_description, 
                   cs.company_website, 
                   cs.support_email, 
                   cs.support_phone
            FROM companies c
            LEFT JOIN company_settings cs ON c.id = cs.company_id
            WHERE c.id = %s
        """, (company_id,))
        
        company_data = cur.fetchone()
        cur.close()
        
        if not company_data:
            return {}
        
        # Always return branding context for company users
        return {
            'white_label_active': True,
            'company_branding': {
                'company_name': company_data.get('company_display_name') or company_data.get('company_name', 'Your Company'),
                'company_logo': company_data.get('company_logo') or company_data.get('logo_url'),
                'logo_url': company_data.get('company_logo') or company_data.get('logo_url'),
                'primary_color': company_data.get('primary_color', '#667eea'),
                'secondary_color': company_data.get('secondary_color', '#764ba2'),
                'accent_color': company_data.get('accent_color', '#28a745'),
                'font_family': company_data.get('font_family', 'Inter, sans-serif'),
                'tagline': company_data.get('company_tagline', ''),
                'website': company_data.get('company_website', ''),
                'support_email': company_data.get('support_email', ''),
                'support_phone': company_data.get('support_phone', ''),
                'description': company_data.get('company_description', '')
            }
        }
        
    except Exception as e:
        current_app.logger.error(f"Error getting company branding context: {e}")
        return {}

def register_white_label_context_processor(app):
    """Register the white-label context processor with the Flask app"""
    
    @app.context_processor
    def inject_white_label_context():
        """Inject white-label branding context into all templates"""
        return get_company_branding_context()
