"""Launch registered applications without accepting browser-supplied URLs."""
from urllib.parse import urlsplit

from flask import current_app


def application_url(client):
    from . import allowed_url
    if not client or not client.active:
        return None
    for callback in client.redirect_uris or []:
        if allowed_url(callback, current_app.config.get('ACCOUNTS_ALLOW_HTTP', False)):
            parsed = urlsplit(callback)
            # Callback origins are explicitly registered by the account admin.
            # Do not launch an OIDC callback without its authorization code.
            return f'{parsed.scheme}://{parsed.netloc}/'
    return None
