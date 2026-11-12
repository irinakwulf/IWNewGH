"""The application's only SQLite data-access module.

Every connection the product opens and every statement it runs goes through this
module: the five service modules call :func:`get_db`, and no other module under
``app/`` calls ``sqlite3.connect``. ``seed.py`` is the deliberate exception -- it
opens its own connection to the path it is handed, because the ``seed-db``
command and the test fixtures both call it outside a request.

The connection is per request, not per call: :func:`get_db` caches it on
``flask.g`` and :func:`close_db` closes it when Flask tears the application
context down, so a page that reads a task, its comments and its assignee uses one
handle. Two settings are applied when the handle is created, and both carry
meaning:

* ``row_factory = sqlite3.Row`` lets every service read a column by name instead
  of by position, which is what keeps a query's column order from mattering.
* ``PRAGMA foreign_keys = ON`` is the only way SQLite enforces the ``REFERENCES``
  clauses and the ``ON DELETE CASCADE`` the schema declares; SQLite leaves the
  pragma off by default, so without it those clauses would be documentation.

:func:`init_db` and :func:`ensure_schema` reach the database through
``current_app`` and an application context rather than through request globals,
so the same code serves a request, the ``init-db`` and ``seed-db`` commands and
the schema bootstrap the application factory performs at start-up.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import click
from flask import current_app, g
from flask.cli import with_appcontext

if TYPE_CHECKING:  # pragma: no cover - import for the annotation only
    from flask import Flask

__all__ = [
    "SCHEMA_FILENAME",
    "close_db",
    "ensure_schema",
    "get_db",
    "init_app",
    "init_db",
]

# The DDL lives beside this module, inside the package, and is read through
# ``current_app.open_resource`` so it resolves from the installed package rather
# than from whatever directory the process happens to run in.
SCHEMA_FILENAME = "schema.sql"

# The attribute of ``flask.g`` the request's connection is cached under. A
# module constant so :func:`get_db` and :func:`close_db` cannot disagree on it.
_DB_KEY = "db"

# Shown when the seed-db command cannot reach the sample dataset. It names the
# fix rather than the failure, because the cause is always the same one.
_MISSING_SEED_MESSAGE = (
    "seed.py was not found at the repository root; run this command from there."
)


def get_db() -> sqlite3.Connection:
    """Return the connection for the current application context.

    The handle is created on first use and cached on ``flask.g``, so every call
    within one request -- or one CLI command -- returns the same connection.

    Returns:
        The ``sqlite3.Connection`` for ``current_app.config["DATABASE"]``, with
        its row factory set to :class:`sqlite3.Row` and foreign-key enforcement
        switched on. The configured path is absolute by the time the application
        factory finishes, so it is used exactly as configured.
    """
    connection: sqlite3.Connection | None = getattr(g, _DB_KEY, None)
    if connection is None:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        # Per connection, not stored in the file: SQLite defaults it off.
        connection.execute("PRAGMA foreign_keys = ON")
        setattr(g, _DB_KEY, connection)
    return connection


def close_db(exception: BaseException | None = None) -> None:
    """Close the context's connection, if one was ever opened.

    Registered on ``teardown_appcontext`` by :func:`init_app`. Tolerates having
    no connection, since a request that never touched the database never created
    one, and a failing request must still tear down cleanly.

    Args:
        exception: The exception that ended the context, when it ended badly.
            Accepted because Flask passes it to teardown functions; the
            connection is closed either way.
    """
    connection: sqlite3.Connection | None = g.pop(_DB_KEY, None)
    if connection is not None:
        connection.close()


def init_db() -> None:
    """Drop the existing tables and recreate the schema.

    ``app/schema.sql`` carries its own child-first ``DROP TABLE IF EXISTS``
    statements, so executing the file is a complete drop-and-recreate and doing
    it twice in a row is safe. Nothing is dropped here in addition: the DDL has
    one home, and a second drop path in Python could drift from it.
    """
    connection = get_db()
    with current_app.open_resource(SCHEMA_FILENAME) as schema_file:
        script = schema_file.read().decode("utf-8")
        schema_path = schema_file.name
    connection.executescript(script)
    connection.commit()
    current_app.logger.info("Applied database schema from %s", schema_path)


def ensure_schema() -> bool:
    """Apply the schema when the database has not been initialised yet.

    The application factory calls this inside an application context, so a fresh
    checkout serves the dashboard, the task list and the project screen -- with
    empty states and zero counts -- without anyone running ``init-db`` first.

    Returns:
        ``True`` when the schema had to be applied, ``False`` when the task table
        was already there.

    Raises:
        sqlite3.Error: Whatever went wrong applying the schema. A half-built
            database is worse than a visible failure, so nothing is swallowed.
    """
    connection = get_db()
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'task'"
    ).fetchone()
    if row is not None:
        return False
    init_db()
    return True


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Clear the existing data and create new tables."""
    init_db()
    click.echo("Initialized the database.")


@click.command("seed-db")
@with_appcontext
def seed_db_command() -> None:
    """Load the sample dataset into the database."""
    try:
        from seed import seed_database
    except ModuleNotFoundError as error:
        # Only a missing top-level ``seed`` module is the run-from-the-wrong-
        # directory case; an import that fails inside seed.py itself is a real
        # error and keeps its traceback.
        if error.name != "seed":
            raise
        raise click.ClickException(_MISSING_SEED_MESSAGE) from error
    # seed_database() rebuilds the schema and prints the seeded summary itself.
    seed_database(current_app.config["DATABASE"])


def init_app(app: Flask) -> None:
    """Register the connection teardown and the two database commands on ``app``.

    This is the only place the ``init-db`` and ``seed-db`` commands are
    registered: the factory calls ``db.init_app(app)`` and must not add them
    itself, or ``flask --app app`` fails with "Command already registered".
    """
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(seed_db_command)
