"""
HR Chatbot Tools — Company-level analytics tools for HR managers.
Separate from employee chatbot tools. Only available in HR dashboard chatbot.
"""
import json
import db_compat  # noqa: F401
import MySQLdb.cursors
from flask import current_app, session
from datetime import datetime, timedelta


# ── Skill-level normalization ──
#
# CANONICAL SCALE: 1..5. Every skill level — HR-assigned or chatbot-derived —
# lives on this single 1-5 scale. The viz (radar max=5, "af 5 mulige", target/
# assign sliders min=1/max=5) is the source of truth for the scale; the data
# sources are normalized to match it.
#
# Two sources of truth for an employee's skill proficiency exist:
#   1) employee_skills_matrix.current_level / company_skill_targets.target_level
#      — INTEGER columns (1..5), HR-managed; already on the canonical scale.
#   2) user_skills.skill_level — a Danish STRING ENUM
#      ('begynder','mellem','avanceret','ekspert'), self-reported via the chatbot
#      profile. The 4-label enum is spread onto the canonical 1-5 scale below so
#      a chatbot-reported "ekspert" reaches the ceiling (5) instead of being
#      permanently capped at 4/5 and manufacturing a phantom gap against a 5/5
#      HR target. begynder/mellem keep their low anchors; avanceret/ekspert take
#      the upper anchors (4/5). BOTH sources must share this scale or gaps flip
#      sign — see the HR_VALUE_PLAN risk register.
#
# The historical bug: skill-gap analytics ran the matrix's INT level through a
# string->int map (e.g. {'begynder':1,...}.get(current_level, 2)). Because
# current_level is already an int, .get(2, 2) ALWAYS returns the default, so
# every gap collapsed to the same wrong baseline. The fix is to normalize ONCE,
# at read/merge time, and NEVER re-map a value that is already numeric.
SKILL_LEVEL_MAP = {
    'begynder': 1,
    'mellem': 2,
    'avanceret': 4,
    'ekspert': 5,
}


def _skill_level_to_int(value, default=0):
    """Normalize a skill level (string label OR already-int) to an int.

    Idempotent guard against the gap bug: if the value is ALREADY numeric it is
    returned unchanged (clamped to 0..5) — the string map is applied ONLY to
    string labels, exactly once.

    Regression contract (the bug this guards against):
        _skill_level_to_int(3)           == 3      # int passes through unchanged
        _skill_level_to_int('avanceret') == 4      # string mapped once (1-5 scale)
        _skill_level_to_int('ekspert')   == 5      # ekspert reaches the ceiling
        _skill_level_to_int('begynder')  == 1
        # the bug was: SKILL_LEVEL_MAP.get(3, default) -> default (WRONG)
    """
    if value is None:
        return default
    # Already numeric (the common matrix case) — never re-map through the string
    # table, just coerce/clamp.
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return max(0, min(5, int(value)))
        except (TypeError, ValueError):
            return default
    # String labels: a pure numeric string ("3") is still a number; a Danish
    # label goes through the canonical map exactly once.
    text = str(value).strip().lower()
    if not text:
        return default
    if text.isdigit():
        return max(0, min(5, int(text)))
    return SKILL_LEVEL_MAP.get(text, default)


# ── HR-specific OpenAI tool definitions ──

HR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_team_training_status",
            "description": "Get training status for a department or the entire company. Shows completed, in-progress, and pending orders per employee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Department name to filter. Leave empty for all departments."
                    },
                    "period_days": {
                        "type": "integer",
                        "description": "Look back period in days. Default 90.",
                        "default": 90
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_skill_gaps",
            "description": "Get skill gap analysis for a department or all departments. Shows targets vs current levels.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Department to analyze. Leave empty for company-wide."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_budget_overview",
            "description": "Get training budget overview per department. Shows annual budget, spent, remaining, and utilization percentage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Department to check. Leave empty for all departments."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_overview",
            "description": "Get details about a specific employee or list employees. Shows training history, skills, activity, and department.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Username of employee to look up. Leave empty to list all."
                    },
                    "department": {
                        "type": "string",
                        "description": "Filter by department."
                    },
                    "inactive_days": {
                        "type": "integer",
                        "description": "Only show employees inactive for this many days."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_training_report",
            "description": "Get aggregated training metrics: total spend, courses completed, average completion time, ROI metrics, top courses ordered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Department filter. Leave empty for company-wide."
                    },
                    "period_days": {
                        "type": "integer",
                        "description": "Reporting period in days. Default 90.",
                        "default": 90
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_actions",
            "description": "Get pending items that need HR attention: unapproved orders, budget alerts, inactive employees, upcoming course deadlines.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_courses_for_team",
            "description": "Search for courses to recommend to team members. Uses the same course catalog as the employee chatbot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g. 'projektledelse', 'ITIL certificering', 'cybersecurity')."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results. Default 5.",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_chatbot_usage_stats",
            "description": "Get employee chatbot usage statistics: active users, popular searches, engagement trends, satisfaction scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period_days": {
                        "type": "integer",
                        "description": "Look back period in days. Default 30.",
                        "default": 30
                    }
                },
                "required": []
            }
        }
    },
]


HR_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": "hr_get_company_learning_context",
            "description": "Get one company-scoped HR context snapshot: budgets, skill gaps, pending actions, supplier agreements, and catalog coverage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {"type": "string", "description": "Optional department filter."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_recommend_training_plan",
            "description": "Recommend a practical Futurematch training plan for a department/team using skill gaps, budget, and catalog courses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {"type": "string", "description": "Department to plan for. Leave empty for company-wide."},
                    "focus": {"type": "string", "description": "Optional focus area such as leadership, ITIL, cybersecurity."},
                    "limit": {"type": "integer", "description": "Max course recommendations. Default 5."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_get_supplier_coverage",
            "description": "Show catalog vendor coverage with company supplier preferences and active agreements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Optional category/topic to filter supplier coverage."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_get_ai_usage_risks",
            "description": "Analyze employee AI/chatbot usage risks: low feedback, slow turns, repeated searches, and weak conversion signals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period_days": {"type": "integer", "description": "Look back period in days. Default 30."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_compliance_status",
            "description": (
                "Vis virksomhedens overholdelses-/compliance-status for lovpligtige og obligatoriske "
                "kurser (fx arbejdsmiljø, GDPR, ISO, certificeringer). For hvert krav vises hvor mange "
                "berørte medarbejdere der er compliant, snart udløber (recertificering), eller mangler/er "
                "forfaldne. Brug dette ved spørgsmål om compliance, overholdelse, certificering, "
                "recertificering eller lovpligtig uddannelse."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Afdeling at filtrere på. Tom for hele virksomheden."
                    },
                    "only_gaps": {
                        "type": "boolean",
                        "description": "Hvis sand: vis kun krav med forfaldne/manglende eller snart udløbende medarbejdere. Standard falsk."
                    }
                },
                "required": []
            }
        }
    },
])


