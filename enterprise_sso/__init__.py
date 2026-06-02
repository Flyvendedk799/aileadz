# enterprise_sso/__init__.py
"""
Enterprise Single Sign-On (SSO) Blueprint
Supports SAML, OAuth2, LDAP, and Active Directory

Security hardening (additive, backward-compatible, boot-safe):
  * SAML XML is parsed with defusedxml (XXE-safe). If defusedxml is missing we
    FAIL CLOSED for SAML only, because an unsafe parse is itself the vuln.
  * SAML responses without a <ds:Signature> element are rejected (unsigned ==
    forge-a-login today). NOTE: this is a presence check only; full crypto
    verification needs python3-saml / signxml+xmlsec (see TODO below).
  * OAuth2 `state` is generated, stored in the session and VALIDATED on the
    callback (CSRF). A `nonce` is added to the OIDC request.
  * SSO client secrets are encrypted at rest with Fernet. Legacy plaintext
    secrets keep working and are re-encrypted on the next write (transitional).
All new crypto deps (defusedxml, cryptography) are imported guarded so a
missing dependency degrades with a warning instead of crashing create_app().
"""

from flask import Blueprint, request, session, redirect, url_for, flash, render_template, current_app
import MySQLdb.cursors
import json
import jwt
import requests
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET  # legacy import kept for compatibility; SAML now uses defusedxml
import base64
import hashlib
import secrets
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded crypto / XML-security imports.
# These MUST NOT crash create_app() if the wheel is missing on PythonAnywhere.
# ---------------------------------------------------------------------------

# defusedxml: XXE-safe XML parsing for attacker-controlled SAMLResponse payloads.
try:
    from defusedxml.ElementTree import fromstring as _safe_xml_fromstring
    _DEFUSEDXML_AVAILABLE = True
except Exception as _defused_err:  # pragma: no cover - depends on deploy env
    _safe_xml_fromstring = None
    _DEFUSEDXML_AVAILABLE = False
    logger.warning(
        "enterprise_sso: defusedxml unavailable (%s); SAML parsing will FAIL CLOSED "
        "until 'defusedxml' is installed.", _defused_err
    )

# cryptography / Fernet: encryption-at-rest for SSO client secrets.
try:
    from cryptography.fernet import Fernet, InvalidToken
    _FERNET_AVAILABLE = True
except Exception as _fernet_err:  # pragma: no cover - depends on deploy env
    Fernet = None
    InvalidToken = Exception
    _FERNET_AVAILABLE = False
    logger.warning(
        "enterprise_sso: cryptography/Fernet unavailable (%s); SSO secrets will be "
        "stored as plaintext until 'cryptography' is installed.", _fernet_err
    )

sso_bp = Blueprint('sso', __name__)

# Marker prefix so we can tell our Fernet ciphertext apart from legacy plaintext.
_ENC_PREFIX = 'fernet$'

# Config keys whose values are sensitive secrets and must be encrypted at rest.
_SECRET_CONFIG_KEYS = ('client_secret',)


def _get_fernet():
    """Return a Fernet instance or None.

    Key resolution order:
      1) SSO_FERNET_KEY env / app config (preferred, a real urlsafe-b64 32-byte key).
      2) Derived from the app SECRET_KEY (weaker, but keeps prod working without
         new env). We log a warning recommending SSO_FERNET_KEY be set.
    Never raises: a crypto failure degrades to plaintext rather than breaking SSO.
    """
    if not _FERNET_AVAILABLE:
        return None
    try:
        import os
        raw_key = os.environ.get('SSO_FERNET_KEY')
        if not raw_key:
            try:
                raw_key = current_app.config.get('SSO_FERNET_KEY')
            except Exception:
                raw_key = None
        if raw_key:
            if isinstance(raw_key, str):
                raw_key = raw_key.encode('utf-8')
            return Fernet(raw_key)

        # Fallback: derive a stable Fernet key from SECRET_KEY (documented weaker).
        secret_key = None
        try:
            secret_key = current_app.config.get('SECRET_KEY')
        except Exception:
            secret_key = None
        if not secret_key:
            secret_key = os.environ.get('SECRET_KEY')
        if not secret_key:
            logger.warning(
                "enterprise_sso: no SSO_FERNET_KEY and no SECRET_KEY available; "
                "SSO secrets cannot be encrypted and will be stored as plaintext."
            )
            return None
        if isinstance(secret_key, str):
            secret_key = secret_key.encode('utf-8')
        derived = base64.urlsafe_b64encode(hashlib.sha256(secret_key).digest())
        logger.warning(
            "enterprise_sso: SSO_FERNET_KEY is not set; deriving SSO secret "
            "encryption key from SECRET_KEY (weaker). Set SSO_FERNET_KEY in the "
            "environment for stronger key separation."
        )
        return Fernet(derived)
    except Exception as e:
        logger.warning("enterprise_sso: could not build Fernet key (%s); secrets stay plaintext.", e)
        return None


