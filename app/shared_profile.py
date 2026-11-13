"""Private central-profile bridge; CSUPOR retains its existing HR read model.

Only the configured account issuer receives the server-held credentials. The
profile API is separate from OIDC claims so other applications receive no HR
identifiers, addresses or medical notes.
"""
import base64
from datetime import date, datetime, timezone
from io import BytesIO
import json
import re

from flask import current_app, g
from PIL import Image
import requests

from . import db
from .models import Gender, MaritalStatus, ProfilePhoto, UserProfile

FIELDS = (
    "full_name", "name_at_birth", "date_of_birth", "place_of_birth", "gender",
    "mothers_maiden_name", "citizenships", "social_security_number", "tax_number",
    "education_number", "teacher_id_card_number", "permanent_residence",
    "temporary_address", "phone_number", "bank_account_number", "marital_status", "disability",
)
_VERSION = re.compile(r"[a-f0-9]{32}\Z")
_PROFILE_LIMIT = 32768
_PHOTO_LIMIT = 2 * 1024 * 1024


def _request_json(method, path, *, token=None, payload=None, limit=_PROFILE_LIMIT, service=False):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    auth = (current_app.config["OIDC_CLIENT_ID"], current_app.config["OIDC_CLIENT_SECRET"]) if service else None
    # Streaming bounds the response before JSON/image decoding; redirects must
    # never forward a bearer or client secret to an unconfigured destination.
    with requests.request(method, current_app.config["OIDC_ISSUER"] + path,
                          headers=headers, auth=auth, json=payload, timeout=5,
                          allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError("Central profile unavailable")
        raw = bytearray()
        for chunk in response.iter_content(16384):
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError("Central profile response too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid central profile")
        return value


def _legacy_fields(user):
    profile = user.profile
    values = {}
    for key in FIELDS:
        value = getattr(profile, key, None)
        if isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, (Gender, MaritalStatus)):
            value = value.value
        values[key] = value
    return values


def _validated_profile(payload, subject, *, complete=True):
    if payload.get("sub") != subject or type(payload.get("legacy_imported")) is not bool:
        raise ValueError("Central profile subject mismatch")
    version = payload.get("version")
    if not isinstance(version, str) or (version and not _VERSION.fullmatch(version)):
        raise ValueError("Invalid central profile version")
    photo_version = payload.get("photo_version") or ""
    if not isinstance(photo_version, str) or (photo_version and not _VERSION.fullmatch(photo_version)):
        raise ValueError("Invalid central photo version")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or (complete and set(fields) != set(FIELDS)) or set(fields) - set(FIELDS):
        raise ValueError("Incomplete central profile")
    values = {}
    for name, value in fields.items():
        if value is not None and not isinstance(value, str):
            raise ValueError("Invalid central profile value")
        if name == "date_of_birth":
            values[name] = date.fromisoformat(value) if value else None
        elif name in {"gender", "marital_status"}:
            enum = Gender if name == "gender" else MaritalStatus
            values[name] = enum(value) if value else None
        else:
            maximum = UserProfile.__table__.columns[name].type.length
            if value is not None and (len(value) > maximum or any(ord(char) < 32 and char not in '\r\n\t' for char in value)):
                raise ValueError("Invalid central profile value")
            values[name] = value
    return values, photo_version


def _import_photo(user, subject, token):
    photo, source = user.photo, user.photo_source
    if not photo:
        return
    if len(photo.data) > 1024 * 1024 or source and len(source.data) > 8 * 1024 * 1024:
        raise ValueError("Legacy photo too large")
    payload = {
        "version": photo.version, "data": base64.b64encode(photo.data).decode("ascii"),
        "width": photo.width, "height": photo.height,
    }
    if source:
        payload.update(source_data=base64.b64encode(source.data).decode("ascii"),
                       source_width=source.width, source_height=source.height,
                       center_x=source.center_x, center_y=source.center_y, zoom=source.zoom)
    result = _request_json("POST", "/api/csupor/profile/photo/import", token=token, payload=payload)
    if result.get("sub") != subject:
        raise ValueError("Central photo subject mismatch")


