"""Private shared-account photo upload and non-destructive crop editor."""

import base64
import binascii
import re
import warnings
from math import isfinite
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePath
from secrets import token_hex

from authlib.integrations.flask_oauth2 import current_token
from flask import Blueprint, abort, flash, jsonify, redirect, request, send_file, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer
from werkzeug.exceptions import RequestEntityTooLarge

from .models import Account, Client, Membership, db
from .photo_models import AccountPhoto as ProfilePhoto, AccountPhotoSource as ProfilePhotoSource
from .security import current_account, require_csrf
from .views import authenticated


profile_photos = Blueprint("account_photos", __name__)
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
        raise PhotoValidationError('JPEG-, PNG- vagy WebP-képet tölts fel.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format != expected_format:
                    raise PhotoValidationError('A kép üres, sérült, vagy nem egyezik a fájltípusával.')
                width, height = source.size
                if width * height > MAX_PHOTO_PIXELS or max(width, height) > MAX_PHOTO_SIDE:
                    raise PhotoValidationError('A kép túl nagy. Legfeljebb 20 megapixeles, oldalanként 10 000 képpontos képet válassz.')
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
        raise PhotoValidationError('A kép túl nagy. Legfeljebb 20 megapixeles, oldalanként 10 000 képpontos képet válassz.') from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise PhotoValidationError('A kép üres, sérült, vagy nem egyezik a fájltípusával.') from None


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
        raise PhotoValidationError('Válassz érvényes képkivágást és 1–8 közötti nagyítást.') from None


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
        raise PhotoValidationError('A kivágás maradjon a képen belül.')
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
    return redirect(url_for("account_profile.edit"))


def _stale_photo_error():
    return _photo_error('A profilképed egy másik ablakban megváltozott. Töltsd újra a szerkesztőt.', 409)


def _editor_metadata(photo, source):
    return {
        "image_url": url_for("account_photos.photo_source", v=photo.version),
        "width": source.width if source else photo.width,
        "height": source.height if source else photo.height,
        "center_x": source.center_x if source else 0.5,
        "center_y": source.center_y if source else 0.5,
        "zoom": source.zoom if source else 1.0,
        "version": photo.version,
    }


def photo_version(subject):
    photo = db.session.get(ProfilePhoto, subject)
    return photo.version if photo else None


def browser_template_context(subject):
    version = photo_version(subject)
    return {
        "central_photo_url": url_for("account_photos.show_photo", v=version) if version else None,
        "central_photo_version": version or "",
        "profile_photo_max_mb": MAX_PHOTO_BYTES // (1024 * 1024),
    }


@profile_photos.route("/profile/photo", methods=["POST"])
@authenticated
def upload_photo():
    authenticated_version = current_account().version
    if request.content_length and request.content_length > MAX_PHOTO_BYTES + 64 * 1024:
        return _photo_error('A profilkép legfeljebb 5 MB lehet.')
    try:
        form, files = request.form, request.files
    except RequestEntityTooLarge:
        if _wants_json():
            return _photo_error('A profilkép legfeljebb 5 MB lehet.')
        raise
    require_csrf()
    use_existing = form.get("use_existing") == "1"
    photo = db.session.get(ProfilePhoto, current_account().subject)
    previous_version = photo.version if photo else ""
    if use_existing and "photo_version" not in form:
        return _photo_error('Mentés előtt töltsd újra a képszerkesztőt.')
    if "photo_version" in form and form.get("photo_version") != previous_version:
        return _stale_photo_error()

    uploaded = files.get("photo")
    try:
        recipe = _parse_crop(form, required=use_existing)
        if use_existing:
            if not photo:
                raise PhotoValidationError('A szerkesztéshez előbb tölts fel profilképet.')
            if uploaded is not None and uploaded.filename:
                raise PhotoValidationError('Új képet vagy a mentett képet válaszd ki.')
            saved_source = db.session.get(ProfilePhotoSource, current_account().subject)
            source_data = saved_source.data if saved_source else photo.data
            image = _decode_photo(source_data, "source.jpg")
            source_width, source_height = image.size
        else:
            if uploaded is None or not uploaded.filename:
                raise PhotoValidationError('Válassz feltöltendő profilképet.')
            data = uploaded.stream.read(MAX_PHOTO_BYTES + 1)
            if len(data) > MAX_PHOTO_BYTES:
                raise PhotoValidationError('A profilkép legfeljebb 5 MB lehet.')
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
    owner_id = current_account().subject
    owner = db.session.execute(select(Account.subject).where(
        Account.subject == owner_id, Account.active.is_(True),
        Account.version == authenticated_version,
    ).with_for_update()).scalar_one_or_none()
    if owner is None:
        db.session.rollback()
        abort(404)
    photo = ProfilePhoto.query.filter_by(account_subject=owner_id).with_for_update().populate_existing().first()
    if (photo.version if photo else "") != previous_version:
        db.session.rollback()
        return _stale_photo_error()
    source = ProfilePhotoSource.query.filter_by(account_subject=owner_id).populate_existing().first()
    new_version = token_hex(16)
    values = dict(
        data=image_data, mime_type="image/jpeg", width=width, height=height,
        size_bytes=len(image_data), version=new_version,
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        if photo is None:
            db.session.add(ProfilePhoto(account_subject=owner_id, **values))
        else:
            # SQLite ignores FOR UPDATE. A conditional write also protects an
            # existing avatar if another save wins between the check and write.
            changed = db.session.execute(
                update(ProfilePhoto).where(ProfilePhoto.account_subject == owner_id, ProfilePhoto.version == previous_version).values(**values),
                execution_options={"synchronize_session": False},
            )
            if changed.rowcount != 1:
                db.session.rollback()
                return _stale_photo_error()
        if source is None:
            source = ProfilePhotoSource(account_subject=owner_id)
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
            "photo_url": url_for("account_photos.show_photo", v=new_version),
            "version": new_version,
            "editor": _editor_metadata(photo, source),
        })
    flash('A profilkép módosult.', "success")
    return redirect(url_for("account_profile.edit"))


