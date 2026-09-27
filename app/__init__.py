import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import Flask, request, session
from flask_babel import Babel
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy


load_dotenv()


db = SQLAlchemy()
babel = Babel()
login_manager = LoginManager()
login_manager.login_view = "login"

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
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = _build_database_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["BABEL_DEFAULT_LOCALE"] = DEFAULT_LOCALE
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = "translations"

    db.init_app(app)
    babel.init_app(app, locale_selector=get_locale)
    login_manager.init_app(app)

    from . import routes  # noqa: F401

    routes.init_routes(app)

    with app.app_context():
        from .schema import create_missing_tables

        create_missing_tables(db.engine, db.metadata)

    return app
