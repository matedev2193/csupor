"""Generate a private signing key once: python -m accounts.keygen /private/key.pem"""
import argparse
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main():
    parser = argparse.ArgumentParser(description='Create a new private RSA account signing key without overwriting files.')
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    descriptor = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(pem)
    print('Private signing key created. Keep it outside the repository and backed up securely.')


if __name__ == '__main__':
    main()
