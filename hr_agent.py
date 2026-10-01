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


HR_SYSTEM_PROMPT = """Du er AI-assistent for HR-ledere i Futurematch-platformen. Du hjælper med at forstå uddannelsesdata, kompetencer og medarbejderudvikling – og du får tingene gjort, ikke bare forklaret.

DIN ROLLE:
- Du er en strategisk HR-rådgiver, der hjælper med datadrevet beslutningstagning.
- Du har adgang til virksomhedens uddannelses- og kompetencedata gennem værktøjer.
- Du giver handlingsrettede anbefalinger baseret på data, ikke bare tal.

HVAD DU KAN:
- Vise træningsstatus pr. afdeling og medarbejder
- Analysere kompetencegab og pege på indsatsområder
- Give budgetoverblik og advarsler
- Finde og anbefale kurser til teams, med konkrete kursuslinks fra kataloget
- Vurdere leverandøraftaler, aktive/inaktive leverandører og katalogdækning
- Lave træningsplaner, der binder kompetencegab, budget og kursuskatalog sammen
- Vise chatbot-brugsstatistik og identificere inaktive medarbejdere og risikoområder
- Generere træningsrapporter
- Åbne den rigtige HR-side (hr_open_in_app) i stedet for kun at fortælle, hvor den ligger

SAMTALEN:
- Vær direkte og handlingsorienteret. HR-ledere har travlt.
- Start med den vigtigste indsigt. Giv 1-2 konkrete handlingsforslag ud fra dataen.
- Brug dansk, professionelt men venligt. Korte, præcise svar; bullet points til data; højst 3-4 sætninger mellem datablokke.
- Mangler du en oplysning, så brug et værktøj eller gæt fornuftigt og sig det – spørg kun, når du virkelig ikke kan komme videre.

REGLER:
- Du har KUN adgang til denne virksomheds data. Nævn aldrig andre virksomheder.
- Vis aldrig personfølsomme data som CPR-numre eller lønoplysninger.
- Hvis der mangler data, så foreslå, hvordan HR kan udfylde det (f.eks. tilføj kompetencemål).
- Brug værktøjer, før du nævner konkrete budgetter, medarbejdertal, kompetencegab, leverandørstatus eller kursusanbefalinger.
- Brug interne Futurematch-links (/products, /categories, /vendors). Brug aldrig gamle webshoplinks.
- Peger dit svar på en side, brugeren skal handle på (compliance, godkendelser, budgetter, kompetencer, leverandører, rapporter), så åbn den med hr_open_in_app. Det ændrer intet – det navigerer kun.
- Står brugeren allerede på den side, du ville åbne, så svar med dataen i stedet.
- Handlinger, der ændrer data, vises altid som et bekræftelseskort; brugeren bekræfter selv.

AFSLUT med 2-3 konkrete forslag til næste skridt:
<suggestions>["forslag 1", "forslag 2", "forslag 3"]</suggestions>
"""

HR_FEW_SHOT = """EKSEMPLER PÅ STIL (HR):
Spørgsmål: Hvad bruger vi på uddannelse?
Svar i stil: Start med forbruget i år og hvor meget der er tilbage, peg på den afdeling, der skiller sig ud, og foreslå at se den nærmere.

Spørgsmål: Find kurser til salgsteamet
Svar i stil: Giv de 2-3 bedste kurser med pris og format, knyt dem til teamets største gab, og tilbyd at lægge dem i en træningsplan."""


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


def _hr_fallback_suggestions(page):
    """Deterministic next steps when the model forgot its <suggestions> tag."""
    by_page = {
        "budgets": ["Hvilke afdelinger er tæt på budgettet?", "Hvad kan vi nå inden årsskiftet?"],
        "compliance": ["Hvem mangler compliance?", "Hvilke krav udløber snart?"],
        "skill_gaps": ["Største kompetencegap lige nu?", "Find kurser til det største gab"],
        "approvals": ["Hvilke ordrer afventer godkendelse?", "Hvad bør jeg prioritere?"],
    }
    return by_page.get(page) or ["Vis træningsstatus", "Hvor står vi på budgettet?", "Største kompetencegap lige nu?"]


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


