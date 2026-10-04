"""Validate, normalise and privately serve employees' profile photos."""

import warnings
from math import isfinite
from datetime import datetime, timezone
from hmac import compare_digest
from io import BytesIO
from pathlib import PurePath
from secrets import token_hex, token_urlsafe

from flask import Blueprint, abort, flash, jsonify, redirect, request, send_file, session, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer
from werkzeug.exceptions import RequestEntityTooLarge

from . import db
from .models import ProfilePhoto, ProfilePhotoSource, User, UserPrivilege


profile_photos = Blueprint("profile_photos", __name__)
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 20_000_000
MAX_PHOTO_SIDE = 10_000
THUMBNAIL_SIDE = 384
SOURCE_SIDE = 2048
DEFAULT_CROP = (0.5, 0.5, 1.0)
ALLOWED_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


class PhotoValidationError(ValueError):
    """A user-facing image validation failure."""


def _decode_photo(data: bytes, filename: str) -> Image.Image:
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
                # Crop coordinates refer to this oriented image, not raw EXIF
                # dimensions. The returned copy outlives the upload decoder.
                return ImageOps.exif_transpose(source)
    except PhotoValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoValidationError(_("The photo is too large. Use an image up to 20 megapixels and 10,000 pixels per side.")) from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise PhotoValidationError(_("The photo is empty, damaged or does not match its file type.")) from None


def _jpeg_bytes(image: Image.Image, *, quality=88) -> bytes:
    # A fresh canvas removes EXIF/GPS, comments and colour profiles, including
    # from the editable source; transparent pixels get a white background.
    rgba = image.convert("RGBA")
    normalised = Image.new("RGB", rgba.size, "white")
    normalised.paste(rgba, mask=rgba.getchannel("A"))
    output = BytesIO()
    normalised.save(output, format="JPEG", quality=quality)
    return output.getvalue()


def _normalise_photo(data: bytes, filename: str) -> tuple[bytes, int, int]:
    image = _decode_photo(data, filename)
    image.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
    return _jpeg_bytes(image), image.width, image.height


def _parse_crop(form, *, required=False):
    fields = ("crop_center_x", "crop_center_y", "crop_zoom")
    supplied = [field in form for field in fields]
    if not any(supplied) and not required:
        return None
    try:
        if not all(supplied) or any(len(form.getlist(field)) != 1 for field in fields):
            raise ValueError
        center_x, center_y, zoom = (float(form[field]) for field in fields)
        if not all(isfinite(value) for value in (center_x, center_y, zoom)):
            raise ValueError
        if not (0 <= center_x <= 1 and 0 <= center_y <= 1 and 1 <= zoom <= 8):
            raise ValueError
        return center_x, center_y, zoom
    except (ValueError, TypeError, OverflowError):
        raise PhotoValidationError(_("Choose a valid crop and a zoom level between 1 and 8.")) from None


def _clamp_crop(recipe, width, height):
    center_x, center_y, zoom = recipe
    side = min(width, height) / zoom
    margin_x, margin_y = side / (2 * width), side / (2 * height)
    return (
        max(margin_x, min(1 - margin_x, center_x)),
        max(margin_y, min(1 - margin_y, center_y)),
        zoom,
    )


def _validate_crop(recipe, width, height):
    clamped = _clamp_crop(recipe, width, height)
    if abs(recipe[0] - clamped[0]) > 1e-6 or abs(recipe[1] - clamped[1]) > 1e-6:
        raise PhotoValidationError(_("Keep the crop inside the photo."))
    return clamped


