from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, Response
import MySQLdb
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import re
import time

import login_guard
import password_tokens
import rate_limit
import two_factor
from password_policy import validate_password, POLICY_HINT

auth_bp = Blueprint('auth', __name__, template_folder='templates')

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
    return filename and ('.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS)

def _apply_session_user_context(user):
    """Set session fields after successful authentication."""
    session['user'] = user['username']
    session['user_id'] = user['id']
    session['credits'] = user['credits']
    session['role'] = user.get('role', 'user')
    if session['role'] == 'admin':
        session['user_type'] = 'platform_admin'
    else:
        session['user_type'] = 'regular'
    session.pop('company_id', None)
    session.pop('company_role', None)
    session.pop('company_name', None)
    try:
        cur2 = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur2.execute("""
            SELECT cu.company_id, cu.role AS company_role, c.company_name, c.company_slug
            FROM company_users cu
            JOIN companies c ON c.id = cu.company_id
            WHERE cu.user_id = %s AND cu.status = 'active'
            ORDER BY cu.added_at DESC LIMIT 1
        """, (user['id'],))
        comp = cur2.fetchone()
        cur2.close()
        if comp:
            session['company_id'] = comp['company_id']
            session['company_role'] = comp['company_role']
            session['company_name'] = comp['company_name']
            session['company_slug'] = comp.get('company_slug', '')
            session['user_type'] = 'company_user'
    except Exception:
        pass


GENERIC_LOGIN_ERROR = 'Forkert brugernavn eller adgangskode.'
GENERIC_RESET_NOTICE = ('Hvis der findes en konto med de oplysninger, har vi sendt et link til '
                        'at nulstille adgangskoden. Tjek din indbakke.')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# Constant-cost decoy so "unknown user" and "wrong password" take similar time.
_DECOY_HASH = generate_password_hash('decoy-password-for-timing-only')


def _is_hashed(stored):
    return isinstance(stored, str) and stored.startswith(('pbkdf2:', 'scrypt:'))


# Values that are NOT plaintext even though they lack the pbkdf2:/scrypt: prefix:
# other hash schemes and the "account erased" marker. They must never be re-hashed.
_NOT_PLAINTEXT = re.compile(
    r"^(!|\$2[abxy]?\$|\$argon2|\$pbkdf2|[a-z0-9_]+\$[^$]+\$[0-9a-f]{32,}$)", re.I)


def rehash_plaintext_passwords(conn, limit=5000):
    """One-off migration (S-2.2): hash every remaining plaintext password in
    place, so login can drop its plaintext fallback. Returns rows converted.

    Plaintext rows are recognisable (no ``pbkdf2:``/``scrypt:`` prefix). Because
    the plaintext is known, this is lossless: users keep their password.
    """
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    converted = 0
    try:
        cur.execute(
            "SELECT id, password FROM users WHERE password IS NOT NULL AND password <> '' "
            "AND password NOT LIKE 'pbkdf2:%%' AND password NOT LIKE 'scrypt:%%' LIMIT %s",
            (int(limit),),
        )
        rows = cur.fetchall() or []
        for row in rows:
            if _NOT_PLAINTEXT.match(row['password'] or ''):
                continue   # another hash scheme / erased marker: leave untouched
            cur.execute("UPDATE users SET password = %s WHERE id = %s",
                        (generate_password_hash(row['password']), row['id']))
            converted += 1
        conn.commit()
    finally:
        cur.close()
    return converted


@auth_bp.before_app_request
def _migrate_plaintext_passwords_once():
    app = current_app._get_current_object()
    if getattr(app, '_plaintext_pw_migrated', False) or app.config.get('TESTING'):
        return None
    # A failed attempt (database briefly unreachable) is retried on a later
    # request, at most once a minute, until it succeeds once per process.
    now = time.time()
    if now < getattr(app, '_plaintext_pw_next_try', 0):
        return None
    app._plaintext_pw_next_try = now + 60
    try:
        total = 0
        while True:
            n = rehash_plaintext_passwords(app.mysql.connection)
            total += n
            if n < 5000:
                break
        app._plaintext_pw_migrated = True
        if total:
            app.logger.warning("S-2.2: hashed %d legacy plaintext password(s).", total)
    except Exception as exc:
        app.logger.info("plaintext password migration will be retried: %s", exc)
    return None


def _find_user_for_login(cur, identifier):
    cur.execute("SELECT * FROM users WHERE username = %s", (identifier,))
    user = cur.fetchone()
    if user is None and '@' in identifier:
        cur.execute("SELECT * FROM users WHERE email = %s LIMIT 2", (identifier.lower(),))
        rows = cur.fetchall() or []
        if len(rows) == 1:  # ambiguous e-mails never authenticate
            user = rows[0]
    return user


@auth_bp.route('/login', methods=['GET', 'POST'])
@auth_bp.route('/login/<slug>', methods=['GET', 'POST'])
def login(slug=None):
    tenant_slug = slug or request.args.get('tenant') or ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        ip = login_guard.client_ip()

        def _back():
            if tenant_slug:
                return redirect(url_for('auth.login', slug=tenant_slug))
            return redirect(url_for('auth.login'))

        allowed, retry_after = login_guard.check(username, ip)
        if not allowed:
            flash(login_guard.locked_message(retry_after), 'danger')
            return _back()

        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        user = _find_user_for_login(cur, username) if username else None
        cur.close()

        # Only hashed passwords authenticate: the plaintext fallback is gone
        # (legacy rows are hashed by rehash_plaintext_passwords at boot).
        stored = (user or {}).get('password') if user else None
        if user and _is_hashed(stored):
            password_valid = check_password_hash(stored, password)
        else:
            check_password_hash(_DECOY_HASH, password)  # keep timing alike
            password_valid = False

        if user and password_valid:
            login_guard.record_success(username, ip)
            try:
                has_2fa = two_factor.is_enabled(current_app.mysql.connection, user['id'])
            except Exception as exc:
                current_app.logger.error("2FA status check failed: %s", exc)
                flash('Vi kunne ikke gennemføre login lige nu. Prøv igen om lidt.', 'danger')
                return _back()
            if has_2fa:
                session.clear()
                session['twofa_pending'] = {'uid': user['id'], 'ts': time.time()}
                return redirect(url_for('auth.login_2fa'))
            return _finish_login(user, twofa_done=False)

        locked = login_guard.record_failure(username, ip)
        if locked:
            allowed, retry_after = login_guard.check(username, ip)
            flash(login_guard.locked_message(retry_after), 'danger')
        else:
            flash(GENERIC_LOGIN_ERROR, 'danger')
        return _back()
    return render_template('fm/login.html', tenant_slug=tenant_slug)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        email = (request.form.get('email') or '').strip().lower()
        if not username or not password or not email:
            flash('Udfyld alle felter.', 'danger')
            return redirect(url_for('auth.register'))
        if not _EMAIL_RE.match(email):
            flash('Angiv en gyldig e-mailadresse.', 'danger')
            return redirect(url_for('auth.register'))
        pw_errors = validate_password(password, username, email)
        if pw_errors:
            for err in pw_errors:
                flash(err, 'danger')
            return redirect(url_for('auth.register'))
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s OR email = %s", (username, email))
        existing_user = cur.fetchone()
        if existing_user:
            flash('Brugernavn eller e-mail er allerede i brug.', 'danger')
            cur.close()
            return redirect(url_for('auth.register'))
        hashed_password = generate_password_hash(password)
        cur.execute("INSERT INTO users (username, password, email) VALUES (%s, %s, %s)", (username, hashed_password, email))
        current_app.mysql.connection.commit()
        cur.close()
        flash('Din konto er oprettet. Log ind for at komme i gang.', 'success')
        if request.args.get('tenant'):
            return redirect(url_for('auth.login', slug=request.args.get('tenant')))
        return redirect(url_for('auth.login'))
    return render_template('fm/register.html', tenant_slug=request.args.get('tenant') or '')


_LOGOUT_CONFIRM_HTML = (
    "<!doctype html><html lang='da'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Log ud</title></head><body style='font-family:system-ui;max-width:26rem;"
    "margin:5rem auto;padding:0 1rem;text-align:center'>"
    "<h1 style='font-size:1.4rem'>Vil du logge ud?</h1>"
    "<form method='post' action='%s'><button type='submit' style='padding:.7rem 1.4rem;"
    "font-size:1rem;border-radius:.6rem;border:0;background:#0b6b63;color:#fff;cursor:pointer'>"
    "Log ud</button></form><p><a href='/'>Annuller</a></p></body></html>"
)


@auth_bp.route('/logout', methods=['GET', 'POST'])
def logout():
    """Logging out changes state, so it is POST-only (and CSRF-protected). A
    stray GET (old bookmark, prefetch, hostile <img>) only shows a confirm page."""
    if request.method == 'GET':
        if not session.get('user'):
            return redirect(url_for('auth.login'))
        return Response(_LOGOUT_CONFIRM_HTML % url_for('auth.logout'), mimetype='text/html')
    session.clear()
    flash('Du er nu logget ud.', 'success')
    return redirect(url_for('auth.login'))


# ---------------------------------------------------------------------------
# Forgot / reset / invite (S-2.4)
# ---------------------------------------------------------------------------

def _reset_email(user, raw_token, purpose):
    """Send the reset / invite e-mail. Best effort: never raises."""
    try:
        from email_service import send_branded_email
        endpoint = 'auth.set_password' if purpose == 'invite' else 'auth.reset_password'
        url = password_tokens.build_url(endpoint, raw_token)
        if purpose == 'invite':
            return send_branded_email(
                user['email'], 'Velkommen – vælg din adgangskode', 'password_invite', {},
                recipient_name=user.get('username') or '', username=user.get('username') or '',
                set_password_url=url, expires_in='7 dage', dedupe_key=None)
        return send_branded_email(
            user['email'], 'Nulstil din adgangskode', 'password_reset', {},
            reset_url=url, expires_in='60 minutter')
    except Exception as exc:
        current_app.logger.warning("reset e-mail failed: %s", exc)
        return False


def send_user_password_link(conn, user, purpose='reset'):
    """Issue a token for ``user`` (dict with id/email/username) and e-mail it.
    Returns True when a mail was handed to the mail layer."""
    if not user or not user.get('email'):
        return False
    raw = password_tokens.issue_token(conn, 'user', user['id'], purpose=purpose)
    return bool(_reset_email(user, raw, purpose))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        identifier = (request.form.get('identifier') or request.form.get('email') or '').strip()
        ip = login_guard.client_ip()
        # Same throttle for every outcome, so this cannot be used to spam a
        # mailbox or to probe which accounts exist.
        ok = rate_limit.hit('forgot:ip:' + ip, 10, 900) and \
            rate_limit.hit('forgot:id:' + identifier.lower(), 3, 900)
        if ok and identifier:
            try:
                cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                user = _find_user_for_login(cur, identifier)
                cur.close()
                if user and user.get('email'):
                    send_user_password_link(current_app.mysql.connection, user, 'reset')
            except Exception as exc:
                current_app.logger.warning("forgot_password failed: %s", exc)
        flash(GENERIC_RESET_NOTICE, 'success')
        return redirect(url_for('auth.forgot_password'))
    return render_template('fm/auth_simple.html',
                           page_title='Glemt adgangskode', heading='Glemt adgangskode',
                           subtitle='Skriv dit brugernavn eller din e-mail, så sender vi et link til at vælge en ny adgangskode.',
                           action=url_for('auth.forgot_password'),
                           fields=[{'name': 'identifier', 'label': 'Brugernavn eller e-mail',
                                    'type': 'text', 'autocomplete': 'username', 'required': True}],
                           submit_label='Send link', back_url=url_for('auth.login'),
                           back_label='Tilbage til login')


def _token_page(token, purpose_label, endpoint):
    """Shared GET/POST for reset (``reset``) and invite (``invite``) links."""
    conn = current_app.mysql.connection
    info = None
    try:
        info = password_tokens.lookup(conn, token, account_type='user')
    except Exception as exc:
        current_app.logger.warning("token lookup failed: %s", exc)
    title = 'Vælg din adgangskode' if purpose_label == 'invite' else 'Vælg ny adgangskode'
    if not info:
        flash('Linket er ugyldigt eller udløbet. Bed om et nyt link.', 'danger')
        return render_template('fm/auth_simple.html', page_title=title, heading=title, invalid=True,
                               back_url=url_for('auth.forgot_password'), back_label='Send et nyt link')

    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT id, username, email FROM users WHERE id = %s", (info['account_id'],))
    user = cur.fetchone()
    cur.close()
    if not user:
        flash('Linket er ugyldigt eller udløbet. Bed om et nyt link.', 'danger')
        return render_template('fm/auth_simple.html', page_title=title, heading=title, invalid=True,
                               back_url=url_for('auth.forgot_password'), back_label='Send et nyt link')

    fields = [
        {'name': 'password', 'label': 'Ny adgangskode', 'type': 'password',
         'autocomplete': 'new-password', 'required': True, 'hint': POLICY_HINT},
        {'name': 'confirm', 'label': 'Gentag adgangskode', 'type': 'password',
         'autocomplete': 'new-password', 'required': True},
    ]

    def _render():
        return render_template('fm/auth_simple.html', page_title=title, heading=title,
                               subtitle='Vælg en adgangskode til din konto.',
                               action=url_for(endpoint, token=token), fields=fields,
                               submit_label='Gem adgangskode', back_url=url_for('auth.login'),
                               back_label='Til login')

    if request.method == 'POST':
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        errors = validate_password(password, user.get('username'), user.get('email'))
        if password != confirm:
            errors.append('De to adgangskoder er ikke ens.')
        if errors:
            for err in errors:
                flash(err, 'danger')
            return _render()
        if not password_tokens.consume(conn, token):
            flash('Linket er ugyldigt eller allerede brugt. Bed om et nyt link.', 'danger')
            return render_template('fm/auth_simple.html', page_title=title, heading=title, invalid=True,
                                   back_url=url_for('auth.forgot_password'), back_label='Send et nyt link')
        cur = conn.cursor()
        cur.execute("UPDATE users SET password = %s WHERE id = %s",
                    (generate_password_hash(password), user['id']))
        conn.commit()
        cur.close()
        login_guard.record_success(user['username'])
        try:
            from security_audit import audit
            audit('user.password_set_via_link', 'user', user['id'],
                  'Adgangskode sat via %s-link' % purpose_label, user_id=user['id'])
        except Exception:
            pass
        flash('Din adgangskode er gemt. Du kan nu logge ind.', 'success')
        return redirect(url_for('auth.login'))
    return _render()


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    return _token_page(token, 'reset', 'auth.reset_password')


@auth_bp.route('/set-password/<token>', methods=['GET', 'POST'])
def set_password(token):
    return _token_page(token, 'invite', 'auth.set_password')


# ---------------------------------------------------------------------------
# Two-factor authentication (S-2.6)
# ---------------------------------------------------------------------------

TWOFA_PENDING_SECONDS = 300
_TWOFA_OPEN_ENDPOINTS = {'auth.account_2fa', 'auth.logout', 'auth.login_2fa', 'static'}


def _finish_login(user, twofa_done):
    """Create the real session after every required factor has passed."""
    session.clear()  # new identity, new session (no fixation)
    _apply_session_user_context(user)
    session['twofa_ok'] = bool(twofa_done)
    if not twofa_done and two_factor.enrollment_required(user.get('role'), session.get('company_role')):
        session['twofa_must_enroll'] = True
    flash('Du er nu logget ind.', 'success')
    if session.get('twofa_must_enroll'):
        flash('Af sikkerhedshensyn skal du slå tofaktorgodkendelse til, før du kan fortsætte.', 'warning')
        return redirect(url_for('auth.account_2fa'))
    return redirect(url_for('dashboard.dashboard'))


@auth_bp.before_app_request
def _twofa_gate():
    if not session.get('user') or request.endpoint in _TWOFA_OPEN_ENDPOINTS or request.endpoint is None:
        return None
    # Sessions created before 2FA existed carry no twofa_ok marker: an admin
    # session like that must log in again through the new flow.
    if (session.get('role') == 'admin' and two_factor.enrollment_required('admin')
            and 'twofa_ok' not in session and not current_app.config.get('TESTING')):
        session.clear()
        flash('Log ind igen for at fortsætte.', 'info')
        return redirect(url_for('auth.login'))
    if session.get('twofa_must_enroll'):
        return redirect(url_for('auth.account_2fa'))
    return None


@auth_bp.route('/login/2fa', methods=['GET', 'POST'])
def login_2fa():
    pending = session.get('twofa_pending') or {}
    if not pending or time.time() - pending.get('ts', 0) > TWOFA_PENDING_SECONDS:
        session.pop('twofa_pending', None)
        flash('Din login-session er udløbet. Log ind igen.', 'warning')
        return redirect(url_for('auth.login'))
    uid = pending['uid']
    guard_key = '2fa:%s' % uid
    ip = login_guard.client_ip()
    if request.method == 'POST':
        allowed, retry_after = login_guard.check(guard_key, ip)
        if not allowed:
            flash(login_guard.locked_message(retry_after), 'danger')
            return redirect(url_for('auth.login_2fa'))
        code = request.form.get('code') or ''
        conn = current_app.mysql.connection
        if two_factor.verify_login(conn, uid, code):
            login_guard.record_success(guard_key, ip)
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
            user = cur.fetchone()
            cur.close()
            if not user:
                session.pop('twofa_pending', None)
                return redirect(url_for('auth.login'))
            return _finish_login(user, twofa_done=True)
        login_guard.record_failure(guard_key, ip)
        flash('Koden er forkert eller udløbet. Prøv igen.', 'danger')
        return redirect(url_for('auth.login_2fa'))
    return render_template('fm/auth_simple.html',
                           page_title='Bekræft din identitet', heading='Bekræft din identitet',
                           subtitle='Skriv den 6-cifrede kode fra din authenticator-app. '
                                    'Har du mistet din telefon, kan du bruge en af dine reservekoder.',
                           action=url_for('auth.login_2fa'),
                           fields=[{'name': 'code', 'label': 'Kode', 'type': 'text',
                                    'autocomplete': 'one-time-code', 'required': True}],
                           submit_label='Bekræft', back_url=url_for('auth.login'),
                           back_label='Annuller')


@auth_bp.route('/account/2fa', methods=['GET', 'POST'])
def account_2fa():
    if not session.get('user') or not session.get('user_id'):
        return redirect(url_for('auth.login'))
    uid = session['user_id']
    conn = current_app.mysql.connection
    must = bool(session.get('twofa_must_enroll'))
    new_codes = None
    action = request.form.get('action') if request.method == 'POST' else None
    guard_key = '2fa-manage:%s' % uid
    ip = login_guard.client_ip()

    if action:
        allowed, retry_after = login_guard.check(guard_key, ip)
        if not allowed:
            flash(login_guard.locked_message(retry_after), 'danger')
            return redirect(url_for('auth.account_2fa'))

    if action == 'confirm':
        codes = two_factor.confirm_enrollment(conn, uid, request.form.get('code') or '')
        if codes:
            login_guard.record_success(guard_key, ip)
            session.pop('twofa_must_enroll', None)
            session['twofa_ok'] = True
            new_codes = codes
            flash('Tofaktorgodkendelse er slået til. Gem dine reservekoder et sikkert sted.', 'success')
        else:
            login_guard.record_failure(guard_key, ip)
            flash('Koden er forkert eller udløbet. Prøv igen.', 'danger')
            return redirect(url_for('auth.account_2fa'))
    elif action in ('disable', 'regenerate'):
        if action == 'disable' and (must or two_factor.enrollment_required(session.get('role'), session.get('company_role'))):
            flash('Tofaktorgodkendelse er påkrævet for din rolle og kan ikke slås fra.', 'danger')
            return redirect(url_for('auth.account_2fa'))
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT password FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
        cur.close()
        stored = (row or {}).get('password') or ''
        ok_pw = _is_hashed(stored) and check_password_hash(stored, request.form.get('password') or '')
        if not (ok_pw and two_factor.verify_login(conn, uid, request.form.get('code') or '')):
            login_guard.record_failure(guard_key, ip)
            flash('Adgangskode eller kode er forkert.', 'danger')
            return redirect(url_for('auth.account_2fa'))
        login_guard.record_success(guard_key, ip)
        if action == 'disable':
            two_factor.disable(conn, uid)
            session.pop('twofa_ok', None)
            flash('Tofaktorgodkendelse er slået fra.', 'success')
            return redirect(url_for('auth.account_2fa'))
        new_codes = two_factor.regenerate_backup_codes(conn, uid)
        flash('Der er genereret nye reservekoder. De gamle virker ikke længere.', 'success')

    enabled = two_factor.is_enabled(conn, uid)
    secret = uri = None
    if not enabled:
        secret = two_factor.pending_secret(conn, uid) or two_factor.start_enrollment(conn, uid)
        uri = two_factor.provisioning_uri(secret, session.get('user'))
    required = two_factor.enrollment_required(session.get('role'), session.get('company_role'))
    return render_template('fm/account_2fa.html', enabled=enabled, required=required, must=must,
                           secret=secret and two_factor.pretty_secret(secret), uri=uri,
                           new_codes=new_codes,
                           codes_left=two_factor.backup_codes_left(conn, uid) if enabled else 0)


@auth_bp.route('/brands')
def brands():
    if 'user' not in session:
        flash('Please log in to access your profile.', 'danger')
        return redirect(url_for('auth.login'))
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM brands WHERE username = %s", (session.get('user'),))
        brands = cur.fetchall()
        cur.close()
    except Exception as e:
        flash("Fejl ved hentning af brands: " + str(e), "danger")
        brands = None
    # Ensure CV/profile tables exist
    try:
        from app1.user_profile_db import ensure_tables
        ensure_tables()
    except Exception:
        pass
    # Pass current timestamp to force image refresh in template
    return render_template('profile.html', username=session['user'], brands=brands, timestamp=int(time.time()))

@auth_bp.route("/update_brand/<int:brand_id>", methods=["POST"])
def update_brand(brand_id):
    if not session.get('user'):
        flash("Du skal logge ind for at opdatere dit brand.", "danger")
        return redirect(url_for('auth.login'))
    username = session.get('user')
    upload_folder = os.path.join(current_app.root_path, "static", "uploads", "brands")
    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)
    brand_name = request.form.get("brand_name").strip()
    brand_site = request.form.get("brand_site").strip()
    brand_fb = request.form.get("brand_fb").strip()
    brand_twitter = request.form.get("brand_twitter").strip()
    brand_instagram = request.form.get("brand_instagram").strip()
    brand_linkedin = request.form.get("brand_linkedin").strip()
    brand_description = request.form.get("brand_description").strip()
    file = request.files.get("brand_logo")
    brand_logo_path = None
    if file and file.filename != "" and allowed_file(file.filename):
        try:
            import upload_guard   # S-5.5: real image bytes, size cap, random name
            new_filename = upload_guard.save_image(file, upload_folder, prefix=f"{brand_id}_")
            brand_logo_path = url_for('static', filename=f"uploads/brands/{new_filename}")
        except upload_guard.UploadRejected as rej:
            flash(str(rej), "danger")
        except Exception as e:
            current_app.logger.error("File save failed: %s", e)
    else:
        current_app.logger.info("No valid file provided for update_brand/%s", brand_id)
    try:
        cur = current_app.mysql.connection.cursor()
        if brand_logo_path:
            cur.execute("""
                UPDATE brands SET brand_name=%s, brand_site=%s, brand_logo=%s, 
                brand_facebook=%s, brand_twitter=%s, brand_instagram=%s, brand_linkedin=%s, brand_description=%s 
                WHERE id=%s AND username=%s
            """, (brand_name, brand_site, brand_logo_path, brand_fb, brand_twitter, brand_instagram, brand_linkedin, brand_description, brand_id, username))
        else:
            cur.execute("""
                UPDATE brands SET brand_name=%s, brand_site=%s, 
                brand_facebook=%s, brand_twitter=%s, brand_instagram=%s, brand_linkedin=%s, brand_description=%s 
                WHERE id=%s AND username=%s
            """, (brand_name, brand_site, brand_fb, brand_twitter, brand_instagram, brand_linkedin, brand_description, brand_id, username))
        current_app.mysql.connection.commit()
        # For debugging: retrieve the updated brand_logo value from DB
        cur.execute("SELECT brand_logo FROM brands WHERE id=%s AND username=%s", (brand_id, username))
        updated = cur.fetchone()
        current_app.logger.info("Updated brand_logo in DB: %s", updated.get("brand_logo") if updated else "None")
        flash("Brand opdateret.", "success")
    except Exception as e:
        flash("Fejl ved opdatering af brand: " + str(e), "danger")
    return redirect(url_for('auth.brands'))

