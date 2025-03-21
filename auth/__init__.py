from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
import MySQLdb
from werkzeug.utils import secure_filename
import os
import uuid
import time

auth_bp = Blueprint('auth', __name__, template_folder='templates')

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
    return filename and ('.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        if user and user['password'] == password:
            session['user'] = user['username']
            session['credits'] = user['credits']
            session['role'] = user.get('role', 'user')  # Set role here
            flash('Login successful!', 'success')
            return redirect(url_for('auth.profile'))
        else:
            flash('Invalid username or password', 'danger')
            return redirect(url_for('auth.login'))
    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email')
        if not username or not password or not email:
            flash('Please fill out all fields.', 'danger')
            return redirect(url_for('auth.register'))
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s OR email = %s", (username, email))
        existing_user = cur.fetchone()
        if existing_user:
            flash('Username or email already exists', 'danger')
            cur.close()
            return redirect(url_for('auth.register'))
        cur.execute("INSERT INTO users (username, password, email) VALUES (%s, %s, %s)", (username, password, email))
        current_app.mysql.connection.commit()
        cur.close()
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('register.html')

@auth_bp.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('credits', None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('auth.login'))

@auth_bp.route('/profile')
def profile():
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
        original_filename = secure_filename(file.filename)
        unique_id = uuid.uuid4().hex
        new_filename = f"{username}_{brand_id}_{unique_id}_{original_filename}"
        file_path = os.path.join(upload_folder, new_filename)
        try:
            file.save(file_path)
            current_app.logger.info("Uploaded new file: %s", new_filename)
            brand_logo_path = url_for('static', filename=f"uploads/brands/{new_filename}")
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
    return redirect(url_for('auth.profile'))

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
            original_filename = secure_filename(file.filename)
            unique_id = uuid.uuid4().hex
            new_filename = f"{username}_{unique_id}_{original_filename}"
            file_path = os.path.join(upload_folder, new_filename)
            try:
                file.save(file_path)
                current_app.logger.info("Uploaded new file: %s", new_filename)
                brand_logo_path = url_for('static', filename=f"uploads/brands/{new_filename}")
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
        return redirect(url_for('auth.profile'))
    return render_template("add_brand.html")
