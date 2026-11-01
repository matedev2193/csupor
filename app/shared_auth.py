"""OpenID Connect client: authentication is shared, HR permissions stay local."""

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import os
import re
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user
import requests

from . import db
from .shared_auth_models import ExternalIdentity, ExternalLoginSession


shared_auth = Blueprint("shared_auth", __name__)
_HANDLE = re.compile(r"oidc:([A-Za-z0-9_-]{43})\Z")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def oidc_enabled():
    return current_app.config.get("AUTH_MODE", "local") == "oidc"


def provider_url(path):
    return current_app.config["OIDC_ISSUER"] + path


def _cipher():
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode()
    return Fernet(base64.urlsafe_b64encode(sha256(b"csupor:oidc:token:v1:" + secret).digest()))


def _client():
    client = current_app.extensions["shared_oauth"].create_client("accounts")
    metadata = client.load_server_metadata()
    issuer = current_app.config["OIDC_ISSUER"]
    if metadata.get("issuer") != issuer:
        raise ValueError("Unexpected account issuer")
    # This deployment uses our independently hosted account service. Do not
    # follow discovery data to another origin or an unregistered endpoint.
    for key, path in {
        "authorization_endpoint": "/authorize", "token_endpoint": "/token",
        "userinfo_endpoint": "/userinfo", "jwks_uri": "/jwks",
    }.items():
        if metadata.get(key) != issuer + path:
            raise ValueError("Unexpected account endpoint")
    if "RS256" not in metadata.get("id_token_signing_alg_values_supported", []):
        raise ValueError("Unsupported account signing algorithm")
    metadata["id_token_signing_alg_values_supported"] = ["RS256"]
    return client


def clear_local_session():
    identity = session.get("_user_id", "")
    match = _HANDLE.fullmatch(identity) if isinstance(identity, str) else None
    if match:
        db.session.execute(db.delete(ExternalLoginSession).where(
            ExternalLoginSession.id_hash == sha256(match[1].encode()).hexdigest(),
        ))
        db.session.commit()
    logout_user()
    for key in ("shared_login", "shared_delete_grant", "shared_delete_intent", "shared_logout_csrf"):
        session.pop(key, None)


def logout_csrf_token():
    return session.setdefault("shared_logout_csrf", token_urlsafe(32))


def require_logout_csrf():
    expected, actual = session.get("shared_logout_csrf"), request.form.get("csrf_token", "")
    if not isinstance(expected, str) or not compare_digest(expected.encode(), actual.encode()):
        abort(400, description="A munkamenet lejárt. Frissítsd az oldalt, majd próbáld újra.")


def end_shared_session():
    """Revoke this account's shared sessions using the server-held bearer only."""
    identity = session.get("_user_id", "")
    match = _HANDLE.fullmatch(identity) if isinstance(identity, str) else None
    saved = db.session.get(ExternalLoginSession, sha256(match[1].encode()).hexdigest()) if match else None
    if saved is None or saved.identity.issuer != current_app.config["OIDC_ISSUER"]:
        return False
    try:
        token = _cipher().decrypt(saved.access_token.encode()).decode()
        # Fixed configured endpoint, no token in a browser URL, no redirects
        # that could forward an Authorization header to another service.
        response = requests.post(provider_url("/end-session"),
                                 headers={"Authorization": "Bearer " + token},
                                 timeout=5, allow_redirects=False)
        return response.status_code == 204
    except Exception:
        current_app.logger.warning("Shared account sign-out could not be completed.")
        return False


def load_oidc_user(identity):
    """Validate provider revocation and application membership on every request."""
    match = _HANDLE.fullmatch(identity) if isinstance(identity, str) else None
    if not match:
        return None
    key = sha256(match[1].encode()).hexdigest()
    saved = db.session.get(ExternalLoginSession, key)
    if saved is None or saved.expires_at <= _now():
        return None
    link = saved.identity
    if link.disabled or link.user_id is None or link.issuer != current_app.config["OIDC_ISSUER"]:
        return None
    try:
        token = _cipher().decrypt(saved.access_token.encode()).decode()
        claims = _client().userinfo(token={"access_token": token, "token_type": "Bearer"}, timeout=5)
        if claims.get("sub") != link.subject:
            return None
    except Exception:
        # No fallback to the legacy password/session on an outage or denial.
        current_app.logger.warning("Shared account session could not be validated.")
        return None
    g.shared_account_claims = claims
    g.shared_login_session = saved
    return link.user


def begin_login(register=False, deletion_target=None):
    if not oidc_enabled():
        abort(404)
    try:
        client = _client()
        kwargs = {}
        if register:
            kwargs["screen_hint"] = "signup"
        if deletion_target is not None:
            session["shared_delete_intent"] = {
                "actor": current_user.id, "target": deletion_target,
                "started": int(datetime.now(timezone.utc).timestamp()),
            }
            kwargs.update(prompt="login", max_age=0)
        else:
            session.pop("shared_delete_intent", None)
        return client.authorize_redirect(current_app.config["OIDC_REDIRECT_URI"], **kwargs)
    except Exception:
        current_app.logger.warning("Shared account sign-in could not be started.")
        return render_template("shared_auth_error.html", message="A közös fiókrendszer jelenleg nem érhető el. Kérjük, próbáld újra később."), 503


