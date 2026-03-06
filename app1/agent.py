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
from app1.tools import OPENAI_TOOLS, PROFILE_TOOLS, execute_tool, classify_intent
from app1.memory_store import log_debug
from . import render_multi_course_media, render_product_media

import uuid
import re as _re

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

# ── Lazy-load persistent store ──
_memory_store = None

def _get_store():
    global _memory_store
    if _memory_store is None:
        from app1 import memory_store
        _memory_store = memory_store
    return _memory_store


SYSTEM_PROMPT = """Du er en venlig, kyndig AI-uddannelsesrådgiver for AiLead. Du hjælper brugere med at finde de bedste kurser og uddannelser baseret på deres mål, behov og situation.

PERSONLIGHED:
- Vær varm, professionel og imødekommende — som en dygtig studievejleder.
- HANDL HURTIGT: Så snart brugeren har givet nok info til at søge (et emne, behov eller krav), SØG MED DET SAMME. Stil kun spørgsmål hvis du virkelig mangler information til at lave en søgning.
- Giv konkrete anbefalinger med begrundelse — forklar HVORFOR et kursus passer til dem.
- Husk hvad brugeren har fortalt dig tidligere i samtalen og byg videre på det.
- Vær proaktiv: efter at have vist kurser, foreslå næste skridt (sammenlign, se detaljer, juster søgning).
- ALDRIG stil mere end ét spørgsmål ad gangen. Og stil ALDRIG et spørgsmål som brugeren allerede har besvaret i samtalen.

VISUEL REGEL (VIGTIGSTE REGEL — OVERTRUMFER ALT ANDET):
Når du bruger tools til at finde kurser, vises de fundne kurser AUTOMATISK på skærmen som interaktive visuelle kort ved siden af din tekst.
Du må ALDRIG:
- Skrive kursusnavne i dit svar
- Liste priser, lokationer eller beskrivelser
- Bruge bulletpoints eller nummererede lister over kurser
- Skrive "1.", "2.", "3." efterfulgt af kursusinfo
- Bruge **fed skrift** til kursusnavne
Dit svar skal KUN være 1-2 korte, naturlige sætninger som "Her er nogle gode muligheder inden for planlægning — tag et kig på kortene!" ALDRIG mere.

VÆRKTØJSSTRATEGI:
- search_courses: Brug til åbne/semantiske forespørgsler ("kurser om ledelse", "noget med kommunikation"). Hybrid søgning (semantisk + nøgleord).
- filter_courses: Brug når brugeren har strukturerede krav: pris, lokation, type, tags, eller kombinationer.
- get_course_details: Hent detaljer om ét kursus via handle.
- compare_courses: Sammenlign 2-4 kurser side om side. Brug når brugeren spørger "hvad er forskellen?" eller "hvilken er bedst?".
- get_user_profile: Hent brugerens profil (kompetencer, erfaring, uddannelse, gennemførte kurser). Brug til personlige anbefalinger og når du skal kende brugerens baggrund.
- update_user_profile: Opdater brugerens profil. Brug dette PROAKTIVT når:
  * Brugeren nævner en kompetence ("jeg kan Python") → tilføj skill
  * Brugeren fortæller om sin baggrund ("jeg arbejder som projektleder") → tilføj erfaring
  * Brugeren har gennemført et kursus → tilføj til gennemførte kurser
  * Brugeren vil fjerne eller ændre noget i sin profil → fjern/opdater
  * Brugeren udtrykker præferencer ("jeg foretrækker e-learning") → opdater summary

PROFILSTRATEGI: Når brugeren fortæller om sig selv, tilføj relevant info til profilen UDEN at spørge om lov. Bekræft kort hvad du har tilføjet. Eksempel: Brugeren siger "Jeg er projektleder med PRINCE2" → tilføj erfaring + skill, og sig "Jeg har noteret at du er projektleder med PRINCE2-kompetence."
- recommend_for_profile: Anbefal kurser baseret på brugerens profil og kompetencehuller. Brug når brugeren spørger "hvad bør jeg lære?", "anbefal noget til mig", eller lignende. Filtrerer automatisk allerede gennemførte kurser fra.

SØGESTRATEGI: Fokuser ALTID på brugerens udtrykte BEHOV eller MÅL — ikke deres jobtitel eller rolle. "Jeg er salgsleder og vil blive bedre til planlægning" → søg "planlægning", IKKE "salgsledelse".

KOMBINATIONSSTRATEGI: For "e-learning om projektledelse under 5000 kr" → filter_courses med product_type="E-learning", price_max=5000, query="projektledelse".

OPFØLGNINGSSTRATEGI: Brug ALTID listen 'VISTE KURSER' til at identificere refererede kurser. "den til 9999 kr" → match pris. "nummer 2" → match indeks. Brug derefter get_course_details.

SAMMENLIGNINGSSTRATEGI: Når brugeren vil sammenligne, brug compare_courses med handles fra VISTE KURSER. Forklar fordele/ulemper for hvert kursus i forhold til brugerens behov.

HUKOMMELSE: Du har adgang til en brugerprofil og samtaleoversigt. Brug dem aktivt til personlige anbefalinger.

PRODUKTREFERENCE: Når brugeren vedhæfter et produkt via "Spørg om"-knappen, vil det fremgå som [VEDHÆFTET KURSUS: ...]. Besvar specifikt om det kursus.

KVALITET: Hvis en søgning returnerer 0 resultater, sig det ærligt og foreslå alternative søgetermer. Gæt aldrig på kurser der ikke eksisterer.

EDGE CASES:
- Engelsk forespørgsel: Hvis brugeren skriver på engelsk, svar ALTID på dansk men forstå den engelske forespørgsel korrekt. Oversæt søgetermer til relevante danske/engelske søgeord.
- Ukendt emne: Hvis emnet er meget nichepræget og du ikke finder resultater, foreslå bredere eller relaterede emner. Sig aldrig "det har vi ikke" uden at have søgt først.
- Kun ét kursus fundet: Præsenter det ene kursus positivt, men nævn at udvalget er begrænset og foreslå en bredere søgning.
- Emneskift: Hvis brugeren skifter emne midt i samtalen, anerkend skiftet kort og søg straks på det nye emne. Glem ikke tidligere kontekst.
- Pris/lokation-spørgsmål ("billigst", "nærmest"): Brug listen VISTE KURSER til at sammenligne priser/lokationer direkte. Svar specifikt med det billigste/nærmeste fra listen."""

