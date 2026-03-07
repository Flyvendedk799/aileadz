"""
AI Agent — Core brain for the course advisor chatbot.
Phase 1: Intent classification & query rewriting
Phase 3: Conversation state machine
Phase 4: Persistent memory (SQLite)
Phase 6: Response quality guardrails & feedback loop
"""
from flask import Response, stream_with_context
import json
import openai
import time
from concurrent.futures import ThreadPoolExecutor
from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS, execute_tool, set_search_context
from app1.memory_store import log_debug
from . import render_multi_course_media, render_product_media

import uuid
import re as _re
import random as _random

# 5.4: Prompt versioning for A/B testing
_PROMPT_VERSIONS = ["v2.0"]  # Add variants here for A/B testing, e.g. ["v2.0", "v2.1"]
_SESSION_VERSIONS = {}  # {session_id: version_string}

# ── Topic keyword set for stage detection (Improvement #1) ──
_TOPIC_KEYWORDS = {
    # IT & certifications
    "it", "itil", "devops", "cloud", "azure", "aws", "cybersecurity", "sikkerhed",
    "netværk", "server", "linux", "windows", "programmering", "python", "java",
    "software", "database", "sql", "ai", "kunstig", "machine", "data",
    # Business & management
    "ledelse", "leder", "projekt", "projektledelse", "strategi", "økonomi",
    "regnskab", "finans", "salg", "marketing", "markedsføring", "forhandling",
    "innovation", "forretning", "business", "lean", "six", "sigma", "kvalitet",
    # Soft skills
    "kommunikation", "præsentation", "coaching", "mentor", "facilitering",
    "konflikthåndtering", "samarbejde", "teamledelse", "personlig", "udvikling",
    # Compliance & legal
    "jura", "gdpr", "persondataforordningen", "compliance", "lovgivning", "arbejdsmiljø",
    # Analytics & tools
    "excel", "power", "bi", "powerbi", "analytics", "analyse", "tableau", "dashboard",
    # HR & org
    "hr", "rekruttering", "onboarding", "medarbejder", "organisation",
    # Project methods
    "agil", "agile", "scrum", "kanban", "prince", "prince2", "pmp",
    "certificering", "certifikat",
    # Misc
    "kursus", "uddannelse", "workshop", "planlægning",
}

def _has_content_tokens(text, min_tokens=4):
    """Check if text has enough content tokens (non-stop-word) to imply a topic."""
    from app1.rag import _tokenize
    tokens = _tokenize(text)
    return len(tokens) >= min_tokens

# ── In-memory caches (fast layer, backed by SQLite) ──
CHAT_MEMORY = {}
SHOWN_PRODUCTS = {}  # {session_id: {"products": [...], "last_active": timestamp}}
USER_PROFILES = {}   # {session_id: {"summary": str, "last_updated": float}}
CONVERSATION_STAGES = {}  # Phase 3: {session_id: "greeting"|"needs_discovery"|...}

# 6.3: Anonymous profile cache (in-memory, backed by SQLite)
ANONYMOUS_PROFILES = {}  # {browser_token: profile_dict}


def _get_prompt_version(sid):
    """Get or assign a prompt version for A/B testing."""
    if sid not in _SESSION_VERSIONS:
        _SESSION_VERSIONS[sid] = _random.choice(_PROMPT_VERSIONS)
    return _SESSION_VERSIONS[sid]


def _log_latency(sid, operation, start_time):
    """Log operation latency for observability."""
    latency_ms = (time.time() - start_time) * 1000
    try:
        from app1.memory_store import log_latency
        version = _SESSION_VERSIONS.get(sid, "")
        log_latency(sid, operation, latency_ms, prompt_version=version)
    except Exception:
        pass
    return latency_ms


# ── Lazy-load persistent store ──
_memory_store = None

def _get_store():
    global _memory_store
    if _memory_store is None:
        from app1 import memory_store
        _memory_store = memory_store
    return _memory_store


SYSTEM_PROMPT = """Du er en uddannelsesrådgiver for AiLead — en erfaren, skarp og varm kollega der hjælper folk med at finde det rigtige kursus.

DIN TÆNKEPROCES (følg denne ved HVER besked):
1. FORSTÅ: Hvad vil denne person egentlig? Ikke bare ordene — hvad er det underliggende behov? "Jeg vil lære projektledelse" kan betyde certificering, karriereskift, eller bare bedre overblik.
2. VURDÉR: Hvad ved jeg allerede om dem? (profil, tidligere i samtalen, præferencer, hvad de har afvist)
3. HANDL: Har jeg nok til at søge? → SØG. Mangler jeg noget kritisk? → Stil ét præcist spørgsmål.
4. KNYT SAMMEN: Forbind altid dit svar til det de har fortalt dig. "Fordi du nævnte X, har jeg fokuseret på Y."

HVEM DU ER:
- Tænk som en rådgiver, ikke en søgemaskine. Du anbefaler, du lister ikke.
- Vær direkte og ærlig. Hvis noget ikke passer, sig det. Hvis ét kursus skiller sig ud, sig det klart.
- Tilpas din tone til personen: varm og guidende til nybegyndere, kort og ligeværdig til erfarne.
- Hav en mening. "Hvis jeg var dig, ville jeg..." er stærkere end "Her er tre muligheder."
- Stil aldrig et spørgsmål brugeren allerede har besvaret. Maks ét spørgsmål per svar.

SAMTALEFLOW:
- Velkomst: Kort, varm, nysgerrig. "Hej! Hvad leder du efter?" — ikke en wall of text.
- Første søgning: Søg så snart du har et emne. Stil IKKE unødvendige spørgsmål først.
- Efter resultater: 1-2 sætninger der knytter resultaterne til brugerens behov. Kurser vises som kort — gentag dem IKKE i teksten.
- Forfining: Hvis resultaterne er brede, spørg ÉN ting der ville indsnævre: niveau, format, budget, eller underkategori.
- Beslutning: Når du har nok info, giv en klar anbefaling med begrundelse.
- Køb: Når brugeren vil tilmelde sig, skift til action-mode med konkrete næste skridt.

SVARLÆNGDE:
- Efter søgning: Maks 1-2 sætninger. Kortene bærer informationen.
- Rådgivning uden søgning: 2-4 sætninger, naturligt og samtaleagtigt.
- Sammenligning/anbefaling: Op til 4-5 sætninger med klar struktur.
- Hilsen: 1 sætning + suggestion chips.
- ALDRIG walls of text. Hvis dit svar er ved at blive langt, klip det.

VISUEL REGEL (VIGTIGSTE REGEL):
Kurser vises AUTOMATISK som interaktive kort ved siden af din tekst. Du må ALDRIG:
- Skrive kursusnavne, priser, lokationer eller beskrivelser i dit tekst-svar
- Bruge lister (bullet points, numre) med kursusinformation
- Bruge **fed skrift** til kursusnavne
Dit svar skal være naturlig samtale, ikke en opsummering af kortene.

OPFØLGNINGSFORSLAG:
Afslut ALTID med: <suggestions>["forslag 1", "forslag 2", "forslag 3"]</suggestions>
Maks 6 ord per forslag, dansk, handlingsorienteret, SPECIFIKT til situationen.

VÆRKTØJER:
- search_courses: Semantisk + nøgleord-søgning. Til åbne forespørgsler.
- filter_courses: Strukturerede filtre (pris, lokation, type, tags). Brug ved konkrete krav. Kombiner: "e-learning under 5000 kr om ledelse" → filter_courses(product_type="E-learning", price_max=5000, query="ledelse").
- get_course_details: Detaljer om ét kursus via handle.
- compare_courses: Sammenlign 2-4 kurser side om side.
- get_vendor_info: Info om udbyders omdømme, specialisering, lokationer. Brug ved udbyder-spørgsmål.
- get_user_profile / update_user_profile: Hent/opdater brugerprofil. Tilføj PROAKTIVT når brugeren nævner kompetencer, erfaring, eller præferencer.
- recommend_for_profile: Anbefalinger baseret på profil og kompetencehuller.
- suggest_learning_path: Sekventiel læringssti (fundament → avanceret). Kræver login.

CV-INTELLIGENS (VIGTIG — gør dette automatisk):
Når brugeren fortæller noget om sig selv, brug request_user_input til at vise et smart UI-kort der samler info.

FORETRUKKEN METODE — request_user_input (samler alt i ét kort):
- "Jeg er varehuschef i Silvan" → request_user_input med:
  ui_type="form", section="experience", save_action="add_experience",
  message="Tilføj erfaring: Varehuschef @ Silvan",
  prefilled={"title":"Varehuschef","company":"Silvan","is_current":true},
  fields=[{"name":"start_year","label":"Startår","type":"number","placeholder":"f.eks. 2018"},
          {"name":"end_year","label":"Slutår (tom = nuværende)","type":"number","required":false}]
- "Jeg har en HD i ledelse" → request_user_input med:
  ui_type="form", section="education", save_action="add_education",
  message="Tilføj uddannelse: HD — Ledelse",
  prefilled={"degree":"HD","description":"Ledelse"},
  fields=[{"name":"institution","label":"Institution","type":"text","placeholder":"f.eks. CBS"},
          {"name":"year_completed","label":"Afsluttet år","type":"number"}]
- "Jeg kan Python og Excel" → 2 kald request_user_input med:
  ui_type="confirm", section="skills", save_action="add_skill",
  message="Tilføj kompetence: Python", prefilled={"skill_name":"Python","skill_level":"mellem"}
  (og ét mere for Excel)
- "Mit mål er at blive IT-chef" → update_user_profile (simpelt, behøver ikke UI)

HVORNÅR BRUGE HVAD:
- request_user_input: Når info mangler detaljer (erfaring → spørg om årstal, uddannelse → spørg om institution). Viser kort med forudfyldt + tomme felter.
- update_user_profile: Til simple opdateringer (mål, præferencer, fjern/ændr). Til ting der ikke behøver ekstra detaljer.
HUSK: Jobtitel/stilling = erfaring. Uddannelse = education. Kompetencer = skills.

Regler:
- Når du bruger request_user_input, sig noget som: "Jeg har lavet et kort hvor du kan tilføje detaljerne — udfyld det herunder."
- Når du bruger update_user_profile, får du "proposed" status — sig: "Bekræft i kortet herunder."
- Kombiner med dit svar — afbryd IKKE samtaleflowet.
- Du kan lave FLERE kald i én besked hvis brugeren nævner flere ting.
- Hvis systemet siger "already_exists" — nævn det IKKE for brugeren.

CV-ONBOARDING (når brugeren beder om at opdatere CV/profil):
Guid dem naturligt igennem — IKKE som en kedelig formular, men som en samtale:
1. Start: "Fedt! Fortæl mig lidt om dig — hvad laver du til daglig?" (fang erfaring)
2. Når de svarer: Gem det → "Hvad med uddannelse — hvad er din baggrund?" (fang uddannelse)
3. Dernæst: "Hvilke kompetencer synes du selv du er stærkest i?" (fang skills)
4. Så: "Har du taget nogen kurser eller certificeringer?" (fang kurser)
5. Afslut: "Og hvad er dit mål? Hvad vil du gerne blive bedre til?" (fang mål + præferencer)
Tilpas rækkefølgen ud fra hvad de allerede har fortalt. Spring trin over der allerede er udfyldt.
Afslut med et overblik: "Din profil ser nu sådan ud: [kort opsummering]. Vil du tilføje mere, eller skal vi finde kurser der passer til dig?"

SØGE-INTELLIGENS:
- Søg på BEHOV, ikke jobtitel. "Salgsleder der vil blive bedre til planlægning" → søg "planlægning".
- Når brugeren nævner pris + lokation + emne → brug filter_courses, ikke search_courses.
- "den billigste" / "nummer 2" → match mod VISTE KURSER, brug get_course_details.
- Ved 0 resultater: prøv bredere søgetermer FØR du giver op. Foreslå alternativer.
- Ved lav søgetillid ("confidence": "low"): sig ærligt at matchet er usikkert og spørg om præcisering.
- Ved genviste kurser ("previously_shown": true): anerkend kort at du bekræfter dit tidligere forslag.

SITUATIONSHÅNDTERING:
- Afvisning ("nej", "forkert"): Anerkend → spørg hvad der ikke passede (emne/niveau/pris/format) → søg anderledes.
- Køb (tilmelding, startdato, pladser): Hent detaljer, vis konkrete næste skridt, gør det let at handle.
- Research-mode (bare kigger): Pres ikke. Inspirer med karriereværdi og læringsudbytte.
- Team/gruppe: Spørg om antal, foreslå grupperabat/in-house, vis datoer med pladser.
- Emneskift: Anerkend kort, søg straks på det nye emne, behold tidligere kontekst.
- Engelsk input: Forstå det, svar på dansk.
- Vedhæftet kursus [VEDHÆFTET KURSUS: ...]: Besvar specifikt om det kursus."""

