"""AI adapters use the same capabilities and department boundary as the pages."""
from flask import has_request_context
from auth_decorators import can, department_scope

HR_CAPABILITIES = {
    'get_team_training_status': 'hr.view',
    'get_employee_overview': 'hr.employees.view',
    'get_budget_overview': 'hr.budget.view',
    'approve_order_from_chat': 'hr.approvals',
    'hr_open_in_app': 'hr.view',
    'hr_search_company_documents': 'hr.view',
    'hr_search_platform_help': 'hr.view',
    'hr_onboarding_readiness': 'company.employees',
    'hr_mail_delivery_status': 'company.employees',
    'search_courses_for_team': 'hr.view',
    'assign_learning_path_to_team': 'company.learning_paths',
    'set_skill_target': 'hr.manage',
    'create_compliance_requirement': 'company.compliance',
    'schedule_recurring_report': 'company.reports',
    'recheck_compliance': 'company.compliance',
    'send_company_email': 'company.notifications_send',
    'send_deadline_reminders': 'hr.manage',
    'create_order_for_employee': 'hr.manage',
    'hr_request_order_change': 'hr.manage',
    'bulk_calendar_invites': 'hr.view',
}
SCOPED_READS = {'get_team_training_status', 'get_employee_overview', 'get_budget_overview'}


def hr_allowed(name):
    if department_scope() == '' and name not in {'hr_open_in_app', 'hr_search_company_documents', 'hr_search_platform_help', 'search_courses_for_team', 'bulk_calendar_invites'}:
        return False
    if department_scope() is not None and name not in SCOPED_READS | {'approve_order_from_chat', 'hr_open_in_app', 'hr_search_company_documents', 'hr_search_platform_help', 'search_courses_for_team', 'bulk_calendar_invites'}:
        return False
    return can(HR_CAPABILITIES.get(name, 'hr.analytics'))


def hr_arguments(name, args):
    if not has_request_context() or not hr_allowed(name):
        return None, {'error': 'not_authorized', 'message': 'Du har ikke adgang til denne handling.'}
    args = dict(args)
    scope = department_scope()
    if name in SCOPED_READS and scope is not None:
        if not scope:
            return None, {'error': 'not_authorized', 'message': 'Din afdeling kunne ikke bekræftes.'}
        args['department'] = scope
    if name == 'approve_order_from_chat' and scope is not None:
        from flask import current_app, session
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            if args.get('order_id'):
                cur.execute('SELECT department FROM course_orders WHERE order_id=%s AND company_id=%s', (args['order_id'],session.get('company_id')))
            else:
                cur.execute('SELECT co.department FROM course_orders co JOIN order_approvals oa ON oa.order_id=co.order_id AND oa.company_id=co.company_id WHERE oa.id=%s AND co.company_id=%s', (args.get('approval_id'),session.get('company_id')))
            row = cur.fetchone()
        finally:
            cur.close()
        if not row or row.get('department') != scope:
            return None, {'error':'not_authorized','message':'Bestillingen blev ikke fundet i din afdeling.'}
    return args, None


def filter_hr_tools(names):
    # Pure selector tests and batch inspection do not have an actor. Execution
    # always checks again, including confirmation and legacy tool-menu paths.
    return [n for n in names if not has_request_context() or hr_allowed(n)]
