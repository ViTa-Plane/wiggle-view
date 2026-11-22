import base64
import hashlib
import os
import platform
import threading
from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(Exception):
    """Raised when encrypting or decrypting data fails."""


_MAGIC_PREFIX = b"WVENC1"
_FERNET_LOCK = threading.Lock()
_CACHED_FERNET = None


def _build_device_seed() -> bytes:
    """Builds a machine-scoped seed without relying solely on mutable hostnames."""
    parts = [
        platform.system(),
        platform.machine(),
        os.environ.get("USERNAME") or os.environ.get("USER") or "",
        os.environ.get("COMPUTERNAME") or platform.node() or "",
    ]
    return "|".join(parts).encode("utf-8", errors="replace")


def get_or_create_device_key(key_path: str | None = None) -> bytes:
    """Returns a valid 44-byte URL-safe base64 Fernet key (decoding to 32 bytes)."""
    if key_path:
        abs_key_path = os.path.abspath(key_path)
        if os.path.exists(abs_key_path):
            with open(abs_key_path, "rb") as f:
                existing = f.read().strip()
            if existing:
                Fernet(existing)
                return existing

        os.makedirs(os.path.dirname(abs_key_path) or ".", exist_ok=True)
        generated = Fernet.generate_key()
        tmp_path = f"{abs_key_path}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(generated)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, abs_key_path)
        return generated

    seed = _build_device_seed()
    salt = b"wiggleview-sqlite-thumb-cache-v1"
    raw_32 = hashlib.pbkdf2_hmac("sha256", seed, salt, iterations=100_000, dklen=32)
    return base64.urlsafe_b64encode(raw_32)


def _get_fernet(key_path: str | None = None) -> Fernet:
    global _CACHED_FERNET
    if _CACHED_FERNET is not None and key_path is None:
        return _CACHED_FERNET
    with _FERNET_LOCK:
        if key_path is None and _CACHED_FERNET is not None:
            return _CACHED_FERNET
        key = get_or_create_device_key(key_path=key_path)
        fernet = Fernet(key)
        if key_path is None:
            _CACHED_FERNET = fernet
        return fernet


def encrypt_data(data: bytes, key_path: str | None = None) -> bytes:
    """Encrypts bytes and prepends a versioned magic header. Raises EncryptionError on failure."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise EncryptionError(f"Expected bytes-like data to encrypt, got {type(data).__name__}")
    raw = bytes(data)
    try:
        token = _get_fernet(key_path=key_path).encrypt(raw)
        return _MAGIC_PREFIX + token
    except Exception as exc:
        raise EncryptionError(f"Failed to encrypt payload: {exc}") from exc


def decrypt_data(payload: bytes, key_path: str | None = None, allow_unencrypted: bool = False) -> bytes:
    """
    Decrypts payloads produced by encrypt_data().
    Raises EncryptionError on invalid/corrupted ciphertext unless allow_unencrypted=True
    and the payload lacks _MAGIC_PREFIX.
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise EncryptionError(f"Expected bytes-like payload to decrypt, got {type(payload).__name__}")
    raw = bytes(payload)

    if not raw.startswith(_MAGIC_PREFIX):
        if allow_unencrypted:
            return raw
        raise EncryptionError("Payload is missing expected encryption header.")

    token = raw[len(_MAGIC_PREFIX):]
    try:
        return _get_fernet(key_path=key_path).decrypt(token)
    except InvalidToken as exc:
        raise EncryptionError("Decryption failed: invalid token or mismatched key.") from exc
    except Exception as exc:
        raise EncryptionError(f"Decryption failed: {exc}") from exc

# def database_doctor ():
#     from cache_db_doctor import audit_database, repair_database
#     report = audit_database("thumbnails_cache.db", magic_prefix=b"WVENC1")
#     if not report.is_healthy:
#         repair_res = repair_database(
#             "thumbnails_cache.db",
#             magic_prefix=b"WVENC1",
#             make_backup=True,
#             run_vacuum=True,
#         )
