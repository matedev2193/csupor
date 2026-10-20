from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re

import pytest
from flask import g, session, template_rendered
from pypdf import PdfWriter

from safety import create_app
from safety.models import Attempt, Course, Member, Progress, ReadingSession, db, now


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import safety.auth as auth

    def load_test_member():
        g.member = db.session.get(Member, session['test_member']) if session.get('test_member') else None
        if g.member and not g.member.active:
            g.member = None

    monkeypatch.setattr(auth, 'load_member', load_test_member)
    application = create_app({
        'TESTING': True, 'SECRET_KEY': 'training-tests-' + 'x' * 40,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(tmp_path / 'training.sqlite'),
        'SAFETY_UPLOAD_DIR': str(tmp_path / 'pdfs'),
        'SAFETY_OIDC_ISSUER': 'http://localhost:9001',
        'SAFETY_OIDC_REDIRECT_URI': 'http://localhost/auth/callback',
        'SAFETY_OIDC_CLIENT_SECRET': 'test-client-' + 'x' * 40,
        'SAFETY_ALLOW_HTTP': True,
    })
    with application.app_context():
        db.create_all()
        db.session.add_all([
            Member(issuer='http://localhost:9001', subject='admin', name='Admin', email='admin@example.hu', role='admin'),
            Member(issuer='http://localhost:9001', subject='worker', name='Dolgozó', email='worker@example.hu'),
            Member(issuer='http://localhost:9001', subject='not-started', name='=CSV teszt', email='notstarted@example.hu'),
        ])
        db.session.commit()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


def client_as(app, member_id):
    client = app.test_client()
    with client.session_transaction() as browser:
        browser['test_member'] = member_id
        browser['safety_csrf'] = 'csrf-token'
    return client


def post(client, path, data=None, json_data=None):
    if json_data is not None:
        return client.post(path, json=json_data, headers={'X-CSRF-Token': 'csrf-token'})
    return client.post(path, data={'csrf_token': 'csrf-token', **(data or {})})


