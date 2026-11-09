"""Kept so the usual launch command still works from the pi folder:

    python -m uvicorn web_app:app --host 127.0.0.1 --port 8000

The web interface lives in carwash/web/app.py.
"""

from carwash.web.app import app

__all__ = ["app"]
