from flask import Response, stream_with_context
import json
import openai
import time
from app1.tools import OPENAI_TOOLS, execute_tool
from . import render_multi_course_media, render_product_media

import uuid

# Server-side memory buffer. Flask session cookies cannot be mutated mid-stream because headers are already sent.
CHAT_MEMORY = {}

# Tracks products shown to each session for follow-up references ("den til 9999 kr", "den anden")
SHOWN_PRODUCTS = {}  # {session_id: {"products": [...], "last_active": timestamp}}

SYSTEM_PROMPT = """Du er en AI-uddannelsesrådgiver for AiLead.

VISUEL REGEL: Når du bruger tools til at finde kurser, vises de fundne kurser automatisk på skærmen som interaktive visuelle kort ved siden af din tekst. Du må ALDRIG skrive lister eller bulletpoints over kursusnavne, priser, lokationer eller beskrivelser i dit eget tekstsvar. Lad kortene tale for sig selv. Skriv naturligt og hjælpende (maks 2-3 sætninger ad gangen).

VÆRKTØJSSTRATEGI:
- search_courses: Brug til åbne/semantiske forespørgsler ("kurser om ledelse", "noget med kommunikation"). Returnerer de mest relevante kurser via embeddings.
- filter_courses: Brug når brugeren har strukturerede krav: pris ("under 5000 kr"), lokation ("i Aarhus"), type ("e-learning"), tags ("IT"), eller kombinationer. Kan også modtage en query til semantisk rangering af de filtrerede resultater.
- get_course_details: Brug til at hente detaljer om et specifikt kursus via dets handle. Brug dette når brugeren vil vide mere om et bestemt kursus.

SØGESTRATEGI: Fokuser ALTID på brugerens udtrykte BEHOV eller MÅL — ikke deres jobtitel eller rolle. Eksempel: "Jeg er salgsleder og vil gerne blive bedre til planlægning" → søg efter "planlægning" (evt. "planlægning ledere"), IKKE "ledelse" eller "salgsledelse". Brugerens rolle er kontekst, men deres behov er søgeordet.

KOMBINATIONSSTRATEGI: For komplekse forespørgsler som "e-learning om projektledelse under 5000 kr", brug filter_courses med product_type="E-learning", price_max=5000, query="projektledelse". Undgå at lave flere separate søgninger når filter_courses kan håndtere det i ét kald.

OPFØLGNINGSSTRATEGI: Brug ALTID listen 'VISTE KURSER' (injiceret som system-besked) til at identificere kurser brugeren refererer til. Eksempler:
- "den til 9999 kr" → find kurset med den pris i VISTE KURSER
- "den anden" / "nummer 2" → brug indeks 2 fra VISTE KURSER
- "den billigste" → sammenlign priser i VISTE KURSER
- "fortæl mere om den fra Aarhus" → match lokation i VISTE KURSER
Brug derefter get_course_details med det korrekte handle."""

SESSION_TTL = 3600  # 1 hour


def _cleanup_stale_sessions():
    """Remove sessions older than SESSION_TTL to prevent memory leaks."""
    now = time.time()
    stale = [sid for sid, data in SHOWN_PRODUCTS.items() if now - data.get("last_active", 0) > SESSION_TTL]
    for sid in stale:
        SHOWN_PRODUCTS.pop(sid, None)
        CHAT_MEMORY.pop(sid, None)


def _track_shown_products(sid, compact_results):
    """Add products to the shown products list for this session."""
    if sid not in SHOWN_PRODUCTS:
        SHOWN_PRODUCTS[sid] = {"products": [], "last_active": time.time()}
    sp = SHOWN_PRODUCTS[sid]
    sp["last_active"] = time.time()
    for cr in compact_results:
        # Avoid duplicates by handle
        if not any(p.get("handle") == cr.get("handle") for p in sp["products"]):
            sp["products"].append({
                "index": len(sp["products"]) + 1,
                "title": cr.get("title"),
                "handle": cr.get("handle"),
                "price": cr.get("price"),
                "vendor": cr.get("vendor"),
                "locations": cr.get("locations", []),
            })


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


def _summarize_pruned_messages(messages_to_prune):
    """Create a conversation summary from pruned messages (no API call, pure extraction)."""
    summary_parts = []
    for msg in messages_to_prune:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content or role == "system":
            continue
        if role == "user":
            summary_parts.append(f"Bruger: {content[:100]}")
        elif role == "assistant":
            summary_parts.append(f"Assistent: {content[:100]}")
        elif role == "tool":
            # Extract titles from tool results
            try:
                data = json.loads(content) if isinstance(content, str) else content
                results = data.get("results", [])
                if results:
                    titles = [r.get("title", "?") for r in results[:3]]
                    summary_parts.append(f"Søgeresultater: {', '.join(titles)}")
            except (json.JSONDecodeError, AttributeError):
                pass
    if not summary_parts:
        return None
    return "SAMTALEOVERSIGT (tidligere i samtalen):\n" + "\n".join(summary_parts)


