"""Annual blank GYAP documents and authenticated downloads."""

from datetime import date, datetime, timezone
from hmac import compare_digest
from io import BytesIO
from pathlib import PurePath
from secrets import token_urlsafe
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy.orm import undefer
from werkzeug.utils import secure_filename

from . import db
from .models import GyapForm
from .routes import privilege_manager_required


gyap = Blueprint("gyap", __name__)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_YEAR = 1970
MAX_YEAR = 2100
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _parse_year(raw):
    value = str(raw or "")
    if not value.isascii() or not value.isdigit() or len(value) != 4:
        return None
    year = int(value)
    return year if MIN_YEAR <= year <= MAX_YEAR else None


def _matches_document_type(data: bytes, extension: str) -> bool:
    """Check file signatures and the two defining parts of a DOCX package."""
    if extension == ".pdf":
        return data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]
    if extension == ".doc":
        return (
            len(data) >= 512
            and data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            and "WordDocument".encode("utf-16-le") in data
        )
    if extension != ".docx" or not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with ZipFile(BytesIO(data)) as package:
            # Bound decompression before parsing: no archive is ever extracted.
            parts = ("[Content_Types].xml", "word/document.xml")
            if any(package.getinfo(part).file_size > 2 * 1024 * 1024 for part in parts):
                return False
            content_types = ElementTree.fromstring(package.read(parts[0]))
            document = ElementTree.fromstring(package.read(parts[1]))
            word_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            content_namespace = "{http://schemas.openxmlformats.org/package/2006/content-types}"
            return (
                content_types.tag == f"{content_namespace}Types"
                and any(
                    node.get("PartName") == "/word/document.xml" and node.get("ContentType") == word_type
                    for node in content_types.findall(f"{content_namespace}Override")
                )
                and document.tag in {
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
                    "{http://purl.oclc.org/ooxml/wordprocessingml/main}document",
                }
            )
    except (BadZipFile, KeyError, RuntimeError, ValueError, ElementTree.ParseError, NotImplementedError, EOFError, ZlibError):
        return False


@gyap.route("/gyap-forms", methods=["GET", "POST"])
@privilege_manager_required
def manage_forms():
    selected_year = _parse_year(request.args.get("year")) or date.today().year
    if request.method == "POST":
        # Check declared size before Flask parses an oversized multipart body.
        if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 64 * 1024:
            flash(_("The GYAP form must be no larger than 10 MB."), "error")
            return redirect(url_for("gyap.manage_forms", year=selected_year))
        token = session.get("gyap_forms_csrf_token")
        if not token or not compare_digest(token.encode(), request.form.get("csrf_token", "").encode()):
            abort(400)
        year = _parse_year(request.form.get("calendar_year"))
        if year is None:
            flash(_("Choose a year between 1970 and 2100."), "error")
            return redirect(url_for("gyap.manage_forms", year=selected_year))

        document = request.files.get("document")
        filename = secure_filename(document.filename or "") if document else ""
        extension = PurePath(filename).suffix.lower()
        error = None
        data = b""
        if not filename:
            error = _("Choose a GYAP form to upload.")
        elif extension not in MIME_TYPES:
            error = _("Upload a PDF, DOC or DOCX document.")
        else:
            data = document.stream.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                error = _("The GYAP form must be no larger than 10 MB.")
            elif not data or not _matches_document_type(data, extension):
                error = _("The document is empty, damaged or does not match its file type.")
        if error:
            flash(error, "error")
            return redirect(url_for("gyap.manage_forms", year=year))

        # Validate completely before touching a previously uploaded document.
        form = db.session.get(GyapForm, year)
        if form is None:
            form = GyapForm(year=year)
            db.session.add(form)
        form.filename = filename[: 255 - len(extension)] + extension if len(filename) > 255 else filename
        form.mime_type = MIME_TYPES[extension]
        form.size_bytes = len(data)
        form.data = data
        form.uploaded_by_id = current_user.id
        form.uploaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        flash(_("GYAP form saved for %(year)s.", year=year), "success")
        return redirect(url_for("gyap.manage_forms", year=year))

    session.setdefault("gyap_forms_csrf_token", token_urlsafe(32))
    return render_template(
        "manage_gyap_forms.html",
        forms=GyapForm.query.order_by(GyapForm.year.desc()).all(),
        selected_year=selected_year,
        csrf_token=session["gyap_forms_csrf_token"],
        max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
    )


@gyap.route("/gyap-forms/<int:year>/download")
@login_required
def download_form(year):
    form = db.session.get(GyapForm, year, options=[undefer(GyapForm.data)])
    if form is None:
        abort(404)
    response = send_file(
        BytesIO(form.data), mimetype=form.mime_type, as_attachment=True,
        download_name=form.filename, conditional=False, etag=False, max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@gyap.app_context_processor
def inject_gyap_forms():
    # Fetch metadata only on pages that display leave requests or this manager.
    by_year = {}
    if current_user.is_authenticated and (
        request.endpoint in {"leaves", "manage_leaves"} or request.blueprint == "gyap"
    ):
        for year, filename in db.session.query(GyapForm.year, GyapForm.filename).order_by(GyapForm.year).all():
            by_year[year] = {"year": year, "filename": filename, "url": url_for("gyap.download_form", year=year)}

    def forms_for_period(start, end=None):
        if start is None:
            return []
        last_year = (end or start).year
        return [
            by_year.get(year, {"year": year, "filename": None, "url": None})
            for year in range(start.year, last_year + 1)
        ]

    return {"gyap_forms_by_year": by_year, "gyap_forms_for_period": forms_for_period}