@profile_photos.route("/profile/photo/editor")
@authenticated
def photo_editor():
    photo = db.session.get(ProfilePhoto, current_account().subject)
    if photo is None:
        return _private_json({"error": 'A szerkesztéshez előbb tölts fel profilképet.'}, 404)
    source = db.session.get(ProfilePhotoSource, current_account().subject)
    return _private_json(_editor_metadata(photo, source))


@profile_photos.route("/profile/photo/source")
@authenticated
def photo_source():
    # No user identifier is accepted: original sources belong only to the owner,
    # including when HR/CEO can see the smaller avatar in personnel records.
    photo = db.session.get(ProfilePhoto, current_account().subject)
    if photo is None:
        abort(404)
    if request.args.get("v") and request.args["v"] != photo.version:
        return _private_json({"error": 'A profilképed egy másik ablakban megváltozott. Töltsd újra a szerkesztőt.'}, 409)
    source = db.session.get(ProfilePhotoSource, current_account().subject, options=[undefer(ProfilePhotoSource.data)])
    response = send_file(
        BytesIO(source.data if source else photo.data), mimetype="image/jpeg", download_name="profile-photo-source.jpg",
        conditional=False, etag=False, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@profile_photos.get("/profile/photo/image")
@authenticated
def show_photo():
    photo = db.session.get(ProfilePhoto, current_account().subject, options=[undefer(ProfilePhoto.data)])
    if photo is None:
        abort(404)
    response = send_file(
        BytesIO(photo.data), mimetype="image/jpeg", download_name="profile-photo.jpg",
        conditional=False, etag=False, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _require_csupor_token():
    # The existing OAuth validator checks expiry, account version, active client
    # and membership. Only the fixed CSUPOR client may exchange this private
    # cache; an account's safety bearer grants no access to HR profile material.
    if (current_token.client_id != 'csupor'
            or not request.headers.get('Authorization', '').lower().startswith('bearer ')):
        abort(403)
    return current_token.account_subject


def _import_image(payload, key, max_bytes, max_side, width_key, height_key):
    encoded = payload.get(key)
    if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
        raise PhotoValidationError('Érvénytelen profilképadatok.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoValidationError('Érvénytelen profilképadatok.') from None
    if not data or len(data) > max_bytes:
        raise PhotoValidationError('Érvénytelen profilképadatok.')
    image = _decode_photo(data, 'profile.jpg')
    if (max(image.size) > max_side
            or type(payload.get(width_key)) is not int
            or type(payload.get(height_key)) is not int
            or image.size != (payload[width_key], payload[height_key])):
        raise PhotoValidationError('Érvénytelen profilképméretek.')
    # Regenerate a clean JPEG even for the trusted application's legacy image.
    # This strips EXIF/GPS and comments without changing the crop geometry.
    normalised = _jpeg_bytes(image, quality=92)
    if len(normalised) > max_bytes:
        raise PhotoValidationError('A profilkép túl nagy.')
    return normalised, image.width, image.height


def register_photo_api(bp, protect):
    @bp.get('/api/csupor/profile/photo')
    @protect('profile')
    def export_profile_photo():
        subject = _require_csupor_token()
        photo = db.session.get(ProfilePhoto, subject)
        if photo is None:
            abort(404)
        return _private_json({
            'sub': subject, 'version': photo.version,
            'data': base64.b64encode(photo.data).decode('ascii'),
            'width': photo.width, 'height': photo.height,
        })

    @bp.post('/api/csupor/profile/photo/import')
    @protect('profile')
    def import_profile_photo():
        subject = _require_csupor_token()
        photo = db.session.get(ProfilePhoto, subject)
        if photo is not None:
            return _private_json({'sub': subject, 'version': photo.version})
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _private_json({'error': 'Érvénytelen profilképadatok.'}, 400)
        try:
            version = payload.get('version')
            if not isinstance(version, str) or re.fullmatch(r'[a-f0-9]{32}', version) is None:
                raise PhotoValidationError('Érvénytelen profilképverzió.')
            data, width, height = _import_image(payload, 'data', 1024 * 1024, THUMBNAIL_SIDE, 'width', 'height')
            if payload.get('source_data') is not None:
                source_data, source_width, source_height = _import_image(
                    payload, 'source_data', 8 * 1024 * 1024, SOURCE_SIDE, 'source_width', 'source_height')
                values = tuple(payload.get(key) for key in ('center_x', 'center_y', 'zoom'))
                if (any(type(value) not in (int, float) or not isfinite(value) for value in values)
                        or not (0 <= values[0] <= 1 and 0 <= values[1] <= 1 and 1 <= values[2] <= 8)):
                    raise PhotoValidationError('Érvénytelen képkivágás.')
                recipe = _validate_crop(values, source_width, source_height)
            else:
                source_data, source_width, source_height = data, width, height
                recipe = _clamp_crop(DEFAULT_CROP, source_width, source_height)
        except PhotoValidationError as error:
            return _private_json({'error': str(error)}, 400)

        # Serialize import against browser edits on MySQL. On SQLite the unique
        # account primary key still makes concurrent first writes no-overwrite.
        owner = db.session.execute(select(Account.subject).join(
            Membership, Membership.account_subject == Account.subject,
        ).join(Client, Client.client_id == Membership.client_id).where(
            Account.subject == subject, Account.active.is_(True),
            Account.version == current_token.version, Client.client_id == 'csupor',
            Client.active.is_(True),
        ).with_for_update()).scalar_one_or_none()
        if current_token.is_expired():
            db.session.rollback()
            abort(401)
        if owner is None:
            abort(404)
        winner = ProfilePhoto.query.filter_by(account_subject=subject).with_for_update().populate_existing().first()
        if winner is not None:
            return _private_json({'sub': subject, 'version': winner.version})
        db.session.add(ProfilePhoto(
            account_subject=subject, version=version, data=data, mime_type='image/jpeg',
            width=width, height=height, size_bytes=len(data),
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.session.add(ProfilePhotoSource(
            account_subject=subject, data=source_data, width=source_width, height=source_height,
            size_bytes=len(source_data), center_x=recipe[0], center_y=recipe[1], zoom=recipe[2],
        ))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            winner = db.session.get(ProfilePhoto, subject)
            if winner is None:
                raise
            version = winner.version
        return _private_json({'sub': subject, 'version': version})
