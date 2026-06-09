"""HR Chatbot Agent — AI assistant for HR managers within the HR dashboard."""
import json
import time
import uuid
from flask import session, current_app, Response, stream_with_context
from db_compat import close_flask_mysql_connection
from hr_tools import execute_hr_tool

# Grounding / prompt-injection hardening helpers. Guarded import so a missing or
# broken module can never crash create_app or the live HR agent loop — we fall
# back to identity delimiting (text passed through unchanged) if it's
# unavailable. Mirrors the employee path (app1/agent.py) verbatim so HR answers
# are as safe as employee answers.
try:
    import grounding as _grounding
except Exception:  # pragma: no cover - boot-safety
    _grounding = None


def _fence(label, text):
    """Wrap tenant/user-supplied text as DATA via grounding.delimit_untrusted.

    Use at every point where tenant-controlled free text (company name, the HR
    user's display name, department label) enters the system prompt/context, so
    a stored prompt-injection in any of those values can't hijack the HR system
    prompt. Falls back to the raw text (identity) if grounding is unavailable or
    raises, so context assembly never breaks and behavior degrades gracefully.
    """
    try:
        if _grounding is not None:
            fenced = _grounding.delimit_untrusted(label, text)
            if fenced:
                return fenced
    except Exception:
        pass
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _hr_grounding_evidence(runtime_result):
    """Build the chain-of-custody evidence base for THIS HR turn.

    Flattens the raw tool-result outputs the model actually saw into a flat list
    of strings that grounding.claims_supported accepts. The HR money-quoting
    figures (budgets, spend, ROI, headcount) live in those tool outputs, so an
    answer that asserts a figure absent from them is a likely hallucination.
    Never raises — degrades to an empty evidence list.
    """
    evidence = []
    try:
        for tr in getattr(runtime_result, "tool_results", None) or []:
            out = getattr(tr, "output", None)
            if out:
                evidence.append(out)
    except Exception:
        pass
    return evidence


HR_CHAT_MEMORY = {}
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


def get_hr_system_prompt():
    try:
        from branding_service import get_branding, is_whitelabel_active
        cid = session.get('company_id')
        if cid and is_whitelabel_active(cid):
            name = get_branding(cid).get('company_name') or 'virksomheden'
            return HR_SYSTEM_PROMPT.replace('Futurematch-platformen', f'{name}s læringsplatform').replace('Futurematch', name)
    except Exception:
        pass
    return HR_SYSTEM_PROMPT


def _cleanup_hr_sessions():
    now = time.time()
    stale = [sid for sid, msgs in HR_CHAT_MEMORY.items()
             if msgs and isinstance(msgs[-1], dict) and
             msgs[-1].get("_ts", 0) < now - HR_SESSION_TTL]
    for sid in stale:
        del HR_CHAT_MEMORY[sid]


def _classify_hr_intent(user_query: str) -> str:
    q = (user_query or "").lower()
    if any(w in q for w in ("hej", "hello", "tak", "thanks", "godmorgen")) and len(q.split()) <= 4:
        return "chit_chat"
    if any(w in q for w in ("budget", "forbrug", "økonomi", "remaining")):
        return "budget"
    if any(w in q for w in ("kompetence", "skill", "gap", "mangler")):
        return "skills"
    if any(w in q for w in ("kursus", "kurser", "uddannelse", "træning", "plan")):
        return "catalog"
    return "general"