def _encrypt_secret_value(plaintext):
    """Encrypt a single secret string. Returns a marker-prefixed token, or the
    original value unchanged if encryption is unavailable (backward-compatible)."""
    if plaintext is None or plaintext == '':
        return plaintext
    if isinstance(plaintext, str) and plaintext.startswith(_ENC_PREFIX):
        # Already encrypted (idempotent write).
        return plaintext
    f = _get_fernet()
    if not f:
        return plaintext
    try:
        token = f.encrypt(plaintext.encode('utf-8')).decode('utf-8')
        return _ENC_PREFIX + token
    except Exception as e:
        logger.warning("enterprise_sso: secret encryption failed (%s); storing plaintext.", e)
        return plaintext


def _decrypt_secret_value(stored):
    """Decrypt a stored secret. TRANSITIONAL: if the value is legacy plaintext
    (no marker / not Fernet-decryptable) it is returned as-is so existing
    configs keep working; it will be re-encrypted on the next write."""
    if stored is None or stored == '':
        return stored
    if not isinstance(stored, str) or not stored.startswith(_ENC_PREFIX):
        # Legacy plaintext secret.
        return stored
    f = _get_fernet()
    if not f:
        # Encrypted at rest but we cannot decrypt right now: do not leak the token.
        logger.warning("enterprise_sso: encountered encrypted SSO secret but Fernet is unavailable.")
        return None
    token = stored[len(_ENC_PREFIX):]
    try:
        return f.decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        logger.warning("enterprise_sso: stored SSO secret failed Fernet decryption (key rotated?).")
        return None
    except Exception as e:
        logger.warning("enterprise_sso: SSO secret decryption error (%s).", e)
        return None


def _decrypt_config_secrets(config):
    """Return a copy of the config dict with sensitive keys decrypted for use."""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for key in _SECRET_CONFIG_KEYS:
        if key in out and out[key]:
            out[key] = _decrypt_secret_value(out[key])
    return out


def _encrypt_config_secrets(config):
    """Return a copy of the config dict with sensitive keys encrypted for storage."""
    if not isinstance(config, dict):
        return config
    out = dict(config)
    for key in _SECRET_CONFIG_KEYS:
        if key in out and out[key]:
            out[key] = _encrypt_secret_value(out[key])
    return out


