"""Resumable assistant links preserve the referenced item, not instructions."""
from urllib.parse import urlencode


def order_chat_url(order_id):
    return '/chat?' + urlencode({'from':'order','focus':'order:' + str(order_id)})


def checkin_focus(username, key):
    if not str(key).isdigit():
        return None
    from flask import current_app
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT id,kind,topic,reason FROM user_profile_checkins WHERE username=%s AND id=%s AND status IN ('pending','asked')", (username,int(key)))
        row = cur.fetchone()
    finally:
        cur.close()
    if not row:
        return None
    import json
    return {'kind':'checkin','lines':[json.dumps(row,ensure_ascii=False,default=str)]}
