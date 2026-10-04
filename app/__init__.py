import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import Flask, request, session
from flask_babel import Babel
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

from .i18n import LOGIN_MESSAGE
from .upload_request import UploadRequest


load_dotenv()


db = SQLAlchemy()
babel = Babel()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = LOGIN_MESSAGE
login_manager.localize_callback = str

SUPPORTED_LOCALES = {
    "en": "English",
    "hu": "Magyar",
}
DEFAULT_LOCALE = "en"


def get_locale() -> str:
    """Select the active locale from the session or request headers."""
    selected_locale = session.get("locale")
    if selected_locale in SUPPORTED_LOCALES:
        return selected_locale

    return request.accept_languages.best_match(SUPPORTED_LOCALES.keys()) or DEFAULT_LOCALE


def _build_database_uri() -> str:
    """Build DB URI from DATABASE_URL or MYSQL_* environment variables."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    mysql_user = os.getenv("MYSQL_USER", "root")
    mysql_password = os.getenv("MYSQL_PASSWORD", "")
    mysql_host = os.getenv("MYSQL_HOST", "localhost")
    mysql_port = os.getenv("MYSQL_PORT", "3306")
    mysql_db = os.getenv("MYSQL_DATABASE", "csupor")

    auth = quote_plus(mysql_user)
    if mysql_password:
        auth = f"{auth}:{quote_plus(mysql_password)}"

    return f"mysql+mysqlconnector://{auth}@{mysql_host}:{mysql_port}/{mysql_db}"


def create_app() -> Flask:
    app = Flask(__name__)
    app.request_class = UploadRequest
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = _build_database_uri()
    app.config["EMAIL_SECRET_KEY"] = os.getenv("EMAIL_SECRET_KEY")
    # SQLite is also used by isolated tests; those run the dispatcher explicitly.
    worker_default = not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")
    app.config["EMAIL_WORKER_ENABLED"] = os.getenv(
        "EMAIL_WORKER_ENABLED", "true" if worker_default else "false",
    ).lower() in {"1", "true", "yes", "on"}
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["BABEL_DEFAULT_LOCALE"] = DEFAULT_LOCALE
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"
    # Bound multipart parsing even for chunked requests without Content-Length;
    # leave room for the existing 10 MiB GYAP document plus form overhead.
    app.config["MAX_CONTENT_LENGTH"] = 11 * 1024 * 1024

    db.init_app(app)
    babel.init_app(app, locale_selector=get_locale)
    login_manager.init_app(app)

    from . import routes  # noqa: F401

    routes.init_routes(app)

    from .gyap_forms import gyap

    app.register_blueprint(gyap)

    from .profile_photos import profile_photos

    app.register_blueprint(profile_photos)

    from .worktime import worktime

    app.register_blueprint(worktime)

    from .mail_settings import mail_settings
    from . import notification_models  # noqa: F401 — register durable queue tables.

    app.register_blueprint(mail_settings)

    from .account_display import account_template_context

    app.context_processor(account_template_context)

    with app.app_context():
        from .schema import (
            create_missing_tables,
            ensure_qualification_date_columns,
            ensure_work_assignment_flexible_shift_column,
        )

        create_missing_tables(db.engine, db.metadata)
        ensure_qualification_date_columns(db.engine)
        ensure_work_assignment_flexible_shift_column(db.engine)
        from .leave_approval import initialise_leave_approval_settings

        initialise_leave_approval_settings()

    from .notification_delivery import init_notifications

    init_notifications(app)

    return app
