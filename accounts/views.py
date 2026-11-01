"""Account pages. Credentials and recovery never leave the account service."""

from functools import wraps
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import unicodedata

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .mail import send_reset_email
from .models import Account, Client, Membership, PasswordReset, db
from .security import current_account, csrf_token, login_account, logout_account, require_csrf, safe_next, throttle, valid_email


web = Blueprint("web", __name__)
_DUMMY_HASH = generate_password_hash(token_urlsafe(32))
_RESET_REPLY = "Ha a megadott e-mail-címhez aktív fiók tartozik, elküldtük a jelszó-visszaállítási útmutatót."


@web.app_context_processor
def account_template_context():
    return {"account": current_account(), "csrf_token": csrf_token}


def authenticated(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_account() is None:
            return redirect(url_for("web.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _password_valid(password):
    return 12 <= len(password) <= 1024


def _username_valid(username):
    return 1 <= len(username) <= 50 and all(
        character.isalnum() or character in "._-" for character in username
    )


def _email_valid(email):
    return (
        len(email) <= 120
        and not any(unicodedata.category(character).startswith("C") for character in email)
        and valid_email(email)
    )


def _password_matches(account, password):
    # Bound work before a password KDF and keep the unknown-user path comparable.
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        return False
    try:
        if account is None:
            check_password_hash(_DUMMY_HASH, password)
            return False
        return account.check_password(password)
    except (ValueError, TypeError):
        return False


def _credential_limit(scope, key, limit=10):
    return throttle(scope, key, limit=limit, window=900)


def _registration_client(next_url):
    parsed = urlsplit(next_url)
    client_id = request.values.get("client_id", "")
    if not client_id and parsed.path == "/authorize":
        client_id = parse_qs(parsed.query).get("client_id", [""])[0]
    client = db.session.get(Client, client_id) if client_id else None
    return client if client and client.active and client.allow_registration else None


def _after_login(next_url):
    if urlsplit(next_url).path != '/authorize':
        return redirect(next_url)
    # A form POST must end on this origin before OIDC redirects to another
    # application. Chromium otherwise applies form-action 'self' to the entire
    # redirect chain and leaves a successfully submitted login form on screen.
    nonce = token_urlsafe(24)
    session['login_continuation'] = {
        'nonce': nonce, 'next': next_url, 'issued_at': int(time()),
    }
    return redirect(url_for('web.login_continue', flow=nonce))


@web.get('/login/continue')
def login_continue():
    account = current_account()
    pending = session.get('login_continuation')
    supplied = request.args.get('flow', '')
    valid = (account is not None and isinstance(pending, dict)
             and isinstance(pending.get('nonce'), str)
             and compare_digest(pending['nonce'].encode(), supplied.encode())
             and type(pending.get('issued_at')) is int
             and 0 <= int(time()) - pending['issued_at'] <= 300)
    if not valid:
        return redirect(url_for('web.account_page' if account else 'web.login'))
    session.pop('login_continuation', None)
    # The destination comes from this browser's signed session, never a query
    # string. Keep the same local-only redirect validation at consumption time.
    next_url = safe_next(pending.get('next'))
    return render_template('login_continue.html', next_url=next_url)


@web.get("/")
def index():
    return redirect(url_for("web.account_page" if current_account() else "web.login"))


@web.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next(request.values.get("next"))
    registration_client = _registration_client(next_url)
    error = None
    if request.method == "POST":
        require_csrf()
        identifier = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        allowed = _credential_limit("login-ip", request.remote_addr or "unknown", 60)
        allowed = _credential_limit("login-id", identifier.casefold()) and allowed
        if not allowed:
            error = "Túl sok próbálkozás. Kérjük, próbáld újra 15 perc múlva."
        else:
            matches = db.session.execute(db.select(Account).where(or_(
                func.lower(Account.email) == identifier.lower(),
                Account.username == identifier,
            ))).scalars().all() if len(identifier) <= 120 else []
            account = matches[0] if len(matches) == 1 else None
            if not _password_matches(account, password) or not account.active:
                error = "Hibás bejelentkezési adatokat adtál meg."
            else:
                login_account(account)
                return _after_login(next_url)
    return render_template("login.html", error=error, next_url=next_url, registration_client=registration_client)


@web.route("/register", methods=["GET", "POST"])
def register():
    next_url = safe_next(request.values.get("next"))
    client = _registration_client(next_url)
    if client is None:
        return render_template("registration_unavailable.html"), 403
    error = None
    if request.method == "POST":
        require_csrf()
        if not _credential_limit("register-ip", request.remote_addr or "unknown", 10):
            error = "Túl sok próbálkozás. Kérjük, próbáld újra 15 perc múlva."
        else:
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not _username_valid(username):
                error = "A felhasználónév 1–50 betűt, számot, pontot, kötőjelet vagy aláhúzást tartalmazhat."
            elif not _email_valid(email):
                error = "Adj meg egy érvényes, legfeljebb 120 karakteres e-mail-címet."
            elif not _password_valid(password):
                error = "A jelszó hossza 12–1024 karakter lehet."
            elif password != request.form.get("password_confirm", ""):
                error = "A két jelszó nem egyezik."
            elif db.session.execute(db.select(Account.subject).where(or_(
                func.lower(Account.email) == email,
                func.lower(Account.username) == username.lower(),
                # Legacy usernames may be email-shaped. Do not create two
                # different owners for the same login identifier.
                func.lower(Account.username) == email,
            ))).first():
                error = "Ezekkel az adatokkal nem hozható létre új fiók. Ha már regisztráltál, jelentkezz be."
            else:
                account = Account(
                    subject=str(uuid4()), username=username, email=email,
                    password_hash=generate_password_hash(password), active=True,
                    version=1, email_verified=False,
                )
                db.session.add(account)
                db.session.add(Membership(account_subject=account.subject, client_id=client.client_id))
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    error = "Ezekkel az adatokkal nem hozható létre új fiók. Ha már regisztráltál, jelentkezz be."
                else:
                    login_account(account)
                    flash("Elkészült a Micimackó-fiókod.", "success")
                    return _after_login(next_url)
    return render_template("register.html", error=error, client=client, next_url=next_url)


@web.route("/account", methods=["GET", "POST"])
@authenticated
def account_page():
    account = current_account()
    error = None
    if request.method == "POST":
        require_csrf()
        email = request.form.get("email", "").strip().lower()
        if not _credential_limit("account-email", account.subject):
            error = "Túl sok próbálkozás. Kérjük, próbáld újra 15 perc múlva."
        elif not _email_valid(email):
            error = "Adj meg egy érvényes, legfeljebb 120 karakteres e-mail-címet."
        elif not _password_matches(account, request.form.get("current_password", "")):
            error = "A jelenlegi jelszó nem megfelelő."
        elif db.session.execute(db.select(Account.subject).where(
            Account.subject != account.subject,
            or_(func.lower(Account.email) == email, func.lower(Account.username) == email),
        )).first():
            error = "Ez az e-mail-cím nem használható. Válassz másik címet."
        elif email == account.email:
            flash("Az e-mail-címed nem változott.", "success")
            return redirect(url_for("web.account_page"))
        else:
            old_version, old_hash, old_email = account.version, account.password_hash, account.email
            try:
                changed = db.session.execute(update(Account).where(
                    Account.subject == account.subject, Account.version == old_version,
                    Account.password_hash == old_hash, Account.email == old_email,
                    Account.active.is_(True),
                ).values(email=email, email_verified=False, version=old_version + 1)
                  .execution_options(synchronize_session=False))
                if changed.rowcount != 1:
                    db.session.rollback()
                    logout_account()
                    flash("A fiókod adatai időközben megváltoztak. Jelentkezz be újra.", "error")
                    return redirect(url_for("web.login"))
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                error = "Ez az e-mail-cím nem használható. Válassz másik címet."
            else:
                db.session.refresh(account)
                login_account(account)
                flash("Az e-mail-címed módosult. A többi bejelentkezésedet lezártuk.", "success")
                return redirect(url_for("web.account_page"))
    memberships = db.session.execute(
        db.select(Client).join(Membership, Membership.client_id == Client.client_id)
        .where(Membership.account_subject == account.subject, Client.active.is_(True)).order_by(Client.name)
    ).scalars().all()
    return render_template("account.html", error=error, memberships=memberships)


@web.route("/password", methods=["GET", "POST"])
@authenticated
def password():
    account = current_account()
    error = None
    if request.method == "POST":
        require_csrf()
        new_password = request.form.get("new_password", "")
        if not _credential_limit("change-password", account.subject):
            error = "Túl sok próbálkozás. Kérjük, próbáld újra 15 perc múlva."
        elif not _password_matches(account, request.form.get("current_password", "")):
            error = "A jelenlegi jelszó nem megfelelő."
        elif not _password_valid(new_password):
            error = "A jelszó hossza 12–1024 karakter lehet."
        elif new_password != request.form.get("new_password_confirm", ""):
            error = "A két új jelszó nem egyezik."
        else:
            old_version, old_hash = account.version, account.password_hash
            changed = db.session.execute(update(Account).where(
                Account.subject == account.subject, Account.version == old_version,
                Account.password_hash == old_hash, Account.active.is_(True),
            ).values(password_hash=generate_password_hash(new_password), version=old_version + 1)
              .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                db.session.rollback()
                logout_account()
                flash("A fiókod adatai időközben megváltoztak. Jelentkezz be újra.", "error")
                return redirect(url_for("web.login"))
            db.session.commit()
            db.session.refresh(account)
            login_account(account)
            flash("A jelszavad módosult. A többi bejelentkezésedet lezártuk.", "success")
            return redirect(url_for("web.account_page"))
    return render_template("password.html", error=error)


@web.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        require_csrf()
        email = request.form.get("email", "").strip().lower()
        allowed = _credential_limit("recovery-ip", request.remote_addr or "unknown", 20)
        allowed = _credential_limit("recovery-email", email, 3) and allowed
        account = db.session.execute(db.select(Account).where(
            func.lower(Account.email) == email, Account.active.is_(True),
        )).scalar_one_or_none() if allowed and _email_valid(email) else None
        if account is not None:
            raw_token = token_urlsafe(32)
            token_hash = sha256(raw_token.encode()).hexdigest()
            db.session.add(PasswordReset(
                token_hash=token_hash, account_subject=account.subject,
                version=account.version, expires_at=int(time()) + 1800,
            ))
            db.session.commit()
            try:
                send_reset_email(account.email, raw_token)
            except Exception:
                # Never log SMTP exceptions, recipient addresses or recovery URLs.
                current_app.logger.warning("Account recovery email could not be delivered.")
                db.session.execute(db.delete(PasswordReset).where(PasswordReset.token_hash == token_hash))
                db.session.commit()
        flash(_RESET_REPLY, "success")
        return redirect(url_for("web.forgot_password"))
    return render_template("forgot_password.html")


@web.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    now = int(time())
    reset = db.session.get(PasswordReset, sha256(token.encode()).hexdigest()) if 20 <= len(token) <= 128 else None
    account = db.session.get(Account, reset.account_subject) if reset else None
    valid = bool(reset and account and account.active and reset.version == account.version
                 and reset.consumed_at is None and reset.expires_at > now)
    error = None
    if not valid:
        return render_template("reset_password.html", invalid=True), 400
    if request.method == "POST":
        require_csrf()
        password = request.form.get("new_password", "")
        if not _credential_limit("reset-password", reset.token_hash):
            error = "Túl sok próbálkozás. Kérj új jelszó-visszaállítási hivatkozást."
        elif not _password_valid(password):
            error = "A jelszó hossza 12–1024 karakter lehet."
        elif password != request.form.get("new_password_confirm", ""):
            error = "A két új jelszó nem egyezik."
        else:
            consumed = db.session.execute(update(PasswordReset).where(
                PasswordReset.token_hash == reset.token_hash,
                PasswordReset.consumed_at.is_(None), PasswordReset.expires_at > now,
            ).values(consumed_at=now).execution_options(synchronize_session=False))
            changed = db.session.execute(update(Account).where(
                Account.subject == account.subject, Account.version == reset.version,
                Account.active.is_(True),
            ).values(password_hash=generate_password_hash(password), version=reset.version + 1)
              .execution_options(synchronize_session=False)) if consumed.rowcount == 1 else None
            if changed is None or changed.rowcount != 1:
                db.session.rollback()
                return render_template("reset_password.html", invalid=True), 400
            db.session.commit()
            logout_account()
            flash("Elmentettük az új jelszavadat. Jelentkezz be vele a fiókodba.", "success")
            return redirect(url_for("web.login"))
    return render_template("reset_password.html", invalid=False, error=error)


@web.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "POST":
        require_csrf()
        account = current_account()
        if account is not None:
            db.session.execute(update(Account).where(Account.subject == account.subject)
                               .values(version=Account.version + 1))
            db.session.commit()
        logout_account()
        flash("Kijelentkeztél a Micimackó-fiókodból.", "success")
        return redirect(url_for("web.login"))
    if current_account() is None:
        # A connected app may already have revoked this session over the
        # authenticated backchannel. Remove its stale cookie without asking for
        # a second click. A valid browser account still requires the POST below.
        logout_account()
        flash("Kijelentkeztél a Micimackó-fiókodból.", "success")
        return redirect(url_for("web.login"))
    return render_template("logout.html")
