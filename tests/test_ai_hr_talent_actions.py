"""T3-2: the HR assistant prepares talent work behind a confirm card.

Two writes, both through ``talent_service`` and both drafts: a role framework
(``hr_draft_role_framework``, never published by the assistant) and a capability
scenario (``hr_run_capability_scenario``, k-anonymous counts; the company's skill
targets change only when HR publishes it on the page). Covered: registration on
both providers, the confirm gate (a model-supplied confirm=true previews only),
permissions (the page's capability, managers only) and tenancy (company_id).
SQLite, no network, no API key, no MySQL.
"""
import datetime
import json
import re
from types import SimpleNamespace
from unittest import mock

import pytest

import ai_actions
import ai_tool_registry as registry
import hr_tools
import talent_service as svc
from schema_registry import TALENT_DDL
from tests.sqlite_platform import PlatformDB, make_app

NAMES = ("hr_draft_role_framework", "hr_run_capability_scenario")
FUTURE = str(datetime.date.today() + datetime.timedelta(days=200))


@pytest.fixture
def env(monkeypatch):
    db = PlatformDB()
    for ddl in TALENT_DDL:
        ddl = re.sub(r'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', '', ddl)
        ddl = re.sub(r'UNIQUE KEY \w+\s*\(', 'UNIQUE (', ddl)
        ddl = re.sub(r',\s*INDEX \w+\s*\([^)]*\)', '', ddl)
        db.raw.execute(ddl)
    db.raw.executescript('''
        CREATE TABLE IF NOT EXISTS user_skills (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT,skill_name TEXT,
            skill_level TEXT,numeric_level INTEGER,category TEXT,source TEXT,UNIQUE(username,skill_name));
        CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, user_id INTEGER,
            action TEXT, action_type TEXT, resource_type TEXT, resource_id TEXT, description TEXT);
        CREATE TABLE IF NOT EXISTS company_skill_targets (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER,
            department TEXT, skill_name TEXT, target_level INTEGER, priority TEXT);
    ''')
    people = [(1, 'ada', 7, 'Salg', 'employee'), (2, 'bo', 7, 'Salg', 'employee'), (3, 'hr', 7, 'Salg', 'hr_manager'),
              (4, 'dea', 7, 'Udvikling', 'employee'), (5, 'eve', 8, 'Salg', 'hr_manager')]
    for user_id, username, cid, dept, role in people:
        db.execute('INSERT INTO users (id,username) VALUES (%s,%s)', (user_id, username))
        db.execute("INSERT INTO company_users (company_id,user_id,username,department,role) VALUES (%s,%s,%s,%s,%s)",
                   (cid, user_id, username, dept, role))
    db.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
    db.execute("INSERT INTO companies (id,company_name) VALUES (8,'Andet firma')")
    db.execute('INSERT INTO talent_settings VALUES (7,1,1,1)')
    db.execute('INSERT INTO talent_settings VALUES (8,1,1,1)')
    cur = db.connection.cursor()
    svc.save_role(cur, 8, 'Teamleder', 'Ledelse | 5')  # another company's role of the same name
    db.connection.commit()
    monkeypatch.setattr(ai_actions, '_db', lambda: None)
    app = make_app(db)
    with mock.patch('security_audit.audit', return_value=True):
        yield db, app


def as_user(app, user_id=3, username='hr', company_id=7, role='hr_manager'):
    ctx = app.test_request_context()
    ctx.push()
    from flask import session
    session.update(user=username, user_id=user_id, company_id=company_id, company_role=role)
    return ctx


def call(name, **args):
    return json.loads(hr_tools.execute_hr_tool(SimpleNamespace(name=name, arguments=dict(args))))


def confirmed(name, **args):
    """What the confirm route does after the user clicks Bekræft: claim the held
    proposal and replay it with the operation active."""
    key = ai_actions.create('chat-1', 'hr', 'confirmation', name, {'args': args})
    # Claimed internally so a refused actor still reaches the tool's own guard
    # (the confirm route would already have refused to claim it for them).
    assert ai_actions.transition(key, ('pending',), 'running', internal=True)
    marker = ai_actions.ACTIVE_OPERATION.set(key)
    try:
        return call(name, **dict(args, confirm=True))
    finally:
        ai_actions.ACTIVE_OPERATION.reset(marker)


