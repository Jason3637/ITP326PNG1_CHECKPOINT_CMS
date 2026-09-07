"""Password hashing, at-rest secret encryption, and backup-code helpers."""

import secrets

from cryptography.fernet import Fernet
from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

_PW_METHOD = "pbkdf2:sha256"  # strong, pure-Python, no OpenSSL/scrypt dependency

# Backup-code alphabet: no 0/O/1/I to avoid transcription errors.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10


# --------------------------------------------------------------------- passwords
def hash_password(plaintext: str) -> str:
    return generate_password_hash(plaintext, method=_PW_METHOD)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    return check_password_hash(stored_hash, plaintext)


# ------------------------------------------------------------- TOTP secret crypto
def _fernet() -> Fernet:
    key = current_app.config.get("MFA_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "MFA_ENCRYPTION_KEY is not configured - cannot encrypt/decrypt TOTP secrets."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a TOTP secret for storage in users.totp_secret."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


# ------------------------------------------------------------------- backup codes
def generate_backup_codes(count: int) -> list[str]:
    """Return `count` fresh plaintext backup codes (format: XXXXX-XXXXX)."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def _normalize_code(code: str) -> str:
    return code.strip().upper().replace(" ", "")


def hash_backup_code(code: str) -> str:
    return generate_password_hash(_normalize_code(code), method=_PW_METHOD)


def verify_backup_code(code: str, stored_hash: str) -> bool:
    return check_password_hash(stored_hash, _normalize_code(code))
