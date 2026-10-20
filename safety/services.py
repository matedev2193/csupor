"""Training validation and persistence, independent of HR and account databases."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from secrets import token_hex

from flask import abort, current_app
from pypdf import PdfReader
from sqlalchemy import select, update

from .models import Course, Member, Progress, db

KINDS = {'fire': 'Tűzvédelem', 'work': 'Munkavédelem'}
ACCEPTANCE = 'Elolvastam és megértettem a tananyagot, az abban foglaltakat elfogadom.'
MAX_PDF_BYTES = 25 * 1024 * 1024


def validate_questions(raw):
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        raise ValueError('A vizsga kérdései nem olvashatók. Ellenőrizd a szerkesztőt.') from None
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError('Legfeljebb 100 kérdés adható meg.')
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Minden kérdéshez szöveg és válaszlehetőségek szükségesek.')
        prompt, options, correct = item.get('prompt'), item.get('options'), item.get('correct_index')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise ValueError('A kérdés szövege 1–2000 karakter lehet.')
        if (not isinstance(options, list) or not 2 <= len(options) <= 6
                or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 500 for x in options)):
            raise ValueError('Kérdésenként 2–6 nem üres, legfeljebb 500 karakteres válasz szükséges.')
        if type(correct) is not int or not 0 <= correct < len(options):
            raise ValueError('Minden kérdésnél jelölj ki egy helyes választ.')
        options = [x.strip() for x in options]
        if len(set(options)) != len(options):
            raise ValueError('Egy kérdés válaszlehetőségei legyenek különbözőek.')
        result.append({'prompt': prompt.strip(), 'options': options, 'correct_index': correct})
    return result


def validate_course_form(form):
    try:
        year = int(form.get('year', ''))
        passing = int(form.get('passing_percent', '80'))
    except (TypeError, ValueError):
        raise ValueError('Az év és a sikerességi küszöb egész szám legyen.') from None
    title = form.get('title', '').strip()
    kind, mode = form.get('kind'), form.get('mode')
    if not 2000 <= year <= 2100:
        raise ValueError('Az év 2000 és 2100 közé essen.')
    if kind not in KINDS or mode not in {'accept', 'exam'}:
        raise ValueError('Válassz tananyagtípust és teljesítési módot.')
    if not 1 <= len(title) <= 160:
        raise ValueError('A cím 1–160 karakter lehet.')
    if not 1 <= passing <= 100:
        raise ValueError('A sikerességi küszöb 1–100% lehet.')
    return dict(year=year, kind=kind, mode=mode, title=title, passing_percent=passing)


def pdf_path(key):
    # No request path, original filename, or editable title is ever used here.
    if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{48}\.pdf', key):
        abort(404)
    return Path(current_app.config['SAFETY_UPLOAD_DIR']) / key


def store_pdf(upload):
    if not upload or not upload.filename:
        return None
    name = upload.filename.replace('\\', '/').rsplit('/', 1)[-1]
    if not name.lower().endswith('.pdf'):
        raise ValueError('A tananyagot PDF formátumban töltsd fel.')
    data = upload.stream.read(MAX_PDF_BYTES + 1)
    if not data or len(data) > MAX_PDF_BYTES:
        raise ValueError('A PDF legfeljebb 25 MB lehet.')
    if not data.startswith(b'%PDF-'):
        raise ValueError('A feltöltött fájl nem érvényes PDF.')
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError('A jelszóval védett PDF nem használható tananyagként.')
        count = len(reader.pages)
        if not 1 <= count <= 500:
            raise ValueError('A PDF 1–500 oldalas lehet.')
        # Parse each page before accepting the file rather than trusting metadata.
        for page in reader.pages:
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise ValueError('A PDF egyik oldalának mérete érvénytelen.')
    except ValueError as exc:
        if str(exc).startswith(('A jelszóval', 'A PDF')):
            raise
        raise ValueError('A PDF sérült vagy nem olvasható. Mentsd újra PDF-ként.') from None
    except Exception:
        raise ValueError('A PDF sérült vagy nem olvasható. Mentsd újra PDF-ként.') from None
    root = Path(current_app.config['SAFETY_UPLOAD_DIR'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = token_hex(24) + '.pdf'
    fd = os.open(root / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(data)
    name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:255] or 'tananyag.pdf'
    return dict(pdf_key=key, pdf_name=name, pdf_sha256=sha256(data).hexdigest(), page_count=count)


def discard_pdf(key):
    if key:
        try:
            pdf_path(key).unlink(missing_ok=True)
        except OSError:
            current_app.logger.warning('An unreferenced training PDF could not be removed.')


def locked_course(course_id, *, published=False):
    # This no-op write also serialises SQLite transactions, where FOR UPDATE is
    # otherwise ignored. MySQL acquires the same row lock. Course-first is the
    # common lock order for reader, heartbeat, completion, and admin writes.
    found = db.session.execute(update(Course).where(Course.id == course_id).values(id=Course.id))
    if found.rowcount != 1:
        abort(404)
    course = db.session.execute(select(Course).where(Course.id == course_id).execution_options(populate_existing=True)).scalar_one()
    if published and course.status != 'published':
        abort(404)
    return course


def progress_for(course_id, member_id, *, create=False):
    progress = db.session.execute(select(Progress).where(
        Progress.course_id == course_id, Progress.member_id == member_id,
    ).execution_options(populate_existing=True)).scalar_one_or_none()
    if progress is None and create:
        progress = Progress(course_id=course_id, member_id=member_id)
        db.session.add(progress)
        db.session.flush()
    return progress


def all_pages_visited(progress, course):
    return bool(progress) and set(progress.visited_pages or []) >= set(range(1, course.page_count + 1))


def public_questions(course):
    return [{'id': q.id, 'prompt': q.prompt, 'options': q.options} for q in course.questions]


def csv_safe(value):
    value = '' if value is None else str(value)
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value
