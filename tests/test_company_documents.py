"""Company documents for the assistants: one company's text never reaches another,
passages are fenced as data and carry their document title. SQLite, no network."""
import io
import json
import re
from unittest import mock

import pytest

import company_documents as docs
from schema_registry import DOCUMENTS_DDL
from tests.sqlite_platform import PlatformDB, make_app

HANDBOOK = (
    "Personalehåndbog for Firma A.\n\n"
    "Barsel: Medarbejdere har ret til fuld løn i 24 uger under barsel. Kontakt HR senest tre måneder før.\n\n"
    "Uddannelse: Alle medarbejdere har et årligt uddannelsesbudget på 15.000 kr. Kurser over beløbet "
    "kræver godkendelse fra nærmeste leder og HR.\n\n"
    "Hjemmearbejde: Du kan arbejde hjemme to dage om ugen efter aftale med din leder.\n"
)
OTHER = "Håndbog for Firma B.\n\nBarsel: Hos Firma B er der fuld løn i 10 uger under barsel. Hemmelig kode: ZEBRA-9.\n" * 2


@pytest.fixture
def env():
    db = PlatformDB()
    for ddl in DOCUMENTS_DDL:
        ddl = re.sub(r'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', '', ddl)
        ddl = re.sub(r',\s*INDEX \w+\s*\([^)]*\)', '', ddl)
        ddl = ddl.replace('BIGINT AUTO_INCREMENT PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT').replace('MEDIUMTEXT', 'TEXT')
        db.raw.execute(ddl)
    for user_id, username, cid, role in ((1, 'ada', 7, 'employee'), (3, 'hr', 7, 'company_admin'), (4, 'other-hr', 8, 'company_admin')):
        db.execute('INSERT INTO users (id,username) VALUES (%s,%s)', (user_id, username))
        db.execute("INSERT INTO company_users (company_id,user_id,username,role) VALUES (%s,%s,%s,%s)", (cid, user_id, username, role))
    for cid in (7, 8):
        db.execute("INSERT INTO companies (id,company_name) VALUES (%s,'Firma')", (cid,))
    cur = db.connection.cursor()
    docs.add_document(cur, 7, 3, 'Personalehåndbog', 'haandbog.txt', HANDBOOK.encode())
    docs.add_document(cur, 8, 4, 'Håndbog B', 'b.md', OTHER.encode())
    db.connection.commit()
    app = make_app(db)
    with mock.patch('security_audit.audit', return_value=True), \
            mock.patch('white_label_global_integration.get_template_context', return_value={}):
        yield db, app


def test_search_finds_the_right_passage_and_only_in_the_own_company(env):
    db, app = env
    cur = db.connection.cursor()
    hits = docs.search(cur, 7, 'Hvor mange uger er der fuld løn under barsel?')
    assert hits and hits[0]['title'] == 'Personalehåndbog' and '24 uger' in hits[0]['content']
    assert all('ZEBRA' not in h['content'] and 'Firma B' not in h['content'] for h in hits)
    assert '10 uger' in docs.search(cur, 8, 'barsel fuld løn')[0]['content']
    assert docs.search(cur, 9, 'barsel') == [] and docs.search(cur, None, 'barsel') == []
    assert docs.search(cur, 7, 'kvantecomputer') == []


def test_long_text_is_split_into_overlapping_passages():
    text = ' '.join('Sætning nummer %d handler om emne %d.' % (i, i) for i in range(400))
    parts = docs.split_passages(text)
    assert len(parts) > 5 and all(len(p) <= docs.CHUNK_CHARS for p in parts)
    assert 'nummer 399' in parts[-1] and 'nummer 0 ' in parts[0]


def test_uploads_are_checked(env):
    db, app = env
    cur = db.connection.cursor()
    for name, data in (('x.exe', b'MZ' + b'a' * 200), ('x.pdf', b'not a pdf at all ' * 20),
                       ('x.txt', b'<html><script>alert(1)</script>' + b'a' * 200), ('x.txt', b'kort'),
                       ('x.txt', b'a' * (docs.MAX_BYTES + 1))):
        with pytest.raises(docs.DocumentError):
            docs.add_document(cur, 7, 3, '', name, data)
    assert len(docs.list_documents(cur, 7)) == 1


def test_removing_is_company_scoped(env):
    db, app = env
    cur = db.connection.cursor()
    theirs = docs.list_documents(cur, 8)[0]['id']
    assert docs.remove_document(cur, 7, theirs) is False
    assert len(docs.list_documents(cur, 8)) == 1
    mine = docs.list_documents(cur, 7)[0]['id']
    assert docs.remove_document(cur, 7, mine) is True
    assert docs.search(cur, 7, 'barsel') == []
    assert db.query('SELECT COUNT(*) AS n FROM company_document_chunks WHERE company_id=7')[0]['n'] == 0