def pdf_bytes(pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    result = BytesIO()
    writer.write(result)
    result.seek(0)
    return result


def make_course(app, admin, *, mode='accept', year=2026, kind='fire', publish=True):
    questions = [{'prompt': 'Melyik helyes?', 'options': ['Helyes válasz', 'Másik'], 'correct_index': 0}]
    response = post(admin, '/admin/courses/new', {
        'year': str(year), 'kind': kind, 'title': f'Éves {kind} tananyag', 'mode': mode,
        'passing_percent': '80', 'questions_json': json.dumps(questions), 'pdf': (pdf_bytes(), 'tananyag.pdf'),
    })
    assert response.status_code == 302
    course_id = int(re.search(r'/courses/(\d+)/edit', response.location).group(1))
    if publish:
        assert post(admin, f'/admin/courses/{course_id}/publish').status_code == 302
    return course_id


@contextmanager
def captured(app):
    contexts = []
    def receiver(sender, template, context, **extra):
        contexts.append((template.name, context))
    template_rendered.connect(receiver, app)
    try:
        yield contexts
    finally:
        template_rendered.disconnect(receiver, app)


def open_reader(app, client, course_id):
    with captured(app) as contexts:
        assert client.get(f'/courses/{course_id}').status_code == 200
    return contexts[-1][1]['reading_token']


def age_session(app, token, seconds=15):
    with app.app_context():
        reading = db.session.get(ReadingSession, sha256(token.encode()).hexdigest())
        reading.last_ping_at = now() - timedelta(seconds=seconds)
        db.session.commit()


def ping(client, course_id, token, sequence, page, seconds=0):
    return post(client, f'/courses/{course_id}/heartbeat', json_data={
        'token': token, 'sequence': sequence, 'page': page, 'active_seconds': seconds,
    })


def visit_all(app, client, course_id):
    token = open_reader(app, client, course_id)
    assert ping(client, course_id, token, 1, 1).status_code == 200
    assert ping(client, course_id, token, 2, 2).status_code == 200
    return token


def test_acceptance_and_annual_versions_preserve_history(app):
    admin, employee = client_as(app, 1), client_as(app, 2)
    course_id = make_course(app, admin)
    assert post(employee, f'/courses/{course_id}/accept', {'accepted': '1'}).status_code == 409
    visit_all(app, employee, course_id)
    assert post(employee, f'/courses/{course_id}/accept').status_code == 400
    assert post(employee, f'/courses/{course_id}/accept', {'accepted': '1'}).status_code == 302
    with app.app_context():
        original = Progress.query.filter_by(member_id=2, course_id=course_id).one().completed_at
    assert post(employee, f'/courses/{course_id}/accept', {'accepted': '1'}).status_code == 302
    assert post(admin, f'/admin/courses/{course_id}/edit', {}).status_code == 409
    replacement = make_course(app, admin)
    with app.app_context():
        assert db.session.get(Course, course_id).status == 'archived'
        assert db.session.get(Course, replacement).revision == 2
        old = Progress.query.filter_by(member_id=2, course_id=course_id).one()
        assert old.completed_at == original and old.accepted_text
        assert Progress.query.filter_by(member_id=2, course_id=replacement).first() is None
    assert employee.get(f'/courses/{course_id}/pdf').status_code == 404
    assert admin.get(f'/courses/{course_id}/pdf').status_code == 200
    next_year = make_course(app, admin, year=2027)
    assert employee.get(f'/courses/{next_year}').status_code == 200
    with app.app_context():
        assert Progress.query.filter_by(member_id=2, course_id=next_year).one().completed_at is None


def test_heartbeat_server_caps_replay_idle_and_multiple_tabs(app):
    admin, employee = client_as(app, 1), client_as(app, 2)
    course_id = make_course(app, admin)
    token = open_reader(app, employee, course_id)
    age_session(app, token, seconds=15)
    response = ping(employee, course_id, token, 1, 1, 100000)
    assert response.json == {'ok': True}
    assert ping(employee, course_id, token, 1, 2, 100000).status_code == 200
    with app.app_context():
        p = Progress.query.filter_by(member_id=2, course_id=course_id).one()
        assert p.active_seconds == 15 and p.last_page == 1 and p.visited_pages == [1]
    age_session(app, token, seconds=120)
    assert ping(employee, course_id, token, 2, 2, 120).status_code == 200
    with app.app_context():
        assert Progress.query.filter_by(member_id=2, course_id=course_id).one().active_seconds == 15
    newer = open_reader(app, employee, course_id)
    assert newer != token
    assert ping(employee, course_id, token, 3, 1, 15).status_code == 409
    assert ping(employee, course_id, newer, 1, 99, 15).status_code == 400
    other = client_as(app, 3)
    assert ping(other, course_id, newer, 1, 1, 15).status_code == 409


def test_exam_is_server_graded_without_answer_key_or_telemetry(app):
    admin, employee = client_as(app, 1), client_as(app, 2)
    course_id = make_course(app, admin, mode='exam')
    assert employee.get(f'/courses/{course_id}/exam').status_code == 409
    visit_all(app, employee, course_id)
    with captured(app) as contexts:
        response = employee.get(f'/courses/{course_id}/exam')
    assert response.status_code == 200
    questions = contexts[-1][1]['questions']
    assert set(questions[0]) == {'id', 'prompt', 'options'}
    assert 'correct_index' not in response.get_data(as_text=True)
    question_id = questions[0]['id']
    assert post(employee, f'/courses/{course_id}/exam', {f'answer_{question_id}': '9'}).status_code == 400
    assert post(employee, f'/courses/{course_id}/accept', {'accepted': '1'}).status_code == 400
    with captured(app) as contexts:
        assert post(employee, f'/courses/{course_id}/exam', {f'answer_{question_id}': '1'}).status_code == 200
    assert contexts[-1][1]['result'] == {'passed': False, 'score': 0, 'total': 1}
    with captured(app) as contexts:
        assert post(employee, f'/courses/{course_id}/exam', {f'answer_{question_id}': '0'}).status_code == 200
    assert contexts[-1][1]['result']['passed'] is True
    assert post(employee, f'/courses/{course_id}/exam', {f'answer_{question_id}': '0'}).status_code == 302
    with app.app_context():
        assert Attempt.query.count() == 2
        p = Progress.query.filter_by(member_id=2, course_id=course_id).one()
        assert p.completed_at and p.completion_kind == 'exam'
    with captured(app) as contexts:
        assert employee.get('/?year=2026').status_code == 200
    assert set(vars(contexts[-1][1]['progress_by_course'][course_id])) == {'completed_at'}


def test_admin_boundaries_csrf_no_start_export_and_private_files(app):
    admin, employee = client_as(app, 1), client_as(app, 2)
    course_id = make_course(app, admin)
    for path in ('/admin', '/admin/members', '/admin/export.csv', f'/admin/courses/{course_id}/edit'):
        assert employee.get(path).status_code == 403
    assert post(employee, '/admin/courses/new').status_code == 403
    assert admin.post(f'/admin/courses/{course_id}/archive').status_code == 400
    assert app.test_client().get(f'/courses/{course_id}/pdf').status_code == 302
    response = admin.get('/admin/export.csv?year=2026')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "'=CSV teszt" in body and 'nem kezdte el' in body
    assert response.headers['Cache-Control'] == 'no-store'
    with app.app_context():
        key = db.session.get(Course, course_id).pdf_key
    assert employee.get('/static/' + key).status_code == 404
    assert post(admin, '/admin/members/2/status', {'active': 'false'}).status_code == 302
    assert employee.get(f'/courses/{course_id}/pdf').status_code == 302


def test_pdf_and_quiz_validation_keep_drafts_and_files_consistent(app):
    admin = client_as(app, 1)
    base = {'year': '2026', 'kind': 'work', 'title': 'Munka', 'mode': 'exam', 'passing_percent': '80', 'questions_json': '[]'}
    response = post(admin, '/admin/courses/new', {**base, 'pdf': (BytesIO(b'not a pdf'), 'fake.pdf')})
    assert response.status_code == 200
    with app.app_context():
        assert Course.query.count() == 0
    assert not list(Path(app.config['SAFETY_UPLOAD_DIR']).iterdir())
    response = post(admin, '/admin/courses/new', {**base, 'pdf': (pdf_bytes(), 'valid.pdf')})
    assert response.status_code == 302
    course_id = int(re.search(r'/courses/(\d+)/edit', response.location).group(1))
    assert post(admin, f'/admin/courses/{course_id}/publish').status_code == 400
    assert post(admin, f'/admin/courses/{course_id}/edit', {**base, 'questions_json': '[{"prompt":"Q","options":["A","B"],"correct_index":true}]'}).status_code == 200
    with app.app_context():
        assert not db.session.get(Course, course_id).questions
        old_key = db.session.get(Course, course_id).pdf_key
    assert post(admin, f'/admin/courses/{course_id}/edit', {**base, 'mode': 'accept', 'pdf': (pdf_bytes(3), 'new.pdf')}).status_code == 302
    with app.app_context():
        assert db.session.get(Course, course_id).page_count == 3
    assert not (Path(app.config['SAFETY_UPLOAD_DIR']) / old_key).exists()


def test_future_draft_does_not_hide_published_year(app):
    admin, employee = client_as(app, 1), client_as(app, 2)
    published = make_course(app, admin, year=2026)
    make_course(app, admin, year=2027, publish=False)
    with captured(app) as contexts:
        assert employee.get('/').status_code == 200
    assert contexts[-1][1]['selected_year'] == 2026
    assert [c.id for c in contexts[-1][1]['courses']] == [published]


def test_report_retains_history_without_inventing_old_assignments(app):
    admin, employee = client_as(app, 1), client_as(app, 2)
    old_id = make_course(app, admin)
    visit_all(app, employee, old_id)
    latest_id = make_course(app, admin)
    with captured(app) as contexts:
        assert admin.get('/admin?year=2026').status_code == 200
    rows = contexts[-1][1]['rows']
    assert {(row['course'].id, row['member'].id) for row in rows if row['course'].id == old_id} == {(old_id, 2)}
    assert {(row['course'].id, row['member'].id) for row in rows if row['course'].id == latest_id} == {(latest_id, 1), (latest_id, 2), (latest_id, 3)}
    assert post(admin, '/admin/members/3/status', {'active': 'false'}).status_code == 302
    with captured(app) as contexts:
        assert admin.get('/admin?year=2026').status_code == 200
    assert not any(row['member'].id == 3 for row in contexts[-1][1]['rows'])