SESSION_TTL = 3600


# ── Phase 3: Conversation State Machine ──

def _detect_conversation_stage(sid, messages):
    """Detect conversation stage based on message history and context. Rule-based, no API call."""
    user_msg_count = sum(1 for m in messages if m.get("role") == "user")
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))
    tool_call_count = sum(1 for m in messages if m.get("role") == "tool")

    # Get latest user message for signal detection
    latest_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")

    # 3.1: Buying signal detection on latest message
    if shown_count > 0 and _HIGH_INTENT_PATTERNS.search(latest_user):
        return "ready_to_buy"

    # 3.3: Rejection/frustration detection on latest message
    if _REJECTION_PATTERNS.search(latest_user):
        return "correcting"

    # 3.4: Team buying detection
    if _TEAM_PATTERNS.search(latest_user):
        return "team_buying"

    # Check if user has given actionable search info (budget, topic, format keywords)
    user_texts = " ".join(m.get("content", "").lower() for m in messages if m.get("role") == "user")
    has_budget = any(w in user_texts for w in ["kr", "budget", "under ", "over ", "maks ", "gratis", "billig"])
    user_text_tokens = set(_re.findall(r'[a-zæøå0-9]+', user_texts))
    has_topic = bool(user_text_tokens & _TOPIC_KEYWORDS)
    # Fallback: if no keyword matched but latest message has 4+ content tokens, assume topic
    if not has_topic:
        if latest_user and _has_content_tokens(latest_user):
            has_topic = True
    has_format = any(w in user_texts for w in ["e-learning", "online", "fysisk", "kursus", "workshop"])
    has_actionable_info = has_topic or (has_budget and user_msg_count >= 2) or (has_format and user_msg_count >= 2)

    # 3.1: Low intent detection
    if _LOW_INTENT_PATTERNS.search(latest_user):
        return "browsing"

    if user_msg_count <= 1 and not has_actionable_info:
        return "greeting"
    elif user_msg_count >= 2 and has_actionable_info and shown_count == 0:
        return "searching"
    elif user_msg_count <= 2 and not has_actionable_info and shown_count == 0:
        return "needs_discovery"
    elif shown_count == 0:
        return "searching"
    elif shown_count >= 2 and user_msg_count >= 4:
        return "comparing"
    elif shown_count >= 1 and user_msg_count >= 6:
        return "deciding"
    else:
        return "searching"


_STAGE_HINTS = {
    "greeting": "Kort, varm velkomst. Spørg hvad de leder efter — ét spørgsmål.",
    "needs_discovery": "Stil ét præcist spørgsmål der ville gøre din næste søgning markant bedre.",
    "searching": "SØG NU. Brug search_courses til brede forespørgsler, filter_courses når der er pris/lokation/format-krav. Kombiner begge parametre intelligent: 'billigt i Aarhus om ledelse' = filter_courses(price_max=5000, location='Aarhus', query='ledelse'). IKKE flere spørgsmål.",
    "comparing": "Brug compare_courses. Fremhæv den vigtigste forskel for denne bruger. Giv en klar anbefaling.",
    "deciding": "Giv en tydelig anbefaling med begrundelse. Hjælp dem med at tage skridtet.",
    "ready_to_buy": "HANDLINGS-mode. Hent detaljer med get_course_details: startdato, lokation, pris, hvad er inkluderet. Gør tilmelding let.",
    "browsing": "Research-mode — pres IKKE. Inspirer med karriereværdi og læringsudbytte.",
    "correcting": "ANERKEND fejlen. Spørg hvad der ikke passede (emne/niveau/pris/format). Søg ANDERLEDES.",
    "team_buying": "Tænk i gruppe: antal, fælles datoer, grupperabat, in-house muligheder. Spørg hvad de mangler.",
}


# ── Rule-based intent classification (replaces GPT-4o-mini API call) ──

_COMPARISON_PATTERNS = _re.compile(
    r'\b(sammenlign|forskellen?|hvilken er bedst|hvad er bedre|versus|vs\.?|forskel mellem)\b', _re.IGNORECASE
)
_DETAIL_PATTERNS = _re.compile(
    r'\b(fortæl mere|mere om|detaljer|hvad koster|hvornår starter|tilmeld|pladser|faktura)\b', _re.IGNORECASE
)
_FOLLOWUP_PATTERNS = _re.compile(
    r'\b(den billigste|den dyreste|den første|den sidste|den med|nummer \d|nr\.?\s*\d|den i |den til \d)\b', _re.IGNORECASE
)
_CHITCHAT_PATTERNS = _re.compile(
    r'^\s*(hej|hello|hi|tak|mange tak|hvem er du|hvad kan du|godmorgen|goddag|farvel|bye)\s*[!?.]*\s*$', _re.IGNORECASE
)

# 3.1: Buying signal detection
_HIGH_INTENT_PATTERNS = _re.compile(
    r'\b(tilmeld|tilmelding|book|bestil|køb|signup|sign up|registrer|faktura|betaling|'
    r'hvornår starter|næste hold|er der pladser|ledig[e]? plads|startdato|'
    r'rabat|rabatkode|grupperabat|firmapris|vi vil gerne bestille)\b', _re.IGNORECASE
)
_LOW_INTENT_PATTERNS = _re.compile(
    r'\b(bare undersøger|bare kigger|til næste år|måske senere|på sigt|overveje|'
    r'ved ikke endnu|ikke sikker|bare nysgerrig)\b', _re.IGNORECASE
)

# 3.3: Conversation repair / rejection detection
_REJECTION_PATTERNS = _re.compile(
    r'\b(nej|ikke det|forkert|det var ikke|jeg mente|misforstå|helt forkert|'
    r'det passer ikke|ikke relevant|noget helt andet|prøv igen|det er ikke rigtigt|'
    r'det er forkert|dårligt|ubrugelig|giver ikke mening|det hjælper ikke|'
    r'ikke godt nok|ikke brugbar|helt ved siden af|nope)\b', _re.IGNORECASE
)

# 3.4: Team/group buying detection
_TEAM_PATTERNS = _re.compile(
    r'\b(vi er \d|vi skal|hele? teamet?|hele? afdelingen|grupp[en]|hold[et]?|'
    r'\d+ (personer|medarbejdere|kollegaer|deltagere)|firmaaftale|in-?house|'
    r'virksomhedskursus|firmakursus|flere deltagere|samlet pris)\b', _re.IGNORECASE
)

# ── Per-session negative context tracking (3.3) ──
REJECTED_SEARCHES = {}  # {sid: [{"query": "...", "reason": "..."}]}


