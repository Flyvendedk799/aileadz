"""Executable talent journeys: real SQLite transactions, real Flask routes, no network."""
import datetime
import re
from unittest import mock
import pytest
from tests.sqlite_platform import PlatformDB, make_app
from schema_registry import TALENT_DDL
import talent_service as svc
from competency_model import snapshot, evaluate


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
        CREATE TABLE user_learning_goals (id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT,title TEXT,status TEXT DEFAULT 'aktiv');
        CREATE TABLE company_skill_targets (id INTEGER PRIMARY KEY AUTOINCREMENT,company_id INTEGER,department TEXT,skill_name TEXT,target_level INTEGER,priority TEXT);
    ''')
    for user_id, username, cid in ((1,'ada',7),(2,'bo',7),(3,'hr',7),(4,'outside',8)):
        db.execute('INSERT INTO users (id,username) VALUES (%s,%s)', (user_id, username))
        db.execute("INSERT INTO company_users (company_id,user_id,username,department,role) VALUES (%s,%s,%s,'Salg',%s)", (cid,user_id,username,'hr_manager' if user_id==3 else 'employee'))
    db.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
    db.execute("INSERT INTO user_learning_goals (id,username,title) VALUES (1,'ada','Privat mål')")
    db.execute('INSERT INTO talent_settings VALUES (7,1,1,1)')
    app = make_app(db)
    def full(username):
        skills = db.query('SELECT * FROM user_skills WHERE username=%s', (username,))
        return dict(username=username, headline='Udvikler', target_role='Privat ambition', bio='Min profil',
                    skills=[dict(name=r['skill_name'],level=r['skill_level'],numeric_level=r['numeric_level'],source=r['source']) for r in skills],
                    learning_goals=[dict(id=1,title='Privat mål',status='aktiv')])
    with mock.patch('app1.user_profile_db.get_full_profile', side_effect=full), mock.patch('catalog_service.search_products', return_value={'products':[]}), mock.patch('security_audit.audit', return_value=True), mock.patch('white_label_global_integration.get_template_context', return_value={}):
        yield db,app


def client(app, user_id=1, company=7, role='employee'):
    c=app.test_client()
    with c.session_transaction() as s:
        s.update(user={1:'ada',2:'bo',3:'hr',4:'outside'}[user_id],user_id=user_id,company_id=company,company_role=role)
    return c


def publish_role(db):
    cur=db.connection.cursor()
    rid=svc.save_role(cur,7,'Udvikler','Python | 4')
    cur.execute('UPDATE talent_role_versions SET published=1 WHERE id=%s',(rid,))
    db.connection.commit()
    return rid


def opportunity(db, rid, kind='role'):
    cur=db.connection.cursor()
    oid=svc.save_opportunity(cur,7,3,dict(role_version_id=rid,kind=kind,title='Ny mulighed',deadline=str(datetime.date.today()+datetime.timedelta(days=30))))
    cur.execute("UPDATE talent_opportunities SET status='published' WHERE id=%s",(oid,))
    db.connection.commit()
    return oid


def test_assessments_do_not_endorse_conflicting_self_claims():
    state=snapshot({'skills':[{'name':'Python3','level':'ekspert'}]},[{'skill_name':'python','current_level':2}])
    r=evaluate(state,[dict(skill='Python',target_level=4)])
    assert state['skills'][0]['disagreement']
    assert r['requirements'][0]['current_level']==2
    assert r['pct']==0
    assert state['skills'][0]['evidence'][0]['level']==2


def test_unknown_and_full_denominator_and_level_three():
    state=snapshot({'skills':[dict(name='SQL',numeric_level=3,level='mellem')]})
    req=[dict(skill='SQL',target_level=3)]+[dict(skill=f'Skill {n}',target_level=4) for n in range(20)]
    r=evaluate(state,req)
    assert r['total']==21 and r['counts']['unknown']==20
    assert r['pct']==5 and r['counts']['provisional']==1
    assert evaluate(snapshot({},available=False),req)['pct'] is None


def test_role_versions_and_tenant_isolation(env):
    db,app=env
    rid=publish_role(db)
    cur=db.connection.cursor()
    role=svc.role(cur,7,rid)
    next_id=svc.save_role(cur,7,'Senior','Python | 5',role['role_id'])
    assert svc.role(cur,7,rid)['requirements'][0]['target_level']==4
    assert svc.role(cur,7,next_id,published=False)['version']==2
    with pytest.raises(ValueError):svc.assign_role(cur,7,4,rid)
    with pytest.raises(ValueError):svc.role(cur,8,rid)
    assert client(app,4,8).get('/muligheder').status_code==404


def test_goal_sharing_and_unsharing_is_explicit(env):
    db,app=env
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.link_goal(cur,7,1,1,'Python | 4','2030-01-01',[dict(kind='practice',title='Byg et værktøj')],False)
    db.connection.commit()
    assert svc.goals(cur,7,hr=True)==[]
    goal=svc.goals(cur,7,1)[0]
    c=client(app)
    c.post('/talent/actions/share_goal',data=dict(id=goal['id'],shared='yes'))
    assert len(svc.goals(cur,7,hr=True))==1
    client(app,2).post('/talent/actions/share_goal',data=dict(id=goal['id'],shared='no'))
    assert len(svc.goals(cur,7,hr=True))==1
    c.post('/talent/actions/share_goal',data=dict(id=goal['id'],shared='no'))
    assert svc.goals(cur,7,hr=True)==[]


def test_assessment_is_idempotent_and_updates_real_matrix(env):
    db,app=env
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.assess(cur,7,1,'Python3',3,'manager',3,'first','Travail')
        svc.assess(cur,7,1,'Python',3,'manager',3,'first','Travail')
        svc.assess(cur,7,1,'Python',4,'manager',3,'second','Resultat')
    assert len(db.query('SELECT * FROM talent_assessments'))==2
    assert len(db.query('SELECT * FROM employee_skills_matrix'))==1
    assert db.one('SELECT current_level FROM employee_skills_matrix')['current_level']==4


def test_full_application_offer_and_separate_placement(env):
    db,app=env
    rid=publish_role(db);oid=opportunity(db,rid)
    c=client(app)
    response=c.get('/muligheder/'+oid+'/ansoeg')
    assert response.status_code==200
    assert 'Privat ambition' not in response.get_data(as_text=True)
    digest=re.search('name="snapshot_digest" value="([^"]+)"',response.get_data(as_text=True))[1]
    sections=['headline','bio','skills','experience','education','certifications','completed_courses','languages','portfolio_links']
    assert c.post('/muligheder/'+oid+'/ansoeg',data=dict(confirm='yes',snapshot_digest=digest,sections=sections)).status_code==302
    application=db.one('SELECT * FROM talent_applications')
    assert 'Privat ambition' not in application['snapshot_json']
    cur=db.connection.cursor()
    with pytest.raises(ValueError):svc.transition(cur,7,2,application['id'],'screening',1)
    for revision,status in enumerate(('screening','interview','offer'),start=1):
        svc.transition(cur,7,3,application['id'],status,revision)
    with pytest.raises(ValueError):svc.transition(cur,7,3,application['id'],'accepted',4)
    svc.transition(cur,7,1,application['id'],'accepted',4)
    assert db.query('SELECT * FROM talent_role_assignments')==[]
    svc.confirm_placement(cur,7,application['id'])
    assert db.one('SELECT * FROM talent_role_assignments')['role_version_id']==rid
    with pytest.raises(ValueError):svc.confirm_placement(cur,7,application['id'])


def test_scenario_draft_does_not_change_targets_and_masks_sparse_cohorts(env):
    db,app=env
    cur=db.connection.cursor()
    svc.save_scenario(cur,7,dict(title='Plan',target_date='2030-01-01',requirements='Python | 4 | 5'))
    assert db.query('SELECT * FROM company_skill_targets')==[]
    scenario=db.one('SELECT * FROM talent_scenarios')
    svc.publish_scenario(cur,7,scenario['id'])
    assert db.one('SELECT * FROM company_skill_targets')['target_level']==4
    states=[snapshot({'skills':[dict(name='Python',level='ekspert')]})]+[snapshot({}) for _ in range(9)]
    assert svc.coverage(states,svc.requirements('Python | 4 | 5'),5)[0]['counts'] is None


def test_employee_and_hr_pages_render_and_authorization(env):
    db,app=env
    rid=publish_role(db)
    opportunity(db,rid)
    assert client(app).get('/udvikling').status_code==200
    assert client(app).get('/muligheder').status_code==200
    assert client(app).get('/hr/talent').status_code==403
    hr=client(app,3,role='hr_manager')
    assert hr.get('/hr/talent').status_code==200
    assert client(app).post('/talent/actions/publish_role',data={'id':rid}).status_code==403


def test_acting_platform_admin_can_manage_without_becoming_employee(env):
    import time
    db, app = env
    c = client(app, 4, company=7, role='company_admin')
    with c.session_transaction() as s:
        s.update(role='admin', admin_acting_company_id=7, admin_acting_until=time.time()+3600)
    assert c.get('/hr/talent').status_code == 200
    assert c.post('/talent/actions/settings', data={'roles_enabled':'on','mobility_enabled':'on','planning_enabled':'on'}).status_code == 302
    rid = publish_role(db)
    data = dict(role_version_id=rid, kind='project', title='Admin project', deadline='2030-01-01', team=['3'])
    assert c.post('/talent/actions/opportunity', data=data).status_code == 302
    opp = db.one('SELECT * FROM talent_opportunities')
    assert opp['owner_user_id'] == 4
    assert c.post('/talent/actions/publish_opportunity', data={'id':opp['id']}).status_code == 302
    assert db.one('SELECT status FROM talent_opportunities')['status'] == 'published'
    assert c.get('/muligheder/ansaettelsesteam').status_code == 200
    # Supporting a company never creates an employee identity or application access.
    assert c.get('/udvikling').status_code == 403
    assert c.get('/api/talent/competencies').status_code == 403
    assert c.get('/muligheder/'+opp['id']+'/ansoeg').status_code == 403
    assert c.post('/talent/actions/self_assessment', data={}).status_code == 403
    assert db.one('SELECT COUNT(*) AS n FROM company_users WHERE company_id=7 AND user_id=4')['n'] == 0


@pytest.mark.parametrize('role,acting_company,until', [('user',7,4102444800),('admin',8,4102444800),('admin',7,1),('admin',None,4102444800)])
def test_management_exception_requires_current_admin_company_session(env, role, acting_company, until):
    _, app = env
    c = client(app, 4, company=7, role='company_admin')
    with c.session_transaction() as s:
        s.update(role=role, admin_acting_company_id=acting_company, admin_acting_until=until)
    assert c.get('/hr/talent').status_code == 403
    assert c.post('/talent/actions/settings', data={'roles_enabled':'on'}).status_code == 403


def test_external_opportunity_owner_cannot_add_nonmember_colleagues(env):
    db, _ = env
    rid = publish_role(db)
    with pytest.raises(ValueError):
        svc.save_opportunity(db.connection.cursor(), 7, 4,
                             dict(role_version_id=rid, kind='project', title='Project', deadline='2030-01-01', team=['99']),
                             external_owner=True)
    assert db.query('SELECT * FROM talent_opportunities') == []


def test_stale_application_preview_requires_another_review(env):
    db,app=env
    oid=opportunity(db,publish_role(db))
    c=client(app)
    c.post('/muligheder/'+oid+'/ansoeg',data=dict(confirm='yes',snapshot_digest='old',sections=['headline']))
    assert db.query('SELECT * FROM talent_applications')==[]


def test_disabled_modules_and_foreign_actions_are_blocked(env):
    db,app=env
    rid=publish_role(db)
    db.execute('UPDATE talent_settings SET roles_enabled=0,mobility_enabled=0,planning_enabled=0')
    hr=client(app,3,role='hr_manager')
    assert hr.post('/talent/actions/assign_role',data=dict(user_id=1,role_version_id=rid)).status_code==404
    assert client(app).get('/muligheder').status_code==404
    assert db.query('SELECT * FROM talent_role_assignments')==[]


def test_hiring_access_and_revision_conflicts(env):
    db,app=env
    oid=opportunity(db,publish_role(db),'project')
    cur=db.connection.cursor()
    svc.apply(cur,7,1,oid,{'headline':'Only the team sees this'})
    db.connection.commit()
    aid=db.one('SELECT id FROM talent_applications')['id']
    svc.transition(cur,7,3,aid,'screening',1)
    with pytest.raises(ValueError):svc.transition(cur,7,3,aid,'offer',1)
    svc.transition(cur,7,3,aid,'offer',2)
    db.connection.commit()
    assert 'Only the team sees this' not in client(app,2).get('/muligheder/ansaettelsesteam').get_data(as_text=True)
    assert 'Only the team sees this' in client(app,3,role='hr_manager').get('/muligheder/ansaettelsesteam').get_data(as_text=True)
    svc.transition(cur,7,1,aid,'withdrawn',3)
    with pytest.raises(ValueError):svc.transition(cur,7,3,aid,'screening',4)


def test_matrix_and_history_roll_back_together(env):
    db,app=env
    cur=db.connection.cursor()
    with app.test_request_context(), mock.patch('skill_history.record_snapshot',return_value=False):
        with pytest.raises(ValueError):svc.assess(cur,7,1,'Python',3,'manager',3,'failure')
        db.connection.rollback()
    assert db.query('SELECT * FROM employee_skills_matrix')==[]
    assert db.query('SELECT * FROM talent_assessments')==[]


def test_linked_course_does_not_complete_from_employee_acknowledgement(env):
    db,app=env
    db.execute("INSERT INTO course_orders (order_id,company_id,user_id,username,product_title,status) VALUES ('own-order',7,1,'ada','Python','booked')")
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.link_goal(cur,7,1,1,'Python | 4','2030-01-01',[dict(kind='course',title='Tag kurset',order_id='own-order')],False)
    db.connection.commit()
    goal=svc.goals(cur,7,1)[0]
    client(app).post('/talent/actions/complete_action',data=dict(id=goal['id'],action_id=goal['actions'][0]['id']))
    assert not svc.goals(cur,7,1)[0]['actions'][0]['done']
    assert db.one("SELECT status FROM course_orders WHERE order_id='own-order'")['status']=='booked'


def test_numeric_self_level_is_consistent_with_cv_and_api(env):
    import cv_service
    db,app=env
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.assess(cur,7,1,'SQL',3,'self',1,'level-three')
        from app1.user_profile_db import get_full_profile, get_skills
        assert get_skills('ada')[0]['numeric_level'] == 3
        p=get_full_profile('ada')
    db.connection.commit()
    assert cv_service.assemble(p)['skills'][0]['score']==3
    assert client(app).get('/api/talent/competencies').get_json()['skills'][0]['level']==3


def test_missing_opportunity_and_false_membership_fail_closed(env):
    db,app=env
    assert client(app).get('/muligheder/missing/ansoeg').status_code==404
    assert client(app,1,company=8).get('/udvikling').status_code==403


def test_import_preserves_stronger_claim_and_its_source(env):
    from app1.user_profile_db import add_skill, get_skills
    db,app=env
    db.execute("INSERT INTO user_skills (username,skill_name,skill_level,numeric_level,source) VALUES ('ada','Python','ekspert',5,'manual')")
    with app.test_request_context():
        assert add_skill('ada','Python3','mellem',source='cv_upload')
        row=get_skills('ada')[0]
    assert row['numeric_level']==5 and row['source']=='manual'


def test_course_handle_alone_does_not_verify_completion():
    import cv_service
    p={'completed_courses':[dict(title='SQL',handle='sql')]}
    assert not cv_service.assemble(p)['courses'][0]['verified']
    assert cv_service.assemble(p,orders=[dict(handle='sql',state='gennemfoert')])['courses'][0]['verified']


def notes(db, username):
    return db.query('SELECT title,message,action_url FROM notifications WHERE user_id=%s ORDER BY id', (username,))


def test_sharing_a_goal_tells_hr_once_and_private_goals_tell_nobody(env):
    db,app=env
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.link_goal(cur,7,1,1,'Python | 4','2030-01-01',[dict(kind='practice',title='Byg et værktøj')],False)
    db.connection.commit()
    assert notes(db,'hr')==[]
    goal=svc.goals(cur,7,1)[0]
    c=client(app)
    c.post('/talent/actions/share_goal',data=dict(id=goal['id'],shared='yes'))
    c.post('/talent/actions/share_goal',data=dict(id=goal['id'],shared='yes'))
    told=notes(db,'hr')
    assert len(told)==1 and 'Privat mål' in told[0]['message'] and told[0]['action_url']=='/hr/talent'
    assert notes(db,'ada')==[] and notes(db,'bo')==[]


def test_manager_assessment_tells_the_employee_not_the_assessor(env):
    db,app=env
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.assess(cur,7,1,'Python',4,'manager',3,'ref-1')
        svc.assess(cur,7,1,'Python',2,'self',1,'ref-2')
    db.connection.commit()
    assert len(notes(db,'ada'))==1 and 'niveau 4 af 5' in notes(db,'ada')[0]['message']
    assert notes(db,'hr')==[]


def test_application_stages_and_placement_reach_the_right_people(env):
    db,app=env
    rid=publish_role(db);oid=opportunity(db,rid)
    db.execute("UPDATE talent_opportunities SET department='Udvikling' WHERE id=%s",(oid,))
    cur=db.connection.cursor()
    svc.apply(cur,7,1,oid,{})
    assert [n['title'] for n in notes(db,'hr')]==['Ny ansøgning']
    application=db.one('SELECT * FROM talent_applications')
    for revision,status in enumerate(('screening','interview','offer'),start=1):
        svc.transition(cur,7,3,application['id'],status,revision)
    assert len(notes(db,'ada'))==3 and 'Tilbud' in notes(db,'ada')[-1]['message']
    svc.transition(cur,7,1,application['id'],'accepted',4)
    assert notes(db,'hr')[-1]['title']=='Ansøgning opdateret'
    svc.confirm_placement(cur,7,application['id'])
    db.connection.commit()
    assert notes(db,'ada')[-1]['title']=='Din placering er bekræftet'
    person=db.one('SELECT job_title,department FROM company_users WHERE company_id=7 AND user_id=1')
    assert person['job_title']=='Udvikler' and person['department']=='Udvikling'


def test_completed_course_ticks_the_goal_step_for_hr_and_tells_the_learner(env):
    import domain_reactions, event_bus
    db,app=env
    db.execute("INSERT INTO course_orders (order_id,company_id,user_id,username,product_title,status) VALUES ('own-order',7,1,'ada','Python','booked')")
    cur=db.connection.cursor()
    with app.test_request_context():
        svc.link_goal(cur,7,1,1,'Python | 4','2030-01-01',[dict(kind='course',title='Tag kurset',order_id='own-order')],True)
    db.connection.commit()
    assert not svc.goals(cur,7,hr=True)[0]['actions'][0]['done']
    db.execute("UPDATE course_orders SET status='completed' WHERE order_id='own-order'")
    # Read side: HR and the learner see the same thing without any event.
    assert svc.goals(cur,7,hr=True)[0]['actions'][0]['done']
    domain_reactions.register()
    with app.app_context():
        for _ in range(2):
            event_bus.publish_internal(7,'course.completed',dict(order_id='own-order',user_id=1,product_title='Python'))
    assert '"done": true' in db.one('SELECT actions_json FROM talent_goal_links')['actions_json']
    told=[n for n in notes(db,'ada') if n['action_url']=='/udvikling']
    assert len(told)==1 and 'Privat mål' in told[0]['message']
    # Another tenant's event for the same order id touches nothing.
    with app.app_context():
        assert event_bus.publish_internal(8,'course.completed',dict(order_id='own-order',user_id=1))==1


def test_a_failing_subscriber_never_reaches_the_emitter_or_its_peers(env):
    import event_bus
    db,app=env
    seen=[]
    def boom(*a): raise RuntimeError('x')
    def ok(cid,kind,payload): seen.append((cid,kind))
    event_bus.subscribe('test.thing',boom); event_bus.subscribe('test.thing',ok); event_bus.subscribe('test.thing',ok)
    try:
        with app.app_context():
            assert event_bus.publish_internal(7,'test.thing',{})==1
        assert seen==[(7,'test.thing')]
    finally:
        event_bus._SUBSCRIBERS.pop('test.thing',None)


def test_an_unmet_requirement_links_to_learning_and_a_met_one_does_not(env):
    from flask import render_template_string
    db,app=env
    app.add_url_rule('/catalog','catalog.catalog_index',lambda:'') if 'catalog.catalog_index' not in app.view_functions else None
    r=dict(available=True,total=2,counts=dict(satisfied=1,unknown=1,provisional=0),requirements=[
        dict(skill='Python',current_level=4,target_level=4,provisional=False),
        dict(skill='Ledelse & co',current_level=None,target_level=3,provisional=False)])
    with app.test_request_context():
        learner=render_template_string("{% import 'fm/_talent.html' as t %}{{ t.readiness(r, links=true) }}",r=r)
        hr=render_template_string("{% import 'fm/_talent.html' as t %}{{ t.readiness(r) }}",r=r)
    assert learner.count('Find læring')==1 and 'q=Ledelse+%26+co' in learner
    assert 'Find læring' not in hr
