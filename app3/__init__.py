import os
import json
from flask import Flask, Blueprint, render_template, request, redirect
import openai

app = Flask(__name__)

# Hardcoded OpenAI API-nøgle (brug denne nøgle indtil videre)
openai.api_key = "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"

app3_bp = Blueprint('app3', __name__, template_folder='templates')

@app3_bp.route('/')
def index():
    return render_template('index3.html')

# Første side: Periodevælger
@app.route('/')
def select_period():
    return render_template('select_period.html')

# Dataindtastningsside – vises efter periodevalg
@app.route('/data')
def data_input():
    period = request.args.get("period")
    if not period:
        return redirect('/')
    return render_template('index3.html', period=period, insights=[], chart_data=None)

# Hjælpefunktion: Hvis kanalen er aktiv, samles de forudbestemte KPI‑værdier
def get_channel_data(channel, kpi_fields):
    active = request.form.get(f"{channel}_active")
    if active == "on":
        lines = []
        for field, label in kpi_fields:
            value = request.form.get(field, "").strip()
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines) if lines else "Ingen data indsendt"
    else:
        return "Ingen data indsendt"

@app.route('/analyze', methods=['POST'])
def analyze():
    period = request.form.get("period", "")
    
    # Definer forudbestemte KPI'er for hver kanal
    website_kpis = [
        ("website_visits", "Antal Besøg"),
        ("website_unique", "Unikke Besøg"),
        ("website_session", "Session Varighed"),
        ("website_bounce", "Bounce Rate"),
        ("website_conversions", "Konverteringer")
    ]
    social_media_kpis = [
        ("social_media_impressions", "Visninger"),
        ("social_media_new_followers", "Nye Følgere"),
        ("social_media_engagement", "Engagement"),
        ("social_media_clicks", "Klik"),
        ("social_media_conversions", "Konverteringer")
    ]
    email_kpis = [
        ("email_sent", "Udsendte E-mails"),
        ("email_open_rate", "Åbningsrate"),
        ("email_click_rate", "Klikrate"),
        ("email_conversions", "Konverteringer")
    ]
    paid_kpis = [
        ("paid_impressions", "Visninger"),
        ("paid_clicks", "Klik"),
        ("paid_cpc", "CPC"),
        ("paid_conversions", "Konverteringer")
    ]
    
    # Saml data for hver kanal
    website_data = get_channel_data("website", website_kpis)
    social_media_data = get_channel_data("social_media", social_media_kpis)
    email_data = get_channel_data("email", email_kpis)
    paid_data = get_channel_data("paid", paid_kpis)
    
    raw_data = (
        f"Periode: {period}\n\n"
        f"Website:\n{website_data}\n\n"
        f"Sociale Medier:\n{social_media_data}\n\n"
        f"E-mail Marketing:\n{email_data}\n\n"
        f"Betalt Søgeannoncering:\n{paid_data}"
    )
    
    # Forbedret prompt med fokus på handling, opmærksomhedsvækkende og marketing-indsigter
    prompt = (
        "Du er en ekspert inden for forretningsanalyse og digital marketing. "
        "Analyser de følgende digitale kanaldata og giv konkrete, handlingsorienterede anbefalinger opdelt i flere kategorier. "
        "For hver kategori skal du levere et 'emne' (kort og slagkraftigt), et kort 'resumé' og yderligere 'detaljer' der forklarer, hvordan tiltagene kan øge ROI, reducere bounce rate og forbedre brugerengagement. "
        "Giv mindst 5 forskellige emner, og for hver indsats skal du inkludere en konkret handlingsplan samt en forventet procentvis forbedring (f.eks. 'Op til 25% forbedring'), hvis alle anbefalinger implementeres fuldt ud. "
        "Hvert indsigtsobjekt skal have felterne: 'emne', 'resumé', 'detaljer' og 'forbedring' (et tal). "
        "Skriv også et DiagramData JSON-objekt med to nøgler: 'etiketter' (liste med metriknavne) og 'værdier' (liste med numeriske værdier). "
        "JSON-objektet skal være gyldigt, uden ekstra tekst eller markdown.\n\n"
        "Svar venligst i præcis følgende format:\n\n"
        "Indsigt:\n"
        "[\n"
        "  {\n"
        "    \"emne\": \"<kategori navn>\",\n"
        "    \"resumé\": \"<kort oversigt over anbefalingerne for denne kategori>\",\n"
        "    \"detaljer\": \"<udvidet forklaring inkl. handlingsplan og forventet forbedring>\",\n"
        "    \"forbedring\": <tal>\n"
        "  },\n"
        "  ... (flere objekter)\n"
        "]\n\n"
        "DiagramData:\n"
        "<gyldigt JSON-objekt med to nøgler: 'etiketter' (liste med metriknavne) og 'værdier' (liste med numeriske værdier)>\n\n"
        f"Data:\n{raw_data}"
    )
    
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Du er en ekspert inden for forretningsanalyse og digital marketing."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1000,
            temperature=0.7,
        )
        
        full_response = response.choices[0].message.content.strip()
        insights = []
        chart_data = None
        
        if "DiagramData:" in full_response:
            parts = full_response.split("DiagramData:")
            insights_part = parts[0].strip()
            diagram_part = parts[1].strip()
            if insights_part.startswith("Indsigt:"):
                insights_json_str = insights_part[len("Indsigt:"):].strip()
            else:
                insights_json_str = insights_part
            try:
                insights = json.loads(insights_json_str)
            except Exception as e:
                insights = [{
                    "emne": "Parsing Fejl",
                    "resumé": f"Fejl ved parsing af indsigt JSON: {e}",
                    "detaljer": ""
                }]
            try:
                chart_data = json.loads(diagram_part)
            except Exception as e:
                chart_data = {"etiketter": ["Metric 1", "Metric 2", "Metric 3"], "værdier": [10, 20, 30]}
        else:
            insights = [{
                "emne": "Ugyldigt Format",
                "resumé": full_response,
                "detaljer": ""
            }]
            chart_data = {"etiketter": ["Metric 1", "Metric 2", "Metric 3"], "værdier": [10, 20, 30]}
    except Exception as e:
        insights = [{
            "emne": "Fejl",
            "resumé": f"Fejl ved generering af anbefalinger: {e}",
            "detaljer": ""
        }]
        chart_data = {"etiketter": ["Metric 1", "Metric 2", "Metric 3"], "værdier": [10, 20, 30]}
    
    if not chart_data.get("etiketter"):
        chart_data = {"etiketter": ["Metric 1", "Metric 2", "Metric 3"], "værdier": [10, 20, 30]}
    
    return render_template('index.html', period=period, insights=insights, chart_data=chart_data)

if __name__ == '__main__':
    app.run(debug=True)