HR_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": "get_team_non_starters",
            "description": (
                "Vis medarbejdere på holdet/virksomheden der IKKE er begyndt på deres tildelte/bestilte "
                "kurser — bestillinger der er godkendt eller afventer, men hvor medarbejderen endnu ikke er "
                "startet (ingen completion_status). Bruges ved spørgsmål som 'hvem på mit team er ikke "
                "startet', 'hvem mangler at begynde', 'hvem er ikke kommet i gang'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Afdeling at filtrere på. Tom for hele virksomheden."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_team_compliance",
            "description": (
                "Vis pr. compliance-/overholdelseskrav hvem på holdet der er forfalden (overdue), snart "
                "udløber (expiring) eller compliant. Samme udledning som get_compliance_status, men kan også "
                "filtreres på kategori. Bruges ved 'compliance', 'overholdelse', 'lovpligtig', 'forfaldne "
                "kurser', 'hvem er overdue'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Afdeling at filtrere på. Tom for hele virksomheden."
                    },
                    "category": {
                        "type": "string",
                        "description": "Valgfri kategori at filtrere krav på (fx 'GDPR', 'arbejdsmiljø', 'ISO')."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_roi_summary",
            "description": (
                "Hent virksomhedens reelle trænings-ROI-tal for et regnskabsår: samlet spend, "
                "gennemførselsrate, spend pr. medarbejder, omkostning pr. gennemførelse og budgetoverskridelser "
                "pr. afdeling. Bruges ved 'roi', 'afkast', 'værdi af træning', 'spend per medarbejder'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "Valgfri afdeling at fremhæve i ROI-opsummeringen."
                    },
                    "year": {
                        "type": "integer",
                        "description": "Regnskabsår. Tom = indeværende år."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_benchmark",
            "description": (
                "Sammenlign virksomhedens nøgletal med en anonym branchekohort (k-anonym). Viser kun "
                "kohortgennemsnit/median/percentil når kohorten er stor nok til at være sikker. Bruges ved "
                "'benchmark', 'sammenlignet med branchen', 'hvordan klarer vi os mod peers'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "Valgfrit nøgletal at fokusere på (fx 'completion_rate', 'spend_per_employee')."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_trial_and_seat_status",
            "description": (
                "Vis virksomhedens abonnementsstatus: plan, prøveperiode (dage tilbage), pladser/licenser brugt "
                "vs. maks., og udnyttelsesgrad. Bruges ved 'abonnement', 'prøveperiode', 'pladser', 'seats', "
                "'licenser', 'hvor mange pladser har vi tilbage'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "approve_order_from_chat",
            "description": (
                "MUTATION: Godkend eller afvis en kursusbestilling der afventer godkendelse. Godkendelse "
                "sætter ordren live og trækker på budgettet; afvisning refunderer. Kræver et eksplicit "
                "confirm=true OG at den aktuelle HR-bruger er manager. Bruges ved 'godkend ordre', 'afvis "
                "ordre', 'godkend bestilling'. Bekræft ALTID med brugeren før confirm=true sættes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "approval_id": {
                        "type": "integer",
                        "description": "ID på order_approvals-rækken. Brug enten denne eller order_id."
                    },
                    "order_id": {
                        "type": "string",
                        "description": "Ordrens order_id. Brug enten denne eller approval_id."
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["approved", "rejected"],
                        "description": "'approved' for at godkende, 'rejected' for at afvise."
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Skal være true for at udføre handlingen. Uden dette returneres kun en forhåndsvisning."
                    }
                },
                "required": ["decision"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "assign_learning_path_to_team",
            "description": (
                "MUTATION: Tildel en læringssti eller et kursus til flere medarbejdere på én gang. Opretter "
                "fremdriftsrækker (employee_learning_progress) og sender en notifikation/nudge til hver "
                "medarbejder. Kræver et eksplicit confirm=true. Virksomheds-scoped. Bruges ved 'tildel', "
                "'tilmeld holdet', 'bulk-tildel', 'tildel læringssti til teamet'. Bekræft ALTID før confirm=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Liste af user_id'er for de medarbejdere der skal tildeles."
                    },
                    "path_id": {
                        "type": "integer",
                        "description": "ID på læringsstien (learning_paths.id). Brug enten denne eller course_handle."
                    },
                    "course_handle": {
                        "type": "string",
                        "description": "Kursus-handle hvis der tildeles et enkelt kursus i stedet for en sti."
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Skal være true for at udføre tildelingen. Uden dette returneres kun en forhåndsvisning."
                    }
                },
                "required": ["employee_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_inactive_employees",
            "description": (
                "Vis medarbejdere der ikke har været aktive i et antal dage (sidste aktivitet eller sidste "
                "chatbot-interaktion). Bruges ved 'inaktive medarbejdere', 'ikke aktive', 'hvem har ikke logget "
                "ind/brugt platformen'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inactive_days": {
                        "type": "integer",
                        "description": "Antal dage uden aktivitet. Standard 30.",
                        "default": 30
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_expiring_agreements",
            "description": (
                "Vis leverandøraftaler der udløber inden for et antal dage (eller allerede er udløbet). Bruges "
                "ved 'udløber aftale', 'leverandøraftaler udløber', 'hvilke aftaler skal fornyes'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "within_days": {
                        "type": "integer",
                        "description": "Vindue i dage. Standard 30.",
                        "default": 30
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_workforce_risk",
            "description": (
                "Tidlig advarsel på medarbejderstyrken: hvor er der fastholdelses-/inaktivitetsrisiko, "
                "væsentlige kompetencegab og stigende læringsbehov. Returnerer ALTID AGGREGEREDE tal pr. "
                "afdeling/kompetence (antal, ikke navne) med k-anonymitet, så ingen enkeltperson udpeges. "
                "Bruges ved 'hvad skal jeg handle på', 'workforce-risiko', 'hvem er i fare for at falde fra', "
                "'fastholdelsesrisiko', 'tidlig advarsel'. Sæt KUN drill_down=true hvis brugeren udtrykkeligt "
                "beder om navne — navne vises kun til ledere/HR-managere og kun for afdelinger der er store nok "
                "til at bevare anonymitet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drill_down": {
                        "type": "boolean",
                        "description": (
                            "Sæt true KUN hvis brugeren eksplicit beder om navne på enkeltpersoner. Kræver "
                            "manager-rolle og afsløres kun for k-sikre afdelinger. Standard false (kun aggregater)."
                        )
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hr_explain_insights",
            "description": (
                "Forklar virksomhedens proaktive AI-indsigter konversationelt: stigende emner, lav tilfredshed, "
                "søgninger uden ordrer, fald i engagement og populære kurser uden ordrer. Aggregerede tal på "
                "virksomhedsniveau (ingen personoplysninger). Bruges ved 'hvad bør jeg vide', 'giv mig "
                "indsigterne', 'forklar advarslerne', 'hvad sker der på platformen'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "description": (
                            "Valgfrit filter på alvorlighed: 'info', 'warning' eller 'critical'. Tom = alle."
                        )
                    }
                },
                "required": []
            }
        }
    },
])


# ── Tool execution functions ──

def _get_cursor():
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _execute_get_team_training_status(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet i session."})

    department = args.get('department', '')
    period_days = args.get('period_days', 90)
    cur = _get_cursor()

    dept_clause = "AND cu.department = %s" if department else ""
    params_base = [company_id]
    if department:
        params_base.append(department)

    # Get employees with their order counts
    cur.execute(f"""
        SELECT cu.department, u.username,
               COUNT(DISTINCT CASE WHEN co.status = 'completed' THEN co.id END) as completed,
               COUNT(DISTINCT CASE WHEN co.status IN ('confirmed','processing') THEN co.id END) as in_progress,
               COUNT(DISTINCT CASE WHEN co.status = 'pending_approval' THEN co.id END) as pending,
               MAX(co.created_at) as last_order_date
        FROM company_users cu
        JOIN users u ON cu.user_id = u.id
        LEFT JOIN course_orders co ON co.user_id = u.id AND co.company_id = cu.company_id
            AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        WHERE cu.company_id = %s AND cu.status = 'active'
        {dept_clause}
        GROUP BY cu.department, u.username
        ORDER BY cu.department, u.username
    """, tuple([period_days] + params_base))
    rows = cur.fetchall()

    # Aggregate by department
    departments = {}
    for r in rows:
        dept = r['department'] or 'Ukendt'
        if dept not in departments:
            departments[dept] = {"employees": [], "total_completed": 0, "total_in_progress": 0, "total_pending": 0}
        departments[dept]["employees"].append({
            "username": r['username'],
            "completed": r['completed'] or 0,
            "in_progress": r['in_progress'] or 0,
            "pending": r['pending'] or 0,
            "last_order": r['last_order_date'].isoformat() if r['last_order_date'] else None
        })
        departments[dept]["total_completed"] += r['completed'] or 0
        departments[dept]["total_in_progress"] += r['in_progress'] or 0
        departments[dept]["total_pending"] += r['pending'] or 0

    cur.close()
    return json.dumps({
        "period_days": period_days,
        "departments": departments,
        "total_employees": len(rows)
    }, default=str)


def _execute_get_company_skill_gaps(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    department = args.get('department', '')
    cur = _get_cursor()

    dept_clause = "AND (cst.department = %s OR cst.department IS NULL)" if department else ""
    params = [company_id]
    if department:
        params.append(department)

    # Get skill targets
    cur.execute(f"""
        SELECT cst.department, cst.skill_name, cst.target_level, cst.priority
        FROM company_skill_targets cst
        WHERE cst.company_id = %s {dept_clause}
        ORDER BY cst.priority DESC, cst.skill_name
    """, tuple(params))
    targets = cur.fetchall()

    if not targets:
        cur.close()
        return json.dumps({"message": "Ingen kompetencemaal defineret endnu.", "targets": [], "gaps": []})

    # Get employee skills
    emp_dept_clause = "AND cu.department = %s" if department else ""
    emp_params = [company_id]
    if department:
        emp_params.append(department)

    cur.execute(f"""
        SELECT cu.department, u.username, esm.skill_name, esm.current_level
        FROM employee_skills_matrix esm
        JOIN users u ON esm.employee_id = u.id
        JOIN company_users cu ON cu.user_id = u.id AND cu.company_id = %s
        WHERE cu.status = 'active' {emp_dept_clause}
    """, tuple(emp_params))
    skills = cur.fetchall()

    # Build gap analysis. Skill levels are INTEGERS in employee_skills_matrix —
    # _skill_level_to_int is idempotent on ints (it passes them through), so it
    # is safe here and also defends against any accidental string contamination.
    # skill_map: (department, lower(skill_name)) -> {username: level}
    # We key by (department, username, skill) so the same skill is not double
    # counted when it appears in both the HR matrix and the chatbot profile;
    # the HR-managed matrix value wins over the self-reported one.
    skill_map = {}

    def _add_level(department, username, skill_name, level_int):
        if not skill_name:
            return
        key = (department or 'Alle', skill_name.strip().lower())
        bucket = skill_map.setdefault(key, {})
        # First writer wins per (skill, employee). The matrix is loaded first, so
        # HR-managed levels take precedence over self-reported chatbot levels.
        bucket.setdefault(username, level_int)

    for s in skills:
        _add_level(
            s['department'],
            s['username'],
            s['skill_name'],
            _skill_level_to_int(s['current_level']),
        )

    # Merge self-reported chatbot profile skills (user_skills, STRING levels).
    # Without this, an employee who recorded "Python: ekspert" via the chatbot
    # is invisible to the HR gap board and counted as level 0 — inflating gaps.
    # The string label is mapped to an int exactly once, here at read time.
    try:
        us_dept_clause = "AND cu.department = %s" if department else ""
        us_params = [company_id]
        if department:
            us_params.append(department)
        cur.execute(f"""
            SELECT cu.department, u.username, usk.skill_name, usk.skill_level
            FROM user_skills usk
            JOIN users u ON usk.username = u.username
            JOIN company_users cu ON cu.user_id = u.id AND cu.company_id = %s
            WHERE cu.status = 'active' {us_dept_clause}
        """, tuple(us_params))
        for s in cur.fetchall():
            _add_level(
                s['department'],
                s['username'],
                s['skill_name'],
                _skill_level_to_int(s['skill_level']),
            )
    except Exception as exc:
        # user_skills may not exist for every tenant; degrade to matrix-only.
        print(f"[HR_TOOLS][skill_gaps] user_skills merge skipped: {exc}")

    cur.close()

    # Map target skill name (lowercased) -> levels for matching the merged map.
    gaps = []
    for t in targets:
        dept = t['department'] or 'Alle'
        key = (dept, (t['skill_name'] or '').strip().lower())
        levels = list(skill_map.get(key, {}).values())
        avg_level = round(sum(levels) / len(levels), 1) if levels else 0
        target_level = _skill_level_to_int(t['target_level'])
        gap = round(target_level - avg_level, 1)
        gaps.append({
            "department": dept,
            "skill": t['skill_name'],
            "target": target_level,
            "avg_current": avg_level,
            "gap": gap,
            "priority": t['priority'],
            "employees_assessed": len(levels),
            "critical": gap >= 2
        })

    gaps.sort(key=lambda g: g['gap'], reverse=True)
    return json.dumps({"gaps": gaps, "total_targets": len(targets)}, default=str)


def _execute_get_budget_overview(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    department = args.get('department', '')
    cur = _get_cursor()

    dept_clause = "AND db.department = %s" if department else ""
    params = [company_id]
    if department:
        params.append(department)

    cur.execute(f"""
        SELECT db.department, db.annual_budget, db.spent, db.fiscal_year,
               (db.annual_budget - db.spent) as remaining,
               ROUND(db.spent / NULLIF(db.annual_budget, 0) * 100, 1) as utilization_pct
        FROM department_budgets db
        WHERE db.company_id = %s {dept_clause}
        ORDER BY db.department
    """, tuple(params))
    budgets = cur.fetchall()
    cur.close()

    if not budgets:
        return json.dumps({"message": "Ingen budgetter oprettet endnu.", "budgets": []})

    total_budget = sum(b['annual_budget'] or 0 for b in budgets)
    total_spent = sum(b['spent'] or 0 for b in budgets)

    result = {
        "budgets": [{
            "department": b['department'],
            "annual_budget": float(b['annual_budget'] or 0),
            "spent": float(b['spent'] or 0),
            "remaining": float(b['remaining'] or 0),
            "utilization_pct": float(b['utilization_pct'] or 0),
            "alert": float(b['utilization_pct'] or 0) > 80
        } for b in budgets],
        "company_total": {"budget": float(total_budget), "spent": float(total_spent), "remaining": float(total_budget - total_spent)}
    }
    return json.dumps(result, default=str)


def _execute_get_employee_overview(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    username = args.get('username', '')
    department = args.get('department', '')
    inactive_days = args.get('inactive_days')
    cur = _get_cursor()

    conditions = ["cu.company_id = %s", "cu.status = 'active'"]
    params = [company_id]

    if username:
        conditions.append("u.username = %s")
        params.append(username)
    if department:
        conditions.append("cu.department = %s")
        params.append(department)
    if inactive_days:
        conditions.append("(cu.last_login IS NULL OR cu.last_login < DATE_SUB(NOW(), INTERVAL %s DAY))")
        params.append(inactive_days)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT u.username, u.email, cu.department, cu.role, cu.hire_date, cu.last_login,
               COUNT(DISTINCT co.id) as total_orders,
               COUNT(DISTINCT CASE WHEN co.status = 'completed' THEN co.id END) as completed_orders,
               COUNT(DISTINCT ci.id) as chatbot_queries,
               MAX(ci.created_at) as last_chatbot_use
        FROM company_users cu
        JOIN users u ON cu.user_id = u.id
        LEFT JOIN course_orders co ON co.user_id = u.id AND co.company_id = cu.company_id
        LEFT JOIN chatbot_interactions ci ON ci.username = u.username AND ci.company_id = %s
        WHERE {where}
        GROUP BY u.username, u.email, cu.department, cu.role, cu.hire_date, cu.last_login
        ORDER BY cu.department, u.username
        LIMIT 50
    """, tuple([company_id] + params))
    employees = cur.fetchall()

    # If single employee, also get their skills
    if username and employees:
        cur.execute("""
            SELECT esm.skill_name, esm.current_level
            FROM employee_skills_matrix esm
            JOIN users u ON esm.employee_id = u.id
            WHERE u.username = %s
        """, (username,))
        skills = cur.fetchall()
        employees[0]['skills'] = skills

    cur.close()

    result = [{
        "username": e['username'],
        "email": e['email'],
        "department": e['department'],
        "role": e['role'],
        "hire_date": e['hire_date'].isoformat() if e.get('hire_date') else None,
        "last_login": e['last_login'].isoformat() if e.get('last_login') else None,
        "total_orders": e['total_orders'] or 0,
        "completed_orders": e['completed_orders'] or 0,
        "chatbot_queries": e['chatbot_queries'] or 0,
        "last_chatbot_use": e['last_chatbot_use'].isoformat() if e.get('last_chatbot_use') else None,
        "skills": e.get('skills', [])
    } for e in employees]

    return json.dumps({"employees": result, "count": len(result)}, default=str)


def _execute_get_training_report(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    department = args.get('department', '')
    period_days = args.get('period_days', 90)
    cur = _get_cursor()

    # Total spend and order stats
    base_where = "co.company_id = %s AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
    params = [company_id, period_days]
    if department:
        base_where += " AND co.username IN (SELECT u.username FROM users u JOIN company_users cu ON u.id = cu.user_id WHERE cu.company_id = %s AND cu.department = %s)"
        params.extend([company_id, department])

    cur.execute(f"""
        SELECT
            COUNT(*) as total_orders,
            COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
            COUNT(CASE WHEN status = 'pending_approval' THEN 1 END) as pending_approval,
            COUNT(CASE WHEN status IN ('confirmed','processing') THEN 1 END) as in_progress,
            COALESCE(SUM(CASE WHEN status != 'cancelled' THEN price ELSE 0 END), 0) as total_spend,
            COUNT(DISTINCT username) as unique_employees,
            AVG(CASE WHEN status = 'completed' AND completion_date IS NOT NULL
                THEN DATEDIFF(completion_date, created_at) END) as avg_completion_days
        FROM course_orders co
        WHERE {base_where}
    """, tuple(params))
    stats = cur.fetchone()

    # Top ordered courses
    cur.execute(f"""
        SELECT co.product_title, COUNT(*) as order_count,
               SUM(price) as total_value
        FROM course_orders co
        WHERE {base_where} AND co.status != 'cancelled'
        GROUP BY co.product_title
        ORDER BY order_count DESC
        LIMIT 10
    """, tuple(params))
    top_courses = cur.fetchall()
    cur.close()

    return json.dumps({
        "period_days": period_days,
        "total_orders": stats['total_orders'] or 0,
        "completed": stats['completed'] or 0,
        "pending_approval": stats['pending_approval'] or 0,
        "in_progress": stats['in_progress'] or 0,
        "total_spend": float(stats['total_spend'] or 0),
        "unique_employees": stats['unique_employees'] or 0,
        "avg_completion_days": round(float(stats['avg_completion_days'] or 0), 1),
        "top_courses": [{"title": c['product_title'], "orders": c['order_count'],
                         "value": float(c['total_value'] or 0)} for c in top_courses]
    }, default=str)


def _execute_get_pending_actions(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    cur = _get_cursor()
    actions = []

    # Pending approvals
    cur.execute("""
        SELECT COUNT(*) as cnt FROM order_approvals
        WHERE company_id = %s AND status = 'pending'
    """, (company_id,))
    pending = cur.fetchone()['cnt']
    if pending > 0:
        actions.append({"type": "approval", "message": f"{pending} kursusbestillinger venter paa godkendelse", "priority": "high", "count": pending})

    # Budget alerts (>80% used)
    cur.execute("""
        SELECT department, ROUND(spent / NULLIF(annual_budget, 0) * 100, 1) as pct
        FROM department_budgets
        WHERE company_id = %s AND spent / NULLIF(annual_budget, 0) > 0.8
    """, (company_id,))
    for b in cur.fetchall():
        actions.append({"type": "budget_alert", "message": f"{b['department']} har brugt {b['pct']}% af budgettet", "priority": "medium"})

    # Inactive employees (30+ days)
    cur.execute("""
        SELECT COUNT(*) as cnt FROM company_users
        WHERE company_id = %s AND status = 'active'
        AND (last_login IS NULL OR last_login < DATE_SUB(NOW(), INTERVAL 30 DAY))
    """, (company_id,))
    inactive = cur.fetchone()['cnt']
    if inactive > 0:
        actions.append({"type": "inactive", "message": f"{inactive} medarbejdere har ikke vaeret aktive i 30+ dage", "priority": "medium", "count": inactive})

    # Critical skill gaps
    cur.execute("""
        SELECT COUNT(*) as cnt FROM company_skill_targets cst
        LEFT JOIN (
            SELECT esm.skill_name, AVG(esm.current_level) as avg_level
            FROM employee_skills_matrix esm
            JOIN company_users cu ON esm.employee_id = cu.user_id AND cu.company_id = %s
            GROUP BY esm.skill_name
        ) skills ON skills.skill_name = cst.skill_name
        WHERE cst.company_id = %s AND (cst.target_level - COALESCE(skills.avg_level, 0)) >= 2
    """, (company_id, company_id))
    critical_gaps = cur.fetchone()['cnt']
    if critical_gaps > 0:
        actions.append({"type": "skill_gap", "message": f"{critical_gaps} kritiske kompetencegab (gab >= 2)", "priority": "high", "count": critical_gaps})

    cur.close()
    return json.dumps({"actions": actions, "total": len(actions)}, default=str)


def _execute_search_courses_for_team(args):
    """Reuse the employee chatbot's search but return results as structured data for HR."""
    query = args.get('query', '')
    limit = args.get('limit', 5)
    if not query:
        return json.dumps({"error": "Angiv en soegning."})

    try:
        from app1.rag import hybrid_rank_products, load_augmented_products
        products = load_augmented_products()
        # hybrid_rank_products signature: (filtered_products, query, all_products, limit)
        # For a global team-course search there is no pre-filter, so the filtered
        # set is the full product list and all_products is that same list.
        results = hybrid_rank_products(products, query, products, limit=limit)
        courses = []
        for p in results:
            if not isinstance(p, dict):
                continue
            tags = p.get("tags", [])
            courses.append({
                "title": p.get("title", ""),
                "handle": p.get("handle", ""),
                "vendor": p.get("vendor", ""),
                "price": p.get("price", ""),
                "product_type": p.get("product_type", ""),
                "tags": tags[:5] if isinstance(tags, list) else [],
            })
        return json.dumps({"query": query, "results": courses, "count": len(courses)}, default=str)
    except Exception as e:
        # Don't silently swallow: this is a flagship HR workflow. Log loudly so
        # the real failure shows up in server logs, but still return a safe,
        # shape-consistent empty result so the agent/UI doesn't break.
        import traceback
        print(f"[HR_TOOLS][search_courses_for_team] ERROR for query={query!r}: {e}")
        traceback.print_exc()
        return json.dumps({"query": query, "results": [], "count": 0, "error": f"Soegefejl: {str(e)}"}, default=str)


def _execute_get_chatbot_usage_stats(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    period_days = args.get('period_days', 30)
    cur = _get_cursor()

    # Active users
    cur.execute("""
        SELECT COUNT(DISTINCT username) as active_users,
               COUNT(*) as total_queries,
               AVG(feedback_rating) as avg_feedback,
               COUNT(DISTINCT session_id) as total_sessions
        FROM chatbot_interactions
        WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
    """, (company_id, period_days))
    stats = cur.fetchone()

    # Popular searches
    cur.execute("""
        SELECT query_text, COUNT(*) as cnt
        FROM chatbot_interactions
        WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND query_text IS NOT NULL AND query_text != ''
        GROUP BY query_text
        ORDER BY cnt DESC
        LIMIT 10
    """, (company_id, period_days))
    popular = cur.fetchall()

    # Daily activity trend
    cur.execute("""
        SELECT DATE(created_at) as day, COUNT(*) as queries, COUNT(DISTINCT username) as users
        FROM chatbot_interactions
        WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        GROUP BY DATE(created_at)
        ORDER BY day
    """, (company_id, period_days))
    daily = cur.fetchall()

    cur.close()
    return json.dumps({
        "period_days": period_days,
        "active_users": stats['active_users'] or 0,
        "total_queries": stats['total_queries'] or 0,
        "avg_feedback": round(float(stats['avg_feedback'] or 0), 2),
        "total_sessions": stats['total_sessions'] or 0,
        "popular_searches": [{"query": p['query_text'][:80], "count": p['cnt']} for p in popular],
        "daily_trend": [{"date": d['day'].isoformat(), "queries": d['queries'], "users": d['users']} for d in daily]
    }, default=str)


def _json_tool(fn, args):
    try:
        return json.loads(fn(args))
    except Exception as exc:
        return {"error": str(exc)}


def _execute_hr_get_company_learning_context(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    department = args.get("department", "")
    budget = _json_tool(_execute_get_budget_overview, {"department": department})
    gaps = _json_tool(_execute_get_company_skill_gaps, {"department": department})
    pending = _json_tool(_execute_get_pending_actions, {})
    suppliers = _json_tool(_execute_hr_get_supplier_coverage, {})

    return json.dumps({
        "department": department or "Alle",
        "budget": budget,
        "skill_gaps": gaps,
        "pending_actions": pending,
        "supplier_coverage": {
            "active_suppliers": suppliers.get("active_suppliers", 0),
            "inactive_suppliers": suppliers.get("inactive_suppliers", 0),
            "agreement_count": suppliers.get("agreement_count", 0),
            "top_vendors": suppliers.get("vendors", [])[:8],
        },
    }, default=str)


def _execute_hr_recommend_training_plan(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    department = args.get("department", "")
    focus = (args.get("focus") or "").strip()
    limit = int(args.get("limit") or 5)
    gaps_payload = _json_tool(_execute_get_company_skill_gaps, {"department": department})
    gaps = [g for g in gaps_payload.get("gaps", []) if g.get("gap", 0) > 0]
    gaps.sort(key=lambda g: (g.get("critical") is not True, -float(g.get("gap", 0))))

    topics = []
    if focus:
        topics.append(focus)
    topics.extend(g.get("skill", "") for g in gaps[:4] if g.get("skill"))
    if not topics:
        topics = [focus or "ledelse"]

    try:
        import catalog_service as catalog
        recommendations = []
        seen = set()
        for topic in topics:
            results = catalog.search_products({"q": topic, "sort": "relevance"}, page=1, per_page=3)
            for product in results.get("products", []):
                if product["handle"] in seen:
                    continue
                seen.add(product["handle"])
                recommendations.append({
                    "topic": topic,
                    "title": product["title"],
                    "handle": product["handle"],
                    "url": catalog.build_product_url(product["handle"]),
                    "vendor": product["vendor"],
                    "vendor_url": f"/vendors/{product['vendor_slug']}",
                    "price": product["price_label"],
                    "format": product["format"],
                    "categories": product["categories"][:4],
                    "reason": f"Matcher kompetencebehovet '{topic}'.",
                })
                if len(recommendations) >= limit:
                    break
            if len(recommendations) >= limit:
                break
    except Exception as exc:
        recommendations = []
        gaps_payload["catalog_error"] = str(exc)

    budget = _json_tool(_execute_get_budget_overview, {"department": department})
    return json.dumps({
        "department": department or "Alle",
        "focus": focus,
        "priority_gaps": gaps[:6],
        "recommended_courses": recommendations,
        "budget": budget,
        "next_actions": [
            "Prioriter kritiske kompetencegab først",
            "Match anbefalinger mod aktive leverandøraftaler",
            "Opret læringssti for berørte medarbejdere",
        ],
    }, default=str)


def _execute_hr_get_supplier_coverage(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    topic = (args.get("topic") or "").strip().lower()
    cur = _get_cursor()
    cur.execute(
        "SELECT vendor_name, is_active, priority, notes FROM company_supplier_preferences WHERE company_id = %s",
        (company_id,),
    )
    prefs = {r["vendor_name"].lower(): r for r in cur.fetchall()}
    cur.execute(
        """
        SELECT vendor_name, discount_type, discount_value, agreement_name, valid_until
        FROM company_supplier_agreements
        WHERE company_id = %s AND is_active = 1
          AND (valid_from IS NULL OR valid_from <= CURDATE())
          AND (valid_until IS NULL OR valid_until >= CURDATE())
        """,
        (company_id,),
    )
    agreements = {r["vendor_name"].lower(): r for r in cur.fetchall()}
    cur.close()

    try:
        import catalog_service as catalog
        vendors = catalog.get_vendors()
        if topic:
            vendors = [v for v in vendors if topic in " ".join(v.get("categories", []) + [v.get("name", "")]).lower()]
        rows = []
        for vendor in vendors[:50]:
            key = vendor["name"].lower()
            pref = prefs.get(key, {})
            agreement = agreements.get(key, {})
            is_active = bool(pref.get("is_active", 1))
            rows.append({
                "name": vendor["name"],
                "url": f"/vendors/{vendor['slug']}",
                "course_count": vendor.get("course_count", 0),
                "categories": vendor.get("categories", [])[:5],
                "is_active": is_active,
                "priority": pref.get("priority"),
                "notes": pref.get("notes", ""),
                "agreement": {
                    "name": agreement.get("agreement_name", ""),
                    "type": agreement.get("discount_type", ""),
                    "value": float(agreement["discount_value"]) if agreement.get("discount_value") is not None else None,
                    "valid_until": agreement.get("valid_until"),
                } if agreement else None,
            })
    except Exception as exc:
        return json.dumps({"error": f"Katalogfejl: {exc}"})

    return json.dumps({
        "topic": topic,
        "vendors": rows,
        "active_suppliers": sum(1 for r in rows if r["is_active"]),
        "inactive_suppliers": sum(1 for r in rows if not r["is_active"]),
        "agreement_count": sum(1 for r in rows if r.get("agreement")),
    }, default=str)


def _execute_hr_get_ai_usage_risks(args):
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    period_days = int(args.get("period_days") or 30)
    cur = _get_cursor()
    cur.execute(
        """
        SELECT
            COUNT(*) AS total_queries,
            COUNT(DISTINCT username) AS active_users,
            AVG(response_time_ms) AS avg_latency,
            SUM(CASE WHEN feedback_rating IS NOT NULL AND feedback_rating > 0 AND feedback_rating <= 2 THEN 1 ELSE 0 END) AS low_feedback,
            SUM(CASE WHEN response_time_ms > 12000 THEN 1 ELSE 0 END) AS slow_turns,
            SUM(CASE WHEN tool_results_count = 0 AND tools_used IS NOT NULL AND tools_used != '' THEN 1 ELSE 0 END) AS zero_result_tool_turns
        FROM chatbot_interactions
        WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """,
        (company_id, period_days),
    )
    stats = cur.fetchone()
    cur.execute(
        """
        SELECT query_text, COUNT(*) AS cnt
        FROM chatbot_interactions
        WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
          AND query_text IS NOT NULL AND query_text != ''
        GROUP BY query_text
        HAVING cnt >= 2
        ORDER BY cnt DESC
        LIMIT 10
        """,
        (company_id, period_days),
    )
    repeated = cur.fetchall()
    cur.close()

    total = stats.get("total_queries") or 0
    risks = []
    if stats.get("slow_turns", 0):
        risks.append({"type": "latency", "count": int(stats["slow_turns"]), "message": "Flere AI-svar tager over 12 sekunder."})
    if stats.get("zero_result_tool_turns", 0):
        risks.append({"type": "search_quality", "count": int(stats["zero_result_tool_turns"]), "message": "Værktøjskald returnerer ofte nul resultater."})
    if stats.get("low_feedback", 0):
        risks.append({"type": "satisfaction", "count": int(stats["low_feedback"]), "message": "Der er lave feedback-ratings i perioden."})

    return json.dumps({
        "period_days": period_days,
        "total_queries": total,
        "active_users": stats.get("active_users") or 0,
        "avg_latency_ms": round(float(stats.get("avg_latency") or 0), 1),
        "risk_count": len(risks),
        "risks": risks,
        "repeated_queries": [{"query": r["query_text"][:120], "count": r["cnt"]} for r in repeated],
    }, default=str)


def _parse_completion_dt(value):
    """Best-effort parse of a completion timestamp (DATETIME or free-text VARCHAR)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 9], fmt)
        except (ValueError, TypeError):
            continue
    # Try the leading date portion (handles "2026-01-15T..." etc.)
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def derive_company_compliance(conn, company_id, department='', only_gaps=False):
    """Derive (read-only) per-requirement compliance status for one company.

    Session-INDEPENDENT core of the compliance tool: takes an explicit DB
    connection + company_id so it can be reused both by the HR chatbot tool
    (request path) AND by the scheduled compliance recheck (no request session).
    Returns a plain ``dict`` (the chatbot wrapper json-dumps it); the scheduler
    reads the aggregate per-requirement counts directly.

    For each compliance_requirements row we find the applicable active employees
    (filtered by applies_to_department / applies_to_role, NULL = all), then derive
    each employee's state from their completed courses:
        compliant  — has a matching completion still inside the recurrence window
        expiring   — compliant but < 60 days from expiry (recurrence-based)
        overdue    — had a matching completion but it expired
        missing    — no matching completion at all
    Completions come from course_orders (completion_status='completed') and the
    employee profile table user_completed_courses, matched by required_course_handle,
    or by title/category text when no handle is set. Nothing is stored — pure read.

    Only AGGREGATE per-requirement status counts are returned — never people-level
    rows — so the result is k-anon-safe to surface as a company-wide notification.
    """
    if not company_id:
        return {"error": "Ingen virksomhed fundet."}

    department = (department or '').strip()
    only_gaps = bool(only_gaps)
    EXPIRING_DAYS = 60
    now = datetime.now()

    cur = conn.cursor(MySQLdb.cursors.DictCursor)

    # 1) Requirements for this company (optionally narrowed to a department:
    #    a requirement applies to the department if it targets that department or
    #    all departments (NULL/empty)).
    req_clause = ""
    req_params = [company_id]
    if department:
        req_clause = "AND (cr.applies_to_department = %s OR cr.applies_to_department IS NULL OR cr.applies_to_department = '')"
        req_params.append(department)
    try:
        cur.execute(f"""
            SELECT cr.id, cr.title, cr.category, cr.applies_to_department,
                   cr.applies_to_role, cr.required_course_handle,
                   cr.recurrence_months, cr.is_statutory
            FROM compliance_requirements cr
            WHERE cr.company_id = %s {req_clause}
            ORDER BY cr.is_statutory DESC, cr.title
        """, tuple(req_params))
        requirements = cur.fetchall()
    except Exception as exc:
        cur.close()
        # Table may not exist yet on this tenant — safe-empty.
        print(f"[HR_TOOLS][compliance] requirements query failed: {exc}")
        return {
            "message": "Ingen compliance-krav fundet endnu.",
            "requirements": [],
            "overall_compliance_pct": 0,
            "total_requirements": 0,
        }

    if not requirements:
        cur.close()
        return {
            "message": "Ingen compliance-krav defineret endnu. Opret lovpligtige/obligatoriske kurser for at spore overholdelse.",
            "requirements": [],
            "overall_compliance_pct": 0,
            "total_requirements": 0,
        }

    # 2) Active employees for this company (id + username + dept + role).
    emp_dept_clause = "AND cu.department = %s" if department else ""
    emp_params = [company_id]
    if department:
        emp_params.append(department)
    cur.execute(f"""
        SELECT u.id AS user_id, u.username, cu.department, cu.role
        FROM company_users cu
        JOIN users u ON cu.user_id = u.id
        WHERE cu.company_id = %s AND cu.status = 'active' {emp_dept_clause}
    """, tuple(emp_params))
    employees = cur.fetchall()

    # 3) Completed courses, keyed by employee, from both sources. Each entry is
    #    (handle_lower, title_lower, completed_dt|None).
    completions_by_user = {}   # user_id -> list
    completions_by_username = {}  # username -> list

    try:
        cur.execute("""
            SELECT user_id, username, product_handle, product_title, completion_date
            FROM course_orders
            WHERE company_id = %s AND completion_status = 'completed'
        """, (company_id,))
        for r in cur.fetchall():
            entry = (
                (r.get('product_handle') or '').strip().lower(),
                (r.get('product_title') or '').strip().lower(),
                _parse_completion_dt(r.get('completion_date')),
            )
            if r.get('user_id') is not None:
                completions_by_user.setdefault(r['user_id'], []).append(entry)
            if r.get('username'):
                completions_by_username.setdefault(r['username'], []).append(entry)
    except Exception as exc:
        print(f"[HR_TOOLS][compliance] course_orders query skipped: {exc}")

    usernames = [e['username'] for e in employees if e.get('username')]
    if usernames:
        try:
            placeholders = ",".join(["%s"] * len(usernames))
            cur.execute(f"""
                SELECT username, course_handle, course_title, completed_date
                FROM user_completed_courses
                WHERE username IN ({placeholders})
            """, tuple(usernames))
            for r in cur.fetchall():
                entry = (
                    (r.get('course_handle') or '').strip().lower(),
                    (r.get('course_title') or '').strip().lower(),
                    _parse_completion_dt(r.get('completed_date')),
                )
                completions_by_username.setdefault(r['username'], []).append(entry)
        except Exception as exc:
            print(f"[HR_TOOLS][compliance] user_completed_courses query skipped: {exc}")

    cur.close()

    def _applies(emp, req):
        dep = req.get('applies_to_department')
        if dep and (emp.get('department') or '') != dep:
            return False
        role = req.get('applies_to_role')
        if role and (emp.get('role') or '') != role:
            return False
        return True

    def _matches(entry, req_handle, req_title, req_category):
        handle, title, _dt = entry
        if req_handle:
            return handle == req_handle
        # No required handle: match on the requirement title or category text.
        needle = req_title or req_category
        if not needle:
            return False
        return bool((title and needle in title) or (req_category and req_category in title))

    def _employee_state(emp, req):
        req_handle = (req.get('required_course_handle') or '').strip().lower()
        req_title = (req.get('title') or '').strip().lower()
        req_category = (req.get('category') or '').strip().lower()
        recurrence = int(req.get('recurrence_months') or 0)

        entries = []
        if emp.get('user_id') is not None:
            entries.extend(completions_by_user.get(emp['user_id'], []))
        if emp.get('username'):
            entries.extend(completions_by_username.get(emp['username'], []))

        matched = [e for e in entries if _matches(e, req_handle, req_title, req_category)]
        if not matched:
            return "missing"

        # One-time requirement (recurrence 0 = never expires): any completion ok.
        if recurrence <= 0:
            return "compliant"

        # Find the most recent completion with a parseable date.
        dated = [e[2] for e in matched if e[2] is not None]
        if not dated:
            # Completed but we cannot date it — treat as compliant (conservative:
            # don't manufacture an overdue we cannot prove).
            return "compliant"
        latest = max(dated)
        # Expiry = completion + recurrence_months (approx 30.4 days/month).
        expiry = latest + timedelta(days=int(recurrence * 30.44))
        days_left = (expiry - now).days
        if days_left < 0:
            return "overdue"
        if days_left <= EXPIRING_DAYS:
            return "expiring"
        return "compliant"

    per_requirement = []
    total_applicable = 0
    total_compliant = 0
    for req in requirements:
        applicable = [e for e in employees if _applies(e, req)]
        counts = {"compliant": 0, "expiring": 0, "overdue": 0, "missing": 0}
        for emp in applicable:
            counts[_employee_state(emp, req)] += 1

        n = len(applicable)
        # "expiring" still counts as compliant-but-due-for-renewal for the % view.
        compliant_n = counts["compliant"] + counts["expiring"]
        total_applicable += n
        total_compliant += compliant_n
        pct = round(compliant_n / n * 100, 1) if n else 0.0
        has_gap = (counts["overdue"] + counts["missing"] + counts["expiring"]) > 0

        per_requirement.append({
            "id": req.get('id'),
            "title": req.get('title'),
            "category": req.get('category'),
            "is_statutory": bool(req.get('is_statutory')),
            "applies_to_department": req.get('applies_to_department') or "Alle afdelinger",
            "applies_to_role": req.get('applies_to_role') or "Alle roller",
            "recurrence_months": int(req.get('recurrence_months') or 0),
            "applicable_employees": n,
            "compliant": counts["compliant"],
            "expiring": counts["expiring"],
            "overdue": counts["overdue"],
            "missing": counts["missing"],
            "compliance_pct": pct,
            "has_gap": has_gap,
        })

    if only_gaps:
        per_requirement = [r for r in per_requirement if r["has_gap"]]

    overall_pct = round(total_compliant / total_applicable * 100, 1) if total_applicable else 0.0

    return {
        "department": department or "Alle",
        "total_requirements": len(requirements),
        "shown_requirements": len(per_requirement),
        "overall_compliance_pct": overall_pct,
        "statutory_requirements": sum(1 for r in requirements if r.get('is_statutory')),
        "requirements": per_requirement,
        "summary_da": (
            f"{overall_pct}% samlet overholdelse på tværs af {len(requirements)} krav."
            if total_applicable else "Ingen berørte medarbejdere for de definerede krav endnu."
        ),
    }


def _execute_get_compliance_status(args):
    """HR-chatbot wrapper around ``derive_company_compliance``.

    Reads the session company + tool args, delegates to the session-independent
    derivation and json-encodes the result for the LLM tool channel. Keeping the
    derivation separate lets the scheduled compliance recheck reuse the exact
    same logic without a request session.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})
    result = derive_company_compliance(
        current_app.mysql.connection,
        company_id,
        department=(args.get('department') or ''),
        only_gaps=bool(args.get('only_gaps')),
    )
    return json.dumps(result, default=str)


def _execute_get_team_non_starters(args):
    """Employees with approved/pending course orders they have NOT started yet.

    "Not started" = course_orders row whose status is approved/pending (i.e. the
    order is live or awaiting approval) but completion_status is NULL / empty /
    'not_started' AND started_at is NULL. Company-scoped to the session company.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet i session."})

    department = (args.get('department') or '').strip()
    cur = _get_cursor()

    dept_clause = "AND cu.department = %s" if department else ""
    params = [company_id]
    if department:
        params.append(department)

    try:
        cur.execute(f"""
            SELECT co.order_id, co.user_id, co.username, co.product_handle,
                   co.product_title, co.status, co.completion_status, co.started_at,
                   co.completion_deadline, co.created_at, co.department,
                   cu.full_name, cu.department AS cu_department, cu.manager_user_id
            FROM course_orders co
            JOIN company_users cu ON cu.user_id = co.user_id AND cu.company_id = co.company_id
            WHERE co.company_id = %s
              AND cu.status = 'active'
              AND co.status IN ('approved', 'pending', 'confirmed', 'processing')
              AND (co.completion_status IS NULL OR co.completion_status = ''
                   OR co.completion_status = 'not_started')
              AND co.started_at IS NULL
              {dept_clause}
            ORDER BY cu.department, co.username, co.created_at
        """, tuple(params))
        rows = cur.fetchall()
    except Exception as exc:
        cur.close()
        print(f"[HR_TOOLS][non_starters] query failed: {exc}")
        return json.dumps({"error": "Kunne ikke hente ikke-startede kurser.", "non_starters": [], "total": 0})

    cur.close()

    by_employee = {}
    for r in rows:
        uname = r.get('username') or f"user_{r.get('user_id')}"
        emp = by_employee.setdefault(uname, {
            "username": uname,
            "user_id": r.get('user_id'),
            "full_name": r.get('full_name') or uname,
            "department": r.get('cu_department') or r.get('department') or "Ukendt",
            "manager_user_id": r.get('manager_user_id'),
            "not_started_courses": [],
        })
        emp["not_started_courses"].append({
            "order_id": r.get('order_id'),
            "product_title": r.get('product_title') or r.get('product_handle') or "",
            "product_handle": r.get('product_handle') or "",
            "status": r.get('status'),
            "deadline": r.get('completion_deadline').isoformat() if r.get('completion_deadline') else None,
            "ordered_at": r.get('created_at').isoformat() if r.get('created_at') else None,
        })

    employees = list(by_employee.values())
    total_courses = sum(len(e["not_started_courses"]) for e in employees)
    return json.dumps({
        "department": department or "Alle",
        "non_starters": employees,
        "total_employees": len(employees),
        "total_not_started_courses": total_courses,
        "summary_da": (
            f"{len(employees)} medarbejder(e) er ikke startet på {total_courses} tildelt(e) kursus/kurser."
            if employees else "Alle medarbejdere er kommet i gang med deres tildelte kurser."
        ),
    }, default=str)


def _execute_hr_team_compliance(args):
    """Per-requirement compliance roll-up, reusing get_compliance_status derivation.

    Thin wrapper: delegates to the canonical compliance derivation (so the
    overdue/expiring/compliant logic lives in exactly one place) and then applies
    an optional category filter on top of the per-requirement rows.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    category = (args.get('category') or '').strip().lower()

    # Reuse the canonical derivation. _execute_get_compliance_status returns a
    # JSON string; we parse, optionally filter by category, and re-serialize so
    # the overdue/expiring/compliant logic is never duplicated.
    base = json.loads(_execute_get_compliance_status({
        "department": args.get('department') or '',
        "only_gaps": False,
    }))
    if "error" in base:
        return json.dumps(base)

    requirements = base.get("requirements", [])
    if category:
        requirements = [
            r for r in requirements
            if category in (str(r.get("category") or "").lower())
            or category in (str(r.get("title") or "").lower())
        ]

    # Recompute the headline % over the (possibly filtered) set.
    total_applicable = sum(r.get("applicable_employees", 0) for r in requirements)
    total_compliant = sum(
        (r.get("compliant", 0) + r.get("expiring", 0)) for r in requirements
    )
    overall_pct = round(total_compliant / total_applicable * 100, 1) if total_applicable else 0.0
    overdue_total = sum(r.get("overdue", 0) for r in requirements)
    expiring_total = sum(r.get("expiring", 0) for r in requirements)
    missing_total = sum(r.get("missing", 0) for r in requirements)

    return json.dumps({
        "department": base.get("department", "Alle"),
        "category": args.get('category') or "Alle",
        "total_requirements": len(requirements),
        "overall_compliance_pct": overall_pct,
        "overdue_total": overdue_total,
        "expiring_total": expiring_total,
        "missing_total": missing_total,
        "requirements": requirements,
        "summary_da": (
            f"{overall_pct}% overholdelse — {overdue_total} forfaldne, {expiring_total} udløber snart, "
            f"{missing_total} mangler helt."
            if requirements else "Ingen compliance-krav matcher filteret."
        ),
    }, default=str)


def _execute_hr_roi_summary(args):
    """Real training-ROI numbers for the company via insights_engine.get_roi_metrics."""
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    fiscal_year = args.get('year')
    try:
        fiscal_year = int(fiscal_year) if fiscal_year not in (None, '') else None
    except (TypeError, ValueError):
        fiscal_year = None

    try:
        import insights_engine
    except Exception as exc:
        print(f"[HR_TOOLS][roi] insights_engine import failed: {exc}")
        return json.dumps({"error": "ROI-beregning er ikke tilgængelig lige nu.", "has_data": False})

    try:
        metrics = insights_engine.get_roi_metrics(current_app, company_id, fiscal_year)
    except Exception as exc:
        print(f"[HR_TOOLS][roi] get_roi_metrics failed: {exc}")
        return json.dumps({"error": "Kunne ikke beregne ROI lige nu.", "has_data": False})

    if not isinstance(metrics, dict):
        return json.dumps({"error": "Uventet ROI-svar.", "has_data": False})

    department = (args.get('department') or '').strip()
    dept_focus = None
    if department:
        for d in (metrics.get('department_roi') or []):
            if (d.get('department') or '') == department:
                dept_focus = d
                break

    # Drop the explicitly-hypothetical scenario block from the model-facing payload
    # so the agent never presents projections as real headline numbers.
    out = {
        "fiscal_year": metrics.get('fiscal_year'),
        "has_data": bool(metrics.get('has_data')),
        "total_training_spend": metrics.get('total_training_spend'),
        "employees_trained": metrics.get('employees_trained'),
        "courses_completed": metrics.get('courses_completed'),
        "courses_total": metrics.get('courses_total'),
        "completion_rate": metrics.get('completion_rate'),
        "spend_per_employee": metrics.get('spend_per_employee'),
        "cost_per_completion": metrics.get('cost_per_completion'),
        "avg_completion_days": metrics.get('avg_completion_days'),
        "budget_total": metrics.get('budget_total'),
        "budget_spent": metrics.get('budget_spent'),
        "budget_remaining": metrics.get('budget_remaining'),
        "budget_utilization": metrics.get('budget_utilization'),
        "budget_overruns": metrics.get('budget_overruns'),
        "department_roi": metrics.get('department_roi'),
        "department_focus": dept_focus,
        "department_roi_anon_note": metrics.get('department_roi_anon_note'),
    }
    if not out["has_data"]:
        out["summary_da"] = "Der er endnu ingen trænings-data til at beregne ROI for dette år."
    else:
        out["summary_da"] = (
            f"Samlet spend {out['total_training_spend']} kr., {out['completion_rate']}% gennemført, "
            f"{out['cost_per_completion']} kr. pr. gennemførelse."
        )
    return json.dumps(out, default=str)


def _execute_hr_benchmark(args):
    """Company value vs the k-anonymous industry cohort, via benchmarking.benchmark."""
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    try:
        import benchmarking
    except Exception as exc:
        print(f"[HR_TOOLS][benchmark] import failed: {exc}")
        return json.dumps({"error": "Benchmarking er ikke tilgængelig lige nu.", "metrics": []})

    try:
        data = benchmarking.benchmark(company_id)
    except Exception as exc:
        print(f"[HR_TOOLS][benchmark] benchmark() failed: {exc}")
        return json.dumps({"error": "Kunne ikke hente benchmark lige nu.", "metrics": []})

    if not isinstance(data, dict):
        return json.dumps({"error": "Uventet benchmark-svar.", "metrics": []})

    metric_filter = (args.get('metric') or '').strip().lower()
    metrics = data.get('metrics', []) or []
    if metric_filter:
        filtered = [
            m for m in metrics
            if metric_filter in (str(m.get('key') or '').lower())
            or metric_filter in (str(m.get('label') or '').lower())
        ]
        if filtered:
            metrics = filtered

    return json.dumps({
        "industry": data.get('industry'),
        "company_size": data.get('company_size'),
        "cohort_size": data.get('cohort_size'),
        "cohort_safe": bool(data.get('safe')),
        "metrics": metrics,
        "overall_note": data.get('overall_note'),
    }, default=str)


def _execute_hr_trial_and_seat_status(args):
    """Subscription / trial / seat utilisation for the company.

    SALES-LED read only: this is an ops snapshot of the company's plan and seat
    utilisation, NOT a self-serve upsell prompt. aileadz is sold and provisioned
    by an account manager, so when the trial has expired or seats are full the
    only correct next step is to contact the account manager — never an
    'upgrade subscription' affordance. The next_step_da field carries that
    routing so the advisor grounds on it instead of inventing an upgrade flow.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    cur = _get_cursor()
    try:
        cur.execute("""
            SELECT company_name, subscription_plan, trial_ends_at,
                   max_employees, current_employee_count, status
            FROM companies
            WHERE id = %s
        """, (company_id,))
        row = cur.fetchone()
    except Exception as exc:
        cur.close()
        print(f"[HR_TOOLS][trial_seat] query failed: {exc}")
        return json.dumps({"error": "Kunne ikke hente abonnementsstatus."})

    if not row:
        cur.close()
        return json.dumps({"error": "Virksomheden blev ikke fundet."})

    # Prefer the live active-employee count over the cached counter if available.
    live_count = None
    try:
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM company_users
            WHERE company_id = %s AND status = 'active'
        """, (company_id,))
        crow = cur.fetchone()
        live_count = int(crow['cnt']) if crow and crow.get('cnt') is not None else None
    except Exception as exc:
        print(f"[HR_TOOLS][trial_seat] live count skipped: {exc}")
    cur.close()

    max_employees = int(row.get('max_employees') or 0)
    seats_used = live_count if live_count is not None else int(row.get('current_employee_count') or 0)
    seats_left = max(0, max_employees - seats_used) if max_employees else None
    utilization_pct = round(seats_used / max_employees * 100, 1) if max_employees else None

    trial_ends_at = row.get('trial_ends_at')
    trial_days_left = None
    on_trial = (str(row.get('subscription_plan') or '').lower() == 'trial')
    if trial_ends_at:
        try:
            trial_days_left = (trial_ends_at - datetime.now()).days
        except Exception:
            trial_days_left = None

    summary_parts = []
    if on_trial and trial_days_left is not None:
        summary_parts.append(
            f"Prøveperioden udløber om {trial_days_left} dag(e)." if trial_days_left >= 0
            else f"Prøveperioden udløb for {abs(trial_days_left)} dag(e) siden."
        )
    if max_employees:
        summary_parts.append(f"{seats_used} af {max_employees} pladser brugt ({utilization_pct}% udnyttelse).")

    # Sales-led routing: when the trial has expired or seats are full, the next
    # step is always to contact the account manager — NEVER a self-serve upgrade.
    trial_expired = bool(on_trial and trial_days_left is not None and trial_days_left < 0)
    seats_full = bool(max_employees and seats_left == 0)
    if trial_expired or seats_full:
        next_step_da = (
            "Kontakt din kundeansvarlige for at tilføje flere pladser eller "
            "forny abonnementet."
        )
    else:
        next_step_da = (
            "Ingen handling nødvendig. Kontakt din kundeansvarlige ved behov "
            "for flere pladser."
        )

    return json.dumps({
        "company_name": row.get('company_name'),
        "subscription_plan": row.get('subscription_plan'),
        "on_trial": on_trial,
        "trial_ends_at": trial_ends_at.isoformat() if hasattr(trial_ends_at, 'isoformat') else trial_ends_at,
        "trial_days_left": trial_days_left,
        "max_employees": max_employees,
        "seats_used": seats_used,
        "seats_left": seats_left,
        "utilization_pct": utilization_pct,
        "seats_full": seats_full,
        "next_step_da": next_step_da,
        "summary_da": " ".join(summary_parts) or "Ingen abonnementsgrænser registreret.",
    }, default=str)


def _execute_approve_order_from_chat(args):
    """MUTATION — approve/reject a pending course order from the HR chat.

    Routes the budget side effects through order_service.set_status (approved ->
    'pending' charges the budget once; rejected refunds once) and records the
    decision on order_approvals. Requires (a) confirm=true and (b) the HR actor be
    a company manager. Strictly company-scoped: the resolved order must belong to
    the session company.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    decision = (args.get('decision') or '').strip().lower()
    if decision not in ('approved', 'rejected'):
        return json.dumps({"error": "decision skal være 'approved' eller 'rejected'."})

    approval_id = args.get('approval_id')
    order_id = (args.get('order_id') or '').strip() or None
    if not approval_id and not order_id:
        return json.dumps({"error": "Angiv enten approval_id eller order_id."})

    # ── Authorization: actor must be a company manager. ──
    try:
        from order_service import OrderContext, set_status as _set_status
    except Exception as exc:
        print(f"[HR_TOOLS][approve_order] order_service import failed: {exc}")
        return json.dumps({"error": "Ordrehåndtering er ikke tilgængelig lige nu."})

    ctx = OrderContext.from_session(source='hr_chat')
    if not ctx.is_manager:
        return json.dumps({
            "error": "not_authorized",
            "message": "Kun ledere/HR-managere kan godkende eller afvise bestillinger.",
        })

    cur = _get_cursor()
    # ── Resolve the order, scoped to THIS company. ──
    try:
        if approval_id and not order_id:
            cur.execute("""
                SELECT order_id FROM order_approvals
                WHERE id = %s AND company_id = %s
            """, (approval_id, company_id))
            arow = cur.fetchone()
            if not arow:
                cur.close()
                return json.dumps({"error": "Godkendelsesanmodningen blev ikke fundet for din virksomhed."})
            order_id = arow.get('order_id')

        cur.execute("""
            SELECT order_id, company_id, username, product_title, price, status
            FROM course_orders
            WHERE order_id = %s AND company_id = %s
        """, (order_id, company_id))
        order = cur.fetchone()
    except Exception as exc:
        cur.close()
        print(f"[HR_TOOLS][approve_order] resolve failed: {exc}")
        return json.dumps({"error": "Kunne ikke slå ordren op."})

    if not order:
        cur.close()
        return json.dumps({"error": "Ordren blev ikke fundet for din virksomhed."})

    # ── Confirmation gate: without confirm, return a preview only. ──
    if not bool(args.get('confirm')):
        cur.close()
        return json.dumps({
            "needs_confirmation": True,
            "action": "approve_order",
            "decision": decision,
            "order_id": order.get('order_id'),
            "product_title": order.get('product_title'),
            "price": float(order.get('price') or 0),
            "employee": order.get('username'),
            "current_status": order.get('status'),
            "message_da": (
                f"Bekræft at du vil {'GODKENDE' if decision == 'approved' else 'AFVISE'} bestillingen "
                f"'{order.get('product_title')}' for {order.get('username')} "
                f"({float(order.get('price') or 0):.0f} kr.). Send confirm=true for at udføre."
            ),
        }, default=str)
    cur.close()

    # approved -> 'pending' (charges budget once); rejected -> 'rejected' (refunds).
    new_status = 'pending' if decision == 'approved' else 'rejected'
    try:
        result = _set_status(ctx, order_id, new_status)
    except Exception as exc:
        print(f"[HR_TOOLS][approve_order] set_status raised: {exc}")
        return json.dumps({"error": "Statusændring fejlede."})

    if not isinstance(result, dict) or not result.get('success'):
        return json.dumps({
            "error": "status_change_failed",
            "message": (result or {}).get('message', 'Statusændring fejlede.') if isinstance(result, dict) else "Statusændring fejlede.",
        })

    # ── Record the decision on order_approvals (best-effort, same company). ──
    try:
        cur2 = _get_cursor()
        cur2.execute("""
            UPDATE order_approvals
            SET status = %s, approver_user_id = %s, decided_at = NOW()
            WHERE order_id = %s AND company_id = %s
        """, (decision, ctx.user_id, order_id, company_id))
        current_app.mysql.connection.commit()
        cur2.close()
    except Exception as exc:
        print(f"[HR_TOOLS][approve_order] order_approvals update skipped: {exc}")

    return json.dumps({
        "success": True,
        "decision": decision,
        "order_id": order_id,
        "new_status": new_status,
        "charged": result.get('charged'),
        "refunded": result.get('refunded'),
        "message_da": (
            f"Bestillingen er {'godkendt' if decision == 'approved' else 'afvist'}."
            + (" Budgettet er trukket." if result.get('charged') else "")
            + (" Budgettet er refunderet." if result.get('refunded') else "")
        ),
    }, default=str)


def _execute_assign_learning_path_to_team(args):
    """MUTATION — bulk-assign a learning path / course to a set of employees.

    Creates one employee_learning_progress row per employee (status not_started)
    and a nudge notification (company_notifications) per employee. Requires
    confirm=true. Strictly company-scoped: every target user_id must be an active
    company_users row for the session company; foreign ids are rejected.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    raw_ids = args.get('employee_ids') or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return json.dumps({"error": "Angiv mindst ét user_id i employee_ids."})

    # Coerce to ints, drop junk.
    employee_ids = []
    for v in raw_ids:
        try:
            employee_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if not employee_ids:
        return json.dumps({"error": "employee_ids indeholder ingen gyldige id'er."})

    path_id = args.get('path_id')
    try:
        path_id = int(path_id) if path_id not in (None, '') else None
    except (TypeError, ValueError):
        path_id = None
    course_handle = (args.get('course_handle') or '').strip() or None
    if not path_id and not course_handle:
        return json.dumps({"error": "Angiv enten path_id eller course_handle."})

    cur = _get_cursor()

    # ── Validate every target belongs to THIS company (cross-tenant guard). ──
    placeholders = ",".join(["%s"] * len(employee_ids))
    try:
        cur.execute(f"""
            SELECT cu.user_id, cu.full_name, cu.username
            FROM company_users cu
            WHERE cu.company_id = %s AND cu.status = 'active'
              AND cu.user_id IN ({placeholders})
        """, tuple([company_id] + employee_ids))
        valid_rows = cur.fetchall()
    except Exception as exc:
        cur.close()
        print(f"[HR_TOOLS][assign_path] validation failed: {exc}")
        return json.dumps({"error": "Kunne ikke validere medarbejdere."})

    valid_map = {r['user_id']: r for r in valid_rows}
    valid_ids = list(valid_map.keys())
    rejected_ids = [uid for uid in employee_ids if uid not in valid_map]

    if not valid_ids:
        cur.close()
        return json.dumps({
            "error": "Ingen af de angivne medarbejdere tilhører din virksomhed.",
            "rejected_user_ids": rejected_ids,
        })

    # ── Resolve a human-readable label for the assignment. ──
    path_name = None
    if path_id:
        try:
            cur.execute("""
                SELECT path_name FROM learning_paths
                WHERE id = %s AND (company_id = %s OR company_id IS NULL)
            """, (path_id, company_id))
            prow = cur.fetchone()
            if not prow:
                cur.close()
                return json.dumps({"error": "Læringsstien blev ikke fundet for din virksomhed."})
            path_name = prow.get('path_name')
        except Exception as exc:
            cur.close()
            print(f"[HR_TOOLS][assign_path] path lookup failed: {exc}")
            return json.dumps({"error": "Kunne ikke slå læringsstien op."})
    content_name = path_name or course_handle

    # ── Confirmation gate: preview only without confirm. ──
    if not bool(args.get('confirm')):
        cur.close()
        return json.dumps({
            "needs_confirmation": True,
            "action": "assign_learning_path",
            "path_id": path_id,
            "course_handle": course_handle,
            "content_name": content_name,
            "target_count": len(valid_ids),
            "targets": [{"user_id": uid, "name": valid_map[uid].get('full_name') or valid_map[uid].get('username')} for uid in valid_ids],
            "rejected_user_ids": rejected_ids,
            "message_da": (
                f"Bekræft at du vil tildele '{content_name}' til {len(valid_ids)} medarbejder(e). "
                f"Send confirm=true for at udføre."
            ),
        }, default=str)

    # ── Execute: progress rows + nudges, idempotent on existing rows. ──
    assigned = 0
    nudged = 0
    conn = current_app.mysql.connection
    for uid in valid_ids:
        try:
            # Skip if an identical assignment already exists (idempotent bulk).
            if path_id:
                cur.execute("""
                    SELECT id FROM employee_learning_progress
                    WHERE user_id = %s AND company_id = %s AND learning_path_id = %s
                """, (uid, company_id, path_id))
            else:
                cur.execute("""
                    SELECT id FROM employee_learning_progress
                    WHERE user_id = %s AND company_id = %s AND course_handle = %s
                """, (uid, company_id, course_handle))
            if cur.fetchone():
                continue
            cur.execute("""
                INSERT INTO employee_learning_progress
                    (user_id, company_id, learning_path_id, course_handle,
                     content_type, content_name, status, progress_percentage, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'not_started', 0, NOW())
            """, (
                uid, company_id, path_id, course_handle,
                'learning_path' if path_id else 'course', content_name,
            ))
            assigned += 1
        except Exception as exc:
            print(f"[HR_TOOLS][assign_path] progress insert skipped for user {uid}: {exc}")
            continue

        # Nudge notification per employee (best-effort).
        try:
            cur.execute("""
                INSERT INTO company_notifications
                    (company_id, recipient_user_id, sender_user_id, title, message, is_urgent, created_at)
                VALUES (%s, %s, %s, %s, %s, 0, NOW())
            """, (
                company_id, uid, session.get('user_id'),
                "Ny læring tildelt",
                f"Du er blevet tildelt '{content_name}'. Gå i gang når du er klar.",
            ))
            nudged += 1
        except Exception as exc:
            print(f"[HR_TOOLS][assign_path] nudge skipped for user {uid}: {exc}")

    try:
        conn.commit()
    except Exception as exc:
        print(f"[HR_TOOLS][assign_path] commit failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        cur.close()
        return json.dumps({"error": "Tildelingen kunne ikke gemmes."})
    cur.close()

    return json.dumps({
        "success": True,
        "content_name": content_name,
        "assigned": assigned,
        "already_assigned": len(valid_ids) - assigned,
        "nudges_sent": nudged,
        "rejected_user_ids": rejected_ids,
        "message_da": (
            f"'{content_name}' tildelt til {assigned} medarbejder(e); {nudged} notifikation(er) sendt."
            + (f" {len(rejected_ids)} id'er blev afvist (ikke i din virksomhed)." if rejected_ids else "")
        ),
    }, default=str)


def _execute_hr_inactive_employees(args):
    """Active employees with no activity for >= inactive_days (default 30)."""
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    try:
        inactive_days = int(args.get('inactive_days') or 30)
    except (TypeError, ValueError):
        inactive_days = 30
    if inactive_days < 0:
        inactive_days = 30

    cur = _get_cursor()
    # Inactive = the most recent of (last_active_at, last_chatbot_interaction,
    # last_login) is NULL or older than the threshold.
    try:
        cur.execute("""
            SELECT cu.user_id, cu.username, cu.full_name, cu.department,
                   cu.manager_user_id, cu.last_active_at, cu.last_chatbot_interaction,
                   cu.last_login,
                   GREATEST(
                       COALESCE(cu.last_active_at, '1970-01-01'),
                       COALESCE(cu.last_chatbot_interaction, '1970-01-01'),
                       COALESCE(cu.last_login, '1970-01-01')
                   ) AS last_seen
            FROM company_users cu
            WHERE cu.company_id = %s AND cu.status = 'active'
              AND (
                  (cu.last_active_at IS NULL OR cu.last_active_at < DATE_SUB(NOW(), INTERVAL %s DAY))
                  AND (cu.last_chatbot_interaction IS NULL OR cu.last_chatbot_interaction < DATE_SUB(NOW(), INTERVAL %s DAY))
                  AND (cu.last_login IS NULL OR cu.last_login < DATE_SUB(NOW(), INTERVAL %s DAY))
              )
            ORDER BY last_seen ASC
        """, (company_id, inactive_days, inactive_days, inactive_days))
        rows = cur.fetchall()
    except Exception as exc:
        cur.close()
        print(f"[HR_TOOLS][inactive_employees] query failed: {exc}")
        return json.dumps({"error": "Kunne ikke hente inaktive medarbejdere.", "employees": [], "total": 0})

    cur.close()

    now = datetime.now()
    employees = []
    for r in rows:
        candidates = [r.get('last_active_at'), r.get('last_chatbot_interaction'), r.get('last_login')]
        last_seen = max((d for d in candidates if isinstance(d, datetime)), default=None)
        days_inactive = (now - last_seen).days if last_seen else None
        employees.append({
            "user_id": r.get('user_id'),
            "username": r.get('username'),
            "full_name": r.get('full_name') or r.get('username'),
            "department": r.get('department') or "Ukendt",
            "manager_user_id": r.get('manager_user_id'),
            "last_seen": last_seen.isoformat() if last_seen else None,
            "days_inactive": days_inactive,
        })

    return json.dumps({
        "inactive_days_threshold": inactive_days,
        "employees": employees,
        "total": len(employees),
        "summary_da": (
            f"{len(employees)} medarbejder(e) har ikke været aktive i {inactive_days}+ dage."
            if employees else f"Ingen medarbejdere har været inaktive i {inactive_days}+ dage."
        ),
    }, default=str)


def _execute_hr_expiring_agreements(args):
    """Supplier agreements expiring within within_days, via catalog_freshness."""
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    try:
        within_days = int(args.get('within_days') or 30)
    except (TypeError, ValueError):
        within_days = 30
    if within_days < 0:
        within_days = 30

    try:
        import catalog_freshness
    except Exception as exc:
        print(f"[HR_TOOLS][expiring_agreements] import failed: {exc}")
        return json.dumps({"error": "Aftaledata er ikke tilgængelig lige nu.", "agreements": [], "total": 0})

    try:
        # Always company-scoped — never platform-wide from the HR chat.
        rows = catalog_freshness.expiring_agreements(company_id=company_id, within_days=within_days)
    except Exception as exc:
        print(f"[HR_TOOLS][expiring_agreements] call failed: {exc}")
        return json.dumps({"error": "Kunne ikke hente udløbende aftaler.", "agreements": [], "total": 0})

    agreements = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        vu = r.get('valid_until')
        agreements.append({
            "vendor_name": r.get('vendor_name'),
            "agreement_name": r.get('agreement_name'),
            "agreement_reference": r.get('agreement_reference'),
            "discount_type": r.get('discount_type'),
            "discount_value": r.get('discount_value'),
            "valid_until": vu.isoformat() if hasattr(vu, 'isoformat') else vu,
            "days_left": r.get('days_left'),
            "is_expired": bool(r.get('is_expired')),
        })

    expired_n = sum(1 for a in agreements if a["is_expired"])
    return json.dumps({
        "within_days": within_days,
        "agreements": agreements,
        "total": len(agreements),
        "expired": expired_n,
        "summary_da": (
            f"{len(agreements)} leverandøraftale(r) udløber inden for {within_days} dage "
            f"({expired_n} allerede udløbet)."
            if agreements else f"Ingen leverandøraftaler udløber inden for {within_days} dage."
        ),
    }, default=str)


def _execute_get_workforce_risk(args):
    """Early-warning workforce risk for the company — AGGREGATE-FIRST and k-anon-safe.

    Wraps insights_engine.get_predictive_data (which returns NAMED individuals with
    NO suppression) and runs it through insights_engine.aggregate_workforce_risk so
    the model only ever sees department/skill-level COUNTS with sub-k cohorts
    suppressed — never identifiable at-risk/churn employees. This is NET-NEW
    suppression (the engine has none); see the HR_VALUE_PLAN risk register.

    Individual NAMES are surfaced ONLY behind an explicit ``drill_down=true`` AND
    the actor being a company manager, and even then only for departments whose
    cohort is itself k-safe (the aggregator enforces this). Strictly company-scoped.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    try:
        import insights_engine
    except Exception as exc:
        print(f"[HR_TOOLS][workforce_risk] insights_engine import failed: {exc}")
        return json.dumps({"error": "Risikoanalyse er ikke tilgængelig lige nu.", "has_data": False})

    # Drill-down to named individuals is doubly gated: an explicit flag AND a
    # manager role. A non-manager (or a missing confirm) never receives names.
    want_drill = bool(args.get('drill_down'))
    is_manager = False
    if want_drill:
        try:
            from order_service import OrderContext
            is_manager = OrderContext.from_session(source='hr_chat').is_manager
        except Exception as exc:
            print(f"[HR_TOOLS][workforce_risk] manager check failed: {exc}")
            is_manager = False
    drill_down = want_drill and is_manager

    try:
        predictions = insights_engine.get_predictive_data(
            current_app._get_current_object(), company_id)
    except Exception as exc:
        print(f"[HR_TOOLS][workforce_risk] get_predictive_data failed: {exc}")
        return json.dumps({"error": "Kunne ikke beregne workforce-risiko lige nu.", "has_data": False})

    try:
        risk = insights_engine.aggregate_workforce_risk(predictions, drill_down=drill_down)
    except Exception as exc:
        print(f"[HR_TOOLS][workforce_risk] aggregation failed: {exc}")
        return json.dumps({"error": "Kunne ikke aggregere workforce-risiko lige nu.", "has_data": False})

    churn_total = risk.get('churn_risk', {}).get('total', 0)
    gap_total = risk.get('skill_gap_risk', {}).get('total', 0)
    trending = risk.get('trending_demand', {}).get('terms', [])
    has_data = bool(churn_total or gap_total or trending)

    out = {
        "has_data": has_data,
        "k": risk.get('k'),
        "drill_down_applied": drill_down,
        "churn_risk": risk.get('churn_risk'),
        "skill_gap_risk": risk.get('skill_gap_risk'),
        "trending_demand": risk.get('trending_demand'),
        "anon_note": risk.get('anon', {}).get('note_da') or None,
    }
    # If a non-manager (or no-confirm) asked to drill down, say so explicitly so
    # the model routes them, rather than silently returning only aggregates.
    if want_drill and not drill_down:
        out["drill_down_denied"] = True
        out["drill_down_message_da"] = (
            "Navne på enkeltpersoner kan kun vises for ledere/HR-managere og kun for "
            "afdelinger der er store nok til at bevare anonymitet. Kontakt en manager for detaljer."
        )

    if not has_data:
        out["summary_da"] = "Ingen aktuelle risikosignaler at handle på lige nu."
    else:
        parts = []
        if churn_total:
            parts.append(f"{churn_total} medarbejder(e) med inaktivitets-/fastholdelsesrisiko")
        if gap_total:
            parts.append(f"{gap_total} medarbejder(e) med væsentlige kompetencegab")
        if trending:
            parts.append(f"{len(trending)} stigende læringsbehov")
        out["summary_da"] = "Tidlig advarsel: " + ", ".join(parts) + "."
    return json.dumps(out, default=str)


def _execute_hr_explain_insights(args):
    """Read-only conversational view of the company's proactive AI insight cards.

    Thin wrapper over insights_engine.generate_company_insights so the advisor can
    explain the same insight cards the dashboard surfaces ('what should I act on
    this week?'). The insights are company-level aggregates (trending topics are
    k-anon-gated inside the engine; the rest are counts) — no people-level rows.
    Severity can be filtered to focus on what needs action.
    """
    company_id = session.get('company_id')
    if not company_id:
        return json.dumps({"error": "Ingen virksomhed fundet."})

    try:
        import insights_engine
    except Exception as exc:
        print(f"[HR_TOOLS][explain_insights] insights_engine import failed: {exc}")
        return json.dumps({"error": "Indsigter er ikke tilgængelige lige nu.", "insights": [], "total": 0})

    try:
        insights = insights_engine.generate_company_insights(
            current_app._get_current_object(), company_id)
    except Exception as exc:
        print(f"[HR_TOOLS][explain_insights] generate_company_insights failed: {exc}")
        return json.dumps({"error": "Kunne ikke hente indsigter lige nu.", "insights": [], "total": 0})

    severity_filter = (args.get('severity') or '').strip().lower()
    rows = []
    for ins in (insights or []):
        if not isinstance(ins, dict):
            continue
        sev = (ins.get('severity') or 'info')
        if severity_filter and sev != severity_filter:
            continue
        rows.append({
            "type": ins.get('type'),
            "severity": sev,
            "title": ins.get('title'),
            "body": ins.get('body'),
            "data": ins.get('data', {}),
        })

    actionable = sum(1 for r in rows if r["severity"] in ('warning', 'critical', 'danger'))
    return json.dumps({
        "insights": rows,
        "total": len(rows),
        "actionable": actionable,
        "summary_da": (
            f"{len(rows)} indsigt(er){' (' + str(actionable) + ' kræver handling)' if actionable else ''}."
            if rows else "Der er endnu ikke nok aktivitet til at generere indsigter."
        ),
    }, default=str)


# ── Tool router ──

def execute_hr_tool(tool_call):
    """Execute an HR tool call and return JSON result."""
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except Exception as e:
        return json.dumps({"error": f"Kunne ikke parse argumenter: {e}"})

    router = {
        "get_team_training_status": _execute_get_team_training_status,
        "get_company_skill_gaps": _execute_get_company_skill_gaps,
        "get_budget_overview": _execute_get_budget_overview,
        "get_employee_overview": _execute_get_employee_overview,
        "get_training_report": _execute_get_training_report,
        "get_pending_actions": _execute_get_pending_actions,
        "search_courses_for_team": _execute_search_courses_for_team,
        "get_chatbot_usage_stats": _execute_get_chatbot_usage_stats,
        "hr_get_company_learning_context": _execute_hr_get_company_learning_context,
        "hr_recommend_training_plan": _execute_hr_recommend_training_plan,
        "hr_get_supplier_coverage": _execute_hr_get_supplier_coverage,
        "hr_get_ai_usage_risks": _execute_hr_get_ai_usage_risks,
        "get_compliance_status": _execute_get_compliance_status,
        "get_team_non_starters": _execute_get_team_non_starters,
        "hr_team_compliance": _execute_hr_team_compliance,
        "hr_roi_summary": _execute_hr_roi_summary,
        "hr_benchmark": _execute_hr_benchmark,
        "hr_trial_and_seat_status": _execute_hr_trial_and_seat_status,
        "approve_order_from_chat": _execute_approve_order_from_chat,
        "assign_learning_path_to_team": _execute_assign_learning_path_to_team,
        "hr_inactive_employees": _execute_hr_inactive_employees,
        "hr_expiring_agreements": _execute_hr_expiring_agreements,
        "get_workforce_risk": _execute_get_workforce_risk,
        "hr_explain_insights": _execute_hr_explain_insights,
    }

    fn = router.get(name)
    if not fn:
        return json.dumps({"error": f"Ukendt HR-funktion: {name}"})

    try:
        return fn(args)
    except Exception as e:
        import traceback
        print(f"[HR Tool Error] {name}: {e}")
        print(f"[HR Tool Traceback] {traceback.format_exc()}")
        return json.dumps({"error": f"Intern fejl i {name}: {str(e)}"})
