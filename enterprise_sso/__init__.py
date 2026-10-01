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
from datetime import datetime
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

# ---------------------------------------------------------------------------
# Server-side provider gate (S-1.8).
#
# SAML and LDAP/Active Directory are DISABLED server-side until S-D.1 (real
# signature verification against the stored certificate, escaped LDAP filters).
# Existing configs are kept in the database but are inert: every route and the
# SSOManager refuse them. OAuth2/OIDC went live with S-3.1 (nonce, id_token
# signature/issuer/audience validation, PKCE, and a real users.id in the session).
# ---------------------------------------------------------------------------
LIVE_SSO_PROVIDERS = frozenset({'oauth2'})
DEFERRED_SSO_PROVIDERS = frozenset({'saml', 'ldap', 'active_directory'})
SSO_DISABLED_MESSAGE = (
    'Denne SSO-metode er ikke aktiveret endnu. Log ind med brugernavn og adgangskode '
    'eller kontakt din administrator.'
)


def sso_provider_enabled(provider):
    """True only for providers that are safe to accept logins for right now."""
    return provider in LIVE_SSO_PROVIDERS

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
    
    def authenticate_user(self, company_id, provider_type, auth_data, context=None):
        """Authenticate user through SSO provider"""
        if not sso_provider_enabled(provider_type):
            logger.warning("enterprise_sso: refused login via disabled provider %r", provider_type)
            return None, SSO_DISABLED_MESSAGE

        provider = self.get_provider(provider_type)
        if not provider:
            return None, "SSO-udbyderen understøttes ikke."
        
        # Get company SSO configuration
        sso_config = self.get_company_sso_config(company_id, provider_type)
        if not sso_config or not sso_config.get('is_enabled'):
            return None, "SSO er ikke sat op for denne virksomhed."
        
        # Authenticate with provider
        if provider_type == 'oauth2':
            user_info = provider.authenticate(auth_data, sso_config['config'], **(context or {}))
        else:
            user_info = provider.authenticate(auth_data, sso_config['config'])
        if not user_info:
            return None, "Login mislykkedes."

        allowed_domains = [d.strip().lower().lstrip('@') for d in
                           (sso_config['config'].get('allowed_domains') or '').replace(';', ',').split(',') if d.strip()]
        if allowed_domains and (user_info.get('email') or '').rsplit('@', 1)[-1].lower() not in allowed_domains:
            logger.warning("enterprise_sso: e-mail domain not allowed for company %s", company_id)
            return None, "Login mislykkedes."
        
        # Auto-provision user if enabled
        if sso_config.get('auto_provision_users', True):
            user = self.provision_user(company_id, user_info, sso_config)
            return user, None
        
        # Find existing user
        user = self.find_user_by_email(company_id, user_info['email'])
        if not user:
            return None, "Brugeren findes ikke, og automatisk oprettelse er slået fra."
        
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
        except Exception:
            return None
    
    def provision_user(self, company_id, user_info, sso_config):
        """Return the ``users`` row for a verified SSO identity, creating the
        login identity and the company membership when auto-provisioning.

        The returned dict is a ``users`` row (``id`` is users.id), never a
        company_users row. Platform admins, ambiguous e-mails and deactivated
        members are refused.
        """
        import identity
        try:
            conn = current_app.mysql.connection
            if not conn:
                return None
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            try:
                user, created = identity.ensure_login_identity(cur, user_info['email'])
                member = identity.get_membership(cur, company_id, user['id'])
                if member:
                    if (member.get('status') or '').lower() != 'active':
                        conn.rollback()
                        return None   # deactivated / removed people stay out
                else:
                    try:
                        import seat_governance
                        seat_ok, _reason = seat_governance.can_add_employee(company_id)
                    except Exception:
                        seat_ok = True
                    if not seat_ok:
                        conn.rollback()
                        return None
                    identity.add_membership(
                        cur, company_id, user,
                        full_name=user_info.get('full_name') or '',
                        role=sso_config.get('default_role') or 'employee',
                        department=user_info.get('department') or '',
                        job_title=user_info.get('job_title') or '')
                conn.commit()
            finally:
                cur.close()
            if created or not member:
                self.log_audit_event(company_id, user['id'], 'user.auto_provisioned',
                                     'user', str(user['id']),
                                     f"SSO-provisioned {user_info['email']}")
            return user
        except Exception as e:
            logger.warning("enterprise_sso: provision_user failed: %s", e)
            try:
                current_app.mysql.connection.rollback()
            except Exception:
                pass
            return None

    def find_user_by_email(self, company_id, email):
        """The ``users`` row of an ACTIVE member of ``company_id`` with this e-mail."""
        import identity
        try:
            conn = current_app.mysql.connection
            if not conn:
                return None
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            try:
                user = identity.find_user_by_email(cur, email)
                if not user or (user.get('role') or '') == identity.PLATFORM_ADMIN_ROLE:
                    return None
                member = identity.get_membership(cur, company_id, user['id'])
                if not member or (member.get('status') or '').lower() != 'active':
                    return None
                return user
            finally:
                cur.close()
        except Exception as e:
            logger.warning("enterprise_sso: find_user_by_email failed: %s", e)
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
        except Exception:
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
            if not sso_provider_enabled('saml'):
                # S-1.8 / S-D.1: presence-only signature checks are forgeable.
                return None
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
        except Exception:
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
    """OpenID Connect authorization-code flow with PKCE.

    The identity comes ONLY from a validated ``id_token`` (signature against the
    issuer's JWKS, issuer, audience, expiry, and the per-login nonce). The
    userinfo endpoint is never trusted for identity.
    """

    def authenticate(self, auth_code, config, expected_nonce=None, code_verifier=None):
        """Return ``{'email','full_name','sub',...}`` or None."""
        import oidc
        try:
            if not auth_code or not expected_nonce:
                return None
            token_response = self.exchange_code_for_token(auth_code, config, code_verifier)
            if not token_response:
                return None
            id_token = token_response.get('id_token')
            if not id_token:
                logger.warning("enterprise_sso: token response had no id_token (OIDC required)")
                return None
            issuer = (config.get('issuer') or '').strip()
            jwks_uri = (config.get('jwks_uri') or '').strip()
            if not issuer or not jwks_uri:
                logger.warning("enterprise_sso: issuer/jwks_uri missing in SSO config")
                return None
            jwks = oidc.fetch_jwks(jwks_uri)
            try:
                claims = oidc.validate_id_token(
                    id_token, jwks=jwks, issuer=issuer, audience=config.get('client_id'),
                    nonce=expected_nonce)
            except oidc.OIDCError as first:
                if 'signing key' not in str(first):
                    raise
                # Key rotation: refetch the JWKS once and retry.
                jwks = oidc.fetch_jwks(jwks_uri, force=True)
                claims = oidc.validate_id_token(
                    id_token, jwks=jwks, issuer=issuer, audience=config.get('client_id'),
                    nonce=expected_nonce)
            return oidc.extract_identity(claims)
        except Exception as e:
            logger.warning("enterprise_sso: OIDC authentication failed: %s", e)
            return None

    def exchange_code_for_token(self, auth_code, config, code_verifier=None):
        """Exchange the authorization code for tokens (server to server)."""
        token_url = config.get('token_url')
        data = {
            'grant_type': 'authorization_code',
            'code': auth_code,
            'redirect_uri': config.get('redirect_uri'),
            'client_id': config.get('client_id'),
            'client_secret': config.get('client_secret'),
        }
        if code_verifier:
            data['code_verifier'] = code_verifier
        import safe_http
        from urllib.parse import urlencode
        try:
            status, raw = safe_http.request(
                'POST', token_url, body=urlencode(data).encode(),
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
                timeout=10, max_bytes=200_000, require_https=True)
        except (safe_http.UnsafeURL, OSError) as e:
            logging.warning("SSO token exchange failed (%s): %s", token_url, e)
            return None
        if status == 200:
            try:
                return json.loads(raw.decode('utf-8'))
            except Exception:
                return None
        return None

