"""Development entrypoint.

``flask run`` uses this via FLASK_APP=run.py, and ``python run.py`` works
too. Production uses gunicorn: ``gunicorn "run:app"``.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
