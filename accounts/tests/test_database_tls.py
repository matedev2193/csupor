"""Exercise the deployed driver against a real, isolated MySQL TLS greeting."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import socket
import ssl
import struct
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import mysql.connector
from mysql.connector.constants import ClientFlag
from mysql.connector.errors import Error as MySQLError
import pytest
from sqlalchemy.exc import InterfaceError

from accounts import create_app
from accounts.models import db
from accounts.database_tls import tls_error_code
from accounts.errors import diagnostic
from accounts.start import _retryable


@pytest.fixture
def connection_args(tmp_path, monkeypatch):
    def build(host, port, *, ca=None, suffix=''):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_file = tmp_path / 'account-signing-key.pem'
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        app = create_app({
            'TESTING': True, 'ACCOUNTS_SECRET_KEY': 'test-key-' + 'x' * 40,
            'ACCOUNTS_DATABASE_URL': f'mysql://test_user:test_password@{host}:{port}/test{suffix}',
            'ACCOUNTS_ISSUER': 'https://accounts.example.invalid',
            'ACCOUNTS_SIGNING_KEY_FILE': str(key_file), 'ACCOUNTS_DATABASE_CA_CERT': ca,
        })
        with app.app_context():
            args, kwargs = db.engine.dialect.create_connect_args(db.engine.url)
            assert not args
            kwargs.update(app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args'])
            db.engine.dispose()
        return kwargs
    return build


def certificates(tmp_path, *, host='localhost', expired=False):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Isolated test CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = (x509.CertificateBuilder()
              .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
              .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(days=2))
              .not_valid_after(now + timedelta(days=-1 if expired else 1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    ca_file, cert_file, key_file = [tmp_path / name for name in ('ca.pem', 'server.pem', 'key.pem')]
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return ca_file, context


def packet(payload, sequence):
    return len(payload).to_bytes(3, 'little') + bytes([sequence]) + payload


def read_exact(stream, length):
    result = b''
    while len(result) < length:
        chunk = stream.recv(length - len(result))
        if not chunk:
            return result
        result += chunk
    return result


def read_packet(stream):
    header = read_exact(stream, 4)
    return read_exact(stream, int.from_bytes(header[:3], 'little')) if len(header) == 4 else b''


@contextmanager
def mysql_peer(context):
    """No DB or credentials required; stop after the authentication boundary."""
    observed = {'plain': b'', 'encrypted': b'', 'errors': []}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)
        port = listener.getsockname()[1]

        def serve():
            try:
                with listener.accept()[0] as stream:
                    stream.settimeout(5)
                    flags = ClientFlag.PROTOCOL_41 | ClientFlag.SECURE_CONNECTION | ClientFlag.PLUGIN_AUTH
                    if context:
                        flags |= ClientFlag.SSL
                    greeting = (b'\x0a8.0.36\x00' + struct.pack('<I', 1) + b'12345678\x00'
                                + struct.pack('<HBHHB', flags & 0xffff, 45, 2, flags >> 16, 21)
                                + b'\x00' * 10 + b'abcdefghijkl\x00mysql_native_password\x00')
                    stream.sendall(packet(greeting, 0))
                    observed['plain'] = read_packet(stream)
                    if not observed['plain'] or context is None:
                        return
                    with context.wrap_socket(stream, server_side=True) as secured:
                        observed['encrypted'] = read_packet(secured)
                        # The fixture intentionally has no real database/login.
                        secured.sendall(packet(b'\xff' + struct.pack('<H', 1045) + b'#28000Test complete', 3))
            except ssl.SSLError:
                pass  # Expected when the client rejects our certificate.
            except Exception as error:
                observed['errors'].append(error)

        thread = Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield port, observed
        finally:
            thread.join(timeout=6)
            assert not thread.is_alive(), 'TLS fixture did not finish'
            assert not observed['errors']


@pytest.mark.parametrize('trust', ['system', 'provider-ca'])
@pytest.mark.parametrize('suffix', ['', '?ssl-mode=REQUIRED'])
def test_verified_tls_needs_no_manual_settings_with_trusted_certificate(tmp_path, monkeypatch, connection_args, trust, suffix):
    ca_file, context = certificates(tmp_path)
    if trust == 'system':
        # Simulate a publicly trusted CA without external network or secrets.
        monkeypatch.setenv('SSL_CERT_FILE', str(ca_file))
    with mysql_peer(context) as (port, observed):
        kwargs = connection_args('localhost', port, ca=ca_file.read_text() if trust == 'provider-ca' else None, suffix=suffix)
        if trust == 'system':
            assert 'ssl_ca' not in kwargs
        with pytest.raises(MySQLError) as result:
            mysql.connector.connect(**kwargs)
        assert result.value.errno == 1045  # TLS passed; reached the test server's auth reply.
    assert len(observed['plain']) == 32  # SSLRequest contains no username/password.
    assert b'test_user\x00' in observed['encrypted']


@pytest.mark.parametrize(('fault', 'code'), [
    ('no-tls', 'DATABASE_TLS_UNSUPPORTED'),
    ('untrusted', 'DATABASE_TLS_CERTIFICATE'),
    ('hostname', 'DATABASE_TLS_HOSTNAME'),
    ('expired', 'DATABASE_TLS_VALIDITY'),
])
def test_tls_failure_never_sends_authentication_or_retries_in_plaintext(tmp_path, monkeypatch, connection_args, fault, code):
    ca_file, context = certificates(tmp_path, host='wrong.invalid' if fault == 'hostname' else 'localhost', expired=fault == 'expired')
    if fault != 'untrusted':
        monkeypatch.setenv('SSL_CERT_FILE', str(ca_file))
    with mysql_peer(None if fault == 'no-tls' else context) as (port, observed):
        with pytest.raises(MySQLError) as failure:
            mysql.connector.connect(**connection_args('localhost', port))
    assert observed['encrypted'] == b''
    assert len(observed['plain']) == (0 if fault == 'no-tls' else 32)
    # Exercise the same SQLAlchemy wrapper the startup path sees, including the
    # pure driver's errno=2055 wrapper around certificate-verification errors.
    error = InterfaceError('PRIVATE_SQL', {'password': 'PRIVATE_PASSWORD'}, failure.value, connection_invalidated=True)
    assert tls_error_code(error) == code
    assert _retryable(error) is False
    message = diagnostic(error)
    assert f'[ACCOUNTS_STARTUP_{code}]' in message
    assert not any(secret in message for secret in ('PRIVATE_SQL', 'PRIVATE_PASSWORD', 'test_user', 'test_password'))
