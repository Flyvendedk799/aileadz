"""Chat IDs correlate complete captured turns and admin-only diagnostic exports."""
import json
import logging
from unittest import mock

from flask import Response, g, session

from app1 import chat_diagnostics as diagnostics
from app1 import memory_store
from tests.test_ai_store import StoreBase
from tests.secapp import client_as, get_app


def event(kind, **data):
    return 'data: ' + json.dumps({'type': kind, **data}, ensure_ascii=False) + '\n\n'


class CaptureTests(StoreBase):
    def _response(self, source, chat_id='chat-123', query='Mit spørgsmål'):
        g.request_id = 'request-123'
        session['user'] = 'ada'
        return diagnostics.instrument_response(Response(source), chat_id=chat_id, query=query, scope='employee')

    def test_complete_long_answer_and_tool_events_survive_chunk_boundaries(self):
        answer = 'Blåbær og læring. ' * 200
        stream = event('tool_call', name='catalog_search', status='success') + event('chunk', content=answer) + 'data: [DONE]\n\n'
        encoded = stream.encode()
        response = self._response(iter([encoded[i:i+1] for i in range(len(encoded))]))
        output = list(response.response)
        self.assertEqual(response.headers['X-Chat-ID'], 'chat-123')
        self.assertIn('chat-123', output[0])
        logs = memory_store.get_debug_logs_for_session('chat-123')
        self.assertEqual([r['step'] for r in logs], ['chat_turn_start', 'chat_turn_end'])
        end = logs[-1]['data']
        self.assertEqual(end['assistant_text'], answer)
        self.assertEqual(end['outcome'], 'completed')
        self.assertEqual(end['events'][0]['name'], 'catalog_search')
        self.assertEqual(end['turn_id'], logs[0]['data']['turn_id'])
        self.assertEqual(end['request_id'], 'request-123')
        response.close()

    def test_confirmation_tokens_stay_on_wire_but_not_in_debug_export(self):
        source = [event('confirm_card', token='one-time-confirmation', details={'api_key': 'private-key'}), event('done')]
        response = self._response(iter(source))
        self.assertEqual(list(response.response)[1:], source)
        end = memory_store.get_debug_logs_for_session('chat-123')[-1]['data']
        self.assertEqual(end['events'][0]['token'], '[redacted]')
        self.assertEqual(end['events'][0]['details']['api_key'], '[redacted]')
        response.close()

    def test_close_captures_partial_text_and_closes_source(self):
        closed = []
        def source():
            try:
                yield event('chunk', content='Delvist svar')
                yield event('chunk', content=' ikke sendt')
            finally:
                closed.append(True)
        response = self._response(source())
        iterator = iter(response.response)
        next(iterator)  # ID available before the provider starts
        next(iterator)
        response.close()
        end = memory_store.get_debug_logs_for_session('chat-123')[-1]['data']
        self.assertEqual(end['assistant_text'], 'Delvist svar')
        self.assertEqual(end['outcome'], 'interrupted')
        self.assertEqual(closed, [True])

    def test_server_error_is_not_changed_to_success_by_done(self):
        response = self._response(iter([event('error', content='Fejl'), event('done'), 'data: [DONE]\n\n']))
        list(response.response)
        logs = memory_store.get_debug_logs_for_session('chat-123')
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[-1]['data']['outcome'], 'error')
        response.close()

    def test_lost_stream_and_logging_failures_do_not_change_chunks(self):
        source = [event('chunk', content='Svar')]
        with mock.patch.object(diagnostics, '_record', side_effect=None) as record:
            response = self._response(iter(source))
            output = list(response.response)
            self.assertEqual(output[1:], source)
            self.assertEqual(record.call_args.args[2]['outcome'], 'interrupted')
            response.close()
        with mock.patch.object(memory_store, 'log_debug', side_effect=RuntimeError('unavailable')):
            response = self._response(iter(source + [event('done')]))
            self.assertEqual(list(response.response)[1:], source + [event('done')])
            response.close()

    def test_exact_id_export_contains_full_turns_and_only_matching_database_rows(self):
        self.db.raw.executescript('''
            CREATE TABLE conversation_history (id INTEGER PRIMARY KEY, session_id TEXT, username TEXT, mode TEXT, title TEXT, messages TEXT, updated_at TEXT);
            CREATE TABLE ai_agent_runs (id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT, created_at TEXT);
            CREATE TABLE ai_tool_runs (id INTEGER PRIMARY KEY, session_id TEXT, run_id TEXT, created_at TEXT);
        ''')
        for sid in ('chat-123', 'other-chat'):
            self.db.execute('INSERT INTO conversation_history (session_id, username, messages) VALUES (%s,%s,%s)', (sid, 'ada', json.dumps([{'role': 'user', 'content': sid}])))
            for table in ('ai_agent_runs', 'ai_tool_runs'):
                self.db.execute(f'INSERT INTO {table} (session_id,run_id) VALUES (%s,%s)', (sid, 'run-'+sid))
        response = self._response(iter([event('chunk', content='Complete answer'), event('done')]))
        list(response.response)
        data = diagnostics.load_diagnostics('chat-123')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['turns'][0]['user_message'], 'Mit spørgsmål')
        self.assertEqual(data['turns'][0]['assistant_text'], 'Complete answer')
        self.assertEqual(data['conversations'][0]['messages'][0]['content'], 'chat-123')
        for key in ('conversations', 'runs', 'tool_runs'):
            self.assertEqual(len(data[key]), 1)
        self.assertEqual(data['runs'][0]['run_id'], 'run-chat-123')
        response.close()

    def test_missing_sources_and_legacy_capture_are_reported(self):
        data = diagnostics.load_diagnostics('old-chat')
        self.assertEqual(data['turns'], [])
        self.assertTrue(any('predate' in warning for warning in data['warnings']))
        self.assertIn('runs unavailable', data['warnings'])

    def test_failure_details_are_redacted_and_correlated(self):
        g.request_id = 'request-123'
        g.chat_turn_id = 'turn-123'
        diagnostics.record_failure('chat-123', ValueError('contact ada@example.com'))
        error = memory_store.get_debug_logs_for_session('chat-123')[0]['data']
        self.assertEqual(error['error_type'], 'ValueError')
        self.assertEqual(error['turn_id'], 'turn-123')
        self.assertEqual(error['request_id'], 'request-123')
        self.assertNotIn('ada@example.com', error['detail'])
        self.assertIn('[email]', error['detail'])

    def test_log_filter_correlates_the_request_by_chat_id(self):
        import observability
        diagnostics.bind_chat('chat-123')
        rec = logging.LogRecord('test', logging.INFO, __file__, 1, 'turn failed', None, None)
        observability.RequestIdFilter().filter(rec)
        self.assertEqual(json.loads(observability.JsonFormatter().format(rec))['chat_id'], 'chat-123')


def test_diagnostics_are_platform_admin_only():
    app = get_app()
    with mock.patch.object(diagnostics, 'load_diagnostics', return_value={'chat_id': 'chat-123', 'logs': [], 'turns': []}) as load:
        for role in ('anon', 'employee', 'hr_manager', 'company_admin'):
            response = client_as(app, role).get('/app1/adminlog/session/chat-123')
            assert response.status_code in (302, 401, 403)
        load.assert_not_called()
        response = client_as(app, 'admin').get('/app1/adminlog/session/chat-123?download=1')
        assert response.status_code == 200
        assert response.json['chat_id'] == 'chat-123'
        assert response.headers['Cache-Control'] == 'no-store'
        assert 'attachment' in response.headers['Content-Disposition']
        load.assert_called_once_with('chat-123')


def test_invalid_id_does_not_query_diagnostics():
    with mock.patch.object(diagnostics, 'load_diagnostics') as load:
        response = client_as(get_app(), 'admin').get('/app1/adminlog/session/bad%20id')
        assert response.status_code == 400
        load.assert_not_called()
