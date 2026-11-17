"""Behavioral boundaries for cross-surface assistant actions and recovery."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from flask import Blueprint, Flask, Response, g, session

import ai_actions
import ai_action_routes
import ai_operations
import assistant_history
import conversation_tokens
from tests.sqlite_mysql import SqliteMysql


@pytest.fixture
def app():
    app = Flask(__name__)
    app.secret_key = 'cross-surface-test'
    app.mysql = SqliteMysql()
    bp = Blueprint('actions_test', __name__, url_prefix='/app1')
    ai_action_routes.register(bp)
    app.register_blueprint(bp)
    app.add_url_rule('/min-ordre/<order_id>', endpoint='futurematch.my_order', view_func=lambda order_id: order_id)
    return app


def login(company=7, user='hr1'):
    session.update(user=user, company_id=company, company_role='hr_manager')


def proposal(app, name='set_skill_target', scope='hr'):
    with app.test_request_context('/'):
        login()
        return ai_actions.create('chat-1', scope, 'confirmation', name,
                                 {'args':{'skill':'Python'},'card':{'summary_da':'Review Python'}})


def client(app, company=7, user='hr1'):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess.update(user=user, company_id=company, company_role='hr_manager')
    return c


def test_company_switch_cannot_confirm_or_read_previous_proposal(app):
    key = proposal(app)
    c = client(app, company=8)
    with mock.patch('hr_tools.execute_hr_tool') as execute:
        assert c.post('/app1/actions/' + key + '/confirm').status_code == 404
        assert c.get('/app1/actions/' + key).status_code == 404
        assert c.get('/app1/actions').json['actions'] == []
        execute.assert_not_called()


def test_success_receipt_survives_memory_clear_and_replays_without_execution(app):
    key = proposal(app)
    c = client(app)
    with mock.patch('hr_tools.execute_hr_tool', return_value=json.dumps({'success':True,'message':'Saved'})) as execute:
        assert c.post('/app1/actions/' + key + '/confirm').json['success']
        ai_actions._MEMORY.clear()
        repeated = c.post('/app1/actions/' + key + '/confirm').json
        assert repeated['status'] == 'already_confirmed'
        assert repeated['message'] == 'Saved'
        assert execute.call_count == 1
    assert c.get('/app1/actions').json['actions'][0]['status'] == 'consumed'


def test_reject_revokes_durable_proposal(app):
    key = proposal(app)
    c = client(app)
    assert c.post('/app1/actions/' + key + '/reject').json['status'] == 'rejected'
    with mock.patch('hr_tools.execute_hr_tool') as execute:
        assert c.post('/app1/actions/' + key + '/confirm').json['status'] == 'rejected'
        execute.assert_not_called()


def test_exception_is_uncertain_and_never_blindly_replayed(app):
    key = proposal(app)
    c = client(app)
    with mock.patch('hr_tools.execute_hr_tool', side_effect=RuntimeError('after commit')) as execute:
        assert c.post('/app1/actions/' + key + '/confirm').json['status'] == 'uncertain'
        assert c.post('/app1/actions/' + key + '/confirm').json['status'] == 'uncertain'
        assert execute.call_count == 1


def test_partial_receipt_stays_partial_on_retry(app):
    key = proposal(app)
    c = client(app)
    with mock.patch('hr_tools.execute_hr_tool', return_value={'success':True,'partial_failure':True,'queued':1}):
        assert c.post('/app1/actions/' + key + '/confirm').json['status'] == 'partial_success'
        assert c.post('/app1/actions/' + key + '/confirm').json['status'] == 'partial_success'


def test_claim_is_compare_and_set(app):
    key = proposal(app)
    with app.test_request_context('/'):
        login()
        assert ai_actions.claim(key)
        assert ai_actions.claim(key) is None


def test_model_cannot_confirm_its_own_proposal(app):
    from tool_confirm import enforce_confirmation
    key = proposal(app)
    with app.test_request_context('/'):
        login()
        assert not enforce_confirmation('set_skill_target', {'confirm':True})['confirm']
        ai_actions.claim(key)
        marker = ai_actions.ACTIVE_OPERATION.set(key)
        try:
            assert enforce_confirmation('set_skill_target', {'confirm':True})['confirm']
            assert not enforce_confirmation('send_company_email', {'confirm':True})['confirm']
        finally:
            ai_actions.ACTIVE_OPERATION.reset(marker)


def test_budget_consent_is_user_input_not_stored_model_argument(app):
    key = proposal(app, name='assign_learning_path_to_team')
    c = client(app)
    with mock.patch('hr_tools.execute_hr_tool', return_value={'success':True}) as execute:
        c.post('/app1/actions/' + key + '/confirm', json={'accept_over_budget':True})
        assert execute.call_args.args[0].arguments['accept_over_budget'] is True


@pytest.mark.parametrize('total,consent,blocked', [(None,True,False),(99,True,False),(float('nan'),True,False),(100,False,False),(100,True,True)])
def test_assignment_review_rejects_stale_missing_or_unconsented_cost(total,consent,blocked):
    from learning_path_service import validate_assignment_review
    with pytest.raises(ValueError):
        validate_assignment_review({'total_now':100,'over_budget':True,'blocked':blocked}, total,consent)


def test_assignment_review_accepts_current_explicit_consent():
    from learning_path_service import validate_assignment_review
    validate_assignment_review({'total_now':100,'over_budget':True,'blocked':False},100,True)


def test_token_is_tab_specific_and_actor_company_bound(app):
    with app.test_request_context('/'):
        login()
        first = conversation_tokens.mint('first-tab')
        second = conversation_tokens.mint('second-tab')
        session['session_id'] = 'cookie-from-second-tab'
        assert conversation_tokens.read(first) == 'first-tab'
        assert conversation_tokens.read(second) == 'second-tab'
        assert conversation_tokens.read(first,'hr') is None
        session['company_id'] = 8
        assert conversation_tokens.read(first) is None
        session['company_id'] = 7
        session['user'] = 'another'
        assert conversation_tokens.read(first) is None


def test_append_only_history_keeps_both_stale_workers_turns(app):
    for query in ('first worker', 'second worker'):
        with app.test_request_context('/'):
            login()
            assistant_history.append('same-chat','hr',query,'answer to ' + query)
    with app.test_request_context('/'):
        login()
        assert [m['content'] for m in assistant_history.load('same-chat','hr') if m['role']=='user'] == ['first worker','second worker']
        session['company_id'] = 8
        assert assistant_history.load('same-chat','hr') == []


def test_transcript_retry_with_same_operation_does_not_append_twice(app):
    with app.test_request_context('/'):
        login()
        marker = ai_actions.ACTIVE_OPERATION.set('run_stable')
        try:
            assistant_history.append('chat','hr','hello','answer')
            assistant_history.append('chat','hr','hello','answer')
        finally:
            ai_actions.ACTIVE_OPERATION.reset(marker)
        assert len(assistant_history.load('chat','hr')) == 2


def test_operation_replays_completed_stream_without_second_factory(app):
    with app.test_request_context('/'):
        login()
        factory = mock.Mock(side_effect=lambda: Response(['data: {"type":"text","content":"Hello"}\n\n','data: {"type":"done"}\n\n'],mimetype='text/event-stream'))
        first = ai_operations.run(factory,scope='hr',sid='chat',request_id='request-1',payload={'query':'hi'})
        content = first.get_data()
        second = ai_operations.run(factory,scope='hr',sid='chat',request_id='request-1',payload={'query':'hi'})
        assert second.get_data() == content
        assert factory.call_count == 1
        mismatch = ai_operations.run(factory,scope='hr',sid='chat',request_id='request-1',payload={'query':'different'})
        assert mismatch[1] == 409


def test_cancelled_operation_stops_later_tool_checkpoints(app):
    with app.test_request_context('/'):
        login()
        key = ai_actions.create('chat','hr','turn','turn')
        ai_actions.claim(key)
        marker = ai_actions.ACTIVE_OPERATION.set(key)
        try:
            ai_actions.cancellation_checkpoint()
            ai_actions.transition(key,('running',),'cancelled')
            with pytest.raises(RuntimeError):
                ai_actions.cancellation_checkpoint()
        finally:
            ai_actions.ACTIVE_OPERATION.reset(marker)


def test_artifact_is_owned_downloadable_and_survives_reload(app):
    with app.test_request_context('/'):
        login()
        event = ai_actions.artifact('chat','hr',{'filename':'invite.ics','content':'BEGIN:VCALENDAR\r\nEND:VCALENDAR'})
    c = client(app)
    response = c.get(event['target'])
    assert response.status_code == 200
    assert b'BEGIN:VCALENDAR' in response.data
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert client(app,company=8).get(event['target']).status_code == 404


def test_search_policy_is_request_local_and_propagates_without_sharing_flask_g(app):
    from app1.tools import set_search_context, search_context
    from ai_runtime import _in_request_context
    with app.test_request_context('/'):
        login()
        g.owner = 'parent'
        set_search_context(blocked_vendors=['Private vendor'],supplier_agreements={'v':{'discount':20}})
        def child():
            assert not hasattr(g,'owner')
            g.owner = 'child'
            return search_context()['blocked_vendors']
        work = _in_request_context(child)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(work).result() == frozenset(['Private vendor'])
        assert g.owner == 'parent'
    with app.test_request_context('/'):
        login(company=8)
        assert search_context() == {}


def test_widget_supplier_notes_use_embedding_company_not_signed_in_company(app):
    from app1.tools import set_search_context, _supplier_state_for_vendor
    cur = app.mysql.connection.cursor()
    cur.execute("INSERT INTO company_supplier_preferences(company_id,vendor_name,notes) VALUES(%s,%s,%s)",
                (7, 'Vendor', 'Private employee company note'))
    cur.execute("INSERT INTO company_supplier_preferences(company_id,vendor_name,notes) VALUES(%s,%s,%s)",
                (8, 'Vendor', 'Embedding company note'))
    app.mysql.connection.commit()
    cur.close()
    with app.test_request_context('/'):
        login(company=7)
        set_search_context(company_id=8)
        assert _supplier_state_for_vendor('Vendor')['notes'] == 'Embedding company note'
        set_search_context(company_id=None)
        assert _supplier_state_for_vendor('Vendor')['known'] is False


def test_department_head_cannot_execute_company_write_or_widen_read(app):
    import ai_permissions
    from auth_decorators import can
    with app.test_request_context('/'):
        session.update(user='head',user_id=10,company_id=7,company_role='department_head')
        member = {'role':'department_head','department':'Sales','permissions':{}}
        with mock.patch('auth_decorators._member_for_session', return_value=member):
            assert not can('hr.manage')
            assert ai_permissions.hr_arguments('set_skill_target',{'confirm':True})[1]['error'] == 'not_authorized'
            args,error = ai_permissions.hr_arguments('get_employee_overview',{'department':'Engineering'})
            assert error is None and args['department'] == 'Sales'


def test_calendar_uses_booking_times_reference_and_join_link(app):
    from order_calendar import build
    with app.test_request_context('/'):
        event,ics = build({'order_id':'o1','product_title':'Python','variant_date':'01/01/2020'},
                          {'start_at':'2026-12-01T09:00:00','end_at':'2026-12-01T11:00:00','join_url':'https://example.org/meeting','reference':'REF42','instructions':'Arrive early'})
        assert event['start'] == '2026-12-01T09:00:00'
        assert 'REF42' in ics and 'https://example.org/meeting' in ics
        assert '20261201' in ics and 'DTEND' in ics


def test_booking_change_uses_authoritative_owner_check_and_shared_service(app):
    import ai_booking_tools
    row = {'order_id':'o1','company_id':7,'product_handle':'python','product_title':'Python'}
    with app.test_request_context('/'):
        login()
        with mock.patch('order_service.get_order',return_value=row), mock.patch('order_service.actors_for',return_value={'owner'}), \
                mock.patch('enrollment_service.quote_course',return_value={'session_id':'s2','price':100}), \
                mock.patch('order_fulfillment.request_change',return_value={'success':True,'pending':True}) as change:
            preview = json.loads(ai_booking_tools.change({'order_id':'o1','action':'reschedule','session_id':'s2'}))
            assert preview['needs_confirmation']
            change.assert_not_called()
            result = json.loads(ai_booking_tools.change({'order_id':'o1','action':'reschedule','session_id':'s2','confirm':True,'expected_price':100}))
            assert result['pending']
            assert change.call_args.args[1:3] == ('o1','reschedule')
        with mock.patch('order_service.get_order',return_value=row), mock.patch('order_service.actors_for',return_value={'manager'}), mock.patch('order_service.cancel_order') as cancel:
            assert json.loads(ai_booking_tools.change({'order_id':'o1','confirm':True}))['status'] == 'error'
            cancel.assert_not_called()


def test_vendor_booking_references_do_not_include_buyers_or_other_vendors(app):
    import vendor_tools
    app.mysql.execute("INSERT INTO course_orders(order_id,vendor_id,product_title,status) VALUES('own',3,'Python','booked')")
    app.mysql.execute("INSERT INTO course_orders(order_id,vendor_id,product_title,status) VALUES('other',4,'Secret','booked')")
    app.add_url_rule('/vendor/orders/<order_id>/booking',endpoint='vendor.vendor_booking',view_func=lambda order_id:order_id)
    with app.test_request_context('/'):
        session.update(user_type='vendor',vendor_id=3,vendor_name='Vendor')
        with mock.patch('vendor_tools._dict_cursor',return_value=app.mysql.connection.cursor()):
            result = vendor_tools.vendor_booking_work({},'Vendor')
        assert [r['order_id'] for r in result['orders']] == ['own']
        assert set(result['orders'][0]) == {'order_id','product_title','status','pending_change','explanation','url'}


def test_deleted_conversation_token_and_late_digest_are_invalid(app):
    from conversation_privacy import mark_deleted, is_deleted
    from app1 import conversation_state
    with app.test_request_context('/'):
        login()
        token = conversation_tokens.mint('deleted-chat')
        mark_deleted('hr1','deleted-chat')
        assert is_deleted('hr1','deleted-chat')
        assert conversation_tokens.read(token) is None
        with mock.patch.object(conversation_state,'_conn') as conn:
            result = conversation_state.save_turn('hr1','deleted-chat','chat',[{'role':'user','content':'late'}])
            assert result['rev'] is None
            conn.assert_not_called()


def test_support_reference_extraction_keeps_nested_order_and_run_keys():
    from ai_support_trace import references
    assert references({'run_ids':['r'],'run_id':'r','results':[{'order_id':'o'},{'order_id':'o','approval_id':12}]}) == {'run_ids':['r'],'order_ids':['o'],'approval_ids':['12']}


def test_legacy_hr_history_is_adopted_only_from_verified_tenant_evidence(app):
    app.mysql.raw.executescript('CREATE TABLE hr_chatbot_interactions(username TEXT,session_id TEXT,company_id INTEGER);')
    app.mysql.execute("INSERT INTO hr_chatbot_interactions VALUES('hr1','old-chat',7)")
    with app.test_request_context('/'):
        login(company=8)
        assert not assistant_history.binding('old-chat','hr')
        login(company=7)
        assert assistant_history.binding('old-chat','hr')
        app.mysql.execute("INSERT INTO hr_chatbot_interactions VALUES('hr1','ambiguous',7)")
        app.mysql.execute("INSERT INTO hr_chatbot_interactions VALUES('hr1','ambiguous',8)")
        assert not assistant_history.binding('ambiguous','hr')


def test_anonymous_widget_usage_charges_the_embedding_company(app):
    import credit_service
    from tests.test_credit_service import EXTRA
    app.mysql.raw.executescript(EXTRA)
    with app.test_request_context('/'):
        charged = credit_service.charge_from_usage(app.mysql,username=None,company_id=7,
            agent_scope='widget',model='test-model',usage={'input_tokens':100,'output_tokens':10})
    assert charged > 0
    row = app.mysql.one('SELECT username,company_id,assistant,credits_used FROM credit_usage')
    assert row['username'] is None and row['company_id'] == 7 and row['assistant'] == 'widget'
    assert row['credits_used'] == charged


def test_employee_accepts_long_message_without_silent_truncation(app):
    import app1
    query = 'x' * 3500
    with app.test_request_context('/app1/ask',method='POST',json={'query':query}):
        login()
        with mock.patch('credit_service.guard',return_value=None), mock.patch.object(app1,'handle_agentic_ask',return_value='stream') as agent:
            assert app1.ask() == 'stream'
            assert agent.call_args.args[0] == query


def test_delete_removes_only_conversation_recall_and_related_actions(app):
    from app1.user_profile_db import delete_conversation
    app.mysql.raw.executescript('''
        CREATE TABLE conversation_history(id INTEGER PRIMARY KEY,username TEXT,session_id TEXT,mode TEXT,messages TEXT);
        CREATE TABLE user_knowledge(username TEXT,source_type TEXT,source_id TEXT,content TEXT);
        CREATE TABLE user_conversation_summaries(username TEXT,mode TEXT,summary TEXT);
        CREATE TABLE user_conversations(username TEXT,summary TEXT);
        CREATE TABLE user_active_sessions(username TEXT,session_id TEXT);
    ''')
    from app1.user_knowledge import _source_id
    app.mysql.execute("INSERT INTO conversation_history VALUES(1,'hr1','delete-me','chat','[]')")
    app.mysql.execute("INSERT INTO user_knowledge VALUES('hr1','conversation',%s,'old recall')", (_source_id('','delete-me'),))
    app.mysql.execute("INSERT INTO user_knowledge VALUES('hr1','memory','explicit','keep this')")
    app.mysql.execute("INSERT INTO user_conversation_summaries VALUES('hr1','chat','mixed digest')")
    app.mysql.execute("INSERT INTO user_conversations VALUES('hr1','old rolling digest')")
    app.mysql.execute("INSERT INTO user_active_sessions VALUES('hr1','delete-me')")
    with app.test_request_context('/'):
        login()
        key = ai_actions.create('delete-me','employee','confirmation','manage_my_order')
        with mock.patch('app1.memory_store.erase_subject') as erase:
            assert delete_conversation('hr1',1)
            erase.assert_called_once_with(session_id='delete-me')
        assert ai_actions.get(key) is None
        assert not delete_conversation('hr1',1)
    assert app.mysql.one("SELECT content FROM user_knowledge WHERE source_type='memory'")['content'] == 'keep this'
    assert app.mysql.one("SELECT content FROM user_knowledge WHERE source_type='conversation'") is None
    assert app.mysql.one('SELECT * FROM user_conversation_summaries') is None
    assert app.mysql.one('SELECT summary FROM user_conversations')['summary'] is None


def test_inbox_keeps_pending_actions_when_many_internal_events_are_newer(app):
    with app.test_request_context('/'):
        login()
        key = ai_actions.create('chat','hr','confirmation','review')
        for i in range(55):
            ai_actions.create('chat','hr','trace','tool ' + str(i))
        assert [r['id'] for r in ai_actions.list_actions()] == [key]


def test_claim_extends_execution_deadline_past_proposal_expiry(app):
    with app.test_request_context('/'):
        login()
        with mock.patch('ai_actions.time.time',return_value=100):
            key = ai_actions.create('chat','hr','confirmation','review',ttl=600)
        with mock.patch('ai_actions.time.time',return_value=699):
            assert ai_actions.claim(key)
        with mock.patch('ai_actions.time.time',return_value=701):
            assert ai_actions.inspect(key)['status'] == 'running'
            ai_actions.finish(key,{'success':True})
            assert ai_actions.inspect(key)['status'] == 'consumed'


def test_hr_receipt_is_hidden_after_hr_access_is_revoked(app):
    key = proposal(app)
    c = client(app)
    with c.session_transaction() as sess:
        sess['company_role'] = 'employee'
    assert c.get('/app1/actions/' + key).status_code == 404
    assert c.get('/app1/actions').json['actions'] == []
    with mock.patch('hr_tools.execute_hr_tool') as execute:
        assert c.post('/app1/actions/' + key + '/confirm').status_code == 404
        execute.assert_not_called()


def test_reschedule_rejects_price_change_before_requesting_supplier_change(app):
    from ai_booking_tools import change
    row = {'order_id':'o1','company_id':7,'product_handle':'python','product_title':'Python'}
    with app.test_request_context('/'):
        login()
        with mock.patch('order_service.get_order',return_value=row), mock.patch('order_service.actors_for',return_value={'owner'}), \
                mock.patch('enrollment_service.quote_course',return_value={'session_id':'s2','price':200}), \
                mock.patch('order_fulfillment.request_change') as mutate:
            result = json.loads(change({'order_id':'o1','action':'reschedule','session_id':'s2','expected_price':100,'confirm':True}))
            assert result['status'] == 'error'
            mutate.assert_not_called()


def test_vendor_catalog_proposal_is_scoped_and_includes_reviewed_revision(app):
    import vendor_tools
    product = {'handle':'python','vendor':'Own vendor','title':'Old title','summary':'Old summary'}
    with app.test_request_context('/'):
        session.update(user_type='vendor',vendor_id=3,vendor_name='Own vendor')
        with mock.patch('catalog_service.get_product_any',return_value=product), mock.patch('catalog_service.product_revision',return_value='revision-1'), \
                mock.patch('catalog_service.save_import_draft') as save:
            result = vendor_tools.vendor_propose_catalog_correction({'handle':'python','title':'New title'},'Own vendor')
            assert result['needs_confirmation']
            assert result['confirmation_args']['revision'] == 'revision-1'
            assert result['details']['before']['title'] == 'Old title'
            assert result['details']['after']['title'] == 'New title'
            assert 'error' in vendor_tools.vendor_propose_catalog_correction({'handle':'python'},'Other vendor')
            save.assert_not_called()


def test_department_approval_id_is_resolved_within_department(app):
    import ai_permissions
    app.mysql.execute("INSERT INTO course_orders(order_id,company_id,department,status) VALUES('own',7,'Sales','pending_approval')")
    app.mysql.execute("INSERT INTO course_orders(order_id,company_id,department,status) VALUES('foreign',7,'Engineering','pending_approval')")
    app.mysql.execute("INSERT INTO order_approvals(id,order_id,company_id) VALUES(1,'own',7)")
    app.mysql.execute("INSERT INTO order_approvals(id,order_id,company_id) VALUES(2,'foreign',7)")
    with app.test_request_context('/'):
        session.update(user='head',user_id=10,company_id=7,company_role='department_head')
        with mock.patch('auth_decorators._member_for_session',return_value={'role':'department_head','department':'Sales','permissions':{}}):
            assert ai_permissions.hr_arguments('approve_order_from_chat',{'approval_id':1})[1] is None
            assert ai_permissions.hr_arguments('approve_order_from_chat',{'approval_id':2})[1]['error'] == 'not_authorized'
