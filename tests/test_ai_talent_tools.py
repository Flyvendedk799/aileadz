"""Assistant tools over the goal model and the talent workspace: the learner sees
their own development and decides goal visibility behind a confirm card; the HR
assistant sees only what the goal model lets HR see. SQLite, no network."""
import json
import re
from unittest import mock

import pytest

import ai_tool_registry as registry
import talent_service as svc
from schema_registry import TALENT_DDL
from tests.sqlite_platform import PlatformDB, make_app


@pytest.fixture
def env():
    db = PlatformDB()
    for ddl in TALENT_DDL:
        ddl = re.sub(r'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', '', ddl)
        ddl = re.sub(r'UNIQUE KEY \w+\s*\(', 'UNIQUE (', ddl)
        ddl = re.sub(r',\s*INDEX \w+\s*\([^)]*\)', '', ddl)
        db.raw.execute(ddl)
    db.raw.executescript('''
        CREATE TABLE user_skills (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT,skill_name TEXT,skill_level TEXT,numeric_level INTEGER,category TEXT,source TEXT,UNIQUE(username,skill_name));
        CREATE TABLE user_learning_goals (id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT,title TEXT,description TEXT,target_date TEXT,
            status TEXT DEFAULT 'aktiv',shared_with_hr INTEGER DEFAULT 0,shared_company_id INTEGER,shared_at TEXT,created_at TEXT,updated_at TEXT);
    ''')
    for user_id, username, cid, role in ((1, 'ada', 7, 'employee'), (3, 'hr', 7, 'hr_manager')):
        db.execute('INSERT INTO users (id,username) VALUES (%s,%s)', (user_id, username))
        db.execute("INSERT INTO company_users (company_id,user_id,username,department,role) VALUES (%s,%s,%s,'Salg',%s)",
                   (cid, user_id, username, role))
    db.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
    db.execute('INSERT INTO talent_settings VALUES (7,1,1,1)')
    db.execute("INSERT INTO user_learning_goals (id,username,title) VALUES (1,'ada','Skifte branche')")
    db.execute("INSERT INTO user_learning_goals (id,username,title) VALUES (2,'ada','Blive teamleder')")
    db.execute("INSERT INTO employee_goals (id,employee_id,company_id,goal_title,shared_with_employee) VALUES (10,1,7,'Fasthold Ada',0)")
    db.execute("INSERT INTO employee_goals (id,employee_id,company_id,goal_title,shared_with_employee) VALUES (11,1,7,'Tag PRINCE2',1)")
    db.execute("INSERT INTO user_skills (username,skill_name,skill_level,numeric_level,source) VALUES ('ada','Python','mellem',2,'manual')")
    app = make_app(db)

    def full(username):
        skills = db.query('SELECT * FROM user_skills WHERE username=%s', (username,))
        return dict(username=username, target_role='Privat ambition',
                    skills=[dict(name=r['skill_name'], level=r['skill_level'], numeric_level=r['numeric_level'], source=r['source']) for r in skills],
                    learning_goals=[])
    with mock.patch('app1.user_profile_db.get_full_profile', side_effect=full), \
            mock.patch('app1.user_profile_db.ensure_tables'), \
            mock.patch('security_audit.audit', return_value=True), \
            mock.patch('goal_sharing.company_shares_with_ai', return_value=True):
        yield db, app


def as_ada(app):
    ctx = app.test_request_context()
    ctx.push()
    from flask import session
    session.update(user='ada', user_id=1, company_id=7, company_role='employee')
    return ctx


def role_and_opportunity(db):
    import datetime
    cur = db.connection.cursor()
    rid = svc.save_role(cur, 7, 'Teamleder', 'Python | 4\nLedelse | 3')
    cur.execute('UPDATE talent_role_versions SET published=1 WHERE id=%s', (rid,))
    oid = svc.save_opportunity(cur, 7, 3, dict(role_version_id=rid, kind='role', title='Teamleder i Salg',
                                              deadline=str(datetime.date.today() + datetime.timedelta(days=30))))
    cur.execute("UPDATE talent_opportunities SET status='published' WHERE id=%s", (oid,))
    db.connection.commit()
    return rid, oid


def test_learner_sees_readiness_for_an_explored_role_and_nothing_is_stored(env):
    from app1 import tools
    db, app = env
    role_and_opportunity(db)
    before = db.query('SELECT COUNT(*) AS n FROM talent_assessments')[0]['n']
    ctx = as_ada(app)
    try:
        out = json.loads(tools._execute_get_my_development({'explore_role': 'teamleder'}, 'ada'))
    finally:
        ctx.pop()
    assert out['status'] == 'development' and out['roles'] == ['Teamleder']
    reqs = {r['skill']: r for r in out['explored_role']['readiness']['requirements']}
    assert reqs['Python']['level'] == 2 and reqs['Python']['target'] == 4 and reqs['Python']['status'] == 'below_target'
    assert reqs['Ledelse']['status'] == 'unknown'
    assert [o['title'] for o in out['opportunities']] == ['Teamleder i Salg']
    assert 'Privat ambition' not in json.dumps(out, ensure_ascii=False)
    assert db.query('SELECT COUNT(*) AS n FROM talent_assessments')[0]['n'] == before
    assert db.query('SELECT * FROM talent_applications') == []