def _mapped_user(claims):
    from .models import User, UserPrivilege, UserProfile

    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
        abort(403)
    issuer = current_app.config["OIDC_ISSUER"]
    link = ExternalIdentity.query.filter_by(issuer=issuer, subject=subject).first()
    if link is not None:
        if link.disabled or link.user_id is None or link.user is None:
            abort(403)
        user = link.user
    else:
        email = claims.get("email", "")
        username = claims.get("preferred_username", "")
        if not isinstance(email, str) or not isinstance(username, str):
            abort(403)
        email = email.strip().lower()
        if not 1 <= len(email) <= 120 or not 1 <= len(username) <= 50:
            abort(403)
        # Legacy email addresses were never ownership-verified. A matching
        # address or username is NOT proof of ownership of an existing record.
        if User.query.filter((db.func.lower(User.email) == email) | (User.username == username)).first():
            abort(403, description="A meglévő CSUPOR-fiókot előbb át kell vezetni a közös fiókrendszerbe.")
        user = User(email=email, username=username, privilege=UserPrivilege.employee,
                    password_hash="!external-account-no-local-password")
        user.profile = UserProfile()
        db.session.add(user)
        db.session.flush()
        link = ExternalIdentity(issuer=issuer, subject=subject, user_id=user.id)
        db.session.add(link)
        db.session.flush()
    # Contact data follows the authenticated stable identity; privileges and HR
    # fields never come from token claims. Do not overwrite another user's data.
    email = claims.get("email")
    username = claims.get("preferred_username")
    if not isinstance(email, str) or not isinstance(username, str):
        abort(403)
    email = email.strip().lower()
    if not 1 <= len(email) <= 120 or not 1 <= len(username) <= 50:
        abort(403)
    if User.query.filter(User.id != user.id, (db.func.lower(User.email) == email) | (User.username == username)).first():
        abort(403, description="A közös fiók adatai ütköznek egy CSUPOR-fiókkal. Kérd az adminisztrátor segítségét.")
    user.email, user.username = email, username
    return user, link


@shared_auth.get("/auth/login")
def login():
    return begin_login()


@shared_auth.get("/auth/callback")
def callback():
    if not oidc_enabled():
        abort(404)
    from werkzeug.exceptions import HTTPException
    from .page_access import can_access_page, landing_url

    try:
        client = _client()
        token = client.authorize_access_token(claims_options={
            "iss": {"essential": True, "value": current_app.config["OIDC_ISSUER"]},
            "sub": {"essential": True}, "exp": {"essential": True},
            "aud": {"essential": True, "value": current_app.config["OIDC_CLIENT_ID"]},
        })
        claims = token.get("userinfo")
        if not token.get("id_token") or not claims or not token.get("access_token") or str(token.get("token_type", "")).lower() != "bearer":
            raise ValueError("Incomplete account response")
        live_claims = client.userinfo(token=token, timeout=5)
        if live_claims.get("sub") != claims.get("sub"):
            raise ValueError("Account subject mismatch")
        # The verified ID token authenticates the session; /userinfo confirms
        # current account status and client membership before local provisioning.
        user, link = _mapped_user(live_claims)
        intent = session.pop("shared_delete_intent", None)
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool):
            raise ValueError("Missing authentication time")
        now_seconds = datetime.now(timezone.utc).timestamp()
        if auth_time > now_seconds + 30:
            raise ValueError("Invalid authentication time")
        if intent is not None and (
            user.id != intent["actor"] or now_seconds - intent["started"] > 600
            or auth_time < intent["started"] - 5
        ):
            raise ValueError("Reauthentication does not match the requested action")
        expires_in = min(int(token.get("expires_in", 0)), current_app.config["OIDC_SESSION_TTL"])
        if expires_in <= 0:
            raise ValueError("Expired account response")
        # Revoke this browser's previous local bearer without touching other
        # applications, and rotate its opaque handle after every authentication.
        previous = session.get("_user_id", "")
        matched = _HANDLE.fullmatch(previous) if isinstance(previous, str) else None
        if matched:
            db.session.execute(db.delete(ExternalLoginSession).where(
                ExternalLoginSession.id_hash == sha256(matched[1].encode()).hexdigest(),
            ))
        db.session.execute(db.delete(ExternalLoginSession).where(ExternalLoginSession.expires_at <= _now()))
        handle = token_urlsafe(32)
        db.session.add(ExternalLoginSession(
            id_hash=sha256(handle.encode()).hexdigest(), identity_id=link.id,
            access_token=_cipher().encrypt(token["access_token"].encode()).decode(),
            expires_at=_now() + timedelta(seconds=expires_in),
            authenticated_at=datetime.fromtimestamp(auth_time, timezone.utc).replace(tzinfo=None),
            deletion_target_id=intent["target"] if intent else None,
            deletion_expires_at=_now() + timedelta(seconds=120) if intent else None,
        ))
        db.session.commit()
        session["shared_login"] = handle
        login_user(user)
        if intent is not None:
            if not can_access_page("manage_user_profiles", user):
                abort(403)
            session.setdefault("profile_delete_csrf_token", token_urlsafe(32))
            from .models import User
            target = db.session.get(User, intent["target"])
            if target is None:
                abort(404)
            return render_template("shared_confirm_delete.html", target_user=target,
                                   csrf_token=session["profile_delete_csrf_token"])
        return redirect(landing_url())
    except HTTPException:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Shared account sign-in was rejected.")
        return render_template("shared_auth_error.html", message="A bejelentkezés nem sikerült. Indítsd újra a CSUPOR bejelentkezési oldaláról."), 400


