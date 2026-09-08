"""Shared model helpers: portable JSON type and enum builder."""

import enum

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db

# JSONB on Postgres (indexable, typed), plain JSON elsewhere (e.g. SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def pg_enum(enum_cls: type[enum.Enum], name: str) -> db.Enum:
    """A native Postgres ENUM that stores the member *value* (not its name)."""
    return db.Enum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )
