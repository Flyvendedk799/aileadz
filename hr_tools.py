"""
HR Chatbot Tools — Company-level analytics tools for HR managers.
Separate from employee chatbot tools. Only available in HR dashboard chatbot.
"""
import json
import db_compat  # noqa: F401
import MySQLdb.cursors
from flask import current_app, session
from datetime import datetime, timedelta


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
    cur.close()

    # Build gap analysis
    skill_map = {}
    for s in skills:
        key = (s['department'] or 'Alle', s['skill_name'])
        if key not in skill_map:
            skill_map[key] = []
        skill_map[key].append({"username": s['username'], "level": s['current_level']})

    gaps = []
    for t in targets:
        dept = t['department'] or 'Alle'
        key = (dept, t['skill_name'])
        employees = skill_map.get(key, [])
        avg_level = round(sum(e['level'] for e in employees) / len(employees), 1) if employees else 0
        gap = round(t['target_level'] - avg_level, 1)
        gaps.append({
            "department": dept,
            "skill": t['skill_name'],
            "target": t['target_level'],
            "avg_current": avg_level,
            "gap": gap,
            "priority": t['priority'],
            "employees_assessed": len(employees),
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
