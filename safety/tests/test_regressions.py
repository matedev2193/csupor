"""Invalid request input must fail closed without an unhandled exception."""
import pytest

from safety import create_app


@pytest.mark.parametrize('token', ['é', '🍯', 'wrong-ascii-token'])
def test_invalid_unicode_csrf_is_rejected_as_bad_request(tmp_path, token):
    app = create_app({
        'TESTING': True,
        'SECRET_KEY': 'csrf-regression-' + 's' * 40,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(tmp_path / 'training.sqlite'),
        'SAFETY_UPLOAD_DIR': str(tmp_path / 'materials'),
        'SAFETY_OIDC_ISSUER': 'http://localhost:9001',
        'SAFETY_OIDC_REDIRECT_URI': 'http://localhost/auth/callback',
        'SAFETY_OIDC_CLIENT_SECRET': 'separate-client-' + 'c' * 40,
        'SAFETY_ALLOW_HTTP': True,
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session['safety_csrf'] = 'expected-ascii-token'
    # The request must be rejected before authentication/database work. TESTING
    # propagates exceptions, catching compare_digest's non-ASCII str failure.
    response = client.post('/auth/logout', data={'csrf_token': token})
    assert response.status_code == 400
    assert response.headers['Cache-Control'] == 'no-store'
