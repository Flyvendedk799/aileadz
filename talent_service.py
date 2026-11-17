"""Company-scoped talent workflows. Callers authorize, own the transaction, and commit.

Role versions and application snapshots are immutable. Exploring roles never writes an
interest signal. Development links extend user_learning_goals; paid actions remain in
the existing assignment review and order services.
"""
import datetime as dt
import json
import uuid
from competency import canonical_skill
from competency_model import identity, snapshot, evaluate

STAGES = {
    'submitted': ('screening', 'rejected'), 'screening': ('interview', 'rejected'),
    'interview': ('offer', 'rejected'), 'offer': ('accepted', 'rejected'),
    'accepted': (), 'rejected': (), 'withdrawn': (),
}
STAGE_LABELS = dict(submitted='Ansøgning modtaget', screening='Gennemgang', interview='Samtale',
                    offer='Tilbud', accepted='Accepteret', rejected='Afslag', withdrawn='Trukket tilbage')


def uid():
    return str(uuid.uuid4())


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def unpack(row):
    row = dict(row)
    for key in list(row):
        if key.endswith('_json'):
            row[key[:-5]] = json.loads(row[key] or '[]')
    return row


def date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise ValueError('Vælg en gyldig dato.') from None


def requirements(value):
    """UI lines: competency | expected level | optional required people."""
    result = []
    lines = value.splitlines() if isinstance(value, str) else value
    if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
        raise ValueError('Angiv 1–100 kompetencekrav.')
    for line in lines:
        if isinstance(line, str):
            parts = line.split('|')
            item = dict(skill=parts[0], target_level=parts[1] if len(parts) > 1 else None,
                        required_people=parts[2] if len(parts) > 2 else 1)
        else:
            item = line
        try:
            name = canonical_skill(item['skill'])
            level, count = int(item['target_level']), int(item.get('required_people', 1))
            if not name or not 1 <= level <= 5 or not 1 <= count <= 100000:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ValueError('Hvert krav skal have et navn, niveau 1–5 og et positivt antal medarbejdere.') from None
        result.append(dict(skill=name, competency_id=identity(name), target_level=level, required_people=count))
    if len({r['competency_id'] for r in result}) != len(result):
        raise ValueError('Samme kompetence må kun stå én gang.')
    return result


def member(cur, company_id, user_id):
    cur.execute("SELECT u.id, u.username, cu.department, cu.job_title FROM users u JOIN company_users cu ON cu.user_id=u.id "
                "WHERE cu.company_id=%s AND u.id=%s AND cu.status='active'", (company_id, user_id))
    row = cur.fetchone()
    if not row:
        raise ValueError('Medarbejderen er ikke aktiv i virksomheden.')
    return row


def flags(cur, company_id):
    cur.execute('SELECT * FROM talent_settings WHERE company_id=%s', (company_id,))
    return cur.fetchone() or dict(roles_enabled=False, mobility_enabled=False, planning_enabled=False)


def set_flags(cur, company_id, data):
    values = [int(bool(data.get(k))) for k in ('roles_enabled', 'mobility_enabled', 'planning_enabled')]
    if (values[1] or values[2]) and not values[0]:
        raise ValueError('Aktivér roller før muligheder og planlægning.')
    cur.execute('INSERT INTO talent_settings (company_id,roles_enabled,mobility_enabled,planning_enabled) VALUES (%s,%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE roles_enabled=VALUES(roles_enabled),mobility_enabled=VALUES(mobility_enabled),planning_enabled=VALUES(planning_enabled)',
                (company_id, *values))


def roles(cur, company_id, published=True):
    cur.execute('SELECT * FROM talent_role_versions WHERE company_id=%s' + (' AND published=1' if published else '') + ' ORDER BY title,version DESC', (company_id,))
    return [unpack(r) for r in cur.fetchall()]


def role(cur, company_id, version_id, *, published=True):
    cur.execute('SELECT * FROM talent_role_versions WHERE company_id=%s AND id=%s' + (' AND published=1' if published else ''), (company_id, version_id))
    row = cur.fetchone()
    if not row:
        raise ValueError('Rollen findes ikke eller er ikke offentliggjort.')
    return unpack(row)


def save_role(cur, company_id, title, reqs, role_id=None):
    if not title.strip():
        raise ValueError('Angiv en rolletitel.')
    version = 1
    if role_id:
        cur.execute('SELECT version FROM talent_role_versions WHERE company_id=%s AND role_id=%s ORDER BY version DESC FOR UPDATE', (company_id, role_id))
        previous = cur.fetchall()
        if not previous:
            raise ValueError('Rollen findes ikke.')
        version = int(previous[0]['version']) + 1
    rid, vid = role_id or uid(), uid()
    cur.execute('INSERT INTO talent_role_versions (id,company_id,role_id,version,title,requirements_json) VALUES (%s,%s,%s,%s,%s,%s)',
                (vid, company_id, rid, version, title.strip()[:255], dump(requirements(reqs))))
    return vid


