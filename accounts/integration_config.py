"""Deployment-selected application roles; no client id is implicitly trusted."""
import re

from flask import abort, current_app, jsonify


_ROLE_KEYS = {'csupor': 'OIDC_CLIENT_ID', 'safety': 'SAFETY_OIDC_CLIENT_ID'}
_CLIENT_ID = re.compile(r'[A-Za-z0-9._-]{1,80}')


def integration_status(role):
    """Describe configuration only, independently of accounts and memberships."""
    key = _ROLE_KEYS[role]
    value = current_app.config.get(key)
    code = message = None
    if value is None or value == '' or (isinstance(value, str) and not value.strip()):
        code = f'ACCOUNTS_INTEGRATION_{role.upper()}_MISSING'
        message = f'A fiókkezelőben hiányzik a(z) {key} környezeti változó. Az alkalmazások összekapcsolása még nincs bekapcsolva.'
    elif not isinstance(value, str) or _CLIENT_ID.fullmatch(value) is None:
        code = f'ACCOUNTS_INTEGRATION_{role.upper()}_INVALID'
        message = f'A fiókkezelőben a(z) {key} értéke érvénytelen. 1–80 betű, szám, pont, aláhúzás vagy kötőjel használható.'
    else:
        other_key = next(other for other_role, other in _ROLE_KEYS.items() if other_role != role)
        other_value = current_app.config.get(other_key)
        if (isinstance(other_value, str) and _CLIENT_ID.fullmatch(other_value)
                and value.casefold() == other_value.casefold()):
            code = f'ACCOUNTS_INTEGRATION_{role.upper()}_CONFLICT'
            message = 'A fiókkezelő OIDC_CLIENT_ID és SAFETY_OIDC_CLIENT_ID változóinak két külön alkalmazást kell azonosítaniuk.'
    return {'role': role, 'env_key': key, 'client_id': value if code is None else None,
            'code': code, 'message': message}


def integration_client_id(role):
    return integration_status(role)['client_id']


def require_integration_client(role):
    status = integration_status(role)
    if status['code']:
        response = jsonify(error='integration_not_configured', code=status['code'], message=status['message'])
        response.status_code = 503
        abort(response)
    return status['client_id']


def init_integration_config(app):
    # Startup records configuration validation without querying any application
    # data or preventing ordinary account login. Guards validate again when used.
    with app.app_context():
        app.extensions['accounts_integration_status'] = {
            role: integration_status(role) for role in _ROLE_KEYS
        }