class LDAPProvider:
    """LDAP Authentication Provider"""
    
    def authenticate(self, credentials, config):
        """Authenticate against LDAP"""
        try:
            if not (sso_provider_enabled('ldap') or sso_provider_enabled('active_directory')):
                # S-1.8 / S-D.1: disabled server-side until the filter is hardened
                # and the flow is reviewed.
                return None
            import ldap3
            from ldap3.utils.conv import escape_filter_chars
            
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
                    username=escape_filter_chars(str(credentials.get('username') or ''))
                )
                
                conn.search(search_base, search_filter, attributes=['*'])
                if conn.entries:
                    entry = conn.entries[0]
                    user_info = self.extract_ldap_attributes(entry, config)
                    return user_info
            
            return None
        except Exception:
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
    if not sso_provider_enabled(provider):
        flash(SSO_DISABLED_MESSAGE, 'error')
        return redirect(url_for('auth.login'))
    # Get company by slug
    company = get_company_by_slug(company_slug)
    if not company:
        flash('Virksomheden blev ikke fundet.', 'error')
        return redirect(url_for('auth.login', slug=company_slug))
    
    # Get SSO configuration
    sso_config = sso_manager.get_company_sso_config(company['id'], provider)
    if not sso_config:
        flash('SSO er ikke sat op for denne virksomhed.', 'error')
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
    if not sso_provider_enabled(provider):
        flash(SSO_DISABLED_MESSAGE, 'error')
        return redirect(url_for('auth.login'))
    company = get_company_by_slug(company_slug)
    if not company:
        flash('Virksomheden blev ikke fundet.', 'error')
        return redirect(url_for('auth.login', slug=company_slug))
    
    # Extract authentication data based on provider
    if provider == 'saml':
        auth_data = request.form.get('SAMLResponse')
    elif provider == 'oauth2':
        # CSRF protection: the `state` returned by the IdP must match the one we
        # stored in the session before the redirect. The nonce and PKCE verifier
        # are one-time values, dropped regardless of outcome.
        returned_state = request.args.get('state')
        expected_state = session.pop('oauth2_state', None)
        oidc_context = {
            'expected_nonce': session.pop('oauth2_nonce', None),
            'code_verifier': session.pop('oauth2_pkce', None),
        }
        if request.args.get('error'):
            flash('Login hos din identitetsudbyder blev afbrudt eller afvist.', 'error')
            return redirect(url_for('auth.login'))
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
    user, error = sso_manager.authenticate_user(
        company['id'], provider, auth_data, context=oidc_context if provider == 'oauth2' else None)
    if error or not user:
        flash(error or 'SSO-login mislykkedes. Kontakt din administrator.', 'error')
        return redirect(url_for('auth.login'))

    # Build the session from the REAL users row (users.id), exactly like a
    # password login, then pin the tenant we just authenticated against.
    from auth import _apply_session_user_context
    session.clear()
    _apply_session_user_context(user)
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT role, status FROM company_users WHERE company_id = %s AND user_id = %s "
        "ORDER BY (status = 'active') DESC LIMIT 1", (company['id'], user['id']))
    member = cur.fetchone()
    cur.close()
    if not member or (member.get('status') or '').lower() != 'active':
        session.clear()
        flash('Din konto er ikke aktiv i denne virksomhed.', 'error')
        return redirect(url_for('auth.login'))
    session['company_id'] = company['id']
    session['company_name'] = company['company_name']
    session['company_slug'] = company.get('company_slug', '')
    session['company_role'] = member['role']
    session['user_type'] = 'company_user'
    session['twofa_ok'] = True   # the identity provider performed authentication

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
    except Exception:
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

    # OIDC replay protection: a nonce bound to this authentication request; the
    # callback requires the id_token to carry exactly this value.
    nonce = secrets.token_urlsafe(32)
    session['oauth2_nonce'] = nonce

    # PKCE (S256): the code is useless to anyone who intercepts it.
    verifier = secrets.token_urlsafe(64)
    session['oauth2_pkce'] = verifier
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()

    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': scope,
        'state': state,
        'nonce': nonce,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    return f"{auth_url}?{urlencode(params)}"

