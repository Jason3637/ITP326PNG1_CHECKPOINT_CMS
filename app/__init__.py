"""Application factory for Prime's Vault backend.

Usage::

    from app import create_app
    app = create_app()

Configuration comes entirely from environment variables. In development a
local ``.env`` file (see ``.env.example``) is loaded via ``python-dotenv``.
"""

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

load_dotenv()  # no-op in production where real env vars are set

from .config import get_config
from .extensions import db, jwt, limiter, migrate


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    _init_extensions(app)
    _register_blueprints(app)
    _register_health(app)

    return app


def _init_extensions(app: Flask) -> None:
    db.init_app(app)

    # Import models so their tables are registered on db.metadata before
    # Flask-Migrate builds its autogenerate comparison.
    from app import models  # noqa: F401

    migrate.init_app(app, db)
    jwt.init_app(app)
    limiter.init_app(app)

    # The Next.js frontend is a separate origin. Allow it to call the API and
    # /health, and to send the Authorization (JWT) + Content-Type headers.
    CORS(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]},
                   r"/health": {"origins": app.config["CORS_ORIGINS"]}},
        allow_headers=["Authorization", "Content-Type"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        max_age=3600,
    )


def _register_blueprints(app: Flask) -> None:
    # Imported here to keep the factory import-safe and avoid circulars.
    from .api import api_bp

    app.register_blueprint(api_bp)


def _register_health(app: Flask) -> None:
    from sqlalchemy import text

    @app.get("/health")
    def health():
        """Liveness + DB connectivity check."""
        db_ok = True
        db_error = None
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - diagnostic path
            db_ok = False
            db_error = str(exc)

        status = 200 if db_ok else 503
        return (
            jsonify(
                {
                    "status": "ok" if db_ok else "degraded",
                    "database": "up" if db_ok else "down",
                    "database_error": db_error,
                    "env": app.config.get("FLASK_ENV"),
                }
            ),
            status,
        )
