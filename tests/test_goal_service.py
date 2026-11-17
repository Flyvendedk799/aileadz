"""One goal model: a person's own goal is private until they share it with their
company's HR; an HR-written goal is HR-only until HR shares it. Real SQLite
transactions and real Flask routes, no network."""
from unittest import mock

import pytest

import goal_service
from tests.sqlite_platform import PlatformDB, make_app


@pytest.fixture
def env():
    db = PlatformDB()
    db.raw.executescript('''
        CREATE TABLE user_learning_goals (id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT,title TEXT,description TEXT,
            target_date TEXT,status TEXT DEFAULT 'aktiv',shared_with_hr INTEGER DEFAULT 0,shared_company_id INTEGER,
            shared_at TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE talent_goal_links (id TEXT PRIMARY KEY,company_id INTEGER,user_id INTEGER,goal_id INTEGER,shared INTEGER DEFAULT 0);
    ''')
    people = ((1, 'ada', 7, 'employee'), (3, 'hr', 7, 'hr_manager'), (4, 'outside-hr', 8, 'hr_manager'))
    for user_id, username, cid, role in people:
        db.execute('INSERT INTO users (id,username) VALUES (%s,%s)', (user_id, username))
        db.execute("INSERT INTO company_users (company_id,user_id,username,department,role) VALUES (%s,%s,%s,'Salg',%s)",
                   (cid, user_id, username, role))
    for cid in (7, 8):
        db.execute("INSERT INTO companies (id,company_name) VALUES (%s,'Firma')", (cid,))
    db.execute("INSERT INTO user_learning_goals (id,username,title) VALUES (1,'ada','Skifte branche')")
    db.execute("INSERT INTO user_learning_goals (id,username,title) VALUES (2,'ada','Blive teamleder')")
    db.execute("INSERT INTO employee_goals (id,employee_id,company_id,goal_title,shared_with_employee) VALUES (10,1,7,'Fasthold Ada',0)")
    db.execute("INSERT INTO employee_goals (id,employee_id,company_id,goal_title,shared_with_employee) VALUES (11,1,7,'Tag PRINCE2',1)")
    app = make_app(db)
    with mock.patch('security_audit.audit', return_value=True), \
            mock.patch('white_label_global_integration.get_template_context', return_value={}):
        yield db, app


def titles(goals):
    return [g['title'] for g in goals]


def test_own_goals_are_private_until_shared_and_only_for_that_company(env):
    db, app = env
    cur = db.connection.cursor()
    assert goal_service.for_hr(cur, 7, 1)['shared_by_employee'] == []
    assert goal_service.share_own_goal(cur, 7, 1, 2, True) == 'Blive teamleder'
    db.connection.commit()
    assert titles(goal_service.for_hr(cur, 7, 1)['shared_by_employee']) == ['Blive teamleder']
    # Another company's HR sees nothing, shared or not.
    assert goal_service.for_hr(cur, 8, 1) == {'shared_by_employee': [], 'shared_with_employee': [], 'hr_only': []}
    # Taking it back is immediate.
    goal_service.share_own_goal(cur, 7, 1, 2, False)
    assert goal_service.for_hr(cur, 7, 1)['shared_by_employee'] == []


def test_a_goal_shared_with_a_company_the_person_left_is_gone(env):
    db, app = env
    cur = db.connection.cursor()
    goal_service.share_own_goal(cur, 7, 1, 2, True)
    db.execute("UPDATE company_users SET status='inactive' WHERE company_id=7 AND user_id=1")
    assert goal_service.for_hr(cur, 7, 1)['shared_by_employee'] == []


def test_nobody_can_share_someone_elses_goal(env):
    db, app = env
    cur = db.connection.cursor()
    assert goal_service.share_own_goal(cur, 7, 3, 2, True) is None
    assert db.one('SELECT shared_with_hr FROM user_learning_goals WHERE id=2')['shared_with_hr'] == 0


