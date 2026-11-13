"""Identity-only data; this package deliberately imports no CSUPOR models."""
import time
from uuid import uuid4

from authlib.oauth2.rfc6749 import ClientMixin, AuthorizationCodeMixin, TokenMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


class Account(db.Model):
    __tablename__ = 'accounts'
    subject = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    email_verified = db.Column(db.Boolean, nullable=False, default=False)

    def check_password(self, password):
        if not isinstance(password, str) or len(password) > 1024:
            return False
        try:
            return check_password_hash(self.password_hash, password)
        except (ValueError, TypeError):
            return False

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.version = (self.version or 0) + 1


class Client(db.Model, ClientMixin):
    __tablename__ = 'clients'
    client_id = db.Column(db.String(80), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    secret_hash = db.Column(db.String(255), nullable=False)
    redirect_uris = db.Column(db.JSON, nullable=False)
    allow_registration = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    def set_secret(self, secret):
        self.secret_hash = generate_password_hash(secret)

    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        # Explicit redirect_uri is required by the authorization endpoint.
        return self.redirect_uris[0]

    def get_allowed_scope(self, scope):
        return ' '.join(s for s in (scope or '').split() if s in {'openid', 'profile', 'email'})

    def check_redirect_uri(self, uri):
        return self.active and uri in self.redirect_uris

    def check_client_secret(self, secret):
        return self.active and isinstance(secret, str) and len(secret) <= 1024 and check_password_hash(self.secret_hash, secret)

    def check_endpoint_auth_method(self, method, endpoint):
        return method == 'client_secret_basic'

    def check_response_type(self, response_type):
        return self.active and response_type == 'code'

    def check_grant_type(self, grant_type):
        return self.active and grant_type == 'authorization_code'


class Membership(db.Model):
    __tablename__ = 'account_clients'
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject'), primary_key=True)
    client_id = db.Column(db.String(80), db.ForeignKey('clients.client_id'), primary_key=True)


class AuthorizationCode(db.Model, AuthorizationCodeMixin):
    __tablename__ = 'authorization_codes'
    code_hash = db.Column(db.String(64), primary_key=True)
    client_id = db.Column(db.String(80), db.ForeignKey('clients.client_id'), nullable=False)
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject'), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    redirect_uri = db.Column(db.String(2048), nullable=False)
    scope = db.Column(db.String(100), nullable=False)
    nonce = db.Column(db.String(255), nullable=False)
    code_challenge = db.Column(db.String(128), nullable=False)
    code_challenge_method = db.Column(db.String(10), nullable=False)
    auth_time = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)
    consumed_at = db.Column(db.Integer, nullable=True)
    __table_args__ = (db.UniqueConstraint('client_id', 'nonce', name='unique_client_nonce'),)

    def get_redirect_uri(self):
        return self.redirect_uri

    def get_scope(self):
        return self.scope

    def get_nonce(self):
        return self.nonce

    def get_auth_time(self):
        return self.auth_time

    def get_acr(self):
        return None

    def get_amr(self):
        return ['pwd']


class AccessToken(db.Model, TokenMixin):
    __tablename__ = 'access_tokens'
    token_hash = db.Column(db.String(64), primary_key=True)
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject'), nullable=False)
    client_id = db.Column(db.String(80), db.ForeignKey('clients.client_id'), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    scope = db.Column(db.String(100), nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)

    def get_scope(self):
        return self.scope

    def get_expires_in(self):
        return max(0, self.expires_at - int(time.time()))

    def is_expired(self):
        return self.expires_at <= int(time.time())

    def is_revoked(self):
        account = db.session.get(Account, self.account_subject)
        client = db.session.get(Client, self.client_id)
        return (not account or not account.active or account.version != self.version
                or not client or not client.active
                or db.session.get(Membership, (self.account_subject, self.client_id)) is None)


class PasswordReset(db.Model):
    __tablename__ = 'password_resets'
    token_hash = db.Column(db.String(64), primary_key=True)
    account_subject = db.Column(db.String(36), db.ForeignKey('accounts.subject'), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)
    consumed_at = db.Column(db.Integer, nullable=True)


class RateLimit(db.Model):
    __tablename__ = 'rate_limits'
    key_hash = db.Column(db.String(64), primary_key=True)
    window_start = db.Column(db.Integer, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=1)
