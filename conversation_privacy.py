"""Deletion invalidates both derived recall and late background writes."""
import hashlib
import ai_actions


def key(username, sid):
    return 'deleted:' + hashlib.sha256((username + ':' + sid).encode()).hexdigest()[:48]


def is_deleted(username, sid):
    return bool(ai_actions.get(key(username, sid), internal=True))


def mark_deleted(username, sid):
    ai_actions.create(sid, 'employee', 'tombstone', 'Deleted conversation',
                      ttl=180 * 86400, action_id=key(username, sid), owner=(username, ''))
