"""Thin wrapper around the Supabase Storage API.

All member files (ID docs, receipts, loan files) live in one **private** bucket
named by ``SUPABASE_STORAGE_BUCKET``. Postgres only ever stores the object path;
the bytes never touch the database or local disk.
"""

from __future__ import annotations

import threading

from flask import current_app
from supabase import create_client

_client = None
_client_lock = threading.Lock()
_ensured_buckets: set[str] = set()


class StorageError(Exception):
    pass


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                url = current_app.config.get("SUPABASE_URL")
                key = current_app.config.get("SUPABASE_SERVICE_KEY")
                if not url or not key:
                    raise StorageError(
                        "SUPABASE_URL / SUPABASE_SERVICE_KEY are not configured."
                    )
                _client = create_client(url, key)
    return _client


def _bucket_name() -> str:
    name = current_app.config.get("SUPABASE_STORAGE_BUCKET")
    if not name:
        raise StorageError("SUPABASE_STORAGE_BUCKET is not configured.")
    return name


def ensure_bucket() -> str:
    """Create the private bucket if it does not exist yet. Idempotent."""
    name = _bucket_name()
    if name in _ensured_buckets:
        return name
    client = _get_client()
    try:
        client.storage.get_bucket(name)
    except Exception:
        try:
            client.storage.create_bucket(
                name,
                name,
                {
                    "public": False,
                    "file_size_limit": current_app.config.get("DOCUMENT_MAX_BYTES"),
                },
            )
        except Exception as exc:  # already exists / race - tolerate
            if "already exists" not in str(exc).lower():
                raise StorageError(f"Could not create bucket '{name}': {exc}") from exc
    _ensured_buckets.add(name)
    return name


def _storage():
    ensure_bucket()
    return _get_client().storage.from_(_bucket_name())


def upload_file(path: str, data: bytes, content_type: str, *, upsert: bool = False) -> str:
    """Upload bytes to `path` inside the bucket. Returns the stored object path."""
    try:
        _storage().upload(
            path,
            data,
            {"content-type": content_type, "upsert": "true" if upsert else "false"},
        )
    except Exception as exc:
        raise StorageError(f"Upload failed for '{path}': {exc}") from exc
    return path


def get_signed_url(path: str, expires_in: int = 600) -> str:
    """Return a time-limited download URL for a private object."""
    try:
        resp = _storage().create_signed_url(path, expires_in)
    except Exception as exc:
        raise StorageError(f"Could not sign '{path}': {exc}") from exc

    signed = None
    if isinstance(resp, dict):
        signed = resp.get("signedURL") or resp.get("signedUrl") or resp.get("signed_url")
    if not signed:
        raise StorageError(f"Supabase returned no signed URL for '{path}': {resp}")

    if signed.startswith("http"):
        return signed
    base = current_app.config["SUPABASE_URL"].rstrip("/")
    return f"{base}/storage/v1{signed if signed.startswith('/') else '/' + signed}"


def delete_file(path: str) -> None:
    try:
        _storage().remove([path])
    except Exception as exc:
        raise StorageError(f"Delete failed for '{path}': {exc}") from exc