# Human-readable Danish label per HR page id, so the model is told which view
# the manager is standing on in the SAME vocabulary the navigation tool uses.
def _hr_page_label(page):
    try:
        from app1.sse_events import HR_DESTINATIONS
        entry = HR_DESTINATIONS.get((page or "").strip())
        return entry[2] if entry else ""
    except Exception:
        return ""




def _log_hr_interaction(flask_session, sid, query, answer, intent, tools_used, latency_ms, message_index):
    """One chatbot_interactions row per HR turn, so HR answers get feedback and show
    up in the same analytics as every other assistant turn. Never raises."""
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute(
            """INSERT INTO chatbot_interactions
                   (company_id, session_id, username, query_text, response_text, query_type, category,
                    response_time_ms, tools_used, conversation_depth, is_logged_in, message_index, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'hr_assistant', %s, %s, %s, 1, %s, NOW())""",
            (flask_session.get("company_id"), sid, flask_session.get("user"), (query or "")[:2000],
             (answer or "")[:2000], intent or "unknown", int(latency_ms or 0),
             ",".join(dict.fromkeys(tools_used))[:500] if tools_used else None, message_index, message_index),
        )
        current_app.mysql.connection.commit()
        cur.close()
    except Exception as exc:
        print(f"[HR interaction log] {exc}")
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass


def _hr_context_layers(flask_session, page):
    """Who is asking and where they stand, as prioritised layers (ai_context_layers)
    instead of a hand-rolled system message that was re-inserted every turn."""
    import ai_context_layers as _ctx
    company_name = flask_session.get('company_name', 'Virksomheden')
    user_role = flask_session.get('company_role', 'hr_manager')
    user_dept = flask_session.get('company_department', '')
    user_name = flask_session.get('user', 'ukendt')
    # The role is an internal enum (trusted); company name, display name and department
    # are tenant-stored free text, so each is fenced as DATA (a stored prompt injection
    # in any of them must not be obeyed as instructions).
    parts = [
        f"HR-BRUGER (rolle: {user_role}): {_fence('HR-BRUGERNAVN', user_name)}",
        f"VIRKSOMHED: {_fence('VIRKSOMHEDSNAVN', company_name)}",
    ]
    if user_dept:
        parts.append(f"AFDELING: {_fence('AFDELING', user_dept)}")
    layers = [_ctx.layer("assistant_context", "\n".join(parts))]
    page_label = _hr_page_label(page)
    if page_label:
        # The page id is an internal enum resolved through HR_DESTINATIONS - our own label.
        layers.append(_ctx.layer(
            "assistant_page",
            f"AKTUEL SIDE: {page_label}. Brugeren står på denne HR-side lige nu – "
            "tolk vage spørgsmål ('hvem mangler her?', 'hvordan ser det ud?') i den kontekst."))
    return layers


