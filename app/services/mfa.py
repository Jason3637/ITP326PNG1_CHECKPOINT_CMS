"""TOTP secret generation, provisioning URIs, and QR rendering."""

import base64
import io

import pyotp
import qrcode
from flask import current_app


def generate_totp_secret() -> str:
    """A fresh base32 TOTP secret (plaintext - encrypt before storing)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str) -> str:
    """otpauth:// URI for an authenticator app."""
    issuer = current_app.config.get("TOTP_ISSUER", "Prime's Vault")
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def qr_data_uri(provisioning_uri_str: str) -> str:
    """Render the provisioning URI as a base64 PNG data URI for <img src=...>."""
    img = qrcode.make(provisioning_uri_str)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """Verify a 6-digit code. `valid_window=1` tolerates +/-30s of clock drift."""
    if not code:
        return False
    return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=valid_window)