def test_learner_sees_all_own_goals_and_only_shared_hr_goals(env):
    db, app = env
    cur = db.connection.cursor()
    seen = goal_service.for_learner(cur, 'ada', 7, 1)
    assert sorted(titles(seen['own'])) == ['Blive teamleder', 'Skifte branche']
    assert titles(seen['from_manager']) == ['Tag PRINCE2']
    hr = goal_service.for_hr(cur, 7, 1)
    assert titles(hr['hr_only']) == ['Fasthold Ada'] and titles(hr['shared_with_employee']) == ['Tag PRINCE2']


def test_sharing_tells_hr_once_and_moves_the_talent_links(env):
    db, app = env
    db.execute("INSERT INTO talent_goal_links (id,company_id,user_id,goal_id,shared) VALUES ('l1',7,1,2,0)")
    cur = db.connection.cursor()
    goal_service.share_own_goal(cur, 7, 1, 2, True)
    goal_service.share_own_goal(cur, 7, 1, 2, True)
    db.connection.commit()
    told = db.query("SELECT message,action_url FROM notifications WHERE user_id='hr'")
    assert len(told) == 1 and told[0]['action_url'] == '/hr/employee/1/goals'
    assert db.query("SELECT * FROM notifications WHERE user_id IN ('ada','outside-hr')") == []
    assert db.one("SELECT shared FROM talent_goal_links WHERE id='l1'")['shared'] == 1
    goal_service.share_own_goal(cur, 7, 1, 2, False)
    assert db.one("SELECT shared FROM talent_goal_links WHERE id='l1'")['shared'] == 0


def test_hr_goal_status_has_a_writer_and_is_company_scoped(env):
    db, app = env
    cur = db.connection.cursor()
    assert goal_service.set_hr_goal_status(cur, 8, 10, 'done') is None
    assert goal_service.set_hr_goal_status(cur, 7, 10, 'done') == 1
    row = db.one('SELECT status,progress FROM employee_goals WHERE id=10')
    assert row['status'] == 'completed' and row['progress'] == 100
    assert goal_service.for_hr(cur, 7, 1)['hr_only'][0]['status'] == 'done'
    with pytest.raises(ValueError):
        goal_service.set_hr_goal_status(cur, 7, 10, 'whatever')


def test_learner_route_shares_and_unshares(env):
    db, app = env
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(user='ada', user_id=1, company_id=7, company_role='employee')
    with mock.patch('app1.user_profile_db.ensure_tables'):
        assert c.post('/mine-maal/2/del', data=dict(shared='yes')).status_code == 302
        assert db.one('SELECT shared_with_hr,shared_company_id FROM user_learning_goals WHERE id=2') == {'shared_with_hr': 1, 'shared_company_id': 7}
        page = c.get('/mine-maal').get_data(as_text=True)
        assert 'Delt med HR' in page and 'Privat · kun du kan se det' in page
        c.post('/mine-maal/2/del', data=dict(shared='no'))
    assert db.one('SELECT shared_with_hr FROM user_learning_goals WHERE id=2')['shared_with_hr'] == 0


def test_hr_page_shows_only_what_the_employee_shared_and_can_set_status(env):
    db, app = env
    cur = db.connection.cursor()
    goal_service.share_own_goal(cur, 7, 1, 2, True)
    db.connection.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(user='hr', user_id=3, company_id=7, company_role='hr_manager')
    response = c.get('/hr/employee/1/goals', headers={'Accept': 'text/html'})
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'Delt af medarbejderen' in page and 'Blive teamleder' in page
    assert 'Skifte branche' not in page
    assert c.post('/hr/goals/10/status', data=dict(status='paused')).status_code == 302
    assert db.one('SELECT status FROM employee_goals WHERE id=10')['status'] == 'paused'
    # A manager of another company cannot touch it.
    other = app.test_client()
    with other.session_transaction() as s:
        s.update(user='outside-hr', user_id=4, company_id=8, company_role='hr_manager')
    other.post('/hr/goals/10/status', data=dict(status='done'))
    assert db.one('SELECT status FROM employee_goals WHERE id=10')['status'] == 'paused'
