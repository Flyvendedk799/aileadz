import re

with open("app1/__init__.py", "r", encoding="utf-8") as f:
    text = f.read()

# Replace the giant ask() function with a clean route that yields our SSE agent
pattern_ask = r"@app1_bp\.route\(\"/ask\", methods=\[\"POST\"\]\)\s*\ndef ask\(\):.*?return jsonify[^}]*?500"
replacement_ask = """
from app1.agent import handle_agentic_ask

@app1_bp.route("/ask", methods=["POST"])
def ask():
    try:
        user_query = request.json.get("query", "").strip()
        if not user_query:
            return jsonify({"answers": [{"type": "text", "content": "Skriv venligst et spørgsmål."}]}), 400
            
        return handle_agentic_ask(user_query, session)
        
    except Exception as ex:
        print(f"Unexpected error: {ex}")
        return jsonify({"answers": [
            {"type": "text", "content": "Der opstod en uventet fejl. Prøv venligst igen."}
        ]}), 500
"""
text = re.sub(pattern_ask, replacement_ask, text, flags=re.DOTALL)

with open('app1/__init__.py', 'w', encoding='utf-8') as f:
    f.write(text)