def handle_hr_ask(user_query, flask_session):
    """Handle an HR chatbot query. Returns SSE stream response."""
    _cleanup_hr_sessions()

    hr_sid = flask_session.get("hr_chat_session_id")
    if not hr_sid:
        hr_sid = f"hr_{uuid.uuid4()}"
        flask_session["hr_chat_session_id"] = hr_sid

    if hr_sid not in HR_CHAT_MEMORY:
        HR_CHAT_MEMORY[hr_sid] = [
            {"role": "system", "content": get_hr_system_prompt()}
        ]

    messages = HR_CHAT_MEMORY[hr_sid]
    messages.append({"role": "user", "content": user_query, "_ts": time.time()})

    company_name = flask_session.get('company_name', 'Virksomheden')
    user_role = flask_session.get('company_role', 'hr_manager')
    user_dept = flask_session.get('company_department', '')
    user_name = flask_session.get('user', 'ukendt')

    # The role is an internal enum (trusted); the company name, the HR user's
    # display name and the department label are tenant-stored free text, so a
    # stored prompt-injection in any of them must NOT be obeyed as instructions.
    # Fence each as DATA (identity fallback keeps the value if grounding is off).
    context_parts = [
        f"HR-BRUGER (rolle: {user_role}): {_fence('HR-BRUGERNAVN', user_name)}",
        f"VIRKSOMHED: {_fence('VIRKSOMHEDSNAVN', company_name)}",
    ]
    if user_dept:
        context_parts.append(f"AFDELING: {_fence('AFDELING', user_dept)}")

    context_msg = {"role": "system", "content": "\n".join(context_parts)}
    if len(messages) > 2 and messages[1].get("role") == "system" and messages[1].get("content", "").startswith("HR-BRUGER"):
        messages[1] = context_msg
    else:
        messages.insert(1, context_msg)

    intent = _classify_hr_intent(user_query)

    def stream_generator():
        try:
            yield f"data: {json.dumps({'type': 'ping', 'content': 'ok'})}\n\n"

            clean_messages = []
            for m in messages:
                if m.get("_ts") and m is not messages[-1] and m.get("role") == "user":
                    continue
                clean_messages.append({k: v for k, v in m.items() if k != "_ts"})

            from ai_context import build_few_shot_message, choose_max_iterations, few_shot_mode, prune_conversation_memory, run_chitchat_turn
            from ai_runtime import (
                PROMPT_VERSION as AI_PROMPT_VERSION,
                build_tool_call_event,
                check_turn_token_budget,
                choose_turn_model,
                estimate_messages_tokens,
                in_rate_limit_cooldown,
                iter_completion_stream,
                log_agent_run,
                log_tool_run,
                make_run_id,
                prepare_messages_for_turn,
                run_agent_with_fallback,
                update_agent_run_quality,
                user_facing_error_message,
            )
            from ai_tool_registry import get_hr_tool_selection, make_tool_choice, tool_name, toolset_enabled

            clean_messages = prune_conversation_memory(clean_messages, keep_recent=14, trigger_at=22)

            pre_turn_estimate = estimate_messages_tokens(prepare_messages_for_turn(clean_messages))
            if len(clean_messages) <= 14 and pre_turn_estimate < 18000 and few_shot_mode() not in {"0", "false", "no", "off", "never"}:
                few_shot = build_few_shot_message("")
                if few_shot:
                    few_shot = {
                        "role": "system",
                        "content": (
                            "EKSEMPLER (HR kort stil):\n"
                            "Bruger: Hvad bruger vi på uddannelse?\n"
                            "Assistent: [budget] Her er et kort overblik — vil du se det per afdeling?\n\n"
                            "Bruger: Find kurser til salgsteamet\n"
                            "Assistent: [katalog] Her er relevante kurser — skal jeg filtrere på format eller budget?"
                        ),
                    }
                    clean_messages.insert(1, few_shot)

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

            token_estimate = estimate_messages_tokens(prepare_messages_for_turn(clean_messages))
            allowed, budget_message, compaction_level = check_turn_token_budget(clean_messages)
            if not allowed:
                yield f"data: {json.dumps({'type': 'text', 'content': budget_message})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            turn_model = choose_turn_model(
                intent=intent,
                tool_count=len(hr_tools),
                token_estimate=token_estimate,
                prefer_quality=intent in {"skills", "catalog", "budget"},
            )
            run_id = make_run_id()

            def _hr_executor(tool_call, username=None, session_id=None):
                return execute_hr_tool(tool_call)

            if not hr_tools and intent == "chit_chat":
                runtime_result = run_chitchat_turn(clean_messages, intent=intent)
            else:
                if hr_tools:
                    yield f"data: {json.dumps({'type': 'thinking', 'content': 'Analyserer…'})}\n\n"
                runtime_result = run_agent_with_fallback(
                    messages=clean_messages,
                    tools=hr_tools,
                    tool_executor=_hr_executor,
                    username=flask_session.get("user"),
                    session_id=hr_sid,
                    model=turn_model,
                    tool_choice=make_tool_choice(toolset_meta.get("forced_tool")),
                    max_iterations=choose_max_iterations(intent, scope="hr"),
                    prompt_cache_key=f"futurematch-hr:{toolset_meta.get('version')}:{AI_PROMPT_VERSION}",
                    agent_scope="hr",
                )
            final_text = runtime_result.text or ""
            if in_rate_limit_cooldown():
                compaction_level = "cooldown"

            try:
                log_agent_run(
                    getattr(current_app, "mysql", None),
                    run_id=run_id,
                    session_id=hr_sid,
                    company_id=flask_session.get("company_id"),
                    username=flask_session.get("user"),
                    agent_scope="hr",
                    runtime=runtime_result.runtime,
                    model=turn_model,
                    prompt_version=AI_PROMPT_VERSION,
                    toolset_version=toolset_meta.get("version", ""),
                    tool_names=toolset_meta.get("tool_names", []),
                    response_id=runtime_result.response_id,
                    status="ok",
                    fallback_reason=runtime_result.fallback_reason,
                    latency_ms=runtime_result.latency_ms,
                    usage=runtime_result.usage,
                    compaction_level=runtime_result.compaction_level or compaction_level,
                    runtime_path=runtime_result.runtime_path or runtime_result.runtime,
                )
            except Exception:
                pass

            for tool_result in runtime_result.tool_results:
                yield f"data: {json.dumps(build_tool_call_event(tool_result, agent_scope='hr'), ensure_ascii=False)}\n\n"
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
            final_messages = list(
                runtime_result.stream_messages or runtime_result.messages or clean_messages
            )
            full_text = runtime_result.text or ""
            if runtime_result.needs_final_stream or not full_text.strip():
                for token in iter_completion_stream(final_messages, model=turn_model):
                    full_text += token
                    yield f"data: {json.dumps({'type': 'text', 'content': token})}\n\n"
            elif full_text:
                yield f"data: {json.dumps({'type': 'text', 'content': full_text})}\n\n"

            # ── Grounding circuit-breaker (HR money-quoting path) ──
            # HR answers quote real budgets, spend, ROI and headcount. After the
            # complete answer has streamed, validate it against THIS turn's tool
            # results (the canonical source for those figures) and, if it asserts
            # a price/date/title not backed by them, append at most ONE guarded
            # Danish disclaimer (log-don't-block — we never suppress the answer,
            # only nudge verification). The violation is logged through the
            # existing ai_runtime quality path so the dormant grounding_violation
            # column finally populates for HR turns. Pure check + trailing note =
            # zero new API cost. Fully guarded so the SSE path is never broken.
            grounding_violation = False
            try:
                if (
                    _grounding is not None
                    and runtime_result.tool_results
                    and full_text.strip()
                ):
                    _verdict = _grounding.grounding_disclaimer(
                        full_text, _hr_grounding_evidence(runtime_result)
                    )
                    if _verdict.get("violation"):
                        grounding_violation = True
                        disclaimer = _verdict.get("disclaimer") or ""
                        if disclaimer:
                            note = "\n\n" + disclaimer
                            full_text += note
                            yield f"data: {json.dumps({'type': 'text', 'content': note})}\n\n"
            except Exception as _grounding_err:
                print(f"[HR Grounding Check Error] {_grounding_err}")

            # Backfill the grounding flag onto the run row logged above (guarded,
            # idempotent, no-op when the flag is False/None or tracing is off).
            try:
                update_agent_run_quality(
                    getattr(current_app, "mysql", None),
                    run_id=run_id,
                    grounding_violation=grounding_violation,
                )
            except Exception:
                pass

            messages.append({"role": "assistant", "content": full_text, "_ts": time.time()})
            HR_CHAT_MEMORY[hr_sid] = prune_conversation_memory(messages, keep_recent=16, trigger_at=28)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            print(f"[HR Agent Error] {e}")
            import traceback
            traceback.print_exc()
            # Resolve the message helper defensively: if the failure happened
            # before the in-try `from ai_runtime import ...` ran, the name would
            # be unbound and raise NameError out of the generator.
            try:
                from ai_runtime import user_facing_error_message as _ufem
                _err_msg = _ufem(e)
            except Exception:
                _err_msg = "Der opstod en fejl. Prøv venligst igen."
            yield f"data: {json.dumps({'type': 'error', 'content': _err_msg})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            close_flask_mysql_connection()

    return Response(
        stream_with_context(stream_generator()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
