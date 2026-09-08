"""Standalone Supabase Postgres connectivity check.

Run BEFORE doing any database modeling:

    python scripts/healthcheck_db.py

Exits 0 on a successful connection, 1 otherwise. Reads DATABASE_URL from
the environment (or a local .env file).
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import os

from sqlalchemy import create_engine, text


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("FAIL: DATABASE_URL is not set (check your .env).")
        return 1

    safe = url
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        safe = f"{scheme}://{user}:***@{host}"
    print(f"Connecting to: {safe}")

    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
            db_name = conn.execute(text("SELECT current_database()")).scalar_one()
        print("OK: connection succeeded.")
        print(f"  database: {db_name}")
        print(f"  server:   {version}")
        return 0
    except Exception as exc:
        print(f"FAIL: could not connect.\n  {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