def assign_role(cur, company_id, user_id, version_id):
    member(cur, company_id, user_id)
    role(cur, company_id, version_id)
    cur.execute('INSERT INTO talent_role_assignments (company_id,user_id,role_version_id) VALUES (%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE role_version_id=VALUES(role_version_id),updated_at=CURRENT_TIMESTAMP', (company_id, user_id, version_id))


def employee_state(cur, company_id, user_id, profile=None):
    who = member(cur, company_id, user_id)
    if profile is None:
        from app1.user_profile_db import get_full_profile
        profile = get_full_profile(who['username'])
    cur.execute('SELECT skill_name,current_level,target_level,updated_at FROM employee_skills_matrix WHERE company_id=%s AND employee_id=%s', (company_id, user_id))
    matrix = list(cur.fetchall())
    state = snapshot(profile, matrix)
    cur.execute('SELECT role_version_id FROM talent_role_assignments WHERE company_id=%s AND user_id=%s', (company_id, user_id))
    assignment = cur.fetchone()
    state['current_role'] = role(cur, company_id, assignment['role_version_id']) if assignment else None
    state['readiness'] = evaluate(state, state['current_role']['requirements'], version=state['current_role']['id']) if state['current_role'] else None
    return state


def assess(cur, company_id, user_id, skill, level, kind, actor_id, source_ref, note=''):
    who = member(cur, company_id, user_id)
    name = canonical_skill(skill)
    if not name or kind not in ('self', 'manager') or not 1 <= int(level) <= 5 or not source_ref:
        raise ValueError('Angiv kompetence og niveau 1–5.')
    if kind == 'self' and user_id != actor_id:
        raise ValueError('Du kan kun selvvurdere dine egne kompetencer.')
    cur.execute('SELECT id FROM talent_assessments WHERE company_id=%s AND user_id=%s AND competency_id=%s AND kind=%s AND source_ref=%s',
                (company_id, user_id, identity(name), kind, source_ref))
    if cur.fetchone():
        return
    cur.execute('INSERT INTO talent_assessments (id,company_id,user_id,competency_id,skill_name,level,kind,actor_user_id,source_ref,note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (uid(), company_id, user_id, identity(name), name, int(level), kind, actor_id, source_ref[:100], note[:4000]))
    if kind == 'manager':
        from skill_history import record_snapshot
        from skill_history import set_matrix_level
        previous = set_matrix_level(cur, company_id, user_id, name, level)
        if not record_snapshot(cur, company_id, user_id, name, level, previous_level=previous, source='manager_review'):
            if previous != int(level):
                raise ValueError('Historikken kunne ikke gemmes. Prøv igen.')
    else:
        from competency import score_to_level, skill_category
        cur.execute('INSERT INTO user_skills (username,skill_name,skill_level,numeric_level,category,source) VALUES (%s,%s,%s,%s,%s,%s) '
                    'ON DUPLICATE KEY UPDATE skill_level=VALUES(skill_level),numeric_level=VALUES(numeric_level),source=VALUES(source)',
                    (who['username'], name, score_to_level(level), int(level), skill_category(name), 'manual'))


def link_goal(cur, company_id, user_id, goal_id, reqs, review_date, actions, shared):
    who = member(cur, company_id, user_id)
    cur.execute('SELECT id FROM user_learning_goals WHERE id=%s AND username=%s', (goal_id, who['username']))
    if not cur.fetchone():
        raise ValueError('Vælg et af dine eksisterende udviklingsmål.')
    reqs = requirements(reqs)
    baseline = evaluate(employee_state(cur, company_id, user_id), reqs)
    clean = []
    for action in actions:
        if action.get('kind') not in ('practice', 'mentoring', 'course', 'assessment') or not action.get('title', '').strip():
            raise ValueError('Vælg en handling og beskriv næste skridt.')
        linked_order = action.get('order_id') or None
        if linked_order:
            cur.execute('SELECT order_id FROM course_orders WHERE company_id=%s AND user_id=%s AND order_id=%s', (company_id, user_id, linked_order))
            if not cur.fetchone() or action['kind'] != 'course':
                raise ValueError('Vælg en af dine egne kursusbestillinger.')
        clean.append(dict(id=uid(), kind=action['kind'], title=action['title'][:500], done=False, order_id=linked_order))
    cur.execute('INSERT INTO talent_goal_links (id,company_id,user_id,goal_id,shared,requirements_json,baseline_json,actions_json,review_date) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (uid(), company_id, user_id, goal_id, int(shared), dump(reqs), dump(baseline), dump(clean), date(review_date)))


def goals(cur, company_id, user_id=None, *, hr=False):
    clause = ' AND l.shared=1' if hr else ' AND l.user_id=%s'
    cur.execute('SELECT l.*,g.title,g.status FROM talent_goal_links l JOIN users u ON u.id=l.user_id '
                'JOIN user_learning_goals g ON g.id=l.goal_id AND g.username=u.username '
                'JOIN company_users cu ON cu.user_id=l.user_id AND cu.company_id=l.company_id '
                "WHERE l.company_id=%s AND cu.status='active'" + clause + ' ORDER BY l.review_date',
                (company_id,) if hr else (company_id, user_id))
    return [unpack(r) for r in cur.fetchall()]


def save_opportunity(cur, company_id, actor_id, data):
    role(cur, company_id, data['role_version_id'])
    if data['kind'] not in ('role', 'project') or not data['title'].strip() or date(data['deadline']) < dt.date.today():
        raise ValueError('Angiv titel, type og en fremtidig frist.')
    team = sorted(set([actor_id] + [int(i) for i in data.get('team', [])]))
    for person in team:
        member(cur, company_id, person)
    oid = uid()
    cur.execute('INSERT INTO talent_opportunities (id,company_id,role_version_id,kind,title,description,department,location,deadline,owner_user_id,hiring_team_json) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (oid, company_id, data['role_version_id'], data['kind'], data['title'][:255], data.get('description','')[:8000],
                 data.get('department','')[:100], data.get('location','')[:150], date(data['deadline']), actor_id, '[]'))
    for person in team:
        cur.execute('INSERT INTO talent_hiring_members (company_id,user_id,opportunity_id) VALUES (%s,%s,%s)', (company_id, person, oid))
    return oid


def opportunity(cur, company_id, oid, lock=False):
    cur.execute('SELECT * FROM talent_opportunities WHERE company_id=%s AND id=%s' + (' FOR UPDATE' if lock else ''), (company_id, oid))
    row = cur.fetchone()
    if not row:
        raise ValueError('Muligheden findes ikke.')
    return unpack(row)


def hiring_access(cur, company_id, user_id, oid):
    cur.execute('SELECT user_id FROM talent_hiring_members WHERE company_id=%s AND user_id=%s AND opportunity_id=%s', (company_id, user_id, oid))
    return bool(cur.fetchone())


def application_snapshot(profile, state, sections):
    allowed = ('headline', 'bio', 'experience', 'education', 'certifications', 'completed_courses', 'languages', 'portfolio_links')
    result = {'name': profile.get('username') or ''}
    result.update({k: profile.get(k) for k in allowed if k in sections})
    if 'skills' in sections:
        result['skills'] = [{k: r[k] for k in ('name', 'level', 'provisional', 'disagreement')} for r in state['skills']]
    return result


def apply(cur, company_id, user_id, oid, snapshot_data):
    member(cur, company_id, user_id)
    opp = opportunity(cur, company_id, oid, lock=True)
    if opp['status'] != 'published' or date(opp['deadline']) < dt.date.today():
        raise ValueError('Muligheden modtager ikke længere ansøgninger.')
    cur.execute('INSERT INTO talent_applications (id,company_id,user_id,opportunity_id,snapshot_json,history_json) VALUES (%s,%s,%s,%s,%s,%s)',
                (uid(), company_id, user_id, oid, dump(snapshot_data), dump([dict(stage='submitted', at=str(dt.datetime.now(dt.timezone.utc)))])))


def transition(cur, company_id, user_id, application_id, target, revision):
    cur.execute('SELECT * FROM talent_applications WHERE company_id=%s AND id=%s FOR UPDATE', (company_id, application_id))
    app = cur.fetchone()
    if not app or int(app['revision']) != int(revision):
        raise ValueError('Ansøgningen er ændret. Genindlæs siden.')
    own = app['user_id'] == user_id
    opp = opportunity(cur, company_id, app['opportunity_id'])
    allowed = list(STAGES.get(app['status'], ()))
    if app['status'] == 'screening' and opp['kind'] == 'project':
        allowed.append('offer')
    if target == 'withdrawn':
        permitted = own and app['status'] not in ('accepted', 'rejected', 'withdrawn')
    elif target == 'accepted':
        permitted = own and app['status'] == 'offer'
    else:
        permitted = target in allowed and target != 'accepted' and hiring_access(cur, company_id, user_id, opp['id'])
    if not permitted:
        raise ValueError('Denne ændring er ikke tilladt.')
    history = json.loads(app['history_json'])
    history.append(dict(stage=target, at=str(dt.datetime.now(dt.timezone.utc)), actor='applicant' if own else 'hiring_team'))
    cur.execute('UPDATE talent_applications SET status=%s,revision=revision+1,history_json=%s,updated_at=CURRENT_TIMESTAMP WHERE company_id=%s AND id=%s',
                (target, dump(history), company_id, application_id))


def confirm_placement(cur, company_id, application_id):
    cur.execute("SELECT * FROM talent_applications WHERE company_id=%s AND id=%s AND status='accepted' FOR UPDATE", (company_id, application_id))
    app = cur.fetchone()
    if not app or app['placement_confirmed']:
        raise ValueError('Placeringen er ikke klar eller allerede bekræftet.')
    member(cur, company_id, app['user_id'])
    opp = opportunity(cur, company_id, app['opportunity_id'])
    if opp['kind'] == 'role':
        assign_role(cur, company_id, app['user_id'], opp['role_version_id'])
    else:
        cur.execute('INSERT INTO talent_project_members (company_id,user_id,opportunity_id) VALUES (%s,%s,%s)', (company_id, app['user_id'], opp['id']))
    cur.execute('UPDATE talent_applications SET placement_confirmed=1 WHERE company_id=%s AND id=%s', (company_id, application_id))


def coverage(states, reqs, k):
    """Each cell is suppressed unless both it and its complement meet the privacy floor."""
    result = []
    for req in reqs:
        buckets = dict(assessed=0, provisional=0, unknown=0, below=0)
        for state in states:
            row = evaluate(state, [req])['requirements'][0]
            key = 'unknown' if row['status'] == 'unknown' else 'below' if row['status'] == 'below_target' else 'provisional' if row['provisional'] else 'assessed'
            buckets[key] += 1
        suppressed = len(states) < k or any(0 < n < k for n in buckets.values())
        result.append({**req, 'counts': None if suppressed else buckets, 'suppressed': suppressed,
                       'remaining': None if suppressed else max(0, req['required_people'] - buckets['assessed'])})
    return result


def save_scenario(cur, company_id, data):
    reqs = requirements(data['requirements'])
    if not data['title'].strip() or date(data['target_date']) < dt.date.today():
        raise ValueError('Angiv titel og en fremtidig måldato.')
    cur.execute('INSERT INTO talent_scenarios (id,company_id,title,department,target_date,requirements_json,actions_json) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (uid(), company_id, data['title'][:255], data.get('department','')[:100], date(data['target_date']), dump(reqs), dump(data.get('actions', []))))


def publish_scenario(cur, company_id, scenario_id):
    cur.execute('SELECT * FROM talent_scenarios WHERE company_id=%s AND id=%s FOR UPDATE', (company_id, scenario_id))
    row = cur.fetchone()
    if not row or row['status'] != 'draft':
        raise ValueError('Scenariet findes ikke som kladde.')
    for req in json.loads(row['requirements_json']):
        cur.execute('SELECT id FROM company_skill_targets WHERE company_id=%s AND COALESCE(department,\'\')=%s AND skill_name=%s FOR UPDATE',
                    (company_id, row['department'] or '', req['skill']))
        target = cur.fetchone()
        if target:
            cur.execute('UPDATE company_skill_targets SET target_level=%s WHERE company_id=%s AND id=%s', (req['target_level'], company_id, target['id']))
        else:
            cur.execute('INSERT INTO company_skill_targets (company_id,department,skill_name,target_level,priority) VALUES (%s,%s,%s,%s,%s)',
                        (company_id, row['department'] or '', req['skill'], req['target_level'], 'medium'))
    cur.execute("UPDATE talent_scenarios SET status='published' WHERE company_id=%s AND id=%s", (company_id, scenario_id))


def cohort_states(cur, company_id, people):
    """Batch planning reads: no biographies, ambitions or private goal data are loaded."""
    ids = {p['id'] for p in people}
    profiles = {user_id: {'skills': []} for user_id in ids}
    matrix = {user_id: [] for user_id in ids}
    cur.execute("SELECT u.id,us.skill_name,us.skill_level,us.numeric_level,us.source FROM user_skills us "
                "JOIN users u ON u.username=us.username JOIN company_users cu ON cu.user_id=u.id "
                "WHERE cu.company_id=%s AND cu.status='active'", (company_id,))
    for row in cur.fetchall():
        if row['id'] in ids:
            profiles[row['id']]['skills'].append(row)
    cur.execute("SELECT esm.employee_id,esm.skill_name,esm.current_level,esm.updated_at FROM employee_skills_matrix esm "
                "JOIN company_users cu ON cu.user_id=esm.employee_id AND cu.company_id=esm.company_id "
                "WHERE esm.company_id=%s AND cu.status='active'", (company_id,))
    for row in cur.fetchall():
        if row['employee_id'] in ids:
            matrix[row['employee_id']].append(row)
    return [snapshot(profiles[user_id], matrix[user_id]) for user_id in ids]
