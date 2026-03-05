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
from app1.tools import OPENAI_TOOLS, execute_tool, classify_intent
from . import render_multi_course_media, render_product_media

import uuid

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
- Stil opfølgende spørgsmål for at forstå brugerens situation bedre (budget, erfaring, lokation, tidsramme, læringsmål).
- Giv konkrete anbefalinger med begrundelse — forklar HVORFOR et kursus passer til dem.
- Husk hvad brugeren har fortalt dig tidligere i samtalen og byg videre på det.
- Vær proaktiv: efter at have vist kurser, foreslå næste skridt (sammenlign, se detaljer, juster søgning).

VISUEL REGEL: Når du bruger tools til at finde kurser, vises de fundne kurser automatisk på skærmen som interaktive visuelle kort ved siden af din tekst. Du må ALDRIG skrive lister eller bulletpoints over kursusnavne, priser, lokationer eller beskrivelser i dit eget tekstsvar. Lad kortene tale for sig selv. Skriv naturligt og hjælpende (maks 2-3 sætninger ad gangen).

VÆRKTØJSSTRATEGI:
- search_courses: Brug til åbne/semantiske forespørgsler ("kurser om ledelse", "noget med kommunikation"). Hybrid søgning (semantisk + nøgleord).
- filter_courses: Brug når brugeren har strukturerede krav: pris, lokation, type, tags, eller kombinationer.
- get_course_details: Hent detaljer om ét kursus via handle.
- compare_courses: Sammenlign 2-4 kurser side om side. Brug når brugeren spørger "hvad er forskellen?" eller "hvilken er bedst?".

SØGESTRATEGI: Fokuser ALTID på brugerens udtrykte BEHOV eller MÅL — ikke deres jobtitel eller rolle. "Jeg er salgsleder og vil blive bedre til planlægning" → søg "planlægning", IKKE "salgsledelse".

KOMBINATIONSSTRATEGI: For "e-learning om projektledelse under 5000 kr" → filter_courses med product_type="E-learning", price_max=5000, query="projektledelse".

OPFØLGNINGSSTRATEGI: Brug ALTID listen 'VISTE KURSER' til at identificere refererede kurser. "den til 9999 kr" → match pris. "nummer 2" → match indeks. Brug derefter get_course_details.

SAMMENLIGNINGSSTRATEGI: Når brugeren vil sammenligne, brug compare_courses med handles fra VISTE KURSER. Forklar fordele/ulemper for hvert kursus i forhold til brugerens behov.

HUKOMMELSE: Du har adgang til en brugerprofil og samtaleoversigt. Brug dem aktivt til personlige anbefalinger.

PRODUKTREFERENCE: Når brugeren vedhæfter et produkt via "Spørg om"-knappen, vil det fremgå som [VEDHÆFTET KURSUS: ...]. Besvar specifikt om det kursus.