SESSION_TTL = 3600


# ── Phase 3: Conversation State Machine ──

def _detect_conversation_stage(sid, messages):
    """Detect conversation stage based on message history and context. Rule-based, no API call."""
    user_msg_count = sum(1 for m in messages if m.get("role") == "user")
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))
    tool_call_count = sum(1 for m in messages if m.get("role") == "tool")

    # Check if user has given actionable search info (budget, topic, format keywords)
    user_texts = " ".join(m.get("content", "").lower() for m in messages if m.get("role") == "user")
    has_budget = any(w in user_texts for w in ["kr", "budget", "under ", "over ", "maks ", "gratis", "billig"])
    user_text_tokens = set(_re.findall(r'[a-zæøå0-9]+', user_texts))
    has_topic = bool(user_text_tokens & _TOPIC_KEYWORDS)
    # Fallback: if no keyword matched but latest message has 4+ content tokens, assume topic
    if not has_topic:
        latest_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if latest_user and _has_content_tokens(latest_user):
            has_topic = True
    has_format = any(w in user_texts for w in ["e-learning", "online", "fysisk", "kursus", "workshop"])
    has_actionable_info = has_topic or (has_budget and user_msg_count >= 2) or (has_format and user_msg_count >= 2)

    if user_msg_count <= 1 and not has_actionable_info:
        return "greeting"
    elif user_msg_count >= 2 and has_actionable_info and shown_count == 0:
        # User has given enough info — time to search, not ask more questions
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
    "greeting": "Velkommen brugeren varmt og kort. Spørg hvad de leder efter.",
    "needs_discovery": "Brugeren har ikke givet nok info endnu. Stil ét enkelt spørgsmål om hvad de gerne vil lære eller forbedre.",
    "searching": "SØG NU med de oplysninger du har. Brug search_courses eller filter_courses. SPØRG IKKE flere spørgsmål — du har nok info til at handle.",
    "comparing": "Hjælp brugeren med at vælge ved at fremhæve fordele og ulemper. Foreslå compare_courses hvis relevant.",
    "deciding": "Opsummer de bedste valgmuligheder og opmuntr brugeren til at tage næste skridt.",
}


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
    # Also clean persistent store
    try:
        _get_store().cleanup_old_sessions(SESSION_TTL)
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


