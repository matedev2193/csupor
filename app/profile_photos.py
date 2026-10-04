"""Validate, normalise and privately serve employees' profile photos."""

import warnings
from datetime import datetime, timezone
from hmac import compare_digest
from io import BytesIO
from pathlib import PurePath
from secrets import token_hex, token_urlsafe

from flask import Blueprint, abort, flash, redirect, request, send_file, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.orm import undefer

from . import db
from .models import ProfilePhoto, UserPrivilege


profile_photos = Blueprint("profile_photos", __name__)
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 20_000_000
MAX_PHOTO_SIDE = 10_000
THUMBNAIL_SIDE = 384
ALLOWED_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


class PhotoValidationError(ValueError):
    """A user-facing image validation failure."""


def _normalise_photo(data: bytes, filename: str) -> tuple[bytes, int, int]:
    expected_format = ALLOWED_FORMATS.get(PurePath(filename).suffix.lower())
    if expected_format is None:
        raise PhotoValidationError(_("Upload a JPEG, PNG or WebP photo."))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format != expected_format:
                    raise PhotoValidationError(_("The photo is empty, damaged or does not match its file type."))
                width, height = source.size
                if width * height > MAX_PHOTO_PIXELS or max(width, height) > MAX_PHOTO_SIDE:
                    raise PhotoValidationError(_("The photo is too large. Use an image up to 20 megapixels and 10,000 pixels per side."))
                source.verify()

            # Reopen after structural verification and force a real pixel decode.
            with Image.open(BytesIO(data)) as source:
                source.seek(0)
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
                # A fresh canvas removes EXIF/GPS, comments, ICC profiles and any
                # source metadata; transparency gets a neutral white background.
                rgba = oriented.convert("RGBA")
                normalised = Image.new("RGB", rgba.size, "white")
                normalised.paste(rgba, mask=rgba.getchannel("A"))
                output = BytesIO()
                normalised.save(output, format="JPEG", quality=88)
                return output.getvalue(), normalised.width, normalised.height
    except PhotoValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoValidationError(_("The photo is too large. Use an image up to 20 megapixels and 10,000 pixels per side.")) from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise PhotoValidationError(_("The photo is empty, damaged or does not match its file type.")) from None


def _can_view_photo(user_id: int) -> bool:
    return current_user.is_authenticated and (
        current_user.id == user_id or current_user.privilege in {UserPrivilege.hr, UserPrivilege.ceo}
    )


def profile_photo_url(user):
    """A versioned URL, without selecting image bytes on normal HTML pages."""
    if not user or not _can_view_photo(user.id):
        return None
    photo = user.photo
    if photo is None:
        return None
    return url_for("profile_photos.show_photo", user_id=user.id, v=photo.version)


@profile_photos.app_context_processor
def inject_profile_photo_context():
    csrf_token = None
    if current_user.is_authenticated:
        csrf_token = session.setdefault("profile_photo_csrf_token", token_urlsafe(32))
    return {
        "profile_photo_url": profile_photo_url,
        "profile_photo_csrf_token": csrf_token,
        "profile_photo_max_mb": MAX_PHOTO_BYTES // (1024 * 1024),
    }


@profile_photos.route("/profile/photo", methods=["POST"])
@login_required
def upload_photo():
    if request.content_length and request.content_length > MAX_PHOTO_BYTES + 64 * 1024:
        flash(_("The profile photo must be no larger than 5 MB."), "error")
        return redirect(url_for("edit_profile"))
    token = session.get("profile_photo_csrf_token")
    if not token or not compare_digest(token.encode(), request.form.get("csrf_token", "").encode()):
        abort(400)
    uploaded = request.files.get("photo")
    if uploaded is None or not uploaded.filename:
        flash(_("Choose a profile photo to upload."), "error")
        return redirect(url_for("edit_profile"))
    data = uploaded.stream.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        flash(_("The profile photo must be no larger than 5 MB."), "error")
        return redirect(url_for("edit_profile"))
    try:
        image_data, width, height = _normalise_photo(data, uploaded.filename)
    except PhotoValidationError as error:
        flash(str(error), "error")
        return redirect(url_for("edit_profile"))

    # Never accept a posted user ID: this endpoint only updates the session owner.
    photo = db.session.get(ProfilePhoto, current_user.id)
    if photo is None:
        photo = ProfilePhoto(user_id=current_user.id)
        db.session.add(photo)
    photo.data = image_data
    photo.mime_type = "image/jpeg"
    photo.width = width
    photo.height = height
    photo.size_bytes = len(image_data)
    photo.version = token_hex(16)
    photo.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    flash(_("Profile photo updated."), "success")
    return redirect(url_for("edit_profile"))


@profile_photos.route("/users/<int:user_id>/photo")
@login_required
def show_photo(user_id):
    if not _can_view_photo(user_id):
        abort(403)
    photo = db.session.get(ProfilePhoto, user_id, options=[undefer(ProfilePhoto.data)])
    if photo is None:
        abort(404)
    response = send_file(
        BytesIO(photo.data), mimetype="image/jpeg", download_name="profile-photo.jpg",
        conditional=False, etag=False, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
