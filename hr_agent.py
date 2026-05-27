"""
HR Chatbot Agent — AI assistant for HR managers within the HR dashboard.
Separate from the employee chatbot. Uses HR-specific tools for company analytics.
"""
import json
import time
import uuid
from flask import session, current_app, Response, stream_with_context
from db_compat import close_flask_mysql_connection
from hr_tools import execute_hr_tool


# In-memory chat history per HR session
HR_CHAT_MEMORY = {}  # {hr_session_id: [messages]}
HR_SESSION_TTL = 3600

HR_SYSTEM_PROMPT = """Du er en AI-assistent for HR-ledere i Futurematch-platformen. Du hjaelper med at analysere uddannelsesdata, kompetencer og medarbejderudvikling.

DIN ROLLE:
- Du er en strategisk HR-raadgiver der hjaelper med datadrevet beslutningstagning
- Du har adgang til virksomhedens uddannelses- og kompetencedata
- Du giver handlingsrettede anbefalinger baseret paa data, ikke bare tal

HVAD DU KAN:
- Vise trainingsstatus per afdeling og medarbejder
- Analysere kompetencegab og anbefale indsatsomraader
- Give budgetoverblik og advarsler
- Finde og anbefale kurser til teams
- Bruge Futurematch-katalogets produkt-, kategori- og leverandørsider som kilde til konkrete kursuslinks
- Vurdere leverandøraftaler, aktive/inaktive leverandører og katalogdækning
- Lave konkrete træningsplaner der binder kompetencegab, budget og kursuskatalog sammen
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
- Brug værktøjer før du nævner konkrete budgetter, medarbejdertal, kompetencegab, leverandørstatus eller kursusanbefalinger.
- Brug interne Futurematch-links (/products, /categories, /vendors). Brug aldrig gamle webshoplinks.

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

            from ai_runtime import (
                PROMPT_VERSION as AI_PROMPT_VERSION,
                log_agent_run,
                log_tool_run,
                main_model,
                make_run_id,
                run_agent_with_fallback,
            )
            from ai_tool_registry import get_hr_tool_selection, make_tool_choice, tool_name, toolset_enabled

            if toolset_enabled():
                hr_tools, toolset_meta = get_hr_tool_selection(
                    company_id=flask_session.get("company_id"),
                    user_query=user_query,
                )
            else:
                from hr_tools import HR_TOOLS
                hr_tools = HR_TOOLS
                toolset_meta = {
                    "version": "legacy-hr-all-tools",
                    "tool_names": [tool_name(t) for t in hr_tools],
                    "forced_tool": None,
                }
            run_id = make_run_id()

            def _hr_executor(tool_call, username=None, session_id=None):
                return execute_hr_tool(tool_call)

            runtime_result = run_agent_with_fallback(
                messages=clean_messages,
                tools=hr_tools,
                tool_executor=_hr_executor,
                username=flask_session.get("user"),
                session_id=hr_sid,
                model=main_model(),
                tool_choice=make_tool_choice(toolset_meta.get("forced_tool")),
                max_iterations=5,
                prompt_cache_key=f"futurematch-hr:{toolset_meta.get('version')}",
            )
            final_text = runtime_result.text or ""

            try:
                log_agent_run(
                    getattr(current_app, "mysql", None),
                    run_id=run_id,
                    session_id=hr_sid,
                    company_id=flask_session.get("company_id"),
                    username=flask_session.get("user"),
                    agent_scope="hr",
                    runtime=runtime_result.runtime,
                    model=main_model(),
                    prompt_version=AI_PROMPT_VERSION,
                    toolset_version=toolset_meta.get("version", ""),
                    tool_names=toolset_meta.get("tool_names", []),
                    response_id=runtime_result.response_id,
                    status="ok",
                    fallback_reason=runtime_result.fallback_reason,
                    latency_ms=runtime_result.latency_ms,
                    usage=runtime_result.usage,
                )
            except Exception:
                pass

            for tool_result in runtime_result.tool_results:
                yield f"data: {json.dumps({'type': 'tool_call', 'name': tool_result.name})}\n\n"
                try:
                    log_tool_run(
                        getattr(current_app, "mysql", None),
                        run_id=run_id,
                        session_id=hr_sid,
                        company_id=flask_session.get("company_id"),
                        username=flask_session.get("user"),
                        agent_scope="hr",
                        result=tool_result,
                    )
                except Exception:
                    pass

            close_flask_mysql_connection()
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
        finally:
            close_flask_mysql_connection()

    return Response(
        stream_with_context(stream_generator()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
