"""
HR Chatbot Tools — Company-level analytics tools for HR managers.
Separate from employee chatbot tools. Only available in HR dashboard chatbot.
"""
import json
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
        LEFT JOIN course_orders co ON co.username = u.username
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
        JOIN users u ON esm.user_id = u.id
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
        LEFT JOIN course_orders co ON co.username = u.username
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
            JOIN users u ON esm.user_id = u.id
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

    dept_join = "JOIN company_users cu ON co.username = (SELECT u2.username FROM users u2 JOIN company_users cu2 ON u2.id = cu2.user_id WHERE cu2.company_id = co.company_id AND cu2.department = %s AND u2.username = co.username LIMIT 1)" if department else ""

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
            COALESCE(SUM(CASE WHEN status != 'cancelled' THEN total_price ELSE 0 END), 0) as total_spend,
            COUNT(DISTINCT username) as unique_employees,
            AVG(CASE WHEN status = 'completed' AND completed_at IS NOT NULL
                THEN DATEDIFF(completed_at, created_at) END) as avg_completion_days
        FROM course_orders co
        WHERE {base_where}
    """, tuple(params))
    stats = cur.fetchone()

    # Top ordered courses
    cur.execute(f"""
        SELECT co.course_title, COUNT(*) as order_count,
               SUM(total_price) as total_value
        FROM course_orders co
        WHERE {base_where} AND co.status != 'cancelled'
        GROUP BY co.course_title
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
        "top_courses": [{"title": c['course_title'], "orders": c['order_count'],
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
            JOIN company_users cu ON esm.user_id = cu.user_id AND cu.company_id = %s
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
        results = hybrid_rank_products(query, products, top_n=limit)
        courses = []
        for r in results:
            p = r if isinstance(r, dict) else r
            courses.append({
                "title": p.get("title", ""),
                "handle": p.get("handle", ""),
                "vendor": p.get("vendor", ""),
                "price": p.get("price", ""),
                "product_type": p.get("product_type", ""),
                "tags": p.get("tags", [])[:5] if isinstance(p.get("tags"), list) else [],
            })
        return json.dumps({"query": query, "results": courses, "count": len(courses)}, default=str)
    except Exception as e:
        return json.dumps({"error": f"Soegefejl: {str(e)}"})


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