@auth_bp.route("/add_brand", methods=["GET", "POST"])
def add_brand():
    if 'user' not in session:
        flash("Du skal logge ind for at tilføje et brand.", "danger")
        return redirect(url_for('auth.login'))
    if request.method == "POST":
        username = session.get('user')
        upload_folder = os.path.join(current_app.root_path, "static", "uploads", "brands")
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)
        brand_name = request.form.get("brand_name").strip()
        brand_site = request.form.get("brand_site").strip()
        brand_fb = request.form.get("brand_fb").strip()
        brand_twitter = request.form.get("brand_twitter").strip()
        brand_instagram = request.form.get("brand_instagram").strip()
        brand_linkedin = request.form.get("brand_linkedin").strip()
        brand_description = request.form.get("brand_description").strip()
        file = request.files.get("brand_logo")
        brand_logo_path = None
        if file and file.filename != "" and allowed_file(file.filename):
            try:
                import upload_guard   # S-5.5: real image bytes, size cap, random name
                new_filename = upload_guard.save_image(file, upload_folder, prefix="brand_")
                brand_logo_path = url_for('static', filename=f"uploads/brands/{new_filename}")
            except upload_guard.UploadRejected as rej:
                flash(str(rej), "danger")
            except Exception as e:
                current_app.logger.error("File save failed: %s", e)
        try:
            cur = current_app.mysql.connection.cursor()
            cur.execute("""
                INSERT INTO brands (username, brand_name, brand_site, brand_logo, brand_facebook, brand_twitter, brand_instagram, brand_linkedin, brand_description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (username, brand_name, brand_site, brand_logo_path, brand_fb, brand_twitter, brand_instagram, brand_linkedin, brand_description))
            current_app.mysql.connection.commit()
            flash("Brand tilføjet.", "success")
        except Exception as e:
            flash("Fejl ved tilføjelse af brand: " + str(e), "danger")
        return redirect(url_for('auth.brands'))
    return render_template("add_brand.html")