# ── Memory Management ──

def _summarize_pruned_messages(messages_to_prune):
    """Create a conversation summary using GPT for intelligent compression."""
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
                {"role": "system", "content": "Du er en assistent der laver kortfattede samtaleoversigter. Opsummer følgende samtale mellem en bruger og en kursusrådgiver. Bevar alle vigtige detaljer: brugerens behov, præferencer, budget, lokation, rolle, interesseområder, og hvilke kurser der blev vist/diskuteret. Maks 150 ord."},
                {"role": "user", "content": transcript}
            ],
            temperature=0.3,
            max_tokens=250
        )
        summary = response.choices[0].message.content.strip()
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


def _build_few_shot_examples():
    """Phase 6: Load top-rated interactions as few-shot examples."""
    try:
        store = _get_store()
        top = store.get_top_rated_interactions(limit=3, min_rating=1)
        if not top:
            return None
        lines = ["EKSEMPLER PÅ GODE SVAR (brug som inspiration):"]
        for item in top:
            q = item.get("query", "")
            extra = item.get("extra", {})
            answer = extra.get("assistant_response", "")
            if q and answer:
                lines.append(f"Bruger: {q[:100]}\nRådgiver: {answer[:200]}")
        if len(lines) <= 1:
            return None
        return {"role": "system", "content": "\n\n".join(lines)}
    except Exception:
        return None


# ── Follow-up Suggestions ──

def _generate_followup_suggestions(shown_products, user_query, stage, user_profile_text=""):
    """Generate 2-3 contextual follow-up suggestion chips based on what was shown."""
    if not shown_products:
        return []

    recent = shown_products[-4:]
    # Build rich product context
    product_lines = []
    for p in recent:
        locs = ", ".join(p.get("locations", [])) if p.get("locations") else "?"
        ptype = p.get("product_type", "?")
        product_lines.append(f'"{p.get("title", "")}" — {p.get("price", "?")} kr — {locs} — {ptype}')
    products_str = "\n".join(product_lines)

    # Detect unexplored filter dimensions
    unexplored = []
    prices = [p.get("price") for p in recent if p.get("price")]
    locations = [loc for p in recent for loc in (p.get("locations") or [])]
    types = [p.get("product_type", "") for p in recent if p.get("product_type")]
    if len(set(str(pr) for pr in prices)) > 1:
        unexplored.append("pris (f.eks. 'Vis kun de billigste')")
    if len(set(locations)) > 1:
        unexplored.append("lokation (f.eks. 'Kun i København')")
    if len(set(types)) > 1:
        unexplored.append("type (f.eks. 'Vis kun e-learning')")

    unexplored_hint = f"\nUudforskede filtre: {', '.join(unexplored)}" if unexplored else ""
    profile_hint = f"\nBrugerprofil: {user_profile_text[:200]}" if user_profile_text else ""

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"""Generer 2-3 korte forslag til opfølgningsspørgsmål som brugeren kan klikke på.
Kontekst: Brugeren søgte "{user_query}" og fik vist disse kurser:
{products_str}
Samtalefase: {stage}.{profile_hint}{unexplored_hint}

Regler:
- Hvert forslag maks 6 ord
- Skriv på dansk
- Gør dem handlingsorienterede og SPECIFIKKE baseret på de viste kurser og uudforskede filtre
- Foreslå filtre brugeren ikke har prøvet endnu
- Eksempler: "Sammenlign de to billigste", "Vis kun e-learning", "Fortæl mere om den første"
- Svar som JSON array: ["forslag1", "forslag2", "forslag3"]"""},
                {"role": "user", "content": "Generer forslag"}
            ],
            temperature=0.4,
            max_tokens=100
        )
        result = json.loads(response.choices[0].message.content.strip())
        if isinstance(result, list):
            # Phase 3C: Validate suggestion chips
            valid = [s.strip() for s in result if isinstance(s, str) and 0 < len(s.strip()) <= 60]
            return valid[:3]
    except Exception as e:
        print(f"[Suggestions Error] {e}")

    return []


