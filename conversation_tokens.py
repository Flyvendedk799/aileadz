"""Signed per-tab conversation handles, bound to actor, tenant and surface."""
from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadData
import ai_actions


def mint(sid, scope='employee'):
    return URLSafeTimedSerializer(current_app.secret_key, salt='ai-conversation-v1').dumps(
        {'sid': sid, 'scope': scope, 'owner': list(ai_actions.identity())})


def read(token, scope='employee'):
    try:
        data = URLSafeTimedSerializer(current_app.secret_key, salt='ai-conversation-v1').loads(token, max_age=30 * 86400)
        if data['owner'] != list(ai_actions.identity()) or data['scope'] != scope:
            return None
        from conversation_privacy import is_deleted
        if is_deleted(data['owner'][0], data['sid']):
            return None
        return data['sid']
    except (BadData, KeyError, TypeError):
        return None