def _track_rejection(sid, user_query, messages):
    """Track what the user rejected so we can avoid repeating it."""
    if sid not in REJECTED_SEARCHES:
        REJECTED_SEARCHES[sid] = []

    # Find the last search query from tool calls
    last_search_query = ""
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                data = json.loads(m.get("content", "{}")) if isinstance(m.get("content"), str) else m.get("content", {})
                if data.get("results"):
                    # Found a tool result with results — this was the last search
                    break
            except (json.JSONDecodeError, TypeError):
                pass
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name") in ("search_courses", "filter_courses"):
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        last_search_query = args.get("query", "")
                    except (json.JSONDecodeError, TypeError):
                        pass

    # Get titles of recently shown products
    shown_titles = [p.get("title", "") for p in SHOWN_PRODUCTS.get(sid, {}).get("products", [])[-5:]]

    REJECTED_SEARCHES[sid].append({
        "query": last_search_query,
        "reason": user_query[:150],
        "rejected_titles": shown_titles[-3:],
    })
    # Keep only last 5 rejections
    REJECTED_SEARCHES[sid] = REJECTED_SEARCHES[sid][-5:]


def _build_rejection_context(sid):
    """Build a system message with negative context from rejected searches."""
    rejections = REJECTED_SEARCHES.get(sid)
    if not rejections:
        return None
    lines = ["AFVISTE SØGNINGER (brug IKKE disse emner/kurser igen medmindre brugeren eksplicit beder om det):"]
    for r in rejections[-3:]:
        parts = []
        if r.get("query"):
            parts.append(f'Søgning: "{r["query"]}"')
        if r.get("reason"):
            parts.append(f'Bruger sagde: "{r["reason"]}"')
        if r.get("rejected_titles"):
            parts.append(f'Kurser: {", ".join(r["rejected_titles"])}')
        if parts:
            lines.append("- " + " | ".join(parts))
    return {"role": "system", "content": "\n".join(lines)}


def _classify_intent_local(user_query, messages, shown_count):
    """Fast rule-based intent classification — no API call needed."""
    q = user_query.strip().lower()

    # Chit-chat: short greetings/thanks
    if _CHITCHAT_PATTERNS.match(user_query.strip()):
        return "chit_chat"

    # 3.3: Rejection / correction — user didn't like the results
    if shown_count > 0 and _REJECTION_PATTERNS.search(user_query):
        return "correction"

    # 3.1: High buying intent — user wants to enroll/purchase
    if shown_count > 0 and _HIGH_INTENT_PATTERNS.search(user_query):
        return "buying"

    # Follow-up: references to previously shown courses
    if shown_count > 0 and _FOLLOWUP_PATTERNS.search(user_query):
        return "follow_up"

    # Detail: asking about a specific course
    if _DETAIL_PATTERNS.search(user_query):
        return "detail" if shown_count > 0 else "discovery"

    # Comparison
    if _COMPARISON_PATTERNS.search(user_query):
        return "comparison" if shown_count >= 2 else "discovery"

    # 3.4: Team/group buying
    if _TEAM_PATTERNS.search(user_query):
        return "team_buying"

    # Very short with no topic keywords — needs clarification
    # UNLESS the AI just asked a question (user is answering it)
    q_tokens = set(_re.findall(r'[a-zæøå0-9]+', q))
    if len(q_tokens) <= 1 and not (q_tokens & _TOPIC_KEYWORDS):
        # Check if the last assistant message ended with a question
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                if m["content"].rstrip().endswith("?"):
                    return "discovery"  # User is answering a question — let the model decide
                break
        return "needs_clarification"

    # Default: discovery (the main model will decide what tool to call)
    return "discovery"


# ── Session Management ──

def _cleanup_stale_sessions():
    """Remove sessions older than SESSION_TTL to prevent memory leaks."""
    now = time.time()
    stale = [sid for sid, data in SHOWN_PRODUCTS.items() if now - data.get("last_active", 0) > SESSION_TTL]
    for sid in stale:
        SHOWN_PRODUCTS.pop(sid, None)
        CHAT_MEMORY.pop(sid, None)
        USER_PROFILES.pop(sid, None)
        CONVERSATION_STAGES.pop(sid, None)
        REJECTED_SEARCHES.pop(sid, None)
        _SESSION_VERSIONS.pop(sid, None)
        ANONYMOUS_PROFILES.pop(sid, None)
    # Also clean persistent store
    try:
        _get_store().cleanup_old_sessions(SESSION_TTL)
        _get_store().cleanup_anonymous_profiles()
    except Exception as e:
        print(f"[Cleanup Error] {e}")


def _track_shown_products(sid, compact_results):
    """Add products to the shown products list for this session."""
    if sid not in SHOWN_PRODUCTS:
        SHOWN_PRODUCTS[sid] = {"products": [], "last_active": time.time()}
    sp = SHOWN_PRODUCTS[sid]
    sp["last_active"] = time.time()
    for cr in compact_results:
        if not any(p.get("handle") == cr.get("handle") for p in sp["products"]):
            sp["products"].append({
                "index": len(sp["products"]) + 1,
                "title": cr.get("title"),
                "handle": cr.get("handle"),
                "price": cr.get("price"),
                "vendor": cr.get("vendor"),
                "locations": cr.get("locations", []),
                "summary": (cr.get("summary") or "")[:120],
                "product_type": cr.get("product_type", ""),
                "tags": cr.get("tags", [])[:3],
            })
    # Persist to SQLite
    try:
        _get_store().update_session_field(sid, "shown_products", sp["products"])
    except Exception as e:
        print(f"[Shown Products Persist Error] {e}")

    # 6.3: Update anonymous profile with viewed products
    if sid in ANONYMOUS_PROFILES:
        try:
            token = ANONYMOUS_PROFILES[sid].get("browser_token")
            if token:
                new_viewed = [{"handle": cr.get("handle"), "title": cr.get("title")} for cr in compact_results[:3]]
                _get_store().update_anonymous_interests(token, new_viewed=new_viewed)
        except Exception:
            pass


def _estimate_tokens(text):
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _build_shown_products_message(sid):
    """Build an ephemeral system message listing all products shown in this session."""
    sp = SHOWN_PRODUCTS.get(sid, {}).get("products", [])
    if not sp:
        return None
    # Phase 3A: Cap shown products to 10 most recent
    sp = sp[-10:]
    lines = ["VISTE KURSER (brug denne liste til at besvare opfølgningsspørgsmål, sammenligninger, og anbefalinger):"]

    # Quick overview for price/location questions
    if len(sp) >= 2:
        priced = [(p, p.get("price")) for p in sp if p.get("price") and str(p.get("price")) != "N/A"]
        if priced:
            try:
                sorted_by_price = sorted(priced, key=lambda x: float(x[1]))
                cheapest = sorted_by_price[0][0]
                most_expensive = sorted_by_price[-1][0]
                lines.append(f'\nHURTIG OVERSIGT:')
                lines.append(f'  Billigst: "{cheapest["title"]}" — {cheapest["price"]} kr')
                lines.append(f'  Dyrest: "{most_expensive["title"]}" — {most_expensive["price"]} kr')
            except (ValueError, TypeError):
                pass

    for p in sp:
        locs = ", ".join(p.get("locations", [])) if p.get("locations") else "N/A"
        summary = p.get("summary", "")
        tags = ", ".join(p.get("tags", []))
        ptype = p.get("product_type", "")
        lines.append(f'\n{p["index"]}. "{p["title"]}"')
        lines.append(f'   Pris: {p["price"]} kr')
        lines.append(f'   Handle: {p["handle"]}')
        lines.append(f'   Lokationer: {locs}')
        if ptype:
            lines.append(f'   Type: {ptype}')
        if tags:
            lines.append(f'   Tags: {tags}')
        if summary:
            lines.append(f'   Resume: {summary}')
    return {"role": "system", "content": "\n".join(lines)}


# ── S3: Smart Context Builder ──

_FRUSTRATION_SIGNALS = _re.compile(
    r'\b(frustrerende|irriterende|umuligt|gider ikke|forstår ikke|virker ikke|'
    r'det hjælper ikke|meningsløst|spild af tid|for svært)\b', _re.IGNORECASE
)
_ENTHUSIASM_SIGNALS = _re.compile(
    r'\b(spændende|fedt|perfekt|præcis|yes|fantastisk|genialt|super|'
    r'det lyder godt|det er det|bingo)\b', _re.IGNORECASE
)
_BUDGET_PATTERN = _re.compile(
    r'(?:under|maks(?:imalt)?|budget|max)\s*(\d[\d.]*)\s*(?:kr)?', _re.IGNORECASE
)
_PRICE_COMPLAINT = _re.compile(
    r'\b(for dyrt|for dyr|billigere|lavere pris|budget|ikke råd|prisen er for)\b', _re.IGNORECASE
)