def handle_hr_ask(user_query, flask_session, page=None):
    """Handle an HR chatbot query. Returns SSE stream response.

    ``page`` is the ``active_hr_page`` id of the view the panel is embedded on
    (the panel posts it). It reaches the model as context AND the tool selector
    as an additive hint, so "hvem mangler her?" resolves against the page the
    manager is actually looking at instead of being answered generically.

    The conversation is durable: it is loaded from / saved to MySQL
    (``hr_conversations``), so a deploy, a second worker or a new tab continues it.
    """
    import ai_reply
    import hr_conversations

    username = flask_session.get("user")
    hr_sid = hr_conversations.resolve_sid(flask_session, username)
    history = hr_conversations.load(username, hr_sid)
    # The turn's own messages: system prompt + stored transcript + this question.
    base_messages = [{"role": "system", "content": get_hr_system_prompt()}] + history
    base_messages.append({"role": "user", "content": user_query})
    context_layers = _hr_context_layers(flask_session, page)
    intent = _classify_hr_intent(user_query)
    sse = ai_reply.sse

    def stream_generator():
        turn_start = time.time()
        try:
            yield sse({'type': 'ping', 'content': 'ok'})

            from ai_context import choose_max_iterations, few_shot_mode, prune_conversation_memory, run_chitchat_turn
            from ai_runtime import (
                PROMPT_VERSION as AI_PROMPT_VERSION,
                build_tool_call_event,
                check_turn_token_budget,
                choose_turn_model,
                estimate_messages_tokens,
                in_rate_limit_cooldown,
                iter_agent_with_live_tool_events,
                iter_buffered_text_chunks,
                iter_completion_stream,
                live_tool_events_enabled,
                log_agent_run,
                log_tool_run,
                make_run_id,
                prepare_messages_for_turn,
                run_agent_with_fallback,
                update_agent_run_quality,
                user_facing_error_message,
            )
            import ai_context_layers as _ctx
            from ai_tool_registry import get_hr_tool_selection, make_tool_choice, tool_name, toolset_enabled

            pruned = prune_conversation_memory(list(base_messages), keep_recent=14, trigger_at=22)
            layers = list(context_layers)
            pre_turn_estimate = estimate_messages_tokens(prepare_messages_for_turn(pruned))
            if len(pruned) <= 14 and pre_turn_estimate < 18000 and few_shot_mode() not in {"0", "false", "no", "off", "never"}:
                layers.append(_ctx.layer("few_shot", HR_FEW_SHOT))
            clean_messages = pruned[:1] + layers + pruned[1:]

            if toolset_enabled():
                hr_tools, toolset_meta = get_hr_tool_selection(
                    company_id=flask_session.get("company_id"),
                    user_query=user_query,
                    page=page,
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
                yield sse({'type': 'text', 'content': budget_message})
                yield sse({'type': 'done'})
                yield "data: [DONE]\n\n"
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

            live_tool_call_ids = set()
            if not hr_tools and intent == "chit_chat":
                runtime_result = run_chitchat_turn(clean_messages, intent=intent)
            else:
                if hr_tools:
                    yield sse({'type': 'thinking', 'content': 'Analyserer…'})
                agent_kwargs = {
                    "messages": clean_messages,
                    "tools": hr_tools,
                    "tool_executor": _hr_executor,
                    "username": flask_session.get("user"),
                    "session_id": hr_sid,
                    "model": turn_model,
                    "tool_choice": make_tool_choice(toolset_meta.get("forced_tool")),
                    "max_iterations": choose_max_iterations(intent, scope="hr"),
                    "prompt_cache_key": f"futurematch-hr:{toolset_meta.get('version')}:{AI_PROMPT_VERSION}",
                    "agent_scope": "hr",
                    "company_scope": str(flask_session.get("company_id") or ""),
                }
                if hr_tools and live_tool_events_enabled():
                    runtime_result = None
                    for _kind, _payload in iter_agent_with_live_tool_events(
                        agent_kwargs, thread_name="hr-agent-live"
                    ):
                        if _kind == "tool_event":
                            if _payload.get("id"):
                                live_tool_call_ids.add(_payload["id"])
                            yield sse(_payload)
                        elif _kind == "ping":
                            yield sse({'type': 'ping', 'content': 'working'})
                        elif _kind == "result":
                            runtime_result = _payload
                    if runtime_result is None:
                        raise RuntimeError("live tool events: HR agent-loopet leverede intet resultat")
                else:
                    runtime_result = run_agent_with_fallback(**agent_kwargs)
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

            tools_used = []
            for tool_result in runtime_result.tool_results:
                tools_used.append(tool_result.name)
                if tool_result.call_id not in live_tool_call_ids:
                    yield sse(build_tool_call_event(tool_result, agent_scope='hr'))
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
                # Generic confirm_card for HR side-effect tools (Phase 8).
                try:
                    _hr_tr_dict = json.loads(tool_result.output or "{}")
                except (json.JSONDecodeError, TypeError):
                    _hr_tr_dict = {}
                if not isinstance(_hr_tr_dict, dict):
                    _hr_tr_dict = {}
                if tool_result.name == "hr_open_in_app" and _hr_tr_dict.get("target"):
                    # Read-only navigation directive → a button in the panel.
                    yield sse({
                        "type": "ui_action",
                        "action": _hr_tr_dict.get("action", "navigate"),
                        "destination": _hr_tr_dict.get("destination", ""),
                        "target": _hr_tr_dict.get("target", ""),
                        "label": _hr_tr_dict.get("label", "Åbn"),
                        "new_tab": bool(_hr_tr_dict.get("new_tab")),
                    })
                if _hr_tr_dict.get("needs_confirmation"):
                    try:
                        from app1 import confirm_store as _cs
                        _token = _cs.store_pending(
                            hr_sid, "hr", tool_result.name, tool_result.arguments or {}
                        )
                        yield sse({
                            "type": "confirm_card",
                            "token": _token,
                            "action": _hr_tr_dict.get("action", tool_result.name),
                            "summary_da": _hr_tr_dict.get("message_da", ""),
                            "details": _hr_tr_dict.get("details"),
                            "recipient_count": _hr_tr_dict.get("recipient_count"),
                            "price": _hr_tr_dict.get("price"),
                        })
                    except Exception as _ce:
                        print(f"[HR confirm_store error] {tool_result.name}: {_ce}")

            close_flask_mysql_connection()
            final_messages = list(
                runtime_result.stream_messages or runtime_result.messages or clean_messages
            )
            raw_text = runtime_result.text or ""
            flt = ai_reply.SuggestionFilter()
            if runtime_result.needs_final_stream or not raw_text.strip():
                raw_text = ""
                for token in iter_completion_stream(final_messages, model=turn_model):
                    raw_text += token
                    shown = flt.feed(token)
                    if shown:
                        yield sse({'type': 'text', 'content': shown})
                tail = flt.flush()
                if tail:
                    yield sse({'type': 'text', 'content': tail})
            elif raw_text:
                # RT-02: the runtime captured the final answer (one completion saved) -
                # chunk it ~3 words at a time so it still feels typewriter-streamed.
                for piece in iter_buffered_text_chunks(ai_reply.strip_suggestions(raw_text)):
                    yield sse({'type': 'text', 'content': piece})
            full_text = ai_reply.strip_suggestions(raw_text)

            # ── Grounding circuit-breaker (HR money-quoting path) ──
            # HR answers quote real budgets, spend, ROI and headcount. After the answer
            # has streamed, validate it against THIS turn's tool results and, if it
            # asserts a price/date/title not backed by them, append at most ONE guarded
            # Danish disclaimer (log-don't-block). Fully guarded so the SSE path never breaks.
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
                            yield sse({'type': 'text', 'content': note})
            except Exception as _grounding_err:
                print(f"[HR Grounding Check Error] {_grounding_err}")

            try:
                update_agent_run_quality(
                    getattr(current_app, "mysql", None),
                    run_id=run_id,
                    grounding_violation=grounding_violation,
                )
            except Exception:
                pass

            # Chips come as their own event (parsed server-side, like the employee chat).
            suggestions = ai_reply.extract_suggestions(raw_text) or _hr_fallback_suggestions(page)
            yield sse({'type': 'suggestions', 'items': suggestions})

            # Persist the durable transcript, log the turn (feedback needs a row), tell
            # the client which answer it can rate.
            stored = history + [{"role": "user", "content": user_query},
                                {"role": "assistant", "content": full_text}]
            hr_conversations.save(username, hr_sid, stored)
            message_index = sum(1 for m in stored if m["role"] == "assistant")
            _log_hr_interaction(flask_session, hr_sid, user_query, full_text, intent, tools_used,
                                (time.time() - turn_start) * 1000, message_index)
            yield sse({'type': 'meta', 'message_index': message_index})

            yield sse({'type': 'done'})
            yield "data: [DONE]\n\n"

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
            yield sse({'type': 'error', 'content': _err_msg})
            yield sse({'type': 'done'})
            yield "data: [DONE]\n\n"
        finally:
            close_flask_mysql_connection()

    return Response(
        stream_with_context(stream_generator()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