def _avatar(image, recipe):
    if recipe is None:
        # Preserve the original non-JavaScript upload behaviour.
        thumbnail = image.copy()
        thumbnail.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
    else:
        center_x, center_y, zoom = recipe
        side = min(image.size) / zoom
        left, top = center_x * image.width - side / 2, center_y * image.height - side / 2
        # Floating point boxes keep drag/zoom coordinates consistent at edges.
        box = (max(0, left), max(0, top), min(image.width, left + side), min(image.height, top + side))
        thumbnail = image.resize((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS, box=box)
    return _jpeg_bytes(thumbnail), thumbnail.width, thumbnail.height


def _wants_json():
    return request.accept_mimetypes.best == "application/json"


def _private_json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _photo_error(message, status=400):
    if _wants_json():
        return _private_json({"error": message}, status)
    flash(message, "error")
    return redirect(url_for("edit_profile"))


def _stale_photo_error():
    return _photo_error(_("Your profile photo changed in another window. Reload the editor and try again."), 409)


def _editor_metadata(photo, source):
    return {
        "image_url": url_for("profile_photos.photo_source", v=photo.version),
        "width": source.width if source else photo.width,
        "height": source.height if source else photo.height,
        "center_x": source.center_x if source else 0.5,
        "center_y": source.center_y if source else 0.5,
        "zoom": source.zoom if source else 1.0,
        "version": photo.version,
    }


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
        return _photo_error(_("The profile photo must be no larger than 5 MB."))
    try:
        form, files = request.form, request.files
    except RequestEntityTooLarge:
        if _wants_json():
            return _photo_error(_("The profile photo must be no larger than 5 MB."))
        raise
    token = session.get("profile_photo_csrf_token")
    if not token or not compare_digest(token.encode(), form.get("csrf_token", "").encode()):
        if _wants_json():
            return _photo_error(_("Your session expired. Reload the page and try again."))
        abort(400)
    use_existing = form.get("use_existing") == "1"
    photo = db.session.get(ProfilePhoto, current_user.id)
    previous_version = photo.version if photo else ""
    if use_existing and "photo_version" not in form:
        return _photo_error(_("Reload the photo editor before saving changes."))
    if "photo_version" in form and form.get("photo_version") != previous_version:
        return _stale_photo_error()

    uploaded = files.get("photo")
    try:
        recipe = _parse_crop(form, required=use_existing)
        if use_existing:
            if not photo:
                raise PhotoValidationError(_("Upload a profile photo before editing it."))
            if uploaded is not None and uploaded.filename:
                raise PhotoValidationError(_("Choose either a new photo or the saved photo."))
            saved_source = db.session.get(ProfilePhotoSource, current_user.id)
            source_data = saved_source.data if saved_source else photo.data
            image = _decode_photo(source_data, "source.jpg")
            source_width, source_height = image.size
        else:
            if uploaded is None or not uploaded.filename:
                raise PhotoValidationError(_("Choose a profile photo to upload."))
            data = uploaded.stream.read(MAX_PHOTO_BYTES + 1)
            if len(data) > MAX_PHOTO_BYTES:
                raise PhotoValidationError(_("The profile photo must be no larger than 5 MB."))
            image = _decode_photo(data, uploaded.filename)
            source_image = image.copy()
            source_image.thumbnail((SOURCE_SIDE, SOURCE_SIDE), Image.Resampling.LANCZOS)
            source_data = _jpeg_bytes(source_image, quality=92)
            source_width, source_height = source_image.size
        if recipe is not None:
            # Validate new uploads against their ORIGINAL oriented dimensions;
            # otherwise rounding during source resizing can reject edge drags.
            recipe = _validate_crop(recipe, image.width, image.height)
        image_data, width, height = _avatar(image, recipe)
        source_recipe = _clamp_crop(recipe or DEFAULT_CROP, source_width, source_height)
    except PhotoValidationError as error:
        return _photo_error(str(error))

    # Serialize MySQL saves even before a first avatar exists. Locking reads
    # bypass repeatable-read snapshots, and populate_existing refreshes ORM state.
    owner_id = current_user.id
    owner = db.session.execute(select(User.id).where(User.id == owner_id).with_for_update()).scalar_one_or_none()
    if owner is None:
        db.session.rollback()
        abort(404)
    photo = ProfilePhoto.query.filter_by(user_id=owner_id).with_for_update().populate_existing().first()
    if (photo.version if photo else "") != previous_version:
        db.session.rollback()
        return _stale_photo_error()
    source = ProfilePhotoSource.query.filter_by(user_id=owner_id).populate_existing().first()
    new_version = token_hex(16)
    values = dict(
        data=image_data, mime_type="image/jpeg", width=width, height=height,
        size_bytes=len(image_data), version=new_version,
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        if photo is None:
            db.session.add(ProfilePhoto(user_id=owner_id, **values))
        else:
            # SQLite ignores FOR UPDATE. A conditional write also protects an
            # existing avatar if another save wins between the check and write.
            changed = db.session.execute(
                update(ProfilePhoto).where(ProfilePhoto.user_id == owner_id, ProfilePhoto.version == previous_version).values(**values),
                execution_options={"synchronize_session": False},
            )
            if changed.rowcount != 1:
                db.session.rollback()
                return _stale_photo_error()
        if source is None:
            source = ProfilePhotoSource(user_id=owner_id)
            db.session.add(source)
        source.data = source_data
        source.width, source.height = source_width, source_height
        source.size_bytes = len(source_data)
        source.center_x, source.center_y, source.zoom = source_recipe
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # A concurrent first insert can race on SQLite; never hide other failed
        # constraints when no competing avatar version was actually committed.
        winner = db.session.get(ProfilePhoto, owner_id)
        if not previous_version and winner is not None:
            return _stale_photo_error()
        raise
    if _wants_json():
        photo = db.session.get(ProfilePhoto, owner_id)
        source = db.session.get(ProfilePhotoSource, owner_id)
        return _private_json({
            "photo_url": url_for("profile_photos.show_photo", user_id=owner_id, v=new_version),
            "version": new_version,
            "editor": _editor_metadata(photo, source),
        })
    flash(_("Profile photo updated."), "success")
    return redirect(url_for("edit_profile"))


@profile_photos.route("/profile/photo/editor")
@login_required
def photo_editor():
    photo = db.session.get(ProfilePhoto, current_user.id)
    if photo is None:
        return _private_json({"error": _("Upload a profile photo before editing it.")}, 404)
    source = db.session.get(ProfilePhotoSource, current_user.id)
    return _private_json(_editor_metadata(photo, source))


@profile_photos.route("/profile/photo/source")
@login_required
def photo_source():
    # No user identifier is accepted: original sources belong only to the owner,
    # including when HR/CEO can see the smaller avatar in personnel records.
    photo = db.session.get(ProfilePhoto, current_user.id)
    if photo is None:
        abort(404)
    if request.args.get("v") and request.args["v"] != photo.version:
        return _private_json({"error": _("Your profile photo changed in another window. Reload the editor and try again.")}, 409)
    source = db.session.get(ProfilePhotoSource, current_user.id, options=[undefer(ProfilePhotoSource.data)])
    response = send_file(
        BytesIO(source.data if source else photo.data), mimetype="image/jpeg", download_name="profile-photo-source.jpg",
        conditional=False, etag=False, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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
