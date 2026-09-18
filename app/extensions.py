"""Shared extension instances.

These are instantiated here without an app and bound later in the
application factory via ``init_app``. Import them from this module
everywhere else to avoid circular imports.
"""

from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()


def _client_ip_key() -> str:
    """Rate-limit key = the real client IP, honoring Railway's proxy the same
    way app.services.audit.client_ip() does (single X-Forwarded-For hop).

    A lazy import (not a top-level one) because app.services.audit imports
    `db` from this module - a top-level import here would be circular.
    """
    from app.services.audit import client_ip

    return client_ip() or "unknown"


# In-memory storage: fine for a single-process deployment (see DEPLOYMENT.md).
# With WEB_CONCURRENCY > 1 each gunicorn worker keeps its own counters, so the
# *effective* limit is roughly (limit x worker count) rather than exact - an
# acceptable tradeoff for now; move to Redis (storage_uri="redis://...") if
# that stops being good enough.
limiter = Limiter(key_func=_client_ip_key, storage_uri="memory://")
