"""Session-wide pytest fixtures: one isolated application per test.

Every fixture here points the application at a SQLite file inside pytest's
``tmp_path``, so the whole suite runs on a temporary database per test and no
test touches ``instance/taskboard.sqlite3``.

The fixtures are built on Flask's own ``app.test_client()``. ``pytest-flask`` is
deliberately not used, so nothing here needs a plugin, a live server or a
``tests/__init__.py``; ``pyproject.toml`` already puts the repository root on
``sys.path`` and owns the coverage gate, so this file overrides neither and
manipulates neither.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

import seed
from app import create_app

# Name of the per-test database inside ``tmp_path``. Held in one constant so the
# application fixture and ``seeded_client`` cannot end up pointing at different
# files.
DATABASE_FILENAME = "taskboard.sqlite3"

# Fixed session key, so the flash cookie and every ``flash()`` assertion are
# deterministic instead of depending on the development fallback that
# ``app/config.py`` supplies when ``SECRET_KEY`` is unset.
TEST_SECRET_KEY = "test-secret-key"


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Return the path of this test's own SQLite database."""
    return str(tmp_path / DATABASE_FILENAME)


@pytest.fixture
def app(db_path: str) -> Iterator[Flask]:
    """Yield the application built on an empty, initialised database of its own."""
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": TEST_SECRET_KEY,
            "DATABASE": db_path,
        }
    )
    yield application


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Return a test client for the unseeded application."""
    return app.test_client()


@pytest.fixture
def seeded_client(app: Flask, db_path: str) -> FlaskClient:
    """Return a test client whose application shares one freshly seeded database."""
    seed.seed_database(db_path)
    return app.test_client()
