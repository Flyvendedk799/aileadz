import os
from flask import Flask, render_template, request, Blueprint, session, current_app, flash, redirect, url_for
import openai
import MySQLdb

# Define the blueprint for app2
app2_bp = Blueprint('app2', __name__, template_folder='templates')

@app2_bp.route("/", methods=["GET", "POST"])
def index():
    generated_post = None
    selected_platform = None
    website_link = None
    company_desc = None

    # If user is logged in, load their brands from the database
    user_brands = []
    if session.get('user'):
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT * FROM brands WHERE username = %s", (session.get('user'),))
            user_brands = cur.fetchall()
            cur.close()
        except Exception as e:
            flash("Fejl ved hentning af brands: " + str(e), "danger")
            user_brands = []

    if request.method == "POST":
        # Ensure the user is logged in
        if not session.get('user'):
            flash("Du skal logge ind for at generere et opslag.", "danger")
            return redirect(url_for('auth.login'))
        
        # If credits are not in session, load them from the database
        if session.get('credits') is None:
            try:
                cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                cur.execute("SELECT credits FROM users WHERE username = %s", (session.get('user'),))
                result = cur.fetchone()
                if result:
                    session['credits'] = result['credits']
                else:
                    session['credits'] = 0
                cur.close()
            except Exception as e:
                flash("Fejl ved hentning af credits: " + str(e), "danger")
                return redirect(url_for('app2.index'))
        
        # Check if the user has at least 4 credits
        try:
            current_credits = int(session.get('credits', 0))
        except ValueError:
            current_credits = 0
        if current_credits < 4:
            flash("Du har ikke nok credits til at generere et opslag. (Kræver 4 credits)", "danger")
            return redirect(url_for('app2.index'))
        
        # Deduct 4 credits from the user's account and log the deduction in credit_usage
        try:
            cur = current_app.mysql.connection.cursor()
            cur.execute("UPDATE users SET credits = credits - %s WHERE username = %s", (4, session.get('user')))
            cur.execute("INSERT INTO credit_usage (username, credits_used, description) VALUES (%s, %s, %s)",
                        (session.get('user'), 4, 'App2 posting generation deduction'))
            current_app.mysql.connection.commit()
            cur.close()
            session['credits'] = current_credits - 4
        except Exception as e:
            flash("Fejl ved fratrækning af credits: " + str(e), "danger")
            return redirect(url_for('app2.index'))
        
        # Retrieve form data
        platform = request.form.get("platform")
        annonce_type = request.form.get("type")
        vision = request.form.get("vision")
        company_desc = request.form.get("company_desc")
        website_link = request.form.get("website_link")
        post_length = request.form.get("post_length")
        selected_platform = platform  # Save for preview

        # Build prompt
        prompt = (
            f"Generer et kreativt, engagerende og visuelt tiltalende social medie opslag på dansk til en virksomhed.\n\n"
            f"Opslaget skal formateres og struktureres, så det passer til den optimale layout og stil for {platform}.\n\n"
            f"Opslaget skal starte med teksten '(upload billede her)' for at indikere, at et billede kan tilføjes senere.\n\n"
            f"Inkludér desuden et link til virksomhedens hjemmeside eller sociale medier: {website_link}. "
            f"Brug en formulering som 'gå til {website_link}' i stedet for 'klik på linket'.\n\n"
            f"**Type:** {annonce_type}\n\n"
            f"**Vision:** {vision}\n\n"
            f"**Virksomhedsbeskrivelse:** {company_desc}\n\n"
            f"**Opslagslængde:** {post_length}\n\n"
            "Opslaget skal være professionelt, relevant for målgruppen og optimeret til den valgte platform. "
            "Sørg for at inkludere passende emojis, ikoner og visuelle elementer, hvor det er relevant, for at gøre opslaget ekstra engagerende."
        )

        try:
            response = openai.chat.completions.create(
                model="gpt-4",  # Or "gpt-3.5-turbo"
                messages=[
                    {
                        "role": "system",
                        "content": "Du er en assistent, der skaber engagerende, visuelt tiltalende og platformoptimerede opslag til sociale medier."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7
            )
            generated_post = response.choices[0].message.content
        except Exception as e:
            generated_post = f"Der opstod en fejl: {str(e)}"
    
    return render_template("index2.html", generated_post=generated_post, selected_platform=selected_platform, website_link=website_link, user_brands=user_brands)

@app2_bp.route("/improve", methods=["POST"])
def improve():
    original_post = request.form.get("original_post")
    improvement_instructions = request.form.get("improve_prompt", "").strip()
    platform = request.form.get("platform", "Generic")
    
    prompt = f"Forbedr følgende sociale medie opslag, så det bliver endnu mere engagerende, visuelt tiltalende og professionelt:\n\n{original_post}\n\n"
    if improvement_instructions:
        prompt += f"Yderligere forbedringsinstruktioner: {improvement_instructions}"
    
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",  # Or "gpt-3.5-turbo"
            messages=[
                {
                    "role": "system",
                    "content": "Du er en assistent, der forbedrer og optimerer sociale medie opslag."
                },
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        improved_post = response.choices[0].message.content
    except Exception as e:
        improved_post = f"Der opstod en fejl under forbedringen: {str(e)}"
    
    return render_template("index2.html", generated_post=improved_post, selected_platform=platform)
