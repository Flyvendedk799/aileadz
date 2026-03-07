"""
HR Chatbot Agent — AI assistant for HR managers within the HR dashboard.
Separate from the employee chatbot. Uses HR-specific tools for company analytics.
"""
import json
import time
import uuid
import openai
from flask import session, current_app, Response, stream_with_context
from hr_tools import HR_TOOLS, execute_hr_tool


# In-memory chat history per HR session
HR_CHAT_MEMORY = {}  # {hr_session_id: [messages]}
HR_SESSION_TTL = 3600

HR_SYSTEM_PROMPT = """Du er en AI-assistent for HR-ledere i AiLeadZ-platformen. Du hjaelper med at analysere uddannelsesdata, kompetencer og medarbejderudvikling.

DIN ROLLE:
- Du er en strategisk HR-raadgiver der hjaelper med datadrevet beslutningstagning
- Du har adgang til virksomhedens uddannelses- og kompetencedata
- Du giver handlingsrettede anbefalinger baseret paa data, ikke bare tal

HVAD DU KAN:
- Vise trainingsstatus per afdeling og medarbejder
- Analysere kompetencegab og anbefale indsatsomraader
- Give budgetoverblik og advarsler
- Finde og anbefale kurser til teams
- Vise chatbot-brugsstatistik for medarbejdere
- Identificere inaktive medarbejdere og risikoomraader
- Generere traeningsrapporter

SAMTALEFLOW:
- Vaer direkte og handlingsorienteret. HR-ledere har travlt.
- Naar du viser data, fremhaev de vigtigste indsigter foerst.
- Giv altid 1-2 konkrete handlingsforslag baseret paa dataen.
- Brug dansk, professionelt men venligt.

SVARLÆNGDE:
- Korte, praecise svar. Brug bullet points til data.
- Maks 3-4 saetninger mellem datablokke.
- Vis altid den vigtigste metrik foerst.

REGLER:
- Du har KUN adgang til denne virksomheds data. Nævn aldrig andre virksomheder.
- Vis aldrig personfoelsomme data som CPR-numre eller loenoplysninger.
- Hvis der mangler data, foreslaa hvordan HR kan udfylde det (f.eks. tilfoej kompetencemaal).

OPFØLGNINGSFORSLAG:
Afslut altid med 2-3 konkrete forslag til naeste skridt, formateret som:
<suggestions>["forslag 1", "forslag 2", "forslag 3"]</suggestions>
"""


def _cleanup_hr_sessions():
    """Remove stale HR chat sessions."""
    now = time.time()
    stale = [sid for sid, msgs in HR_CHAT_MEMORY.items()
             if msgs and isinstance(msgs[-1], dict) and
             msgs[-1].get("_ts", 0) < now - HR_SESSION_TTL]
    for sid in stale:
        del HR_CHAT_MEMORY[sid]


def handle_hr_ask(user_query, flask_session):
    """Handle an HR chatbot query. Returns SSE stream response."""
    _cleanup_hr_sessions()

    hr_sid = flask_session.get("hr_chat_session_id")
    if not hr_sid:
        hr_sid = f"hr_{uuid.uuid4()}"
        flask_session["hr_chat_session_id"] = hr_sid

    # Initialize chat memory
    if hr_sid not in HR_CHAT_MEMORY:
        HR_CHAT_MEMORY[hr_sid] = [
            {"role": "system", "content": HR_SYSTEM_PROMPT}
        ]

    messages = HR_CHAT_MEMORY[hr_sid]
    messages.append({"role": "user", "content": user_query, "_ts": time.time()})

    # Inject company context
    company_name = flask_session.get('company_name', 'Virksomheden')
    user_role = flask_session.get('company_role', 'hr_manager')
    user_dept = flask_session.get('company_department', '')

    context_parts = [f"HR-BRUGER: {flask_session.get('user', 'ukendt')} (rolle: {user_role})"]
    context_parts.append(f"VIRKSOMHED: {company_name}")
    if user_dept:
        context_parts.append(f"AFDELING: {user_dept}")

    # Check if context message already exists, update it
    context_msg = {"role": "system", "content": "\n".join(context_parts)}
    if len(messages) > 2 and messages[1].get("role") == "system" and messages[1].get("content", "").startswith("HR-BRUGER:"):
        messages[1] = context_msg
    else:
        messages.insert(1, context_msg)

    def stream_generator():
        try:
            yield f"data: {json.dumps({'type': 'ping', 'content': 'ok'})}\n\n"

            ephemeral_messages = [m for m in messages if not m.get("_ts") or m.get("role") != "user" or m == messages[-1]]
            # Clean _ts from messages for API
            clean_messages = []
            for m in ephemeral_messages:
                clean = {k: v for k, v in m.items() if k != "_ts"}
                clean_messages.append(clean)

            max_iterations = 5
            iteration = 0
            final_text = ""

            while iteration < max_iterations:
                iteration += 1
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=clean_messages,
                    tools=HR_TOOLS,
                    stream=False
                )

                choice = response.choices[0]
                msg = choice.message

                if msg.tool_calls:
                    # Execute tools
                    clean_messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls
                        ]
                    })

                    for tc in msg.tool_calls:
                        yield f"data: {json.dumps({'type': 'tool_call', 'name': tc.function.name})}\n\n"
                        result = execute_hr_tool(tc)
                        clean_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result
                        })

                    # If there was partial text before tools, stream it
                    if msg.content:
                        yield f"data: {json.dumps({'type': 'text', 'content': msg.content})}\n\n"
                else:
                    # Final text response
                    final_text = msg.content or ""
                    break

            if final_text:
                # Stream the response in chunks for a nice UX
                chunk_size = 20
                for i in range(0, len(final_text), chunk_size):
                    chunk = final_text[i:i+chunk_size]
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"

            # Store assistant response in memory
            messages.append({"role": "assistant", "content": final_text, "_ts": time.time()})

            # Prune if too long
            if len(messages) > 30:
                system_msgs = [m for m in messages[:3] if m.get("role") == "system"]
                recent = messages[-20:]
                HR_CHAT_MEMORY[hr_sid] = system_msgs + recent

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            print(f"[HR Agent Error] {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'content': f'Der opstod en fejl: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(stream_generator()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