def test_an_unknown_role_is_said_plainly(env):
    from app1 import tools
    db, app = env
    role_and_opportunity(db)
    ctx = as_ada(app)
    try:
        out = json.loads(tools._execute_get_my_development({'explore_role': 'astronaut'}, 'ada'))
    finally:
        ctx.pop()
    assert out['explored_role'] is None and 'ingen offentliggjort rolle' in out['explore_note']


def test_goal_tool_shows_visibility_and_only_shared_manager_goals(env):
    from app1 import tools
    db, app = env
    db.execute('UPDATE user_learning_goals SET shared_with_hr=1,shared_company_id=7 WHERE id=2')
    ctx = as_ada(app)
    try:
        out = json.loads(tools._execute_get_learning_goals({}, 'ada'))
    finally:
        ctx.pop()
    assert {g['title']: g['visibility'] for g in out['goals']} == {'Skifte branche': 'privat', 'Blive teamleder': 'delt med HR'}
    assert [g['title'] for g in out['from_manager']] == ['Tag PRINCE2']
    assert 'Fasthold Ada' not in json.dumps(out, ensure_ascii=False)


def test_sharing_a_goal_only_proposes_until_confirmed(env):
    from app1 import tools
    db, app = env
    ctx = as_ada(app)
    try:
        preview = json.loads(tools._execute_share_learning_goal({'goal_id': 2, 'share': True}, 'ada'))
        assert preview['needs_confirmation'] and 'Blive teamleder' in preview['message_da']
        assert db.one('SELECT shared_with_hr FROM user_learning_goals WHERE id=2')['shared_with_hr'] == 0
        done = json.loads(tools._execute_share_learning_goal({'goal_id': 2, 'share': True, 'confirm': True}, 'ada'))
        assert done['status'] == 'success' and done['visibility'] == 'delt med HR'
        assert db.one('SELECT shared_with_hr,shared_company_id FROM user_learning_goals WHERE id=2') == {'shared_with_hr': 1, 'shared_company_id': 7}
        missing = json.loads(tools._execute_share_learning_goal({'goal_id': 99, 'share': True, 'confirm': True}, 'ada'))
        assert missing['status'] == 'not_found'
    finally:
        ctx.pop()


def test_hr_assistant_sees_shared_goals_and_role_readiness_but_never_private_goals(env):
    import hr_tools
    db, app = env
    rid, _ = role_and_opportunity(db)
    cur = db.connection.cursor()
    svc.assign_role(cur, 7, 1, rid)
    db.execute('UPDATE user_learning_goals SET shared_with_hr=1,shared_company_id=7 WHERE id=2')
    db.connection.commit()
    dev = hr_tools._employee_development(db.connection.cursor(), 7, 'ada')
    text = json.dumps(dev, ensure_ascii=False)
    assert [g['title'] for g in dev['goals']['shared_by_employee']] == ['Blive teamleder']
    assert [g['title'] for g in dev['goals']['written_by_hr_not_shared']] == ['Fasthold Ada']
    assert 'Skifte branche' not in text and 'Privat ambition' not in text
    assert dev['role']['title'] == 'Teamleder' and dev['role']['readiness']['total'] == 2
    assert hr_tools._employee_development(db.connection.cursor(), 8, 'ada') is None


def test_selector_offers_the_tools_on_need_and_only_inside_a_company():
    def names(query, company_id=7, **kw):
        tools, _ = registry.get_employee_tool_selection(logged_in=True, company_id=company_id, intent='general',
                                                        user_query=query, **kw)
        return {t['function']['name'] for t in tools}
    assert 'get_my_development' in names('hvad mangler jeg for at blive teamleder?')
    assert 'get_my_development' in names('er der nogen interne muligheder for mig?')
    assert {'share_learning_goal', 'get_learning_goals'} <= names('del mit mål med HR')
    assert 'share_learning_goal' not in names('find et kursus i excel')
    solo = names('hvad mangler jeg for at blive teamleder? del mit mål med HR', company_id=None)
    assert 'get_my_development' not in solo and 'share_learning_goal' not in solo


def test_coming_from_the_development_workspace_brings_its_read_tools():
    from app1.surface_context import build_layer, normalize_context, origin_tool_names
    ctx = normalize_context({'from': 'development'})
    assert ctx == {'from': 'development'}
    assert 'get_my_development' in origin_tool_names(ctx)
    header, body, fenced = build_layer(ctx)
    assert 'Min udvikling' in header and not fenced
