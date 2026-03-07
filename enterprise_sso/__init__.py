# enterprise_sso/__init__.py
"""
Enterprise Single Sign-On (SSO) Blueprint
Supports SAML, OAuth2, LDAP, and Active Directory
"""

from flask import Blueprint, request, session, redirect, url_for, flash, render_template, current_app
import MySQLdb.cursors
import json
import jwt
import requests
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import base64
import hashlib
import secrets

sso_bp = Blueprint('sso', __name__)

class SSOManager:
    """Enterprise SSO Manager supporting multiple providers"""
    
    def __init__(self):
        self.providers = {
            'saml': SAMLProvider(),
            'oauth2': OAuth2Provider(),
            'ldap': LDAPProvider(),
            'active_directory': ActiveDirectoryProvider()
        }
    
    def get_provider(self, provider_type):
        return self.providers.get(provider_type)
    
    def authenticate_user(self, company_id, provider_type, auth_data):
        """Authenticate user through SSO provider"""
        provider = self.get_provider(provider_type)
        if not provider:
            return None, "Unsupported SSO provider"
        
        # Get company SSO configuration
        sso_config = self.get_company_sso_config(company_id, provider_type)
        if not sso_config or not sso_config.get('is_enabled'):
            return None, "SSO not configured for this company"
        
        # Authenticate with provider
        user_info = provider.authenticate(auth_data, sso_config['config'])
        if not user_info:
            return None, "Authentication failed"
        
        # Auto-provision user if enabled
        if sso_config.get('auto_provision_users', True):
            user = self.provision_user(company_id, user_info, sso_config)
            return user, None
        
        # Find existing user
        user = self.find_user_by_email(company_id, user_info['email'])
        if not user:
            return None, "User not found and auto-provisioning is disabled"
        
        return user, None
    
    def get_company_sso_config(self, company_id, provider_type):
        """Get SSO configuration for company"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return None

            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT * FROM company_sso_configs
                WHERE company_id = %s AND provider = %s AND is_enabled = 1
            """, (company_id, provider_type))
            
            config = cur.fetchone()
            cur.close()
            return config
        except Exception as e:
            return None
    
    def provision_user(self, company_id, user_info, sso_config):
        """Auto-provision user from SSO"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return None

            cur = conn.cursor(MySQLdb.cursors.DictCursor)

            # Check if user already exists
            cur.execute("""
                SELECT * FROM company_users 
                WHERE company_id = %s AND email = %s
            """, (company_id, user_info['email']))
            
            existing_user = cur.fetchone()
            if existing_user:
                cur.close()
                return existing_user
            
            # Create new user
            cur.execute("""
                INSERT INTO company_users (
                    company_id, full_name, email, role, status,
                    job_title, department, added_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                company_id,
                user_info.get('full_name', ''),
                user_info['email'],
                sso_config.get('default_role', 'employee'),
                'active',
                user_info.get('job_title', ''),
                user_info.get('department', ''),
                datetime.now()
            ))
            
            user_id = cur.lastrowid
            conn.commit()
            
            # Get the created user
            cur.execute("""
                SELECT * FROM company_users WHERE id = %s
            """, (user_id,))
            
            user = cur.fetchone()
            cur.close()
            
            # Log the auto-provisioning
            self.log_audit_event(company_id, None, 'user.auto_provisioned', 
                               'user', str(user_id), f"Auto-provisioned user {user_info['email']}")
            
            return user
        except Exception as e:
            return None
    
    def find_user_by_email(self, company_id, email):
        """Find existing user by email"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return None

            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT * FROM company_users
                WHERE company_id = %s AND email = %s AND status = 'active'
            """, (company_id, email))
            
            user = cur.fetchone()
            cur.close()
            return user
        except Exception as e:
            return None
    
    def log_audit_event(self, company_id, user_id, action, resource_type, resource_id, description):
        """Log audit event"""
        try:
            conn = current_app.mysql.connection
            if not conn:
                return

            cur = conn.cursor()
            cur.execute("""
                INSERT INTO audit_log (
                    company_id, user_id, action, resource_type, resource_id,
                    description, ip_address, user_agent, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                company_id, user_id, action, resource_type, resource_id,
                description, request.remote_addr, request.user_agent.string,
                datetime.now()
            ))
            conn.commit()
            cur.close()
        except Exception as e:
            pass

class SAMLProvider:
    """SAML 2.0 SSO Provider"""
    
    def authenticate(self, saml_response, config):
        """Authenticate SAML response"""
        try:
            # Decode SAML response
            decoded_response = base64.b64decode(saml_response)
            root = ET.fromstring(decoded_response)
            
            # Extract user attributes
            user_info = self.extract_saml_attributes(root, config)
            return user_info
        except Exception as e:
            return None
    
    def extract_saml_attributes(self, saml_root, config):
        """Extract user attributes from SAML response"""
        # This is a simplified implementation
        # In production, you'd use a proper SAML library like python3-saml
        user_info = {}
        
        # Extract email
        email_element = saml_root.find('.//saml:Attribute[@Name="email"]', 
                                     {'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'})
        if email_element is not None:
            user_info['email'] = email_element.find('.//saml:AttributeValue', 
                                                  {'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'}).text
        
        # Extract other attributes based on configuration
        attribute_mapping = config.get('attribute_mapping', {})
        for saml_attr, user_field in attribute_mapping.items():
            attr_element = saml_root.find(f'.//saml:Attribute[@Name="{saml_attr}"]',
                                        {'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'})
            if attr_element is not None:
                value_element = attr_element.find('.//saml:AttributeValue',
                                                {'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'})
                if value_element is not None:
                    user_info[user_field] = value_element.text
        
        return user_info

class OAuth2Provider:
    """OAuth 2.0 / OpenID Connect Provider"""
    
    def authenticate(self, auth_code, config):
        """Authenticate OAuth2 authorization code"""
        try:
            # Exchange code for token
            token_response = self.exchange_code_for_token(auth_code, config)
            if not token_response:
                return None
            
            # Get user info
            user_info = self.get_user_info(token_response['access_token'], config)
            return user_info
        except Exception as e:
            return None
    
    def exchange_code_for_token(self, auth_code, config):
        """Exchange authorization code for access token"""
        token_url = config.get('token_url')
        client_id = config.get('client_id')
        client_secret = config.get('client_secret')
        redirect_uri = config.get('redirect_uri')
        
        data = {
            'grant_type': 'authorization_code',
            'code': auth_code,
            'redirect_uri': redirect_uri,
            'client_id': client_id,
            'client_secret': client_secret
        }
        
        response = requests.post(token_url, data=data)
        if response.status_code == 200:
            return response.json()
        return None
    
    def get_user_info(self, access_token, config):
        """Get user information using access token"""
        userinfo_url = config.get('userinfo_url')
        headers = {'Authorization': f'Bearer {access_token}'}
        
        response = requests.get(userinfo_url, headers=headers)
        if response.status_code == 200:
            return response.json()
        return None

class LDAPProvider:
    """LDAP Authentication Provider"""
    
    def authenticate(self, credentials, config):
        """Authenticate against LDAP"""
        try:
            import ldap3
            
            server = ldap3.Server(config.get('server_url'))
            conn = ldap3.Connection(
                server,
                user=credentials.get('username'),
                password=credentials.get('password'),
                auto_bind=True
            )
            
            if conn.bind():
                # Search for user attributes
                search_base = config.get('search_base')
                search_filter = config.get('search_filter', '(uid={username})').format(
                    username=credentials.get('username')
                )
                
                conn.search(search_base, search_filter, attributes=['*'])
                if conn.entries:
                    entry = conn.entries[0]
                    user_info = self.extract_ldap_attributes(entry, config)
                    return user_info
            
            return None
        except Exception as e:
            return None
    
    def extract_ldap_attributes(self, ldap_entry, config):
        """Extract user attributes from LDAP entry"""
        user_info = {}
        attribute_mapping = config.get('attribute_mapping', {})
        
        for ldap_attr, user_field in attribute_mapping.items():
            if hasattr(ldap_entry, ldap_attr):
                user_info[user_field] = str(getattr(ldap_entry, ldap_attr))
        
        return user_info

class ActiveDirectoryProvider:
    """Active Directory Authentication Provider"""
    
    def authenticate(self, credentials, config):
        """Authenticate against Active Directory"""
        # This would use the same LDAP implementation
        # but with AD-specific configuration
        ldap_provider = LDAPProvider()
        return ldap_provider.authenticate(credentials, config)

# Initialize SSO Manager
sso_manager = SSOManager()

@sso_bp.route('/sso/login/<company_slug>/<provider>')
def sso_login(company_slug, provider):
    """Initiate SSO login"""
    # Get company by slug
    company = get_company_by_slug(company_slug)
    if not company:
        flash('Company not found', 'error')
        return redirect(url_for('auth.login'))
    
    # Get SSO configuration
    sso_config = sso_manager.get_company_sso_config(company['id'], provider)
    if not sso_config:
        flash('SSO not configured for this company', 'error')
        return redirect(url_for('auth.login'))
    
    # Generate SSO request based on provider
    if provider == 'saml':
        return redirect(generate_saml_request(sso_config))
    elif provider == 'oauth2':
        return redirect(generate_oauth2_request(sso_config))
    else:
        return render_template('sso_login.html', 
                             company=company, 
                             provider=provider,
                             config=sso_config)

@sso_bp.route('/sso/callback/<company_slug>/<provider>', methods=['GET', 'POST'])
def sso_callback(company_slug, provider):
    """Handle SSO callback"""
    company = get_company_by_slug(company_slug)
    if not company:
        flash('Company not found', 'error')
        return redirect(url_for('auth.login'))
    
    # Extract authentication data based on provider
    if provider == 'saml':
        auth_data = request.form.get('SAMLResponse')
    elif provider == 'oauth2':
        auth_data = request.args.get('code')
    else:
        auth_data = {
            'username': request.form.get('username'),
            'password': request.form.get('password')
        }
    
    # Authenticate user
    user, error = sso_manager.authenticate_user(company['id'], provider, auth_data)
    if error:
        flash(error, 'error')
        return redirect(url_for('auth.login'))
    
    # Set session
    session['user'] = user['email']
    session['user_id'] = user['id']
    session['company_id'] = company['id']
    session['company_name'] = company['company_name']
    session['company_role'] = user['role']
    
    # Log successful login
    sso_manager.log_audit_event(
        company['id'], user['id'], 'user.sso_login',
        'authentication', str(user['id']),
        f"SSO login via {provider}"
    )
    
    return redirect(url_for('dashboard.dashboard'))

def get_company_by_slug(slug):
    """Get company by slug"""
    try:
        conn = current_app.mysql.connection
        if not conn:
            return None

        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM companies WHERE company_slug = %s", (slug,))
        company = cur.fetchone()
        cur.close()
        return company
    except Exception as e:
        return None

def generate_saml_request(sso_config):
    """Generate SAML authentication request"""
    # This is a simplified implementation
    # In production, use a proper SAML library
    saml_request = f"""
    <samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
                        ID="{secrets.token_hex(16)}"
                        Version="2.0"
                        IssueInstant="{datetime.utcnow().isoformat()}Z"
                        Destination="{sso_config['config']['sso_url']}"
                        AssertionConsumerServiceURL="{sso_config['config']['acs_url']}">
        <saml:Issuer xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
            {sso_config['config']['issuer']}
        </saml:Issuer>
    </samlp:AuthnRequest>
    """
    
    encoded_request = base64.b64encode(saml_request.encode()).decode()
    return f"{sso_config['config']['sso_url']}?SAMLRequest={encoded_request}"

def generate_oauth2_request(sso_config):
    """Generate OAuth2 authentication request"""
    auth_url = sso_config['config']['authorization_url']
    client_id = sso_config['config']['client_id']
    redirect_uri = sso_config['config']['redirect_uri']
    scope = sso_config['config'].get('scope', 'openid email profile')
    state = secrets.token_hex(16)
    
    # Store state in session for validation
    session['oauth2_state'] = state
    
    return f"{auth_url}?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}&scope={scope}&state={state}"

@sso_bp.route('/admin/sso/config/<int:company_id>')
def sso_config(company_id):
    """SSO configuration page for company admins"""
    # Check if user is company admin
    if session.get('company_role') != 'company_admin':
        flash('Access denied', 'error')
        return redirect(url_for('dashboard.dashboard'))
    
    # Get existing SSO configurations
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("""
            SELECT * FROM company_sso_configs
            WHERE company_id = %s
        """, (company_id,))
        
        sso_configs = cur.fetchall()
        cur.close()
        
        return render_template('sso_config.html', 
                             company_id=company_id,
                             sso_configs=sso_configs)
    except Exception as e:
        flash('Error loading SSO configuration', 'error')
        return redirect(url_for('dashboard.dashboard'))

@sso_bp.route('/admin/sso/config/<int:company_id>', methods=['POST'])
def save_sso_config(company_id):
    """Save SSO configuration"""
    if session.get('company_role') != 'company_admin':
        flash('Access denied', 'error')
        return redirect(url_for('dashboard.dashboard'))
    
    provider = request.form.get('provider')
    provider_name = request.form.get('provider_name')
    is_enabled = request.form.get('is_enabled') == 'on'
    config_data = {
        'sso_url': request.form.get('sso_url'),
        'issuer': request.form.get('issuer'),
        'certificate': request.form.get('certificate'),
        'attribute_mapping': {
            'email': 'email',
            'firstName': 'first_name',
            'lastName': 'last_name',
            'department': 'department',
            'jobTitle': 'job_title'
        }
    }
    
    try:
        cur = current_app.mysql.connection.cursor()

        # Insert or update SSO configuration
        cur.execute("""
            INSERT INTO company_sso_configs (
                company_id, provider, provider_name, config, is_enabled,
                auto_provision_users, default_role, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                provider_name = VALUES(provider_name),
                config = VALUES(config),
                is_enabled = VALUES(is_enabled),
                updated_at = CURRENT_TIMESTAMP
        """, (
            company_id, provider, provider_name, json.dumps(config_data),
            is_enabled, True, 'employee', datetime.now()
        ))
        
        current_app.mysql.connection.commit()
        cur.close()

        flash('SSO configuration saved successfully', 'success')
    except Exception as e:
        flash('Error saving SSO configuration', 'error')
    
    return redirect(url_for('sso.sso_config', company_id=company_id))