@sso_bp.route('/admin/sso/config/<int:company_id>')
def sso_config(company_id):
    """SSO configuration page for company admins"""
    # Check if user is company admin
    if session.get('company_role') != 'company_admin':
        flash('Du har ikke adgang til denne side.', 'error')
        return redirect(url_for('dashboard.dashboard'))
    # Tenant isolation: admins may only view their own company's SSO config.
    if session.get('company_id') != company_id:
        flash('Du har ikke adgang til denne side.', 'error')
        return redirect(url_for('dashboard.dashboard'))

    # N-7.1: SSO setup lives in the company settings hub (OIDC-only UX).
    return redirect(url_for('settings_hub.tab', tab='sso'))

@sso_bp.route('/admin/sso/config/<int:company_id>', methods=['POST'])
def save_sso_config(company_id):
    """Save SSO configuration"""
    if session.get('company_role') != 'company_admin':
        flash('Du har ikke adgang til denne side.', 'error')
        return redirect(url_for('dashboard.dashboard'))
    # Tenant isolation: admins may only modify their own company's SSO config.
    if session.get('company_id') != company_id:
        flash('Du har ikke adgang til denne side.', 'error')
        return redirect(url_for('dashboard.dashboard'))

    provider = request.form.get('provider')
    provider_name = request.form.get('provider_name')
    is_enabled = request.form.get('is_enabled') == 'on'
    if provider not in ('saml', 'oauth2', 'ldap', 'active_directory'):
        flash('Ukendt SSO-udbyder.', 'error')
        return redirect(url_for('sso.sso_config', company_id=company_id))
    if is_enabled and not sso_provider_enabled(provider):
        # Keep the config, but never let a disabled method go live (S-1.8).
        is_enabled = False
        flash('Konfigurationen er gemt, men metoden er ikke aktiveret endnu og forbliver inaktiv.', 'info')
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
        'authorization_url', 'token_url', 'userinfo_url', 'jwks_uri',
        'redirect_uri', 'scope', 'client_id', 'client_secret', 'allowed_domains',
    ):
        _val = request.form.get(_oauth_field)
        if _val is not None and _val != '':
            config_data[_oauth_field] = _val

    # OIDC endpoints must be https and the issuer/jwks are mandatory for a
    # config to go live (S-3.1); otherwise it is stored but inactive.
    if provider == 'oauth2':
        https_fields = ('authorization_url', 'token_url', 'jwks_uri', 'issuer')
        bad = [f for f in https_fields
               if config_data.get(f) and not str(config_data[f]).lower().startswith('https://')]
        if bad:
            flash('Disse felter skal være https-adresser: ' + ', '.join(bad), 'error')
            return redirect(url_for('sso.sso_config', company_id=company_id))
        missing = [f for f in ('authorization_url', 'token_url', 'jwks_uri', 'issuer', 'client_id', 'redirect_uri')
                   if not config_data.get(f)]
        if is_enabled and missing:
            is_enabled = False
            flash('Konfigurationen er gemt, men mangler: ' + ', '.join(missing) + '. Den er ikke aktiv endnu.', 'warning')

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

        flash('SSO-opsætningen er gemt.', 'success')
    except Exception:
        flash('SSO-opsætningen kunne ikke gemmes.', 'error')
    
    return redirect(url_for('sso.sso_config', company_id=company_id))