ROLE = dict(title='Teamleder', requirements=[{'skill': 'Ledelse', 'target_level': 4}, {'skill': 'Forhandling', 'level': 3}])
SCENARIO = dict(title='Salg 2027', target_date=FUTURE, department='Salg',
                requirements=[{'skill': 'Python', 'target_level': 3, 'required_people': 2}])


# ── registration ─────────────────────────────────────────────────────────────

def test_both_tools_are_registered_confirm_gated_hr_writes_on_both_providers():
    schemas = {t['function']['name']: t for t in hr_tools.HR_TOOLS}
    for name in NAMES:
        meta = registry.get_tool_meta(name)
        assert meta.agent_scope == 'hr' and meta.confirm_required and meta.manager_only
        assert meta.side_effect and not meta.parallel_safe and meta.company_required
        claude = registry.to_anthropic_tool(schemas[name])
        assert claude['name'] == name and claude['input_schema']['properties']['requirements']['type'] == 'array'
        assert 'confirm' in schemas[name]['function']['parameters']['properties']


def test_selector_offers_the_writes_on_intent_and_never_from_page_or_reads():
    def names(query, company_id=7, page=None):
        tools, _ = registry.get_hr_tool_selection(company_id=company_id, user_query=query, page=page)
        return {t['function']['name'] for t in tools}
    assert 'hr_draft_role_framework' in names('Lav en rolleramme for en teamleder i Salg')
    assert 'hr_run_capability_scenario' in names('Har vi folkene til fem Python-udviklere inden 2027? Kør et scenarie.')
    read = names('hvilke rollerammer og scenarier har vi?', page='talent')
    assert 'hr_talent_overview' in read and not read & set(NAMES)
    assert not names('Lav en rolleramme for en teamleder', company_id=None) & set(NAMES)


# ── confirm gate ─────────────────────────────────────────────────────────────

def test_role_draft_previews_until_confirmed_and_is_never_published(env):
    db, app = env
    ctx = as_user(app)
    try:
        preview = call('hr_draft_role_framework', **ROLE, confirm=True)  # the model cannot confirm itself
        assert preview['needs_confirmation'] and preview['action'] == 'hr_draft_role_framework'
        assert 'kladde' in preview['message_da'] and preview['details']['version'] == 1
        assert db.query('SELECT * FROM talent_role_versions WHERE company_id=7') == []
        done = confirmed('hr_draft_role_framework', **ROLE)
    finally:
        ctx.pop()
    assert done['success'] and done['published'] is False and done['page'] == '/hr/talent'
    row = db.one('SELECT * FROM talent_role_versions WHERE company_id=7')
    assert row['published'] == 0 and row['title'] == 'Teamleder'
    assert {r['skill']: r['target_level'] for r in json.loads(row['requirements_json'])} == {'Ledelse': 4, 'Forhandling': 3}
    assert db.one("SELECT action,company_id FROM audit_log WHERE action='hr_draft_role_framework'") == \
        {'action': 'hr_draft_role_framework', 'company_id': 7}


def test_role_draft_based_on_an_existing_role_adds_a_version_in_the_own_company_only(env):
    db, app = env
    cur = db.connection.cursor()
    svc.save_role(cur, 7, 'Teamleder', 'Ledelse | 3')
    db.connection.commit()
    ctx = as_user(app)
    try:
        preview = call('hr_draft_role_framework', **dict(ROLE, based_on='teamleder'))
        assert preview['details']['version'] == 2
        done = confirmed('hr_draft_role_framework', **dict(ROLE, based_on='teamleder'))
        missing = call('hr_draft_role_framework', **dict(ROLE, based_on='Astronaut'))
    finally:
        ctx.pop()
    assert done['version'] == 2
    own = db.query('SELECT role_id,version,published FROM talent_role_versions WHERE company_id=7 ORDER BY version')
    assert [r['version'] for r in own] == [1, 2] and own[0]['role_id'] == own[1]['role_id']
    assert all(r['published'] == 0 for r in own)
    assert [r['version'] for r in db.query('SELECT version FROM talent_role_versions WHERE company_id=8')] == [1]
    assert missing['error'] == 'role_not_found' and missing['roles'] == ['Teamleder']


