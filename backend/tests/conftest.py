"""
Test setup: no Docker, Postgres, Redis or MLflow needed.
  - SQLite replaces Postgres (DATABASE_URL is read at import time, so set it first)
  - mlflow is stubbed (it is a heavy dependency and irrelevant to these tests)
  - Celery `.delay()` is patched per test where an endpoint queues work
"""
import os
import sys
import types
import tempfile
from pathlib import Path

_db = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
os.environ["API_KEY"] = "test-key"
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

if "mlflow" not in sys.modules:
    stub = types.ModuleType("mlflow")
    stub.set_tracking_uri = lambda *a, **k: None
    stub.set_experiment = lambda *a, **k: None
    sys.modules["mlflow"] = stub

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