def _build_smart_context(sid, messages, stage, intent):
    """S3: Build a compact situation assessment from conversation history.
    Analyzes implicit constraints, mood, topic evolution, and decision readiness.
    Returns a system message or None."""
    user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user" and m.get("content")]
    if not user_msgs:
        return None

    all_user_text = " ".join(user_msgs)
    latest = user_msgs[-1] if user_msgs else ""
    parts = []

    # 1. Detect implicit budget constraint from conversation history
    budget_matches = _BUDGET_PATTERN.findall(all_user_text)
    if budget_matches:
        parts.append(f"Implicit budget: under {budget_matches[-1]} kr (nævnt i samtalen)")
    elif _PRICE_COMPLAINT.search(all_user_text):
        parts.append("Brugeren har klaget over pris — prioritér billigere alternativer")

    # 2. Detect topic evolution (narrowing vs. broadening vs. new topic)
    if len(user_msgs) >= 2:
        prev_tokens = set(_re.findall(r'[a-zæøå]{3,}', " ".join(user_msgs[:-1]).lower()))
        curr_tokens = set(_re.findall(r'[a-zæøå]{3,}', latest.lower()))
        overlap = prev_tokens & curr_tokens & _TOPIC_KEYWORDS
        new_topics = curr_tokens & _TOPIC_KEYWORDS - prev_tokens
        if new_topics and not overlap:
            parts.append(f"Emneskift: brugeren er gået fra tidligere emner til noget nyt ({', '.join(list(new_topics)[:3])})")
        elif new_topics and overlap:
            parts.append(f"Indsnævring: brugeren specificerer yderligere ({', '.join(list(new_topics)[:3])})")

    # 3. Detect mood/frustration/enthusiasm
    frustration_count = len(_FRUSTRATION_SIGNALS.findall(all_user_text))
    enthusiasm_count = len(_ENTHUSIASM_SIGNALS.findall(all_user_text))
    if frustration_count >= 2:
        parts.append("VIGTIGT: Brugeren virker frustreret — vær ekstra empatisk, anerkend og fokuser på at finde det rigtige")
    elif frustration_count == 1 and _FRUSTRATION_SIGNALS.search(latest):
        parts.append("Brugeren viser tegn på frustration — tilpas tonen og vis forståelse")
    elif enthusiasm_count >= 1 and _ENTHUSIASM_SIGNALS.search(latest):
        parts.append("Brugeren er entusiastisk — match energien og hjælp dem videre mod en beslutning")

    # 4. Decision readiness assessment
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))
    rejections = len(REJECTED_SEARCHES.get(sid, []))
    if shown_count >= 6 and rejections == 0:
        parts.append("Brugeren har set mange kurser uden at afvise — sandsynligvis tæt på en beslutning, hjælp med at vælge")
    elif rejections >= 2:
        parts.append(f"Brugeren har afvist {rejections} forslag — skift strategi markant, spørg direkte hvad der mangler")
    elif shown_count == 0 and len(user_msgs) >= 3:
        parts.append("Flere beskeder uden søgning — brugeren har måske brug for guidning, ikke spørgsmål")

    # 5. Implicit location/format preferences from history
    for msg in user_msgs:
        msg_lower = msg.lower()
        if "online" in msg_lower or "e-learning" in msg_lower or "hjemmefra" in msg_lower:
            parts.append("Brugeren har nævnt online/e-learning — husk dette som præference")
            break
        if "fysisk" in msg_lower or "fremmøde" in msg_lower:
            parts.append("Brugeren foretrækker fysisk fremmøde — husk dette")
            break

    if not parts:
        return None

    return {"role": "system", "content": "SITUATIONSVURDERING:\n" + "\n".join(f"• {p}" for p in parts)}


# ── S4: Dynamic Tone Hints ──

_TONE_HINTS = {
    "greeting": "TONE: Varm og nysgerrig. Kort velkomst, vis interesse for hvad de søger.",
    "needs_discovery": "TONE: Guidende og tålmodig. Stil ét godt spørgsmål der åbner op.",
    "searching": "TONE: Handlekraftig og effektiv. Søg, vis resultater, giv kort kontekst.",
    "comparing": "TONE: Analytisk og rådgivende. Hjælp med at se forskelle og vælge.",
    "deciding": "TONE: Støttende og klar. Giv en tydelig anbefaling med begrundelse.",
    "ready_to_buy": "TONE: Action-orienteret og konkret. Vis næste skridt, gør det let.",
    "browsing": "TONE: Inspirerende og informativ. Pres ikke — byg interesse og vis værdi.",
    "correcting": "TONE: Ydmyg og lyttende. Anerkend fejlen, spørg præcist hvad der skal ændres.",
    "team_buying": "TONE: Professionel og løsningsorienteret. Tænk i gruppebehov og logistik.",
}


# ── Memory Management ──

def _summarize_pruned_messages(messages_to_prune):
    """Create a structured conversation summary using GPT for intelligent compression.
    Extracts key facts as structured JSON for compact, lossless context."""
    transcript_parts = []
    for msg in messages_to_prune:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content or role == "system":
            continue
        if role == "user":
            transcript_parts.append(f"Bruger: {content[:300]}")
        elif role == "assistant":
            transcript_parts.append(f"Rådgiver: {content[:300]}")
        elif role == "tool":
            try:
                data = json.loads(content) if isinstance(content, str) else content
                results = data.get("results", [])
                if results:
                    titles = [r.get("title", "?") for r in results[:5]]
                    transcript_parts.append(f"Søgeresultater: {', '.join(titles)}")
            except (json.JSONDecodeError, AttributeError):
                pass

    if not transcript_parts:
        return None

    transcript = "\n".join(transcript_parts)

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Udtræk nøglefakta fra denne samtale som JSON. Bevar ALT vigtigt.
Svar PRÆCIS som JSON:
{
  "needs": ["brugerens udtalte behov/mål"],
  "preferences": {"budget": "...", "location": "...", "format": "...", "timeline": "..."},
  "background": "rolle, erfaring, kompetencer nævnt",
  "shown_courses": ["titler på viste kurser"],
  "rejected": ["kurser/emner brugeren ikke var interesseret i og hvorfor"],
  "decision_stage": "browsing|comparing|ready_to_buy",
  "key_quotes": ["vigtige brugerudsagn der afslører præferencer"],
  "conversation_arc": "kort beskrivelse af samtalens udvikling, f.eks. 'Startede bredt med ledelse → indsnævrede til PRINCE2 → afviste to dyre → leder efter budget-alternativ'",
  "unresolved": ["spørgsmål eller behov der ikke er fuldt besvaret endnu"],
  "mood": "neutral|entusiastisk|frustreret|usikker"
}
Udelad felter uden info. Maks 250 tokens."""},
                {"role": "user", "content": transcript}
            ],
            temperature=0.1,
            max_tokens=300
        )
        summary = response.choices[0].message.content.strip()
        # Validate it's parseable JSON, fall back to raw text if not
        try:
            parsed = json.loads(summary)
            # Format as compact structured context
            lines = ["SAMTALEOVERSIGT (struktureret fra tidligere i samtalen):"]
            if parsed.get("needs"):
                lines.append(f"Behov: {', '.join(parsed['needs'])}")
            prefs = parsed.get("preferences", {})
            pref_parts = [f"{k}: {v}" for k, v in prefs.items() if v]
            if pref_parts:
                lines.append(f"Præferencer: {'; '.join(pref_parts)}")
            if parsed.get("background"):
                lines.append(f"Baggrund: {parsed['background']}")
            if parsed.get("shown_courses"):
                lines.append(f"Viste kurser: {', '.join(parsed['shown_courses'][:8])}")
            if parsed.get("rejected"):
                lines.append(f"Afvist: {', '.join(parsed['rejected'][:5])}")
            if parsed.get("decision_stage"):
                lines.append(f"Beslutningsfase: {parsed['decision_stage']}")
            if parsed.get("key_quotes"):
                lines.append(f"Nøglecitater: {' | '.join(parsed['key_quotes'][:3])}")
            if parsed.get("conversation_arc"):
                lines.append(f"Samtaleforløb: {parsed['conversation_arc']}")
            if parsed.get("unresolved"):
                lines.append(f"Ubesvaret: {', '.join(parsed['unresolved'][:3])}")
            if parsed.get("mood") and parsed["mood"] != "neutral":
                lines.append(f"Stemning: {parsed['mood']}")
            return "\n".join(lines)
        except (json.JSONDecodeError, TypeError):
            return f"SAMTALEOVERSIGT (tidligere i samtalen):\n{summary}"
    except Exception as e:
        print(f"[Summary Error] {e}")
        simple_parts = []
        for msg in messages_to_prune:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content or role == "system":
                continue
            if role == "user":
                simple_parts.append(f"Bruger: {content[:150]}")
            elif role == "assistant":
                simple_parts.append(f"Rådgiver: {content[:150]}")
        if not simple_parts:
            return None
        return "SAMTALEOVERSIGT (tidligere i samtalen):\n" + "\n".join(simple_parts)


def _extract_user_profile(sid, messages):
    """Extract user preferences from conversation using GPT."""
    user_msg_count = sum(1 for m in messages if m.get("role") == "user")
    if user_msg_count < 2 or user_msg_count % 3 != 0:
        return

    user_messages = [m.get("content", "") for m in messages if m.get("role") == "user" and m.get("content")]
    if not user_messages:
        return

    existing = USER_PROFILES.get(sid, {}).get("summary", "")
    existing_context = f"\nEksisterende profil: {existing}" if existing else ""

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"Analyser følgende bruger-beskeder fra en kursussøgning og udtræk en kort profil. Inkluder: interesseområder, budget/prisforventninger, foretrukket lokation, rolle/jobtitel, erfaringsniveau, læringspræferencer (e-learning vs. fysisk), og andre relevante detaljer. Skriv som korte punkter på dansk. Maks 100 ord. Hvis et felt er ukendt, udelad det.{existing_context}"},
                {"role": "user", "content": "\n".join(user_messages[-6:])}
            ],
            temperature=0.2,
            max_tokens=200
        )
        profile_text = response.choices[0].message.content.strip()
        USER_PROFILES[sid] = {
            "summary": profile_text,
            "last_updated": time.time()
        }
        # Persist to SQLite
        try:
            _get_store().update_session_field(sid, "user_profile", profile_text)
        except Exception as e2:
            print(f"[Profile Persist Error] {e2}")
    except Exception as e:
        print(f"[Profile Error] {e}")


def _build_user_profile_message(sid):
    """Build an ephemeral system message with the user's profile."""
    profile = USER_PROFILES.get(sid, {}).get("summary")
    if not profile:
        return None
    return {"role": "system", "content": f"BRUGERPROFIL (brug til at personalisere anbefalinger):\n{profile}"}