def test_the_assistant_gets_fenced_passages_with_the_document_named(env):
    from app1 import tools
    db, app = env
    with app.test_request_context():
        from flask import session
        session.update(user='ada', user_id=1, company_id=7, company_role='employee')
        out = json.loads(tools._execute_search_company_documents({'query': 'hvad er uddannelsesbudgettet?'}, 'ada'))
        nothing = json.loads(tools._execute_search_company_documents({'query': 'firmabil leasing'}, 'ada'))
    assert out['status'] == 'company_documents' and out['passages'][0]['document'] == 'Personalehåndbog'
    text = out['passages'][0]['text']
    assert '15.000 kr' in text and 'VIRKSOMHEDSDOKUMENT: Personalehåndbog' in text
    assert 'ZEBRA' not in json.dumps(out, ensure_ascii=False)
    assert nothing['status'] == 'no_match' and 'HR' in nothing['message']


def test_a_person_without_a_company_gets_no_documents(env):
    from app1 import tools
    db, app = env
    with app.test_request_context():
        from flask import session
        session.update(user='solo')
        out = json.loads(tools._execute_search_company_documents({'query': 'barsel'}, 'solo'))
    assert out['status'] == 'no_company'


def test_settings_tab_uploads_lists_and_removes_and_employees_are_refused(env):
    db, app = env
    hr = app.test_client()
    with hr.session_transaction() as s:
        s.update(user='hr', user_id=3, company_id=7, company_role='company_admin')
    page = hr.get('/virksomhed/indstillinger/dokumenter')
    assert page.status_code == 200 and 'Personalehåndbog' in page.get_data(as_text=True)
    assert 'Håndbog B' not in page.get_data(as_text=True)
    upload = hr.post('/virksomhed/indstillinger/dokumenter/upload', content_type='multipart/form-data',
                     data={'title': 'Rejsepolitik', 'file': (io.BytesIO(('Rejser bestilles via rejsebureauet. ' * 5).encode()), 'rejse.txt')})
    assert upload.status_code == 302
    assert {d['title'] for d in docs.list_documents(db.connection.cursor(), 7)} == {'Personalehåndbog', 'Rejsepolitik'}
    theirs = docs.list_documents(db.connection.cursor(), 8)[0]['id']
    hr.post('/virksomhed/indstillinger/dokumenter/%s/fjern' % theirs)
    assert len(docs.list_documents(db.connection.cursor(), 8)) == 1

    ada = app.test_client()
    with ada.session_transaction() as s:
        s.update(user='ada', user_id=1, company_id=7, company_role='employee')
    ada.post('/virksomhed/indstillinger/dokumenter/upload', content_type='multipart/form-data',
             data={'file': (io.BytesIO(b'Uautoriseret tekst der er lang nok til at blive gemt.'), 'x.txt')})
    assert len(docs.list_documents(db.connection.cursor(), 7)) == 2


def test_selector_offers_documents_on_policy_questions_only():
    import ai_tool_registry as registry

    def names(query, company_id=7):
        tools, _ = registry.get_employee_tool_selection(logged_in=True, company_id=company_id, intent='general', user_query=query)
        return {t['function']['name'] for t in tools}
    assert 'search_company_documents' in names('hvad står der i personalehåndbogen om barsel?')
    assert 'search_company_documents' not in names('find et kursus i excel')
    assert 'search_company_documents' not in names('hvad gælder for barsel hos os?', company_id=None)
    hr_tools, _ = registry.get_hr_tool_selection(company_id=7, user_query='hvad siger vores uddannelsespolitik?')
    assert 'hr_search_company_documents' in {t['function']['name'] for t in hr_tools}


def test_danish_inflections_and_compounds_still_match(env):
    db, app = env
    cur = db.connection.cursor()
    assert docs.same_word('budgettet', 'budget') and docs.same_word('hjemmearbejdsdage', 'hjemmearbejde')
    assert not docs.same_word('kursus', 'kursist') and not docs.same_word('ferie', 'fest')
    for question in ('Hvad er uddannelsesbudgettet?', 'hvor mange hjemmearbejdsdage har jeg', 'barselsorlov løn'):
        hits = docs.search(cur, 7, question)
        assert hits and hits[0]['title'] == 'Personalehåndbog', question


def _docx(paragraphs):
    import zipfile
    body = "".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % p for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s</w:body></w:document>' % body)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("[Content_Types].xml", "<Types/>")
    return buffer.getvalue()


def test_word_documents_are_read_and_bad_ones_refused(env):
    db, app = env
    cur = db.connection.cursor()
    data = _docx(["Rejsepolitik for Firma A.", "Flyrejser over fire timer må bestilles på business class efter godkendelse fra direktionen.",
                  "Hotel bestilles via rejsebureauet."])
    docs.add_document(cur, 7, 3, "Rejsepolitik", "rejsepolitik.docx", data)
    hit = docs.search(cur, 7, "må jeg flyve business class?")[0]
    assert hit["title"] == "Rejsepolitik" and "business class" in hit["content"]
    for bad in (b"PK" + b"\\x00" * 200, b"ikke en zip-fil" * 20):
        with pytest.raises(docs.DocumentError):
            docs.add_document(cur, 7, 3, "", "x.docx", bad)
    # An entity bomb in the XML is refused by the parser, not expanded.
    import zipfile
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w") as archive:
        archive.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>')
    with pytest.raises(docs.DocumentError):
        docs.add_document(cur, 7, 3, "", "bomb.docx", bomb.getvalue())
