"""WSGI entrypoint.

``app`` is the WSGI callable used in every environment:

  * production : ``gunicorn "run:app"``  (see Procfile)
  * dev        : ``flask run``  (FLASK_APP=run.py, set in .flaskenv)
                 or ``python run.py``  (the block below)

The ``app.run()`` dev server below runs ONLY when this file is executed
directly - it is never reached under gunicorn or ``flask run``.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