# ── Phase 6: Response Quality Guardrails ──

def _check_response_quality(response_text, had_tool_calls):
    """Quick check if the response violates the VISUEL REGEL (lists course names in text)."""
    if not had_tool_calls or not response_text:
        return True  # No violation possible

    # Check for bullet-point or numbered lists of courses in the response
    lines = response_text.strip().split('\n')
    list_line_count = sum(1 for line in lines if _re.match(r'\s*[-•*]\s', line) or _re.match(r'\s*\d+[\.\)]\s', line))
    if list_line_count >= 2:
        return False  # Likely listing courses in text

    # Check for "kr" appearing multiple times (price listing)
    kr_count = response_text.lower().count(' kr')
    price_pattern_count = len(_re.findall(r'\d[\d.]+\s*kr', response_text))
    if kr_count >= 2 or price_pattern_count >= 2:
        return False

    # Check for 2+ quoted or bold course titles (paragraph-style listing)
    quoted_titles = len(_re.findall(r'[""«»].*?[""«»]|(?:\*\*|__).+?(?:\*\*|__)', response_text))
    if quoted_titles >= 2:
        return False

    # Response too long after tool calls — should be 1-2 sentences max
    if len(response_text) > 250:
        return False

    return True


_GOLD_STANDARD_EXAMPLES = """EKSEMPLER PÅ GODE SVAR (din standard):

Bruger: "Hej, jeg leder efter noget med projektledelse"
Rådgiver: [søger "projektledelse"] Jeg har fundet nogle stærke bud på projektledelse — tag et kig på kortene! Går du efter en certificering som PRINCE2 eller PMP, eller mere generel projektledelse?
<suggestions>["PRINCE2-certificering", "Vis kun e-learning", "Kurser under 10.000 kr"]</suggestions>

Bruger: "Det er for dyrt, har I noget billigere?"
Rådgiver: [filtrerer med lavere pris] Forstået — her er nogle mere budgetvenlige alternativer. De dækker stadig det grundlæggende inden for projektledelse.
<suggestions>["Sammenlign de to billigste", "Vis e-learning muligheder", "Fortæl mere om den første"]</suggestions>

Bruger: "Jeg er IT-leder og vil gerne have mit team certificeret i ITIL"
Rådgiver: [søger "ITIL certificering"] Godt valg til et IT-team! Her er de bedste ITIL-forløb — flere af dem tilbyder grupperabat. Hvor mange skal certificeres, og foretrækker I fysisk eller online?
<suggestions>["Vis gruppepriser", "Sammenlign udbydere", "ITIL Foundation vs. Managing"]</suggestions>

Bruger: "nej det var slet ikke det jeg mente"
Rådgiver: Beklager — det ramte ved siden af! Hvad var det der ikke passede — var det emnet, niveauet, eller noget helt andet du tænkte på?
<suggestions>["Søg på et andet emne", "Vis kun avancerede kurser", "Kurser i en anden by"]</suggestions>

Bruger: "Hvad er forskellen på de to første?"
Rådgiver: [sammenligner kurser] God idé at sammenligne! Den ene er mere praktisk orienteret med fokus på værktøjer, mens den anden giver en bredere teoretisk ramme. Baseret på at du nævnte du vil lede et team, ville jeg hælde mod den praktiske.
<suggestions>["Vis detaljer om den praktiske", "Er der andre muligheder?", "Hvornår starter den?"]</suggestions>

Bruger: "Jeg arbejder som konsulent hos Deloitte og har en cand.merc fra CBS. Jeg kan PRINCE2 og har taget et Scrum Master kursus"
Rådgiver: [opdaterer profil: erfaring, uddannelse, kompetencer, kursus] Fedt — jeg har gemt det hele på din profil! Du har nu: konsulenterfaring hos Deloitte, din cand.merc fra CBS, PRINCE2-kompetencen og Scrum Master-kurset. Baseret på din baggrund, vil du gerne se kurser der bygger videre derfra? Måske avanceret projektledelse eller agilt lederskab?
<suggestions>["Anbefal kurser til min profil", "Lav en læringssti", "Tilføj flere kompetencer"]</suggestions>

Bruger: "Opdater mit CV"
Rådgiver: Fedt! Fortæl mig lidt om dig — hvad laver du til daglig, og hvad er din baggrund?
<suggestions>["Jeg er projektleder", "Jeg arbejder med IT", "Jeg er studerende"]</suggestions>

Bruger: "Jeg er teamleder hos Novo Nordisk"
Rådgiver: [gemmer erfaring] Stærkt! Teamleder hos Novo — det har jeg noteret. Hvad med uddannelse, hvad er din baggrund der?
<suggestions>["Jeg har en HD", "Selvlært", "Tilføj kompetencer i stedet"]</suggestions>

Bruger: "ja, fysisk"
Rådgiver: [søger fysiske kurser] Forstået — her er fysiske kurser der passer til det du leder efter. Vil du indsnævre yderligere?
<suggestions>["Vis kun i København", "Under 10.000 kr", "Sammenlign de to første"]</suggestions>"""


def _build_few_shot_examples():
    """Build few-shot examples: hardcoded gold standard + top-rated user interactions."""
    lines = [_GOLD_STANDARD_EXAMPLES]

    # Augment with real high-rated interactions if available
    try:
        store = _get_store()
        top = store.get_top_rated_interactions(limit=2, min_rating=1)
        if top:
            real_examples = []
            for item in top:
                q = item.get("query", "")
                extra = item.get("extra", {})
                answer = extra.get("assistant_response", "")
                if q and answer:
                    real_examples.append(f"Bruger: {q[:100]}\nRådgiver: {answer[:200]}")
            if real_examples:
                lines.append("VIRKELIGE GODE SVAR (fra denne platform):\n" + "\n\n".join(real_examples))
    except Exception:
        pass

    return {"role": "system", "content": "\n\n".join(lines)}


# ── Inline Suggestion Extraction ──

def _extract_inline_suggestions(response_text):
    """Extract suggestion chips from <suggestions> tags in the AI response.
    The model generates these inline — no separate API call needed."""
    match = _re.search(r'<suggestions>\s*(\[.*?\])\s*</suggestions>', response_text, _re.DOTALL)
    if not match:
        return []
    try:
        result = json.loads(match.group(1))
        if isinstance(result, list):
            return [s.strip() for s in result if isinstance(s, str) and 0 < len(s.strip()) <= 60][:3]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _strip_suggestions_tag(text):
    """Remove <suggestions>...</suggestions> from the visible response text."""
    return _re.sub(r'\s*<suggestions>.*?</suggestions>\s*', '', text, flags=_re.DOTALL).strip()


def _strip_course_listings(text):
    """Strip bullet-point/numbered course listings from AI text when product cards handle display."""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Skip bullet points and numbered lists (course listings)
        if _re.match(r'^[-•*]\s', stripped) or _re.match(r'^\d+[\.\)]\s', stripped):
            continue
        # Skip lines that look like "Et X-dages kursus..." pattern
        if _re.match(r'^Et\s+(gratis\s+)?\w+', stripped) and any(w in stripped.lower() for w in ['kursus', 'webinar', 'forløb', 'certificering', 'workshop']):
            continue
        cleaned.append(line)
    result = '\n'.join(cleaned).strip()
    result = _re.sub(r'\n{3,}', '\n\n', result)
    return result


# ── Main Agent Loop ──