def _generate_alternative_searches(user_query, user_profile=""):
    """When search returns 0 results, suggest alternative search terms."""
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"""Brugerens søgning "{user_query}" gav 0 resultater i en kursuskatalog.
{('Brugerprofil: ' + user_profile) if user_profile else ''}
Foreslå 2-3 alternative søgetermer der kunne give bedre resultater.
Tænk på bredere termer, relaterede emner, eller alternative formuleringer.
Svar som JSON array: ["alternativ1", "alternativ2", "alternativ3"]"""},
                {"role": "user", "content": "Foreslå alternativer"}
            ],
            temperature=0.5,
            max_tokens=80
        )
        result = json.loads(response.choices[0].message.content.strip())
        if isinstance(result, list):
            return result[:3]
    except Exception as e:
        print(f"[Alternatives Error] {e}")
    return []


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

        # Ensure session exists in persistent store
        try:
            _get_store().save_session(sid)
        except Exception as e:
            print(f"[Session Save Error] {e}")

    messages = CHAT_MEMORY[sid]
    messages.append({"role": "user", "content": user_query})

    if sid in SHOWN_PRODUCTS:
        SHOWN_PRODUCTS[sid]["last_active"] = time.time()

    # Phase 1: Intent classification (now with conversation history)
    user_profile_text = USER_PROFILES.get(sid, {}).get("summary", "")
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))
    intent_result = classify_intent(
        user_query,
        user_profile=user_profile_text,
        shown_products_count=shown_count,
        conversation_messages=messages
    )
    intent = intent_result.get("intent", "discovery")
    intent_hint = intent_result.get("hint", "")
    rewritten_query = intent_result.get("search_query", "").strip()

    # Debug: log user query + intent classification
    try:
        log_debug(sid, "user_query", {"query": user_query, "intent": intent, "hint": intent_hint, "rewritten_query": rewritten_query})
    except Exception:
        pass

    # Phase 3: Detect conversation stage
    stage = _detect_conversation_stage(sid, messages)
    CONVERSATION_STAGES[sid] = stage
    stage_hint = _STAGE_HINTS.get(stage, "")

    # Improvement #2: Reconcile stage vs intent conflicts
    _original_stage = stage
    if intent in ("discovery", "comparison", "detail", "follow_up") and stage == "needs_discovery":
        stage = "searching"
        CONVERSATION_STAGES[sid] = stage
        stage_hint = _STAGE_HINTS.get(stage, "")
    elif intent == "needs_clarification" and stage == "searching":
        stage = "needs_discovery"
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

    # Memory pruning at 15+ messages
    if len(messages) > 15:
        system_msg = messages[0]
        to_prune = messages[1:-10]
        recent = messages[-10:]

        summary_text = _summarize_pruned_messages(to_prune)
        new_messages = [system_msg]
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

    # Log the query (Phase 4 analytics)
    try:
        _get_store().log_event(sid, "user_query", query_text=user_query,
                               extra={"intent": intent, "stage": stage})
    except Exception:
        pass

    def stream_generator():
        try:
            yield f"data: {json.dumps({'type': 'ping', 'content': 'ok'})}\n\n"

            buffered_ui_html = []

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

            # Phase 1 + 3: Inject intent + stage hints + rewritten query as guidance
            guidance_parts = []
            if intent_hint:
                guidance_parts.append(f"INTENT: {intent} — {intent_hint}")
            if stage_hint:
                guidance_parts.append(f"SAMTALEFASE: {stage} — {stage_hint}")
            if rewritten_query and rewritten_query.lower() != user_query.lower():
                guidance_parts.append(f"OPTIMERET SØGETERM (brug denne i stedet for brugerens rå tekst når du kalder search_courses eller filter_courses): \"{rewritten_query}\"")
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

            while iteration < max_iterations:
                iteration += 1
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=ephemeral_messages,
                    tools=all_tools,
                    stream=False
                )

                message = response.choices[0].message
                messages.append(message.model_dump())
                ephemeral_messages.append(message.model_dump())

                if message.tool_calls:
                    had_tool_calls = True
                    for tool_call in message.tool_calls:
                        tool_result_str = execute_tool(tool_call, username=logged_in_user)
                        # Phase 3B: Safe JSON parsing
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

                        # Log tool call (Phase 4)
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
                                    "selected": search_debug.get("selected", []),
                                }
                            log_debug(sid, "tool_call", debug_payload)
                        except Exception:
                            pass

                        # UI Interceptor
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

                        elif fn == "update_user_profile" and tool_result_dict.get("status") == "success":
                            # Phase 4B: Emit profile update toast via SSE
                            buffered_ui_html.append(None)  # placeholder so we don't trigger "no results"
                            profile_update_msg = tool_result_dict.get("message", "Profil opdateret")
                            yield f"data: {json.dumps({'type': 'profile_update', 'message': profile_update_msg})}\n\n"

                        elif fn == "compare_courses" and "raw_products" in tool_result_dict:
                            # Show compared courses as cards
                            raw_products = tool_result_dict["raw_products"]
                            if raw_products:
                                ui_html = render_multi_course_media(raw_products)
                                buffered_ui_html.append(ui_html)

                    continue

                # LLM wants to talk — break
                break

            # Phase 6: Quality guardrail check
            full_text = message.content or ""

            quality_ok = _check_response_quality(full_text, had_tool_calls)

            # Debug: log AI response
            try:
                log_debug(sid, "ai_response", {
                    "response_text": full_text[:500],
                    "had_tool_calls": had_tool_calls,
                    "quality_ok": quality_ok,
                    "iterations": iteration,
                })
            except Exception:
                pass

            if not quality_ok:
                # Response violates visual rule — regenerate with correction
                ephemeral_messages.append({
                    "role": "system",
                    "content": "KORREKTION: Dit svar BRYDER den vigtigste regel. Du har listet kursusnavne, priser eller beskrivelser i teksten. Kursuskortene vises AUTOMATISK — brugeren kan allerede SE dem. Omskriv dit svar til PRÆCIS 1-2 korte sætninger der IKKE nævner nogen kursusnavne, priser, lokationer eller beskrivelser. Eksempel: 'Her er nogle gode muligheder inden for planlægning — se kortene for detaljer!'"
                })
                try:
                    correction = openai.chat.completions.create(
                        model="gpt-4o",
                        messages=ephemeral_messages,
                        stream=False
                    )
                    corrected = correction.choices[0].message.content
                    if corrected:
                        full_text = corrected
                        messages[-1] = correction.choices[0].message.model_dump()
                except Exception:
                    pass  # Use original if correction fails

            # Smart failure recovery: when 0 results, suggest alternatives
            if had_tool_calls and not buffered_ui_html:
                alt_suggestions = _generate_alternative_searches(user_query, user_profile_text)
                if alt_suggestions:
                    if "ingen" not in full_text.lower() and "no_results" not in full_text.lower():
                        full_text += "\n\n*Jeg fandt desværre ingen kurser der matchede.*"
                    # Alt suggestions will be sent as suggestion chips below
                elif "no_results" not in full_text.lower() and "ingen" not in full_text.lower():
                    full_text += "\n\n*Jeg fandt desværre ingen kurser der matchede. Prøv eventuelt at omformulere din søgning.*"
                    alt_suggestions = []
            else:
                alt_suggestions = None

            # Stream text response
            chunk_size = 12
            for i in range(0, len(full_text), chunk_size):
                yield f"data: {json.dumps({'type': 'chunk', 'content': full_text[i:i+chunk_size]})}\n\n"

            # Stream buffered product HTML cards
            for html_chunk in buffered_ui_html:
                yield f"data: {json.dumps({'type': 'product', 'html': html_chunk})}\n\n"

            # Generate and stream follow-up suggestions
            suggestions = []
            if alt_suggestions:
                # Failure recovery: suggest alternative searches
                suggestions = alt_suggestions
            elif buffered_ui_html and had_tool_calls:
                # Success: suggest follow-up actions
                sp = SHOWN_PRODUCTS.get(sid, {}).get("products", [])
                suggestions = _generate_followup_suggestions(sp, user_query, stage, user_profile_text=user_profile_text)

            if suggestions:
                yield f"data: {json.dumps({'type': 'suggestions', 'items': suggestions})}\n\n"

            # Debug: log suggestions
            try:
                log_debug(sid, "suggestions", {
                    "type": "failure_recovery" if alt_suggestions else "follow_up",
                    "items": suggestions,
                    "count": len(suggestions),
                    "stage": stage,
                    "had_results_ui": bool(buffered_ui_html),
                })
            except Exception:
                pass

            # Send message index for feedback tracking (Phase 6)
            msg_index = len([m for m in messages if m.get("role") == "assistant"])
            yield f"data: {json.dumps({'type': 'meta', 'message_index': msg_index})}\n\n"

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
