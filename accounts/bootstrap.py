"""Own-database bootstrap and encrypted, persistent account signing key."""
import base64
from contextlib import contextmanager
from hashlib import sha256
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import current_app
from sqlalchemy import event, select, text

from .models import db
from .errors import StartupError


class StoredSigningKey(db.Model):
    __tablename__ = 'accounts_signing_keys'
    name = db.Column(db.String(32), primary_key=True)
    encrypted_pem = db.Column(db.Text, nullable=False)
    public_key_id = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.Integer, nullable=False, default=lambda: int(time.time()))


def key_id(private_key):
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return sha256(public_der).hexdigest()[:32]


def _cipher():
    # The effective Flask secret is the validated ACCOUNTS_SECRET_KEY (or an
    # explicit test configuration override). It is never stored alongside PEM.
    secret = current_app.secret_key.encode('utf-8')
    return Fernet(base64.urlsafe_b64encode(sha256(b'micimacko-accounts:signing-key:v1:' + secret).digest()))


def _plain_parameters(connection, cursor, statement, parameters, context, executemany):
    """mysql-connector cannot convert SQLAlchemy quoted_name string subclasses."""
    def plain(value):
        return str(value) if isinstance(value, str) and type(value) is not str else value
    def row(values):
        if isinstance(values, dict):
            return {str(k): plain(v) for k, v in values.items()}
        if isinstance(values, (tuple, list)):
            return tuple(plain(v) for v in values)
        return values
    return statement, [row(values) for values in parameters] if executemany else row(parameters)


@contextmanager
def _database_lock():
    with db.engine.connect() as connection:
        dialect = connection.dialect.name
        if dialect not in {'sqlite', 'mysql', 'mariadb'}:
            raise StartupError('ACCOUNTS_DATABASE_URL', 'Automatic account preparation supports SQLite and MySQL/MariaDB.')
        lock_name = None
        if dialect in {'mysql', 'mariadb'}:
            event.listen(connection, 'before_cursor_execute', _plain_parameters, retval=True)
            database = connection.exec_driver_sql('SELECT DATABASE()').scalar_one()
            lock_name = 'accounts-bootstrap-' + sha256(str(database).encode()).hexdigest()[:40]
            locked = connection.execute(text('SELECT GET_LOCK(:name, 20)'), {'name': str(lock_name)}).scalar_one()
            if locked != 1:
                raise StartupError('DATABASE_LOCK', 'Account database preparation is busy; restart to try again.')
            connection.commit()
        try:
            if dialect == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if lock_name is not None:
                connection.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': str(lock_name)})
                connection.commit()


def _signing_key(connection):
    row = connection.execute(select(StoredSigningKey.__table__).where(StoredSigningKey.name == 'active')).mappings().first()
    if row is None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
        connection.execute(StoredSigningKey.__table__.insert().values(
            name='active', encrypted_pem=_cipher().encrypt(pem).decode('ascii'),
            public_key_id=key_id(private_key), created_at=int(time.time()),
        ))
        return private_key
    try:
        pem = _cipher().decrypt(row['encrypted_pem'].encode('ascii'))
        private_key = serialization.load_pem_private_key(pem, password=None)
        if (not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048
                or key_id(private_key) != row['public_key_id']):
            raise ValueError('Invalid key')
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        # Never silently rotate an unreadable key: that could invalidate all
        # clients and hide loss of the original deployment secret.
        raise StartupError('SIGNING_KEY', 'The stored account signing key cannot be opened with the configured secret; no key was replaced.') from None
    return private_key


def prepare_database(*, with_signing_key=False):
    """Serialise initialisation on fresh and existing databases, without resets.

    All account-package models, including admin metadata, must be imported by
    the factory before this function is called. No data is copied from CSUPOR.
    """
    db.session.remove()
    with _database_lock() as connection:
        db.metadata.create_all(connection)
        return _signing_key(connection) if with_signing_key else None
