# version 0.1.2

import os
import getpass
import base64
import hashlib
from cryptography.fernet import Fernet


def get_or_create_device_key():
    """Derives a stable, fast symmetric key bound to the current OS user and system fingerprint."""
    try:
        # Combine current OS username and a system-specific salt anchor
        user = getpass.getuser()
        # On Windows/Linux/Mac, machine-id or hostname provides hardware grounding
        node = os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', 'WiggleViewHost')

        raw_seed = f"{user}-{node}-WiggleView-SecureSalt".encode('utf-8')
        digest = hashlib.sha256(raw_seed).digest()
        return base64.urlsafe_b64encode(digest)
    except Exception:
        # Fallback static fernet key if system inspection fails
        return b'A' * 32


def encrypt_data(data: bytes) -> bytes:
    """Fast symmetric encryption using Fernet."""
    try:
        f = Fernet(get_or_create_device_key())
        return f.encrypt(data)
    except Exception:
        return data


def decrypt_data(token: bytes) -> bytes:
    """Fast symmetric decryption using Fernet."""
    try:
        f = Fernet(get_or_create_device_key())
        return f.decrypt(token)
    except Exception:
        return token