def _normalize_config(raw):
    """The `config` column is stored as a JSON string. Parse it to a dict and
    decrypt secrets for in-process use. Tolerates dicts (already parsed) and
    bad/empty JSON without raising."""
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, (bytes, bytearray)):
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except Exception:
            return {}
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
    else:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _decrypt_config_secrets(parsed)

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
            # Normalize the JSON `config` column to a dict and decrypt any
            # secrets at rest, so callers (providers, request builders) get a
            # ready-to-use dict with usable client_secret values.
            if config is not None and 'config' in config:
                config['config'] = _normalize_config(config.get('config'))
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

    # XML-DSig signature element, used for the (mandatory) presence check.
    _DSIG_NS = 'http://www.w3.org/2000/09/xmldsig#'

    def authenticate(self, saml_response, config):
        """Authenticate SAML response.

        Hardening:
          * XXE: parsed with defusedxml. If defusedxml is unavailable we FAIL
            CLOSED (return None) rather than fall back to unsafe stdlib parsing,
            because the unsafe parse is the vulnerability we are mitigating.
          * Forgery: a SAMLResponse with no <ds:Signature> element is rejected.
            This is a PRESENCE check only -- see _has_signature() TODO; full
            cryptographic verification is a separate, heavier work item.
        """
        try:
            if not saml_response:
                return None

            # FAIL CLOSED: never parse attacker-controlled XML without defusedxml.
            if not _DEFUSEDXML_AVAILABLE or _safe_xml_fromstring is None:
                logger.error(
                    "enterprise_sso: refusing to parse SAMLResponse because defusedxml "
                    "is not installed (XXE protection unavailable)."
                )
                return None

            # Decode SAML response
            decoded_response = base64.b64decode(saml_response)
            root = _safe_xml_fromstring(decoded_response)

            # Reject unsigned SAML responses (today these are silently accepted,
            # which lets anyone forge a login by POSTing a hand-crafted assertion).
            if not self._has_signature(root):
                logger.warning(
                    "enterprise_sso: rejected SAMLResponse with no <ds:Signature> element."
                )
                return None

            # Extract user attributes
            user_info = self.extract_saml_attributes(root, config)
            return user_info
        except Exception as e:
            return None

    def _has_signature(self, saml_root):
        """Return True if the SAML document contains at least one XML-DSig
        <ds:Signature> element (on the Response or the Assertion).

        TODO(security): This is a PRESENCE check only and does NOT verify the
        signature cryptographically (digest, signature value, certificate trust,
        canonicalization, audience/recipient/conditions, replay). Full validation
        requires python3-saml or signxml + xmlsec, which are heavy native deps on
        PythonAnywhere and are tracked as a separate follow-up item. Until then a
        forged-but-"signed" assertion from an untrusted IdP could still pass; the
        presence check closes only the trivial "no signature at all" forgery.
        """
        try:
            for elem in saml_root.iter():
                tag = getattr(elem, 'tag', '')
                if isinstance(tag, str) and tag == '{%s}Signature' % self._DSIG_NS:
                    return True
            return False
        except Exception:
            # On any traversal error, be conservative and treat as unsigned.
            return False

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
        return redirect(url_for('auth.login', slug=company_slug))
    
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
        return redirect(url_for('auth.login', slug=company_slug))
    
    # Extract authentication data based on provider
    if provider == 'saml':
        auth_data = request.form.get('SAMLResponse')
    elif provider == 'oauth2':
        # CSRF protection: the `state` returned by the IdP must match the one we
        # stored in the session before the redirect. Reject missing/mismatched
        # state (this is what was missing today and enabled login CSRF).
        returned_state = request.args.get('state')
        expected_state = session.pop('oauth2_state', None)
        # Drop the one-time nonce regardless of outcome.
        session.pop('oauth2_nonce', None)
        if not expected_state or not returned_state or not secrets.compare_digest(
                str(expected_state), str(returned_state)):
            flash('Ugyldig eller manglende SSO-sikkerhedstoken (state). Prøv at logge ind igen.', 'error')
            return redirect(url_for('auth.login'))
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
    from urllib.parse import urlencode

    auth_url = sso_config['config']['authorization_url']
    client_id = sso_config['config']['client_id']
    redirect_uri = sso_config['config']['redirect_uri']
    scope = sso_config['config'].get('scope', 'openid email profile')

    # CSRF protection: unguessable state, stored server-side in the session and
    # validated on the callback (see sso_callback).
    state = secrets.token_urlsafe(32)
    session['oauth2_state'] = state

    # OIDC replay protection: nonce bound to this authentication request. We
    # persist it so a later id_token nonce check can be added without a new flow.
    nonce = secrets.token_urlsafe(32)
    session['oauth2_nonce'] = nonce

    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': scope,
        'state': state,
        'nonce': nonce,
    }
    return f"{auth_url}?{urlencode(params)}"

@sso_bp.route('/admin/sso/config/<int:company_id>')
def sso_config(company_id):
    """SSO configuration page for company admins"""
    # Check if user is company admin
    if session.get('company_role') != 'company_admin':
        flash('Access denied', 'error')
        return redirect(url_for('dashboard.dashboard'))
    # Tenant isolation: admins may only view their own company's SSO config.
    if session.get('company_id') != company_id:
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
        
        return render_template('fm/sso_config.html',
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
    # Tenant isolation: admins may only modify their own company's SSO config.
    if session.get('company_id') != company_id:
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

    # OAuth2 / OIDC fields, included only when the form provides them so existing
    # SAML-shaped submissions are unchanged.
    for _oauth_field in (
        'authorization_url', 'token_url', 'userinfo_url',
        'redirect_uri', 'scope', 'client_id', 'client_secret',
    ):
        _val = request.form.get(_oauth_field)
        if _val is not None and _val != '':
            config_data[_oauth_field] = _val

    # Encrypt sensitive secrets (e.g. client_secret) at rest before storing.
    # Re-encrypts any legacy plaintext on this write (transitional path).
    config_to_store = _encrypt_config_secrets(config_data)

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
            company_id, provider, provider_name, json.dumps(config_to_store),
            is_enabled, True, 'employee', datetime.now()
        ))
        
        current_app.mysql.connection.commit()
        cur.close()

        flash('SSO configuration saved successfully', 'success')
    except Exception as e:
        flash('Error saving SSO configuration', 'error')
    
    return redirect(url_for('sso.sso_config', company_id=company_id))
