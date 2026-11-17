"""HTTP boundary for the shared action inbox and safe confirmation/recovery."""
import json
import types

from flask import jsonify, request, Response
import ai_actions as actions


def require_actor():
    return bool(actions.identity()[0])


def confirm(key):
    if not require_actor():
        return jsonify(error='Log ind for at fortsætte.'), 401
    row = actions.inspect(key)
    if not row or row['kind'] != 'confirmation':
        return jsonify(status='unknown_token', message='Handlingen findes ikke i denne virksomhed.'), 404
    if row['status'] != 'pending':
        return jsonify(_receipt(row)), 200
    entry = actions.claim(key)
    if not entry:
        return jsonify(_receipt(actions.inspect(key))), 409
    args = dict(entry['payload'].get('args') or {})
    args['confirm'] = True
    args['accept_over_budget'] = (request.get_json(silent=True) or {}).get('accept_over_budget') is True
    call = types.SimpleNamespace(name=entry['name'], arguments=args,
                                 function=types.SimpleNamespace(name=entry['name'], arguments=json.dumps(args)))
    marker = actions.ACTIVE_OPERATION.set(key)
    try:
        if entry['scope'] == 'hr':
            from hr_tools import execute_hr_tool
            raw = execute_hr_tool(call)
        elif entry['scope'] == 'vendor':
            from vendor_tools import execute_vendor_action
            raw = execute_vendor_action(entry['name'], args)
        else:
            from app1.tools import execute_tool
            raw = execute_tool(call, username=entry['username'], session_id=entry['session_id'])
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict):
            result = {'status': 'error', 'message': 'Handlingen gav ikke et gyldigt resultat.'}
        if entry['name'] == 'create_course_order' and entry['scope'] == 'employee':
            from app1.order_confirmation import order_confirmation_text
            confirmation = order_confirmation_text(result, args)
            if confirmation:
                result['confirmation_text'] = confirmation
                from app1 import _record_assistant_message
                _record_assistant_message(entry['username'], entry['session_id'], confirmation)
        status = actions.finish(key, result)
        result = dict(result, operation_id=key, status_url='/app1/actions/' + key)
        if status == 'partial':
            result['status'] = 'partial_success'
        return jsonify(result)
    except Exception:
        # Execution may have committed before failing to return its receipt.
        actions.transition(key, ('running',), 'uncertain', receipt={'message': 'Resultatet er usikkert. Kontroller status før en ny handling.'})
        return jsonify(status='uncertain', message='Resultatet er usikkert. Kontroller status før en ny handling.'), 500
    finally:
        actions.ACTIVE_OPERATION.reset(marker)


def _receipt(row):
    if not row:
        return {'status': 'unknown_token', 'message': 'Handlingen blev ikke fundet.'}
    messages = {'expired': 'Bekræftelsen er udløbet. Bed om en ny gennemgang.',
                'running': 'Handlingen behandles. Kontroller status om lidt.',
                'uncertain': 'Resultatet er usikkert. Kontroller status før en ny handling.',
                'rejected': 'Handlingen er afvist.', 'cancelled': 'Handlingen er stoppet.',
                'failed': 'Handlingen fejlede. Se resultatet før du prøver igen.'}
    status = row['status']
    result = dict(row.get('receipt') or {})
    result.pop('stream', None)
    result.update(status='already_confirmed' if status == 'consumed' else ('partial_success' if status == 'partial' else status),
                  operation_id=row['id'], status_url='/app1/actions/' + row['id'])
    result.setdefault('message', messages.get(status, 'Handlingen er gennemført.'))
    return result


def reject(key):
    row = actions.inspect(key)
    if not require_actor() or not row:
        return jsonify(status='unknown_token'), 404
    actions.transition(key, ('pending',), 'rejected')
    return jsonify(_receipt(actions.inspect(key)))


def register(bp):
    @bp.route('/actions', methods=['GET'])
    def ai_action_inbox():
        if not require_actor():
            return jsonify(error='Log ind for at se dine handlinger.'), 401
        return jsonify(actions=actions.list_actions(sid=request.args.get('session_id')))

    @bp.route('/actions/<key>', methods=['GET'])
    def ai_action_status(key):
        if not require_actor():
            return jsonify(error='Log ind for at se handlingen.'), 401
        row = actions.inspect(key)
        return (jsonify(actions.public(row)) if row else (jsonify(error='Handlingen blev ikke fundet.'), 404))

    @bp.route('/actions/<key>/download', methods=['GET'])
    def ai_artifact_download(key):
        row = actions.inspect(key)
        if not require_actor() or not row or row['kind'] != 'artifact' or row['status'] != 'complete':
            return jsonify(error='Filen blev ikke fundet.'), 404
        import re
        filename = re.sub(r'[^A-Za-z0-9_.-]', '_', row['payload'].get('filename', 'kursus.ics'))[:100]
        response = Response(row['payload'].get('content', ''), mimetype='text/calendar')
        response.headers['Content-Disposition'] = 'attachment; filename="' + filename + '"'
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @bp.route('/actions/<key>/confirm', methods=['POST'])
    def ai_action_confirm(key):
        return confirm(key)

    @bp.route('/actions/<key>/reject', methods=['POST'])
    def ai_action_reject(key):
        return reject(key)

    @bp.route('/actions/<key>/cancel', methods=['POST'])
    def ai_operation_cancel(key):
        row = actions.inspect(key)
        if not require_actor() or not row or row['kind'] != 'turn':
            return jsonify(error='Handlingen blev ikke fundet.'), 404
        actions.transition(key, ('pending', 'running'), 'cancelled', receipt={'message': 'Stoppet. Allerede udførte handlinger er ikke fortrudt.'})
        return jsonify(_receipt(actions.inspect(key)))