def handle_agentic_ask(user_query, session):
    """
    Core Agent Loop for Phase 2 & 3.
    Maintains memory Server-Side, executes tools natively, and yields SSE streams.
    """
    # Periodic cleanup of stale sessions
    _cleanup_stale_sessions()

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())

    sid = session["session_id"]

    # 1. Initialize or load Conversation Memory
    if sid not in CHAT_MEMORY:
        CHAT_MEMORY[sid] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    messages = CHAT_MEMORY[sid]
    messages.append({"role": "user", "content": user_query})

    # Update session activity timestamp
    if sid in SHOWN_PRODUCTS:
        SHOWN_PRODUCTS[sid]["last_active"] = time.time()

    # Smarter memory management — summarization-based pruning at 15 messages
    if len(messages) > 15:
        system_msg = messages[0]
        # Messages to prune: everything between system prompt and the last 10
        to_prune = messages[1:-10]
        recent = messages[-10:]

        summary_text = _summarize_pruned_messages(to_prune)
        new_messages = [system_msg]
        if summary_text:
            new_messages.append({"role": "system", "content": summary_text})
        new_messages.extend(recent)
        CHAT_MEMORY[sid] = new_messages
        messages = CHAT_MEMORY[sid]

    def stream_generator():
        try:
            # Ping Nginx immediately to keep the HTTP connection alive while the LLM thinks
            yield f"data: {json.dumps({'type': 'ping', 'content': 'ok'})}\n\n"

            buffered_ui_html = []

            # Build ephemeral messages list with SHOWN_PRODUCTS context
            ephemeral_messages = list(messages)
            shown_msg = _build_shown_products_message(sid)
            if shown_msg:
                # Insert after system prompt, before conversation
                ephemeral_messages.insert(1, shown_msg)

            # Agent Loop
            while True:
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=ephemeral_messages,
                    tools=OPENAI_TOOLS,
                    stream=False
                )

                message = response.choices[0].message
                messages.append(message.model_dump())
                ephemeral_messages.append(message.model_dump())

                # If the LLM wants to use a tool
                if message.tool_calls:
                    for tool_call in message.tool_calls:
                        # Execute tool natively
                        tool_result_str = execute_tool(tool_call)
                        tool_result_dict = json.loads(tool_result_str)

                        # Store tool response (strip raw_ fields to save tokens)
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": json.dumps({k: v for k, v in tool_result_dict.items() if not k.startswith("raw_")})
                        }
                        messages.append(tool_msg)
                        ephemeral_messages.append(tool_msg)

                        # ---> UI Interceptor <---
                        fn = tool_call.function.name
                        if fn in ("search_courses", "filter_courses") and "raw_products" in tool_result_dict:
                            raw_products = tool_result_dict["raw_products"]
                            if raw_products:
                                ui_html = render_multi_course_media(raw_products)
                                buffered_ui_html.append(ui_html)
                                # Track shown products for follow-up references
                                compact = tool_result_dict.get("results", [])
                                _track_shown_products(sid, compact)

                        elif fn == "get_course_details" and "raw_product" in tool_result_dict:
                            ui_html = render_product_media(tool_result_dict["raw_product"])
                            buffered_ui_html.append(ui_html)

                    # Loop continues, allowing LLM to read the tool output
                    continue

                # If the LLM just wants to talk, break to stream text
                break

            # Stream the already-generated response in small chunks (no second API call)
            full_text = message.content or ""
            chunk_size = 12
            for i in range(0, len(full_text), chunk_size):
                yield f"data: {json.dumps({'type': 'chunk', 'content': full_text[i:i+chunk_size]})}\n\n"

            # After text has finished typing out, yield any buffered product HTML cards!
            for html_chunk in buffered_ui_html:
                yield f"data: {json.dumps({'type': 'product', 'html': html_chunk})}\n\n"

        except Exception as e:
            print(f"[Agent Error] {e}")
            error_msg = "Beklager, der opstod en teknisk fejl. Prøv venligst igen."
            yield f"data: {json.dumps({'type': 'chunk', 'content': error_msg})}\n\n"
        finally:
            # Always send DONE to close the SSE connection cleanly
            yield "data: [DONE]\n\n"

    response = Response(stream_with_context(stream_generator()), mimetype="text/event-stream")
    # Crucial for PythonAnywhere (uWSGI/Nginx) to prevent buffering and Broken Pipe errors
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response