def deletion_reauthenticated(target_id):
    """Consume a short-lived grant tied to this exact administrator and target."""
    identity = session.get("_user_id", "")
    matched = _HANDLE.fullmatch(identity) if isinstance(identity, str) else None
    if not matched:
        return False
    permitted = db.session.execute(db.update(ExternalLoginSession).where(
        ExternalLoginSession.id_hash == sha256(matched[1].encode()).hexdigest(),
        ExternalLoginSession.deletion_target_id == target_id,
        ExternalLoginSession.deletion_expires_at > _now(),
        ExternalLoginSession.identity_id.in_(db.select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == current_user.id, ExternalIdentity.disabled.is_(False),
        )),
    ).values(deletion_target_id=None, deletion_expires_at=None).execution_options(synchronize_session=False))
    # Persist consumption before the destructive transaction, including when
    # subsequent deletion validation rejects the action. Cookie replay cannot
    # restore this grant or reuse it against a later account with the same ID.
    db.session.commit()
    return permitted.rowcount == 1


def init_shared_auth(app):
    mode = os.getenv("AUTH_MODE", "local").lower()
    if mode not in {"local", "oidc"}:
        raise RuntimeError("AUTH_MODE must be local or oidc.")
    app.config["AUTH_MODE"] = mode
    allow_http = os.getenv("AUTH_ALLOW_HTTP", "false").lower() in {"1", "true", "yes"}
    app.config["AUTH_ALLOW_HTTP"] = allow_http
    app.register_blueprint(shared_auth)
    app.context_processor(lambda: {"shared_accounts_enabled": oidc_enabled(),
                                  "shared_account_url": provider_url("/account") if oidc_enabled() else None,
                                  "shared_logout_csrf": logout_csrf_token})
    if mode == "local":
        return
    issuer = os.getenv("OIDC_ISSUER", "").rstrip("/")
    callback_url = os.getenv("OIDC_REDIRECT_URI", "")
    for value in (issuer, callback_url):
        parsed = urlsplit(value)
        local_http = allow_http and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("Configure HTTPS OIDC_ISSUER and OIDC_REDIRECT_URI; AUTH_ALLOW_HTTP is restricted to loopback development URLs.")
    if urlsplit(callback_url).path != "/auth/callback" or len(issuer) > 255:
        raise RuntimeError("OIDC_REDIRECT_URI must use the /auth/callback path.")
    client_id, client_secret = os.getenv("OIDC_CLIENT_ID", ""), os.getenv("OIDC_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("OIDC_CLIENT_ID and OIDC_CLIENT_SECRET are required in OIDC mode.")
    if len(app.config.get("SECRET_KEY", "")) < 32:
        raise RuntimeError("OIDC mode requires a unique SECRET_KEY of at least 32 characters.")
    try:
        ttl = int(os.getenv("OIDC_SESSION_TTL", "3600"))
    except ValueError:
        raise RuntimeError("OIDC_SESSION_TTL must be between 60 and 3600 seconds.") from None
    if not 60 <= ttl <= 3600:
        raise RuntimeError("OIDC_SESSION_TTL must be between 60 and 3600 seconds.")
    app.config.update(OIDC_ISSUER=issuer, OIDC_CLIENT_ID=client_id, OIDC_CLIENT_SECRET=client_secret,
                      OIDC_REDIRECT_URI=callback_url, OIDC_SESSION_TTL=ttl,
                      SESSION_COOKIE_NAME="csupor_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=urlsplit(callback_url).scheme == "https",
                      SESSION_COOKIE_DOMAIN=None)
    if allow_http and urlsplit(issuer).scheme == "http":
        # Authlib independently rejects HTTP; only the validated loopback-only
        # development opt-in above permits this transport exception.
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
    logging.getLogger("authlib").setLevel(logging.WARNING)
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register("accounts", client_id=client_id, client_secret=client_secret,
                   server_metadata_url=issuer + "/.well-known/openid-configuration",
                   client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256",
                                  "token_endpoint_auth_method": "client_secret_basic", "default_timeout": 5})
    app.extensions["shared_oauth"] = oauth

    @app.after_request
    def private_auth_response(response):
        if request.endpoint and request.endpoint.startswith("shared_auth."):
            response.cache_control.no_store = True
            response.headers["Referrer-Policy"] = "no-referrer"
        return response
