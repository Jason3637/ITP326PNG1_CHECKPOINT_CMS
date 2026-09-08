"""Shared extension instances.

These are instantiated here without an app and bound later in the
application factory via ``init_app``. Import them from this module
everywhere else to avoid circular imports.
"""

from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
jwt = JWTManager()
