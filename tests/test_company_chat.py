"""Company chat never accepts a recipient outside the active company."""
import unittest
from unittest import mock

from flask import Flask

import futurematch_ui


class ChatCursor:
    lastrowid = 17

    def __init__(self, members, messages=None):
        self.members = members
        self.messages = messages or []
        self.queries = []

    def execute(self, sql, params):
        self.queries.append((sql, params))

    def fetchall(self):
        return self.messages if 'FROM company_chat_messages' in self.queries[-1][0] else self.members

    def close(self):
        pass


class CompanyChatTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.app.register_blueprint(futurematch_ui.futurematch_bp)
        self.me = {'id': 2, 'user_id': 10, 'name': 'Mig', 'role': 'employee'}
        self.peer = {'id': 3, 'user_id': 11, 'name': 'HR', 'role': 'hr_manager'}
        self.cursor = ChatCursor([self.me, self.peer])
        connection = mock.Mock()
        connection.cursor.return_value = self.cursor
        self.app.mysql = mock.Mock(connection=connection)
        self.connection = connection
        self.live = mock.patch('auth_decorators._ensure_live_or_deny', return_value=None)
        self.live.start()
        self.addCleanup(self.live.stop)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user='mig', user_id=10, company_id=7)

    def test_cannot_message_a_person_outside_the_company(self):
        response = self.client.post('/kollega-chat', json={'recipient_id': 99, 'body': 'Hej'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(any('INSERT INTO company_chat_messages' in sql for sql, _ in self.cursor.queries))

    def test_member_can_message_hr_with_company_scope(self):
        response = self.client.post('/kollega-chat', json={'recipient_id': 3, 'body': '  Hej HR  '})
        self.assertEqual(response.status_code, 201)
        sql, params = self.cursor.queries[-1]
        self.assertIn('INSERT INTO company_chat_messages', sql)
        self.assertEqual(params, (7, 2, 3, 'Hej HR'))
        self.connection.commit.assert_called_once()

    def test_message_read_is_limited_to_this_pair_and_company(self):
        response = self.client.get('/kollega-chat?format=json&recipient_id=3')
        self.assertEqual(response.status_code, 200)
        sql, params = self.cursor.queries[-1]
        self.assertIn('company_id=%s', sql)
        self.assertEqual(params, (7, 2, 3, 3, 2))


if __name__ == '__main__':
    unittest.main()