KVALITET: Hvis en søgning returnerer 0 resultater, sig det ærligt og foreslå alternative søgetermer. Gæt aldrig på kurser der ikke eksisterer."""

SESSION_TTL = 3600


# ── Phase 3: Conversation State Machine ──

def _detect_conversation_stage(sid, messages):
    """Detect conversation stage based on message history and context. Rule-based, no API call."""
    user_msg_count = sum(1 for m in messages if m.get("role") == "user")
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))
    has_profile = sid in USER_PROFILES and USER_PROFILES[sid].get("summary")
    tool_call_count = sum(1 for m in messages if m.get("role") == "tool")

    if user_msg_count <= 1:
        return "greeting"
    elif user_msg_count <= 3 and not has_profile and shown_count == 0:
        return "needs_discovery"
    elif shown_count == 0 or (user_msg_count <= 4 and tool_call_count <= 2):
        return "searching"
    elif shown_count >= 2 and user_msg_count >= 4:
        return "comparing"
    elif shown_count >= 1 and user_msg_count >= 6:
        return "deciding"
    else:
        return "searching"


_STAGE_HINTS = {
    "greeting": "Velkommen brugeren varmt. Spørg hvad de leder efter og om de har specifikke behov.",
    "needs_discovery": "Stil 1-2 opfølgende spørgsmål om brugerens mål, budget, lokation eller tidsramme før du søger.",
    "searching": "Forklar kort hvorfor de viste kurser er relevante for brugerens behov.",
    "comparing": "Hjælp brugeren med at vælge ved at fremhæve fordele og ulemper ift. deres behov. Foreslå compare_courses hvis relevant.",
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
    except Exception:
        pass


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
            })
    # Persist to SQLite
    try:
        _get_store().update_session_field(sid, "shown_products", sp["products"])
    except Exception:
        pass


def _build_shown_products_message(sid):
    """Build an ephemeral system message listing all products shown in this session."""
    sp = SHOWN_PRODUCTS.get(sid, {}).get("products", [])
    if not sp:
        return None
    lines = ["VISTE KURSER (brug denne liste til at besvare opfølgningsspørgsmål):"]
    for p in sp:
        locs = ", ".join(p.get("locations", [])) if p.get("locations") else "N/A"
        lines.append(f"{p['index']}. \"{p['title']}\" — {p['price']} kr — handle: {p['handle']} — lokationer: {locs}")
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
        except Exception:
            pass
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

    # Check for bullet-point lists of courses in the response
    lines = response_text.strip().split('\n')
    list_line_count = sum(1 for line in lines if line.strip().startswith(('-', '•', '*', '1.', '2.', '3.')))
    if list_line_count >= 3:
        return False  # Likely listing courses in text

    # Check for "kr" appearing multiple times (price listing)
    kr_count = response_text.lower().count(' kr')
    if kr_count >= 3:
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


# ── Main Agent Loop ──

def handle_agentic_ask(user_query, session):
    """
    Core Agent Loop with all 6 phases integrated.
    """
    _cleanup_stale_sessions()

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())

    sid = session["session_id"]

    # Initialize or load conversation memory
    if sid not in CHAT_MEMORY:
        # Try loading from persistent store first (Phase 4)
        try:
            stored = _get_store().load_session(sid)
            if stored and stored.get("user_profile"):
                USER_PROFILES[sid] = {"summary": stored["user_profile"], "last_updated": time.time()}
            if stored and stored.get("shown_products"):
                SHOWN_PRODUCTS[sid] = {"products": stored["shown_products"], "last_active": time.time()}
        except Exception:
            pass

        CHAT_MEMORY[sid] = [{"role": "system", "content": SYSTEM_PROMPT}]

        # Ensure session exists in persistent store
        try:
            _get_store().save_session(sid)
        except Exception:
            pass

    messages = CHAT_MEMORY[sid]
    messages.append({"role": "user", "content": user_query})

    if sid in SHOWN_PRODUCTS:
        SHOWN_PRODUCTS[sid]["last_active"] = time.time()

    # Phase 1: Intent classification (lightweight, fast)
    user_profile_text = USER_PROFILES.get(sid, {}).get("summary", "")
    shown_count = len(SHOWN_PRODUCTS.get(sid, {}).get("products", []))
    intent_result = classify_intent(user_query, user_profile=user_profile_text, shown_products_count=shown_count)
    intent = intent_result.get("intent", "discovery")
    intent_hint = intent_result.get("hint", "")

    # Phase 3: Detect conversation stage
    stage = _detect_conversation_stage(sid, messages)
    CONVERSATION_STAGES[sid] = stage
    stage_hint = _STAGE_HINTS.get(stage, "")

    # Extract user profile periodically
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
        except Exception:
            pass

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

            # User profile context
            profile_msg = _build_user_profile_message(sid)
            if profile_msg:
                ephemeral_messages.insert(insert_idx, profile_msg)
                insert_idx += 1

            # Shown products context
            shown_msg = _build_shown_products_message(sid)
            if shown_msg:
                ephemeral_messages.insert(insert_idx, shown_msg)
                insert_idx += 1

            # Phase 1 + 3: Inject intent + stage hints as guidance
            guidance_parts = []
            if intent_hint:
                guidance_parts.append(f"INTENT: {intent} — {intent_hint}")
            if stage_hint:
                guidance_parts.append(f"SAMTALEFASE: {stage} — {stage_hint}")
            if guidance_parts:
                ephemeral_messages.insert(insert_idx, {
                    "role": "system",
                    "content": "\n".join(guidance_parts)
                })

            # Agent Loop
            had_tool_calls = False
            max_iterations = 5  # Safety limit
            iteration = 0

            while iteration < max_iterations:
                iteration += 1
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=ephemeral_messages,
                    tools=OPENAI_TOOLS,
                    stream=False
                )

                message = response.choices[0].message
                messages.append(message.model_dump())
                ephemeral_messages.append(message.model_dump())

                if message.tool_calls:
                    had_tool_calls = True
                    for tool_call in message.tool_calls:
                        tool_result_str = execute_tool(tool_call)
                        tool_result_dict = json.loads(tool_result_str)

                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": json.dumps({k: v for k, v in tool_result_dict.items() if not k.startswith("raw_")})
                        }
                        messages.append(tool_msg)
                        ephemeral_messages.append(tool_msg)

                        # Log tool call (Phase 4)
                        try:
                            results_count = tool_result_dict.get("count", len(tool_result_dict.get("results", [])))
                            _get_store().log_event(sid, "tool_call",
                                                   query_text=user_query,
                                                   tool_used=tool_call.function.name,
                                                   results_count=results_count)
                        except Exception:
                            pass

                        # UI Interceptor
                        fn = tool_call.function.name
                        if fn in ("search_courses", "filter_courses") and "raw_products" in tool_result_dict:
                            raw_products = tool_result_dict["raw_products"]
                            if raw_products:
                                ui_html = render_multi_course_media(raw_products)
                                buffered_ui_html.append(ui_html)
                                compact = tool_result_dict.get("results", [])
                                _track_shown_products(sid, compact)

                        elif fn == "get_course_details" and "raw_product" in tool_result_dict:
                            ui_html = render_product_media(tool_result_dict["raw_product"])
                            buffered_ui_html.append(ui_html)

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

            if not _check_response_quality(full_text, had_tool_calls):
                # Response violates visual rule — regenerate with correction
                ephemeral_messages.append({
                    "role": "system",
                    "content": "KORREKTION: Dit svar indeholder en liste over kurser i teksten. Husk VISUEL REGEL: kursuskortene vises automatisk. Omskriv dit svar til 1-2 naturlige sætninger uden lister, priser eller kursusnavne."
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

            # Phase 6: Low-confidence warning
            if had_tool_calls and not buffered_ui_html:
                # Tools were called but no products rendered = likely 0 results
                if "no_results" not in full_text.lower() and "ingen" not in full_text.lower():
                    full_text += "\n\n*Jeg fandt desværre ingen kurser der matchede. Prøv eventuelt at omformulere din søgning.*"

            # Stream text response
            chunk_size = 12
            for i in range(0, len(full_text), chunk_size):
                yield f"data: {json.dumps({'type': 'chunk', 'content': full_text[i:i+chunk_size]})}\n\n"

            # Stream buffered product HTML cards
            for html_chunk in buffered_ui_html:
                yield f"data: {json.dumps({'type': 'product', 'html': html_chunk})}\n\n"

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
