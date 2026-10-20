"""Small OIDC provider: Authlib implements the protocol and signed ID tokens."""
import hashlib
import re
import time

from authlib.integrations.flask_oauth2 import AuthorizationServer, ResourceProtector, current_token
from authlib.oauth2.rfc6749 import grants, OAuth2Error, InvalidRequestError
from authlib.oauth2.rfc6750 import BearerTokenValidator
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oidc.core import grants as oidc_grants, UserInfo
from flask import Blueprint, abort, current_app, jsonify, redirect, request, session, url_for
from sqlalchemy.exc import IntegrityError

from .models import Account, AccessToken, AuthorizationCode, Client, Membership, db
from .security import current_account, throttle


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def user_info(account, scope):
    info = UserInfo(sub=account.subject)
    if 'profile' in scope.split():
        info['preferred_username'] = account.username
    if 'email' in scope.split():
        info.update(email=account.email, email_verified=account.email_verified)
    return info


class S256Challenge(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ['S256']

    def validate_code_challenge(self, grant, redirect_uri):
        data = grant.request.payload.data
        if data.get('code_challenge_method') != 'S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data.get('code_challenge', '')):
            raise InvalidRequestError('S256 PKCE is required.')
        super().validate_code_challenge(grant, redirect_uri)


class CodeGrant(grants.AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_basic']

    def save_authorization_code(self, code, oauth_request):
        data = oauth_request.payload.data
        item = AuthorizationCode(
            code_hash=digest(code), client_id=oauth_request.client.client_id,
            account_subject=oauth_request.user.subject, version=oauth_request.user.version,
            redirect_uri=oauth_request.payload.redirect_uri, scope=oauth_request.scope,
            nonce=data['nonce'], code_challenge=data['code_challenge'],
            code_challenge_method='S256', auth_time=session['account_auth_time'],
            expires_at=int(time.time()) + 120,
        )
        db.session.add(item)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise InvalidRequestError('This authorization request has already been used.') from None

    def query_authorization_code(self, code, client):
        if not isinstance(code, str) or len(code) > 255:
            return None
        key = digest(code)
        # Claim the code atomically before emitting credentials. Two workers can
        # never exchange the same code, even on SQLite (where FOR UPDATE is ignored).
        changed = db.session.execute(db.update(AuthorizationCode).where(
            AuthorizationCode.code_hash == key, AuthorizationCode.client_id == client.client_id,
            AuthorizationCode.consumed_at.is_(None), AuthorizationCode.expires_at > int(time.time()),
        ).values(consumed_at=int(time.time())))
        db.session.commit()
        return db.session.get(AuthorizationCode, key) if changed.rowcount == 1 else None

    def delete_authorization_code(self, authorization_code):
        # Keep the used nonce until the daily cleanup; the raw code is never stored.
        pass

    def authenticate_user(self, code):
        account = db.session.get(Account, code.account_subject)
        if (account and account.active and account.version == code.version
                and db.session.get(Membership, (account.subject, code.client_id))):
            return account
        return None


class OpenIDCode(oidc_grants.OpenIDCode):
    def resolve_client_private_key(self, client):
        return current_app.extensions['accounts_signing_key']

    def get_client_algorithm(self, client):
        return 'RS256'

    def get_encode_header(self, client):
        return {'alg': 'RS256', 'kid': current_app.extensions['accounts_key_id'], 'typ': 'JWT'}

    def get_client_claims(self, client):
        return {'iss': current_app.config['ACCOUNTS_ISSUER'], 'aud': [client.client_id],
                'exp': int(time.time()) + current_app.config['ACCOUNTS_TOKEN_TTL']}

    def exists_nonce(self, nonce, oauth_request):
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 255:
            raise InvalidRequestError('A nonce of 16 to 255 characters is required.')
        return db.session.execute(db.select(AuthorizationCode.code_hash).where(
            AuthorizationCode.client_id == oauth_request.payload.client_id,
            AuthorizationCode.nonce == nonce,
        )).first() is not None

    def generate_user_info(self, user, scope):
        return user_info(user, scope)


class TokenValidator(BearerTokenValidator):
    def authenticate_token(self, token_string):
        if len(token_string) > 512:
            return None
        return db.session.get(AccessToken, digest(token_string))


def init_provider(app):
    def query_client(client_id):
        client = db.session.get(Client, client_id)
        return client if client and client.active else None

    def save_token(token, oauth_request):
        db.session.add(AccessToken(
            token_hash=digest(token['access_token']), account_subject=oauth_request.user.subject,
            client_id=oauth_request.client.client_id, version=oauth_request.authorization_code.version,
            scope=token['scope'], expires_at=int(time.time()) + token['expires_in'],
        ))
        db.session.commit()

    server = AuthorizationServer(app, query_client=query_client, save_token=save_token)
    server.register_grant(CodeGrant, [OpenIDCode(require_nonce=True), S256Challenge(required=True)])
    protect = ResourceProtector()
    protect.register_token_validator(TokenValidator())
    bp = Blueprint('oidc', __name__)

    @bp.get('/.well-known/openid-configuration')
    def discovery():
        issuer = app.config['ACCOUNTS_ISSUER']
        return jsonify({
            'issuer': issuer, 'authorization_endpoint': issuer + '/authorize',
            'token_endpoint': issuer + '/token', 'userinfo_endpoint': issuer + '/userinfo',
            'jwks_uri': issuer + '/jwks', 'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code'], 'subject_types_supported': ['public'],
            'id_token_signing_alg_values_supported': ['RS256'],
            'token_endpoint_auth_methods_supported': ['client_secret_basic'],
            'code_challenge_methods_supported': ['S256'],
            'scopes_supported': ['openid', 'profile', 'email'],
            'claims_supported': ['sub', 'iss', 'aud', 'iat', 'exp', 'auth_time', 'nonce',
                                 'preferred_username', 'email', 'email_verified'],
        })

    @bp.get('/jwks')
    def jwks():
        return jsonify(keys=[app.extensions['accounts_public_key']])

    @bp.get('/authorize')
    def authorize():
        account = current_account()
        # Reject malformed clients/callbacks before asking for a password.
        try:
            grant = server.get_consent_grant(end_user=account)
        except OAuth2Error as error:
            return server.handle_error_response(None, error)
        if not request.args.get('redirect_uri') or 'openid' not in (request.args.get('scope') or '').split():
            return jsonify(error='invalid_request', error_description='Explicit redirect_uri and openid scope are required.'), 400
        if not request.args.get('state') or len(request.args['state']) > 2048:
            return jsonify(error='invalid_request', error_description='A state parameter is required.'), 400
        max_age = request.args.get('max_age')
        if max_age is not None and (not max_age.isascii() or not max_age.isdigit() or len(max_age) > 9):
            return jsonify(error='invalid_request'), 400
        marker = digest(request.full_path)
        completed = session.pop('reauth_completed', None) == marker
        force = ('login' in request.args.get('prompt', '').split()
                 or max_age is not None and int(time.time()) - session.get('account_auth_time', 0) >= int(max_age))
        if not account or force and not completed:
            if request.args.get('prompt') == 'none':
                return jsonify(error='login_required'), 400
            session['reauth_request'] = marker
            endpoint = 'web.register' if request.args.get('screen_hint') == 'signup' and not account else 'web.login'
            return redirect(url_for(endpoint, next=request.full_path, client_id=grant.request.client.client_id))
        if not db.session.get(Membership, (account.subject, grant.request.client.client_id)):
            # A central identity does not automatically confer access to any app.
            abort(403, description='Ehhez az alkalmazáshoz még nincs hozzáférésed. Kérd az adminisztrátor segítségét.')
        return server.create_authorization_response(grant_user=account, grant=grant)

    @bp.post('/token')
    def token():
        if not throttle('token', request.remote_addr or '', limit=120, window=60):
            return jsonify(error='temporarily_unavailable'), 429
        return server.create_token_response()

    @bp.get('/userinfo')
    @protect('openid')
    def userinfo():
        account = db.session.get(Account, current_token.account_subject)
        return jsonify(dict(user_info(account, current_token.scope)))

    app.register_blueprint(bp)
    app.extensions['accounts_oauth'] = server
