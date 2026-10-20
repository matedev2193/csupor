"""Employee learning and administrator training management."""
import csv
from datetime import datetime, timedelta
from hashlib import sha256
from io import StringIO
import math
from secrets import token_urlsafe
from types import SimpleNamespace

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .auth import admin_required, login_required
from .models import Attempt, Course, Member, Progress, Question, ReadingSession, db, now
from .services import (ACCEPTANCE, KINDS, all_pages_visited, csv_safe, discard_pdf, locked_course,
                       pdf_path, progress_for, public_questions, store_pdf, validate_course_form, validate_questions)

web = Blueprint('web', __name__)


def _year_filter(query):
    years = [row[0] for row in query.with_entities(Course.year).distinct().order_by(Course.year.desc()).all()]
    selected = request.args.get('year', type=int)
    if selected is None:
        selected = years[0] if years else datetime.now().year
    return query.filter(Course.year == selected), years, selected


def _published_course(course_id):
    course = db.session.get(Course, course_id)
    if course is None or course.status != 'published':
        abort(404)
    return course


@web.get('/')
@login_required
def index():
    query, years, year = _year_filter(Course.query.filter_by(status='published'))
    courses = query.order_by(Course.kind).all()
    # A deliberately small presentation object; no telemetry enters this view.
    progress = {p.course_id: SimpleNamespace(completed_at=p.completed_at) for p in
                Progress.query.filter(Progress.member_id == g.member.id,
                                      Progress.course_id.in_([c.id for c in courses])).all()}
    return render_template('index.html', courses=courses, progress_by_course=progress, years=years,
                           selected_year=year)


@web.get('/privacy')
def privacy():
    return render_template('privacy.html')


@web.get('/courses/<int:course_id>')
@login_required
def reader(course_id):
    course = locked_course(course_id, published=True)
    progress = progress_for(course.id, g.member.id, create=True)
    db.session.execute(delete(ReadingSession).where(ReadingSession.progress_id == progress.id))
    token = token_urlsafe(32)
    page = max(1, min(progress.last_page or 1, course.page_count))
    db.session.add(ReadingSession(id_hash=sha256(token.encode()).hexdigest(), progress_id=progress.id,
                                  last_ping_at=now(), expires_at=now() + timedelta(hours=8), page=page))
    db.session.commit()
    return render_template('reader.html', course=course, start_page=page, reading_token=token,
                           completed_at=progress.completed_at, acceptance_text=ACCEPTANCE)


@web.get('/courses/<int:course_id>/pdf')
@login_required
def pdf(course_id):
    course = db.session.get(Course, course_id)
    if course is None or (course.status != 'published' and g.member.role != 'admin'):
        abort(404)
    path = pdf_path(course.pdf_key)
    if not path.is_file():
        abort(404)
    response = send_file(path, mimetype='application/pdf', as_attachment=False,
                         download_name=course.pdf_name or 'tananyag.pdf', conditional=True)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@web.post('/courses/<int:course_id>/heartbeat')
