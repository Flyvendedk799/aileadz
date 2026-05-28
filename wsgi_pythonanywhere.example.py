"""
PythonAnywhere WSGI entrypoint template.

Copy to your PA Web → WSGI configuration file and fill in secrets + project path.
Do not commit real API keys to git.
"""
import os
import sys

# --- Secrets (required) ---
os.environ["OPENAI_API_KEY"] = "YOUR_OPENAI_API_KEY"
os.environ["UNSPLASH_ACCESS_KEY"] = "YOUR_UNSPLASH_ACCESS_KEY"

# --- AI production tuning (recommended values from .env.example) ---
_AI_ENV = {
    "AI_MAIN_MODEL": "gpt-4o",
    "AI_FAST_MODEL": "gpt-4o-mini",
    "AI_RUNTIME": "responses",
    "AI_MAX_INPUT_TOKENS": "20000",
    "AI_TPM_BUDGET": "26000",
    "AI_MAX_OUTPUT_TOKENS": "450",
    "AI_MAX_TOOL_ITERATIONS": "3",
    "AI_RATE_LIMIT_RETRY_SECONDS": "2.5",
    "AI_RATE_LIMIT_COOLDOWN_SECONDS": "90",
    "AI_OPENAI_TIMEOUT_SECONDS": "40",
    "AI_FEW_SHOT": "compact",
    "AI_SUMMARY_MODE": "rules",
    "AI_TOOL_ROUTER_V2": "1",
    "AI_EMBEDDING_MODEL": "text-embedding-3-small",
    "AI_EMBEDDING_DIMENSIONS": "1024",
    "AI_RAG_CROSS_ENCODER": "auto",
    "AI_RAG_CROSS_ENCODER_MAX_CANDIDATES": "6",
    "AI_TRACE_SAMPLE_RATE": "1",
    "AI_WARMUP_ON_IMPORT": "1",
}
for _key, _value in _AI_ENV.items():
    os.environ.setdefault(_key, _value)

# uWSGI: Flask-MySQLdb needs threads
try:
    import uwsgi

    uwsgi.opt["enable-threads"] = True
except ImportError:
    pass

# Project root on PythonAnywhere
sys.path.insert(0, "/home/TobiasMastek/dashboard")

from run import create_app

application = create_app()