def _read_photo(subject, token, version):
    payload = _request_json("GET", "/api/csupor/profile/photo", token=token, limit=_PHOTO_LIMIT)
    if payload.get("sub") != subject or payload.get("version") != version:
        raise ValueError("Central photo subject or version mismatch")
    encoded = payload.get("data")
    if not isinstance(encoded, str) or len(encoded) > 1400000:
        raise ValueError("Invalid central photo")
    data = base64.b64decode(encoded, validate=True)
    if not 0 < len(data) <= 1024 * 1024:
        raise ValueError("Invalid central photo size")
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if image.format != "JPEG" or not 1 <= width <= 384 or not 1 <= height <= 384:
            raise ValueError("Invalid central photo image")
        if payload.get("width") != width or payload.get("height") != height:
            raise ValueError("Central photo dimensions mismatch")
        image.verify()
    return dict(data=data, mime_type="image/jpeg", width=width, height=height,
                size_bytes=len(data), version=version,
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None))


def sync_own_profile(user, subject, token):
    """Keep authentication usable on rollout/outage; never discard local data."""
    g.shared_profile_ready = False
    try:
        payload = _request_json("GET", "/api/csupor/profile", token=token)
        _validated_profile(payload, subject, complete=False)
        if not payload["legacy_imported"]:
            payload = _request_json("POST", "/api/csupor/profile/import", token=token,
                                    payload={"fields": _legacy_fields(user)})
        values, photo_version = _validated_profile(payload, subject)
        if not payload["legacy_imported"]:
            raise ValueError("Central profile import unfinished")
        if user.photo and not photo_version:
            _import_photo(user, subject, token)
            payload = _request_json("GET", "/api/csupor/profile", token=token)
            values, photo_version = _validated_profile(payload, subject)
        photo_values = None
        if photo_version and (not user.photo or user.photo.version != photo_version):
            photo_values = _read_photo(subject, token, photo_version)
        profile = user.profile
        if profile is None:
            profile = UserProfile(user_id=user.id)
            user.profile = profile
            db.session.add(profile)
        for name, value in values.items():
            setattr(profile, name, value)
        if photo_values:
            photo = user.photo
            if photo is None:
                photo = ProfilePhoto(user_id=user.id)
                user.photo = photo
                db.session.add(photo)
            for name, value in photo_values.items():
                setattr(photo, name, value)
        # Original sources remain untouched as a lossless local migration copy.
        db.session.commit()
        g.shared_profile_ready = True
    except Exception:
        db.session.rollback()
        current_app.logger.warning("A közös profil frissítése nem sikerült; a meglévő CSUPOR-adatok megmaradtak.")
    return g.shared_profile_ready


def sync_report_profiles(user):
    """Refresh existing HR mirrors in bounded batches before reports/calculation.

    This API cannot create/import profiles. The caller requests only identities
    already linked in CSUPOR; the provider additionally enforces app membership.
    """
    from flask import request
    from .shared_auth_models import ExternalIdentity
    endpoint = request.endpoint or ""
    relevant = (endpoint in {"dashboard", "manage_user_profiles", "edit_user_profile"}
                or endpoint.startswith(("worktime.", "qualification_reports.", "gyap."))
                or "leave" in endpoint or "contract" in endpoint)
    if not relevant:
        return
    try:
        links = db.session.execute(db.select(ExternalIdentity).where(
            ExternalIdentity.issuer == current_app.config["OIDC_ISSUER"],
            ExternalIdentity.disabled.is_(False), ExternalIdentity.user_id.is_not(None),
            ExternalIdentity.user_id != user.id,
        )).scalars().all()
        for offset in range(0, len(links), 100):
            targets = {link.subject: link for link in links[offset:offset + 100] if link.user is not None}
            if not targets:
                continue
            response = _request_json("POST", "/api/csupor/profiles/read", service=True,
                                     payload={"subjects": list(targets)}, limit=100 * _PROFILE_LIMIT)
            rows = response.get("profiles")
            if not isinstance(rows, list) or len(rows) > len(targets):
                raise ValueError("Invalid central profile batch")
            validated, seen = [], set()
            for row in rows:
                if not isinstance(row, dict) or row.get("sub") not in targets or row["sub"] in seen:
                    raise ValueError("Unexpected central profile subject")
                seen.add(row["sub"])
                values, _photo_version = _validated_profile(row, row["sub"])
                if not row["legacy_imported"]:
                    continue
                validated.append((targets[row["sub"]].user, values))
            for owner, values in validated:
                profile = owner.profile
                if profile is None:
                    profile = UserProfile(user_id=owner.id)
                    owner.profile = profile
                    db.session.add(profile)
                for name, value in values.items():
                    setattr(profile, name, value)
            db.session.commit()
    except Exception:
        db.session.rollback()
        g.shared_profile_cache_stale = True
        current_app.logger.warning("A közös személyes adatok frissítése nem sikerült; a tárolt CSUPOR-adatok megmaradtak.")
