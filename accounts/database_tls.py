"""Use the runtime's trusted roots automatically; never learn trust from a peer."""
import ssl

from mysql.connector.errors import Error as MySQLError
from sqlalchemy.exc import DBAPIError


def tls_connect_args():
    # Connector/Python's pure implementation loads the OS trust store with
    # SSLContext.load_default_certs(). The C extension uses libmysql's separate
    # CA configuration and can reject public certificates without ssl_ca.
    # Verification also rejects a greeting without TLS BEFORE authentication.
    return {
        'use_pure': True,
        'ssl_disabled': False,
        'ssl_verify_cert': True,
        'ssl_verify_identity': True,
    }


TLS_MESSAGES = {
    'DATABASE_TLS_UNSUPPORTED': 'Az adatbázis-kiszolgáló nem kínál TLS-titkosítást ezen a címen és porton. A szolgáltatónál TLS-képes végpont szükséges. Az alkalmazás nem küldte el a belépési adatokat.',
    'DATABASE_TLS_HOSTNAME': 'Az adatbázis tanúsítványa más kiszolgálónévre szól. A szolgáltató TLS-kapcsolathoz megadott kiszolgálónevét használd az adatbázis-URL-ben, ne IP-címet vagy saját álnevet.',
    'DATABASE_TLS_CERTIFICATE': 'Az adatbázis tanúsítványát az automatikusan használt megbízható tanúsítványtár nem igazolja. A szolgáltatónak érvényes, teljes tanúsítványláncot vagy külön hiteles CA-tanúsítványt kell biztosítania.',
    'DATABASE_TLS_VALIDITY': 'Az adatbázis tanúsítványa lejárt vagy még nem érvényes. A szolgáltatónak a tanúsítványt, illetve a kiszolgáló idejét kell ellenőriznie.',
    'DATABASE_TLS': 'Az adatbázis titkosított kapcsolata sikertelen. Az automatikus tanúsítványkezelés aktív; ellenőrizendő a szolgáltató TLS-végpontja és tanúsítványa. Titkosítatlan kapcsolatra nem váltottunk.',
}


def tls_error_code(error):
    """Inspect typed exception causes; only constant messages reach the logs."""
    pending, seen = [error], set()
    generic_tls = False
    while pending and len(seen) < 20:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            code = getattr(current, 'verify_code', None)
            if code in {62, 64}:
                return 'DATABASE_TLS_HOSTNAME'
            if code in {9, 10}:
                return 'DATABASE_TLS_VALIDITY'
            return 'DATABASE_TLS_CERTIFICATE'
        if isinstance(current, ssl.SSLError):
            generic_tls = True
        if isinstance(current, MySQLError) and current.errno in {2026, 3159}:
            generic_tls = True
            # Exact message from the pinned driver's no-TLS handshake branch.
            # Never echo driver messages, which may contain credentials/SQL.
            if current.errno == 2026 and current.msg == "SSL is required but the server doesn't support it":
                return 'DATABASE_TLS_UNSUPPORTED'
        if isinstance(current, DBAPIError):
            pending.append(current.orig)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
    return 'DATABASE_TLS' if generic_tls else None