def test_scenario_preview_holds_kanonymous_coverage_and_saves_a_draft_only_on_confirm(env):
    db, app = env
    for username in ('ada', 'bo'):
        db.execute("INSERT INTO user_skills (username,skill_name,skill_level,numeric_level,source) VALUES (%s,'Python','mellem',3,'manual')",
                   (username,))
    ctx = as_user(app)
    try:
        with mock.patch('kanon._resolve_k_default', return_value=1):
            preview = call('hr_run_capability_scenario', **SCENARIO, confirm=True)
        assert preview['needs_confirmation'] and preview['details']['department'] == 'Salg'
        cell = preview['coverage'][0]
        assert cell['skill'] == 'Python' and cell['hidden_small_group'] is False
        assert cell['assessed_at_level'] + cell['self_assessed_only'] == 2  # ada and bo, not dea in Udvikling
        assert 'ada' not in json.dumps(preview) and 'user_id' not in json.dumps(preview)
        assert db.query('SELECT * FROM talent_scenarios') == []
        # Under the default privacy floor three people are too few to show.
        small = call('hr_run_capability_scenario', **SCENARIO)
        assert small['coverage'][0]['hidden_small_group'] is True and small['coverage'][0]['assessed_at_level'] is None
        done = confirmed('hr_run_capability_scenario', **SCENARIO)
    finally:
        ctx.pop()
    assert done['success'] and done['status'] == 'draft'
    row = db.one('SELECT * FROM talent_scenarios')
    assert (row['company_id'], row['status'], row['department']) == (7, 'draft', 'Salg')
    assert db.query("SELECT * FROM company_skill_targets") == []


def test_scenario_input_is_checked_before_anything_is_proposed(env):
    db, app = env
    ctx = as_user(app)
    try:
        past = call('hr_run_capability_scenario', **dict(SCENARIO, target_date='2020-01-01'))
        unknown = call('hr_run_capability_scenario', **dict(SCENARIO, department='Marketing'))
        bad = call('hr_run_capability_scenario', **dict(SCENARIO, requirements=[{'skill': 'Python', 'target_level': 9}]))
    finally:
        ctx.pop()
    assert 'fremtidig' in past['error'] and unknown['error'] == 'department_not_found'
    assert unknown['departments'] == ['Salg', 'Udvikling'] and 'niveau 1–5' in bad['error']


# ── permissions ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('role', ['employee', 'department_head'])
def test_roles_that_cannot_use_the_talent_page_are_refused(env, role):
    db, app = env
    ctx = as_user(app, user_id=1, username='ada', role=role)
    try:
        for name, args in (('hr_draft_role_framework', ROLE), ('hr_run_capability_scenario', SCENARIO)):
            assert call(name, **args)['error'] == 'not_authorized'
            assert confirmed(name, **args)['error'] == 'not_authorized'
    finally:
        ctx.pop()
    assert db.query('SELECT * FROM talent_role_versions WHERE company_id=7') == []
    assert db.query('SELECT * FROM talent_scenarios') == []


def test_a_switched_off_module_is_said_plainly_and_nothing_is_written(env):
    db, app = env
    db.execute('UPDATE talent_settings SET planning_enabled=0 WHERE company_id=7')
    ctx = as_user(app)
    try:
        out = confirmed('hr_run_capability_scenario', **SCENARIO)
    finally:
        ctx.pop()
    assert out['error'] == 'module_off' and out['page'] == '/hr/talent'
    assert db.query('SELECT * FROM talent_scenarios') == []


# ── tenancy ──────────────────────────────────────────────────────────────────

def test_an_hr_manager_not_active_in_the_session_company_cannot_write_there(env):
    db, app = env
    ctx = as_user(app, user_id=5, username='eve', company_id=7)  # eve belongs to company 8
    try:
        out = confirmed('hr_draft_role_framework', **ROLE)
    finally:
        ctx.pop()
    assert out == {'error': 'not_authorized', 'message': 'Du er ikke aktiv i virksomheden.'}
    assert db.query('SELECT * FROM talent_role_versions WHERE company_id=7') == []


def test_the_scenario_counts_only_the_session_companys_people(env):
    db, app = env
    db.execute("INSERT INTO user_skills (username,skill_name,skill_level,numeric_level,source) VALUES ('eve','Python','ekspert',5,'manual')")
    ctx = as_user(app)
    try:
        with mock.patch('kanon._resolve_k_default', return_value=1):
            out = call('hr_run_capability_scenario', **SCENARIO)
    finally:
        ctx.pop()
    cell = out['coverage'][0]
    assert cell['assessed_at_level'] + cell['self_assessed_only'] == 0  # eve (company 8, Salg) is not counted