@login_required
def heartbeat(course_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    token, sequence, page, seconds = (payload.get(k) for k in ('token', 'sequence', 'page', 'active_seconds'))
    if (not isinstance(token, str) or len(token) != 43 or type(sequence) is not int
            or not 1 <= sequence <= 2**31 - 1 or type(page) is not int
            or type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0):
        abort(400)
    course = locked_course(course_id, published=True)
    if not 1 <= page <= course.page_count:
        abort(400)
    progress = progress_for(course.id, g.member.id)
    reading = db.session.get(ReadingSession, sha256(token.encode()).hexdigest())
    current = now()
    if progress is None or reading is None or reading.progress_id != progress.id or reading.expires_at <= current:
        abort(409, description='Az olvasási munkamenet lejárt vagy másik lapon folytatódik. Töltsd újra az oldalt.')
    if sequence <= reading.sequence:
        db.session.rollback()
        return {'ok': True}
    elapsed = max(0, (current - reading.last_ping_at).total_seconds())
    # Client activity is an estimate, capped by server time. A long gap never
    # accrues offline/hidden-tab reading, and duplicate sequences cannot add time.
    credit = int(min(seconds, elapsed, 20)) if elapsed <= 45 else 0
    reading.sequence, reading.page, reading.last_ping_at = sequence, page, current
    progress.last_page = page
    progress.furthest_page = max(progress.furthest_page, page)
    progress.visited_pages = sorted(set(progress.visited_pages or []) | {page})
    progress.last_seen_at = current
    progress.active_seconds += credit
    db.session.commit()
    return {'ok': True}


def _completion_progress(course_id, mode):
    course = locked_course(course_id, published=True)
    if course.mode != mode:
        abort(400, description='Ehhez a tananyaghoz más teljesítési mód tartozik.')
    progress = progress_for(course.id, g.member.id)
    if not all_pages_visited(progress, course):
        abort(409, description='Előbb nyisd meg a tananyag minden oldalát, és várd meg a mentést.')
    return course, progress


@web.post('/courses/<int:course_id>/accept')
@login_required
def accept(course_id):
    course, progress = _completion_progress(course_id, 'accept')
    if request.form.get('accepted') != '1':
        abort(400, description='Az elfogadáshoz jelöld be a nyilatkozatot.')
    if progress.completed_at is None:
        progress.completed_at, progress.completion_kind, progress.accepted_text = now(), 'accept', ACCEPTANCE
    db.session.commit()
    flash('Az elfogadást rögzítettük. A tananyag teljesítve.', 'success')
    return redirect(url_for('web.index', year=course.year))


@web.route('/courses/<int:course_id>/exam', methods=['GET', 'POST'])
@login_required
def exam(course_id):
    course, progress = _completion_progress(course_id, 'exam')
    if progress.completed_at is not None:
        db.session.rollback()
        flash('Ezt a tananyagot már sikeresen teljesítetted.', 'success')
        return redirect(url_for('web.index', year=course.year))
    result = None
    if request.method == 'POST':
        answers = {}
        for question in course.questions:
            answer = request.form.get(f'answer_{question.id}')
            try:
                chosen = int(answer)
            except (ValueError, TypeError):
                abort(400, description='Minden kérdéshez válassz egy választ.')
            if not 0 <= chosen < len(question.options):
                abort(400, description='Érvénytelen válaszlehetőség.')
            answers[str(question.id)] = chosen
        if not course.questions:
            abort(409, description='A vizsga nem tartalmaz kérdést.')
        score = sum(answers[str(q.id)] == q.correct_index for q in course.questions)
        total = len(course.questions)
        passed = score * 100 >= course.passing_percent * total
        db.session.add(Attempt(progress_id=progress.id, score=score, total=total, passed=passed, answers=answers))
        if passed:
            progress.completed_at, progress.completion_kind = now(), 'exam'
        result = {'passed': passed, 'score': score, 'total': total}
        db.session.commit()
    else:
        db.session.commit()
    return render_template('exam.html', course=course, questions=public_questions(course), result=result,
                           completed_at=progress.completed_at)


def _report():
    query, years, year = _year_filter(Course.query)
    courses = query.order_by(Course.kind, Course.revision.desc()).all()
    members = Member.query.order_by(Member.name, Member.id).all()
    progresses = Progress.query.filter(Progress.course_id.in_([c.id for c in courses])).all()
    progress_map = {(p.course_id, p.member_id): p for p in progresses}
    attempts = Attempt.query.filter(Attempt.progress_id.in_([p.id for p in progresses])).order_by(Attempt.created_at, Attempt.id).all()
    attempt_map = {}
    for attempt in attempts:
        attempt_map.setdefault(attempt.progress_id, []).append(attempt)
    rows = []
    for course in courses:
        if course.status == 'draft':
            continue
        for member in members:
            progress = progress_map.get((course.id, member.id))
            # Historical rows represent actual records. A new colleague was not
            # assigned an archived revision merely because they joined later.
            if progress is None and (course.status != 'published' or not member.active):
                continue
            rows.append({'course': course, 'member': member, 'progress': progress,
                         'attempts': attempt_map.get(progress.id, []) if progress else []})
    return dict(courses=courses, members=members, rows=rows, years=years, selected_year=year)


@web.get('/admin')
@admin_required
def admin():
    return render_template('admin.html', **_report())


def _admin_questions(course):
    return [{'prompt': q.prompt, 'options': q.options, 'correct_index': q.correct_index} for q in course.questions]


def _form_questions(course=None):
    if request.method == 'POST':
        try:
            return validate_questions(request.form.get('questions_json', '[]'))
        except ValueError:
            pass
    return _admin_questions(course) if course else []


def _save_course(course=None):
    uploaded = None
    old_key = None
    try:
        values = validate_course_form(request.form)
        questions = validate_questions(request.form.get('questions_json', '[]'))
        if course is not None and (values['year'] != course.year or values['kind'] != course.kind):
            raise ValueError('A meglévő tananyag éve és típusa nem módosítható. Hozz létre új tananyagot.')
        uploaded = store_pdf(request.files.get('pdf'))
        if course is None:
            revision = (db.session.query(func.max(Course.revision)).filter_by(year=values['year'], kind=values['kind']).scalar() or 0) + 1
            course = Course(**values, revision=revision)
            db.session.add(course)
        else:
            for key, value in values.items():
                setattr(course, key, value)
        if uploaded:
            old_key = course.pdf_key
            for key, value in uploaded.items():
                setattr(course, key, value)
        course.questions = [Question(position=i, **item) for i, item in enumerate(questions)]
        db.session.commit()
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        if uploaded:
            discard_pdf(uploaded['pdf_key'])
        flash(str(exc) if isinstance(exc, ValueError) else 'Közben változott a tananyag. Frissítsd az oldalt és próbáld újra.', 'error')
        return None
    except Exception:
        db.session.rollback()
        if uploaded:
            discard_pdf(uploaded['pdf_key'])
        raise
    if old_key and old_key != course.pdf_key:
        discard_pdf(old_key)
    flash('A piszkozatot mentettük. Közzététel után elérhető a kollégáknak.', 'success')
    return course


@web.route('/admin/courses/new', methods=['GET', 'POST'])
@admin_required
def course_new():
    if request.method == 'POST':
        saved = _save_course()
        if saved:
            return redirect(url_for('web.course_edit', course_id=saved.id))
    selected_year = request.args.get('year', type=int) or datetime.now().year
    if not 2000 <= selected_year <= 2100:
        selected_year = datetime.now().year
    return render_template('course_form.html', course=None, questions=_form_questions(),
                           selected_year=selected_year, form_data=request.form if request.method == 'POST' else {})


@web.route('/admin/courses/<int:course_id>/edit', methods=['GET', 'POST'])
@admin_required
def course_edit(course_id):
    course = locked_course(course_id) if request.method == 'POST' else db.get_or_404(Course, course_id)
    if course.status != 'draft':
        abort(409, description='A közzétett tananyag megőrzött változat. Módosításhoz hozz létre új változatot ugyanarra az évre és típusra.')
    if request.method == 'POST':
        saved = _save_course(course)
        if saved:
            return redirect(url_for('web.course_edit', course_id=saved.id))
    return render_template('course_form.html', course=course, questions=_form_questions(course),
                           selected_year=course.year, form_data=request.form if request.method == 'POST' else {})


@web.post('/admin/courses/<int:course_id>/publish')
@admin_required
def course_publish(course_id):
    # A stable first-course lock serialises competing publishers, including when
    # neither draft has a published predecessor yet. Acquire it before target.
    first_id = db.session.query(func.min(Course.id)).scalar()
    if first_id is not None:
        locked_course(first_id)
    course = locked_course(course_id)
    if course.status != 'draft':
        abort(409, description='Csak piszkozat tehető közzé.')
    if not course.pdf_key or course.page_count < 1 or not pdf_path(course.pdf_key).is_file():
        abort(400, description='A közzétételhez tölts fel érvényes PDF-et.')
    if course.mode == 'exam' and not course.questions:
        abort(400, description='A vizsgához legalább egy kérdés szükséges.')
    # Revisions and their progress stay intact. Only the current assignment is
    # replaced, and completion is never copied across changed material.
    Course.query.filter(Course.year == course.year, Course.kind == course.kind,
                        Course.status == 'published', Course.id != course.id).update({'status': 'archived'})
    course.status, course.published_at = 'published', now()
    db.session.commit()
    flash('A tananyag elérhető a kollégáknak. A korábbi változat eredményeit megőriztük.', 'success')
    return redirect(url_for('web.admin', year=course.year))


@web.post('/admin/courses/<int:course_id>/archive')
@admin_required
def course_archive(course_id):
    course = locked_course(course_id)
    if course.status != 'published':
        abort(409, description='Csak közzétett tananyag archiválható.')
    course.status = 'archived'
    db.session.commit()
    flash('A tananyagot archiváltuk, a teljesítési adatok megmaradtak.', 'success')
    return redirect(url_for('web.admin', year=course.year))


@web.route('/admin/members', methods=['GET', 'POST'])
@admin_required
def members():
    if request.method == 'POST':
        subject, name, email = (request.form.get(k, '').strip() for k in ('subject', 'name', 'email'))
        if (not 1 <= len(subject) <= 255 or not 1 <= len(name) <= 120 or len(email) > 254
                or any(ord(c) < 32 or ord(c) == 127 for c in subject + name + email)
                or email and ('@' not in email or ' ' in email)):
            flash('Adj meg érvényes fiókazonosítót, nevet és e-mail-címet.', 'error')
        else:
            member = Member(issuer=current_app.config['SAFETY_OIDC_ISSUER'], subject=subject, name=name,
                            email=email, role='employee', active=True)
            db.session.add(member)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash('Ez a fiók már szerepel a munkatársak között.', 'error')
            else:
                flash('A munkatársat felvettük. A közös fiókrendszerben is szükséges a hozzáférés engedélyezése.', 'success')
                return redirect(url_for('web.members'))
    return render_template('members.html', members=Member.query.order_by(Member.name, Member.id).all())


@web.post('/admin/members/<int:member_id>/status')
@admin_required
def member_status(member_id):
    member = db.get_or_404(Member, member_id)
    value = request.form.get('active')
    if value not in {'true', 'false'}:
        abort(400)
    if member.id == g.member.id and value == 'false':
        abort(400, description='Saját hozzáférésed itt nem kapcsolható ki.')
    member.active = value == 'true'
    db.session.commit()
    flash('A helyi hozzáférést módosítottuk. A korábbi eredmények megmaradtak.', 'success')
    return redirect(url_for('web.members'))


@web.get('/admin/export.csv')
@admin_required
def export():
    report = _report()
    output = StringIO(newline='')
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Név', 'E-mail', 'Aktív', 'Év', 'Típus', 'Változat', 'Tananyag', 'Tananyag állapota',
                     'Teljesítés', 'Utolsó oldal', 'Legtávolabbi oldal', 'Oldalak száma', 'Megnyitott oldalak',
                     'Aktív idő (mp)', 'Utolsó aktivitás (UTC)', 'Teljesítve (UTC)', 'Vizsgakísérletek',
                     'Utolsó pontszám', 'Utolsó maximum', 'Tananyag SHA256'])
    for row in report['rows']:
        c, m, p, attempts = row['course'], row['member'], row['progress'], row['attempts']
        last = attempts[-1] if attempts else None
        values = [m.name, m.email, 'igen' if m.active else 'nem', c.year, KINDS[c.kind], c.revision,
                  c.title, c.status, 'teljesítve' if p and p.completed_at else 'folyamatban' if p and p.last_page else 'nem kezdte el',
                  p.last_page if p else 0, p.furthest_page if p else 0, c.page_count,
                  len(p.visited_pages or []) if p else 0, p.active_seconds if p else 0,
                  p.last_seen_at.isoformat(' ', timespec='seconds') if p and p.last_seen_at else '',
                  p.completed_at.isoformat(' ', timespec='seconds') if p and p.completed_at else '',
                  len(attempts), last.score if last else '', last.total if last else '', c.pdf_sha256]
        writer.writerow([csv_safe(value) for value in values])
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="kepzesek-{report["selected_year"]}.csv"'})