def handle_agentic_ask(user_query, session):
    """
    Core Agent Loop with all 6 phases integrated.
    """
    _cleanup_stale_sessions()

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())

    sid = session["session_id"]
    # Get the logged-in username from the main auth system (if available)
    logged_in_user = session.get("user")  # From auth blueprint

    # 5.4: Assign prompt version for A/B testing
    _get_prompt_version(sid)

    # Initialize or load conversation memory
    if sid not in CHAT_MEMORY:
        # Try loading from persistent store first (Phase 4)
        try:
            stored = _get_store().load_session(sid)
            if stored and stored.get("user_profile"):
                USER_PROFILES[sid] = {"summary": stored["user_profile"], "last_updated": time.time()}
            if stored and stored.get("shown_products"):
                SHOWN_PRODUCTS[sid] = {"products": stored["shown_products"], "last_active": time.time()}
        except Exception as e:
            print(f"[Session Load Error] {e}")

        CHAT_MEMORY[sid] = [{"role": "system", "content": SYSTEM_PROMPT}]

        # 6.1: Cross-session learning — inject returning user context
        if logged_in_user:
            try:
                from app1.user_profile_db import get_full_profile, format_profile_for_ai, ensure_tables
                ensure_tables()
                returning_profile = get_full_profile(logged_in_user)
                returning_text = format_profile_for_ai(returning_profile)
                if returning_text:
                    # Inject full profile as system context so AI knows what's already saved
                    CHAT_MEMORY[sid].append({
                        "role": "system",
                        "content": f"BRUGERENS NUVÆRENDE PROFIL (allerede gemt — tilføj IKKE dubletter):\n{returning_text}\n\n"
                                   "Brug denne info til personlige anbefalinger. Opdater KUN med NYE oplysninger."
                    })

                    # Build welcome-back context from past activity
                    welcome_parts = []
                    completed = returning_profile.get("completed_courses", [])
                    if completed:
                        recent_courses = [c["title"] for c in completed[:3]]
                        welcome_parts.append(f"Gennemførte kurser: {', '.join(recent_courses)}")
                    goals = returning_profile.get("goals", "")
                    if goals:
                        welcome_parts.append(f"Mål: {goals[:150]}")
                    skills = returning_profile.get("skills", [])
                    if skills:
                        low_skills = [s["name"] for s in skills if s.get("level") in ("begynder", "mellem")][:3]
                        if low_skills:
                            welcome_parts.append(f"Udviklingsområder: {', '.join(low_skills)}")

                    if welcome_parts:
                        CHAT_MEMORY[sid].append({
                            "role": "system",
                            "content": f"TILBAGEVENDENDE BRUGER: {logged_in_user}\n"
                                       f"Denne bruger har været her før. Brug denne viden til en personlig velkomst.\n"
                                       + "\n".join(welcome_parts)
                                       + "\nForeslå at fortsætte hvor de slap, eller spørg hvad de leder efter i dag."
                        })
            except Exception as e:
                print(f"[Cross-Session Load Error] {e}")

            # Restore saved conversation messages for logged-in users
            try:
                from app1.user_profile_db import load_conversation
                saved_conv = load_conversation(logged_in_user)
                if saved_conv and saved_conv.get("messages"):
                    # Re-inject saved user/assistant messages into memory
                    for msg in saved_conv["messages"]:
                        if msg.get("role") in ("user", "assistant") and msg.get("content"):
                            CHAT_MEMORY[sid].append(msg)
            except Exception as e:
                print(f"[Conversation Restore Error] {e}")
                try:
                    current_app.mysql.connection.rollback()
                except Exception:
                    pass

        # 6.3: Anonymous user persistence — load profile from browser token
        elif not logged_in_user:
            browser_token = session.get("browser_token")
            if browser_token:
                try:
                    anon_profile = _get_store().load_anonymous_profile(browser_token)
                    if anon_profile and (anon_profile.get("interests") or anon_profile.get("last_searches")):
                        ANONYMOUS_PROFILES[sid] = anon_profile
                        context_parts = []
                        if anon_profile.get("interests"):
                            context_parts.append(f"Interesseområder: {', '.join(anon_profile['interests'][:5])}")
                        if anon_profile.get("last_searches"):
                            context_parts.append(f"Tidligere søgninger: {', '.join(anon_profile['last_searches'][:3])}")
                        if anon_profile.get("last_viewed"):
                            titles = [v.get("title", "?") for v in anon_profile["last_viewed"][:3]]
                            context_parts.append(f"Sidst set kurser: {', '.join(titles)}")
                        if anon_profile.get("preferred_location"):
                            context_parts.append(f"Foretrukken lokation: {anon_profile['preferred_location']}")
                        # Include conversation summary from previous sessions
                        prev_summary = anon_profile.get("conversation_summary", "")
                        if prev_summary:
                            context_parts.append(f"Tidligere samtaleopsummering: {prev_summary[:500]}")

                        if context_parts:
                            CHAT_MEMORY[sid].append({
                                "role": "system",
                                "content": "TILBAGEVENDENDE ANONYM BRUGER:\n"
                                           "Baseret på tidligere besøg har vi denne viden:\n"
                                           + "\n".join(context_parts)
                                           + "\nBrug dette til at give en personlig start, f.eks. 'Sidst kiggede du på X — vil du fortsætte der?'"
                            })
                except Exception as e:
                    print(f"[Anon Profile Load Error] {e}")

        # Ensure session exists in persistent store
        try:
            _get_store().save_session(sid)
        except Exception as e:
            print(f"[Session Save Error] {e}")

    messages = CHAT_MEMORY[sid]
    messages.append({"role": "user", "content": user_query})

    if sid in SHOWN_PRODUCTS:
        SHOWN_PRODUCTS[sid]["last_active"] = time.time()

    # Phase 1: Lightweight intent + stage detection (no API call — rule-based)
    user_profile_text = USER_PROFILES.get(sid, {}).get("summary", "")
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))

    # Rule-based intent detection (replaces GPT-4o-mini API call)
    intent = _classify_intent_local(user_query, messages, shown_count)
    rewritten_query = ""  # The main model handles query optimization via tools

    # Debug: log user query + intent classification
    try:
        log_debug(sid, "user_query", {
            "query": user_query,
            "intent": intent,
            "logged_in": logged_in_user or False,
            "hint": "",
            "rewritten_query": rewritten_query,
        })
    except Exception:
        pass

    # Phase 3: Detect conversation stage
    stage = _detect_conversation_stage(sid, messages)
    CONVERSATION_STAGES[sid] = stage
    stage_hint = _STAGE_HINTS.get(stage, "")

    # Reconcile stage vs intent conflicts
    _original_stage = stage
    if intent in ("discovery", "comparison", "detail", "follow_up") and stage == "needs_discovery":
        stage = "searching"
        CONVERSATION_STAGES[sid] = stage
        stage_hint = _STAGE_HINTS.get(stage, "")
    elif intent == "needs_clarification" and stage == "searching":
        stage = "needs_discovery"
        CONVERSATION_STAGES[sid] = stage
        stage_hint = _STAGE_HINTS.get(stage, "")
    # 3.1/3.3/3.4: Intent-driven stage overrides
    elif intent == "buying":
        stage = "ready_to_buy"
        CONVERSATION_STAGES[sid] = stage
        stage_hint = _STAGE_HINTS.get(stage, "")
    elif intent == "correction":
        stage = "correcting"
        CONVERSATION_STAGES[sid] = stage
        stage_hint = _STAGE_HINTS.get(stage, "")
        # 3.3: Track what was rejected for negative context
        _track_rejection(sid, user_query, messages)
    elif intent == "team_buying":
        stage = "team_buying"
        CONVERSATION_STAGES[sid] = stage
        stage_hint = _STAGE_HINTS.get(stage, "")

    # Debug: log stage detection
    try:
        log_debug(sid, "stage_detection", {
            "stage": stage,
            "original_stage": _original_stage,
            "intent": intent,
            "override": stage != _original_stage,
            "hint": stage_hint,
            "user_profile": user_profile_text[:200] if user_profile_text else None,
            "shown_count": shown_count,
        })
    except Exception:
        pass

    # Extract user profile periodically — skip for logged-in users (Phase 2A)
    if not logged_in_user:
        _extract_user_profile(sid, messages)

    # Memory pruning at 25+ messages (GPT-4o handles 128K context, no need to prune early)
    if len(messages) > 25:
        system_msg = messages[0]
        # Preserve profile-related system messages during pruning
        _PROFILE_MARKERS = ("BRUGERENS NUVÆRENDE PROFIL", "TILBAGEVENDENDE BRUGER", "TILBAGEVENDENDE ANONYM")
        protected = []
        to_prune = []
        for msg in messages[1:-16]:
            if msg.get("role") == "system" and any(m in (msg.get("content") or "") for m in _PROFILE_MARKERS):
                protected.append(msg)
            else:
                to_prune.append(msg)
        recent = messages[-16:]

        summary_text = _summarize_pruned_messages(to_prune)
        new_messages = [system_msg] + protected
        if summary_text:
            new_messages.append({"role": "system", "content": summary_text})
        new_messages.extend(recent)
        CHAT_MEMORY[sid] = new_messages
        messages = CHAT_MEMORY[sid]

        # Persist summary
        try:
            if summary_text:
                _get_store().update_session_field(sid, "conversation_summary", summary_text)
        except Exception as e:
            print(f"[Summary Persist Error] {e}")

        # Persist summary to anonymous profile for cross-session memory
        if not logged_in_user and summary_text:
            browser_token = session.get("browser_token")
            if browser_token:
                try:
                    _get_store().update_anonymous_summary(browser_token, summary_text)
                except Exception:
                    pass

    # Log the query (Phase 4 analytics)
    try:
        _get_store().log_event(sid, "user_query", query_text=user_query,
                               extra={"intent": intent, "stage": stage,
                                      "prompt_version": _get_prompt_version(sid)})
    except Exception:
        pass

    # 6.3: Track anonymous searches
    if not logged_in_user and intent == "discovery":
        browser_token = session.get("browser_token")
        if browser_token:
            try:
                if sid not in ANONYMOUS_PROFILES:
                    ANONYMOUS_PROFILES[sid] = _get_store().load_anonymous_profile(browser_token) or {"browser_token": browser_token}
                _get_store().update_anonymous_interests(browser_token, new_search=user_query[:100])
            except Exception:
                pass

    def stream_generator():
        try:
            yield f"data: {json.dumps({'type': 'ping', 'content': 'ok'})}\n\n"

            buffered_ui_html = []
            buffered_profile_events = []

            # Build ephemeral messages with all context layers
            ephemeral_messages = list(messages)
            insert_idx = 1

            # Phase 6: Few-shot examples from top-rated interactions
            few_shot_msg = _build_few_shot_examples()
            if few_shot_msg:
                ephemeral_messages.insert(insert_idx, few_shot_msg)
                insert_idx += 1

            # User profile context — prefer MySQL profile if logged in
            db_profile_text = ""
            if logged_in_user:
                try:
                    from app1.user_profile_db import get_full_profile, format_profile_for_ai, ensure_tables, update_profile_summary
                    ensure_tables()
                    db_profile = get_full_profile(logged_in_user)
                    db_profile_text = format_profile_for_ai(db_profile)

                    # Phase 2B: Seed DB profile from session profile on first login
                    if not db_profile_text and not session.get("_profile_seeded"):
                        session_profile = USER_PROFILES.get(sid, {}).get("summary", "")
                        if session_profile:
                            try:
                                update_profile_summary(logged_in_user, bio=session_profile)
                                db_profile = get_full_profile(logged_in_user)
                                db_profile_text = format_profile_for_ai(db_profile)
                                session["_profile_seeded"] = True
                            except Exception:
                                pass

                    if db_profile_text:
                        ephemeral_messages.insert(insert_idx, {
                            "role": "system",
                            "content": f"BRUGERPROFIL (fra database, logget ind som '{logged_in_user}'):\n{db_profile_text}"
                        })
                        insert_idx += 1
                except Exception as e:
                    print(f"[DB Profile Error] {e}")

            # Fallback: session-based profile if no DB profile
            if not db_profile_text:
                profile_msg = _build_user_profile_message(sid)
                if profile_msg:
                    ephemeral_messages.insert(insert_idx, profile_msg)
                    insert_idx += 1

            # Shown products context
            shown_msg = _build_shown_products_message(sid)
            if shown_msg:
                ephemeral_messages.insert(insert_idx, shown_msg)
                insert_idx += 1

            # 3.3: Rejection context — what the user didn't like
            rejection_msg = _build_rejection_context(sid)
            if rejection_msg:
                ephemeral_messages.insert(insert_idx, rejection_msg)
                insert_idx += 1

            # S3: Smart context — situation assessment from conversation history
            smart_ctx = _build_smart_context(sid, messages, stage, intent)
            if smart_ctx:
                ephemeral_messages.insert(insert_idx, smart_ctx)
                insert_idx += 1

            # Phase 1 + 3 + S4: Inject intent + stage hints + tone as guidance
            guidance_parts = []
            # S4: Dynamic tone hint based on stage
            tone_hint = _TONE_HINTS.get(stage, "")
            if tone_hint:
                guidance_parts.append(tone_hint)
            if stage_hint:
                guidance_parts.append(f"SAMTALEFASE: {stage} — {stage_hint}")
            guidance_parts.append(f"INTENT: {intent}")
            if logged_in_user:
                guidance_parts.append(f"LOGGET IND SOM: {logged_in_user} — du har adgang til profil-værktøjer (get_user_profile, update_user_profile, recommend_for_profile). Brug dem til personalisering og proaktiv profilopdatering.")

                # Phase 5B: Auto-inject profile preferences as search defaults
                if db_profile_text:
                    try:
                        pref_parts = []
                        if db_profile.get("preferred_location"):
                            pref_parts.append(f"- Lokation: {db_profile['preferred_location']}")
                        if db_profile.get("preferred_format"):
                            pref_parts.append(f"- Format: {db_profile['preferred_format']}")
                        if db_profile.get("budget_range"):
                            pref_parts.append(f"- Budget: {db_profile['budget_range']}")
                        if pref_parts:
                            guidance_parts.append("BRUGERENS FASTE PRAEFERENCER (brug som standard-filtre medmindre brugeren siger andet):\n" + "\n".join(pref_parts))
                    except Exception:
                        pass

                # Phase 5C: Completed course deduplication
                try:
                    completed = db_profile.get("completed_courses", [])
                    if completed:
                        handles = [c.get("handle") for c in completed if c.get("handle")]
                        titles = [c.get("title") for c in completed if c.get("title")]
                        dedup_items = handles[:10] if handles else titles[:10]
                        if dedup_items:
                            guidance_parts.append("ALLEREDE GENNEMFORTE (anbefal IKKE disse): " + ", ".join(dedup_items))
                except Exception:
                    pass
            else:
                guidance_parts.append("BRUGER IKKE LOGGET IND — profil-værktøjer er ikke tilgængelige.")
            if guidance_parts:
                ephemeral_messages.insert(insert_idx, {
                    "role": "system",
                    "content": "\n".join(guidance_parts)
                })

            # Agent Loop — include profile tools if user is logged in
            all_tools = OPENAI_TOOLS + (PROFILE_TOOLS if logged_in_user else [])
            had_tool_calls = False
            max_iterations = 5  # Safety limit
            iteration = 0

            # 2.6: Set search context — shown handles + user prefs for contextual search
            shown_handles = {p.get("handle") for p in SHOWN_PRODUCTS.get(sid, {}).get("products", []) if p.get("handle")}
            search_user_prefs = {}
            try:
                if logged_in_user and db_profile_text:
                    search_user_prefs = {
                        "location": db_profile.get("preferred_location", "") if isinstance(db_profile, dict) else "",
                        "format": db_profile.get("preferred_format", "") if isinstance(db_profile, dict) else "",
                    }
            except (NameError, Exception):
                pass
            set_search_context(shown_handles=shown_handles, user_prefs=search_user_prefs)

            # Tool-calling loop (non-streaming for tool iterations)
            last_message_is_final = False
            while iteration < max_iterations:
                iteration += 1
                api_start = time.time()
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=ephemeral_messages,
                    tools=all_tools,
                    stream=False
                )
                _log_latency(sid, "llm_tool_call", api_start)

                message = response.choices[0].message

                if not message.tool_calls:
                    # Model wants to talk — DON'T append yet, we'll re-generate with streaming
                    last_message_is_final = True
                    break

                # Tool call iteration — append and continue
                messages.append(message.model_dump())
                ephemeral_messages.append(message.model_dump())
                had_tool_calls = True

                # 5.2: Parallel tool execution when multiple independent tool calls
                tool_results_map = {}
                tool_start = time.time()
                if len(message.tool_calls) > 1:
                    with ThreadPoolExecutor(max_workers=4) as executor:
                        futures = {
                            executor.submit(execute_tool, tc, username=logged_in_user, session_id=sid): tc.id
                            for tc in message.tool_calls
                        }
                        for future in futures:
                            tc_id = futures[future]
                            try:
                                tool_results_map[tc_id] = future.result()
                            except Exception as e:
                                tool_results_map[tc_id] = json.dumps({"status": "error", "message": str(e)})

                for tool_call in message.tool_calls:
                    if tool_call.id in tool_results_map:
                        tool_result_str = tool_results_map[tool_call.id]
                    else:
                        tool_result_str = execute_tool(tool_call, username=logged_in_user, session_id=sid)
                    try:
                        tool_result_dict = json.loads(tool_result_str)
                    except (json.JSONDecodeError, TypeError) as parse_err:
                        print(f"[Tool JSON Error] {tool_call.function.name}: {parse_err}")
                        tool_result_dict = {"status": "error", "message": f"Ugyldigt værktøjssvar: {str(parse_err)[:100]}"}
                        tool_result_str = json.dumps(tool_result_dict)

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": json.dumps({k: v for k, v in tool_result_dict.items() if not k.startswith("raw_")})
                    }
                    messages.append(tool_msg)
                    ephemeral_messages.append(tool_msg)

                    # Log tool call
                    results_count = tool_result_dict.get("count", len(tool_result_dict.get("results", [])))
                    try:
                        _get_store().log_event(sid, "tool_call",
                                               query_text=user_query,
                                               tool_used=tool_call.function.name,
                                               results_count=results_count)
                    except Exception:
                        pass

                    # Debug: log tool call details
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                        result_titles = [r.get("title", "?") for r in tool_result_dict.get("results", [])[:5]]
                        search_debug = tool_result_dict.get("search_debug") if isinstance(tool_result_dict, dict) else None

                        debug_payload = {
                            "tool": tool_call.function.name,
                            "args": tool_args,
                            "results_count": results_count,
                            "result_titles": result_titles,
                            "status": tool_result_dict.get("status", "unknown"),
                        }
                        if search_debug:
                            debug_payload["matching_debug"] = {
                                "query_tokens": search_debug.get("query_tokens", []),
                                "core_query_tokens": search_debug.get("core_query_tokens", []),
                                "expanded_query_tokens": search_debug.get("expanded_query_tokens", [])[:20],
                                "preferences": search_debug.get("preferences", {}),
                                "vector_candidates": search_debug.get("vector_candidates", 0),
                                "bm25_candidates": search_debug.get("bm25_candidates", 0),
                                "fused_candidates": search_debug.get("fused_candidates", 0),
                                "top_vector_score": search_debug.get("top_vector_score", 0),
                                "confidence": search_debug.get("confidence", "unknown"),
                                "fuzzy_corrections": search_debug.get("fuzzy_corrections", []),
                                "filtered_below_threshold": search_debug.get("filtered_below_threshold", 0),
                                "cross_encoder_applied": search_debug.get("cross_encoder_applied", False),
                                "selected": search_debug.get("selected", []),
                            }
                        log_debug(sid, "tool_call", debug_payload)
                    except Exception:
                        pass

                    # Log tool errors for visibility
                    if tool_result_dict.get("status") == "error":
                        error_msg = tool_result_dict.get("message", "Ukendt fejl")
                        print(f"[Tool Error] {tool_call.function.name}: {error_msg}")
                        try:
                            log_debug(sid, "tool_error", {
                                "tool": tool_call.function.name,
                                "error": error_msg,
                                "args": json.loads(tool_call.function.arguments),
                            })
                        except Exception:
                            pass

                    # UI Interceptor — buffer product cards
                    fn = tool_call.function.name
                    if fn in ("search_courses", "filter_courses", "recommend_for_profile") and "raw_products" in tool_result_dict:
                        raw_products = tool_result_dict["raw_products"]
                        if raw_products:
                            ui_html = render_multi_course_media(raw_products)
                            buffered_ui_html.append(ui_html)
                            compact = tool_result_dict.get("results", [])
                            _track_shown_products(sid, compact)

                        if fn == "search_courses":
                            try:
                                dbg = tool_result_dict.get("search_debug", {})
                                log_debug(sid, "matching_summary", {
                                    "query": dbg.get("query", user_query),
                                    "preferences": dbg.get("preferences", {}),
                                    "core_query_tokens": dbg.get("core_query_tokens", []),
                                    "selected_count": len(dbg.get("selected", [])),
                                    "top_selected": dbg.get("selected", [])[:3],
                                })
                            except Exception:
                                pass

                    elif fn == "get_course_details" and "raw_product" in tool_result_dict:
                        ui_html = render_product_media(tool_result_dict["raw_product"])
                        buffered_ui_html.append(ui_html)

                    elif fn == "update_user_profile":
                        tool_status = tool_result_dict.get("status", "")
                        if tool_status == "proposed":
                            buffered_profile_events.append(json.dumps({
                                'type': 'profile_confirm_request',
                                'message': tool_result_dict.get('message', ''),
                                'section': tool_result_dict.get('section', ''),
                                'confirm': tool_result_dict.get('confirm', {})
                            }))
                        elif tool_status in ("success", "already_exists"):
                            buffered_profile_events.append(json.dumps({
                                'type': 'profile_update',
                                'message': tool_result_dict.get('message', 'Profil opdateret'),
                                'section': tool_result_dict.get('section', '')
                            }))
                        # Log profile event
                        try:
                            log_debug(sid, "profile_event", {
                                "action": tool_args.get("action", ""),
                                "status": tool_status,
                                "section": tool_result_dict.get("section", ""),
                                "message": tool_result_dict.get("message", "")[:200],
                                "data": tool_args.get("data", {}),
                            })
                        except Exception:
                            pass

                    elif fn == "request_user_input":
                        if tool_result_dict.get("status") == "ui_card":
                            buffered_profile_events.append(json.dumps({
                                'type': 'ui_card',
                                'ui_type': tool_result_dict.get('ui_type', 'confirm'),
                                'message': tool_result_dict.get('message', ''),
                                'section': tool_result_dict.get('section', ''),
                                'save_action': tool_result_dict.get('save_action', ''),
                                'prefilled': tool_result_dict.get('prefilled', {}),
                                'fields': tool_result_dict.get('fields', []),
                                'choices': tool_result_dict.get('choices', []),
                            }))
                            # Log UI card event
                            try:
                                log_debug(sid, "ui_card", {
                                    "ui_type": tool_result_dict.get("ui_type", ""),
                                    "section": tool_result_dict.get("section", ""),
                                    "save_action": tool_result_dict.get("save_action", ""),
                                    "message": tool_result_dict.get("message", "")[:200],
                                    "fields_count": len(tool_result_dict.get("fields", [])),
                                    "prefilled_keys": list(tool_result_dict.get("prefilled", {}).keys()),
                                })
                            except Exception:
                                pass

                    elif fn == "compare_courses" and "raw_products" in tool_result_dict:
                        raw_products = tool_result_dict["raw_products"]
                        if raw_products:
                            ui_html = render_multi_course_media(raw_products)
                            buffered_ui_html.append(ui_html)

                # 5.4: Log tool execution latency
                _log_latency(sid, "tool_execution", tool_start)

                continue

            # Phase 2: True streaming of the final text response
            # Stream the response in real-time, buffering only the <suggestions> tag
            def _stream_final_response(msgs):
                """Stream GPT-4o response, yielding text chunks and returning full text.
                Uses a state machine to cleanly handle <suggestions> tag detection across chunk boundaries."""
                resp = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=msgs,
                    stream=True
                )
                full = ""
                _TAG = "<suggestions>"
                _TAG_LEN = len(_TAG)
                state = "streaming"  # "streaming" | "buffering" | "in_tag"
                buffer = ""

                for chunk in resp:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if not delta or not delta.content:
                        continue

                    text_piece = delta.content
                    full += text_piece

                    if state == "in_tag":
                        continue  # Silently consume everything after <suggestions>

                    # Accumulate into buffer for tag detection
                    buffer += text_piece

                    # Check if full tag is present in buffer
                    tag_pos = buffer.find(_TAG)
                    if tag_pos >= 0:
                        # Flush text before the tag
                        before = buffer[:tag_pos]
                        if before:
                            yield f"data: {json.dumps({'type': 'chunk', 'content': before})}\n\n"
                        state = "in_tag"
                        buffer = ""
                        continue

                    # Check if buffer could still be a partial tag match
                    # e.g., buffer ends with "<", "<s", "<su", ..., "<suggestion"
                    could_be_partial = False
                    for i in range(1, min(len(buffer) + 1, _TAG_LEN)):
                        if buffer.endswith(_TAG[:i]):
                            could_be_partial = True
                            break

                    if could_be_partial:
                        # Keep potential tag prefix in buffer, flush the safe prefix
                        # Find the start of the partial match
                        for i in range(min(len(buffer), _TAG_LEN), 0, -1):
                            if buffer.endswith(_TAG[:i]):
                                safe = buffer[:-i]
                                if safe:
                                    yield f"data: {json.dumps({'type': 'chunk', 'content': safe})}\n\n"
                                buffer = buffer[-i:]
                                break
                    else:
                        # No tag possibility — flush entire buffer
                        yield f"data: {json.dumps({'type': 'chunk', 'content': buffer})}\n\n"
                        buffer = ""

                # Flush any remaining buffer (model ended without <suggestions>)
                if buffer and state != "in_tag":
                    yield f"data: {json.dumps({'type': 'chunk', 'content': buffer})}\n\n"

                # Store full text for post-processing
                _stream_final_response._full_text = full

            _stream_final_response._full_text = ""

            # Add reinforcement before final response when we have product cards
            if had_tool_calls and buffered_ui_html:
                ephemeral_messages.append({
                    "role": "system",
                    "content": "PÅMINDELSE: Kurserne vises allerede som interaktive kort. "
                               "Dit svar SKAL være 1-2 korte sætninger der knytter resultaterne til brugerens behov. "
                               "ALDRIG liste kursusnavne, priser, beskrivelser eller detaljer i teksten."
                })

            # Stream the final text response FIRST for natural top-to-bottom flow
            if last_message_is_final:
                full_text = message.content or ""
                visible_text = _strip_suggestions_tag(full_text)
                # Strip course listings when product cards are shown
                if had_tool_calls and buffered_ui_html:
                    visible_text = _strip_course_listings(visible_text)
                # Progressive chunking for smooth streaming UX
                _csz = 8
                for _ci in range(0, len(visible_text), _csz):
                    yield f"data: {json.dumps({'type': 'chunk', 'content': visible_text[_ci:_ci+_csz]})}\n\n"
            else:
                # Max iterations exhausted — log warning and stream a fresh response
                try:
                    log_debug(sid, "iteration_limit", {"iterations": iteration, "max": max_iterations})
                except Exception:
                    pass
                print(f"[Agent Warning] Max iterations ({max_iterations}) reached for session {sid}")
                stream_start = time.time()
                for sse_chunk in _stream_final_response(ephemeral_messages):
                    yield sse_chunk
                _log_latency(sid, "llm_stream_response", stream_start)
                full_text = _stream_final_response._full_text
            messages.append({"role": "assistant", "content": _strip_suggestions_tag(full_text)})

            # ── Stream profile events AFTER text (natural reading order) ──
            for evt in buffered_profile_events:
                yield f"data: {evt}\n\n"

            # ── Stream product cards AFTER text + profile events ──
            for html_chunk in buffered_ui_html:
                if html_chunk is not None:
                    yield f"data: {json.dumps({'type': 'product', 'html': html_chunk})}\n\n"

            # Phase 6: Quality guardrail
            visible_text = _strip_suggestions_tag(full_text)
            quality_ok = _check_response_quality(visible_text, had_tool_calls)

            # Debug: log AI response + length validation
            response_len = len(visible_text)
            verbose = had_tool_calls and buffered_ui_html and response_len > 300
            try:
                log_debug(sid, "ai_response", {
                    "response_text": full_text[:500],
                    "had_tool_calls": had_tool_calls,
                    "quality_ok": quality_ok,
                    "iterations": iteration,
                    "response_length": response_len,
                    "verbose_warning": verbose,
                })
            except Exception:
                pass
            if verbose:
                print(f"[Agent Warning] Verbose response ({response_len} chars) after tool calls in session {sid}")

            # Smart failure recovery: when tool calls returned 0 results
            if had_tool_calls and not buffered_ui_html:
                if "ingen" not in full_text.lower():
                    no_results_msg = "\n\n*Jeg fandt desværre ingen kurser der matchede. Prøv eventuelt at omformulere din søgning.*"
                    yield f"data: {json.dumps({'type': 'chunk', 'content': no_results_msg})}\n\n"

            # Extract suggestions from the AI response (inline via <suggestions> tag)
            suggestions = _extract_inline_suggestions(full_text)

            if suggestions:
                yield f"data: {json.dumps({'type': 'suggestions', 'items': suggestions})}\n\n"

            # Debug: log suggestions
            try:
                log_debug(sid, "suggestions", {
                    "type": "inline",
                    "items": suggestions,
                    "count": len(suggestions),
                    "stage": stage,
                    "had_results_ui": bool(buffered_ui_html),
                })
            except Exception:
                pass

            # Send message index for feedback tracking
            msg_index = len([m for m in messages if m.get("role") == "assistant"])
            yield f"data: {json.dumps({'type': 'meta', 'message_index': msg_index})}\n\n"

            # Persist conversation for logged-in users
            if logged_in_user:
                try:
                    from app1.user_profile_db import save_conversation, save_conversation_history, ensure_tables
                    ensure_tables()
                    save_conversation(logged_in_user, sid, messages)
                    save_conversation_history(logged_in_user, sid, messages)
                except Exception as e:
                    print(f"[Conversation Save Error] {e}")
                    try:
                        current_app.mysql.connection.rollback()
                    except Exception:
                        pass

        except Exception as e:
            print(f"[Agent Error] {e}")
            error_msg = "Beklager, der opstod en teknisk fejl. Prøv venligst igen."
            yield f"data: {json.dumps({'type': 'chunk', 'content': error_msg})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    response = Response(stream_with_context(stream_generator()), mimetype="text/event-stream")
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response
