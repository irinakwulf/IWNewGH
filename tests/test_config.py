"""Tests for :mod:`app.config` and the configuration the factory loads from it.

The module is the lowest layer ``create_app`` reads, so it is exercised here
directly instead of through a request: the default mapping, the environment it
reads, the override a test supplies, and the path resolution that makes the
database absolute. Every expected value comes from the module's own constants or
from ``monkeypatch``, so no test restates a default that the module could change
without this file noticing.

Two factory behaviours are covered as well, because both are what an operator
sees first: the start-up record naming the database a running instance serves,
which must reach the log at the level the documented run command leaves in place,
and a ``DATABASE`` value the process cannot use, which must arrive as one
readable line naming the setting, the resolved path and the fix rather than as a
library traceback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import click
import pytest

import app.config
from app import create_app
from app.config import (
    DEFAULT_DATABASE,
    DEV_SECRET_KEY,
    config_mapping,
    resolve_database_path,
)

# The two variables the module reads, and the only two it may read.
ENVIRONMENT_VARIABLES = ("DATABASE", "SECRET_KEY")

# The five keys the mapping holds, fixed by the plan's configuration contract.
MAPPING_KEYS = {
    "DATABASE",
    "SECRET_KEY",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_HTTPONLY",
    "TESTING",
}

# The module's public names: the two constants, the mapping and the path helper.
PUBLIC_NAMES = {
    "DEFAULT_DATABASE",
    "DEV_SECRET_KEY",
    "config_mapping",
    "resolve_database_path",
}

# The prefix of the application's single start-up record, which ends with the
# resolved database path.
STARTUP_PREFIX = "Taskboard ready; database at "

# The record the schema bootstrap writes when it has just applied the schema.
SCHEMA_APPLIED_PREFIX = "Applied database schema from "

# Bytes that are not a SQLite database, written to a ``DATABASE`` path a test needs
# the application to reject.
NOT_A_DATABASE = b"this file is not a SQLite database"

# What every unusable-database message must say: the setting, and the documented
# command that rebuilds a database whose file cannot be read.
UNUSABLE_SETTING = "DATABASE"
RECOVERY_COMMAND = "flask --app app seed-db"


def database_under_a_regular_file(tmp_path: Path) -> str:
    """Return a database path whose parent is a regular file, so no folder can exist."""
    blocker = tmp_path / "regular.txt"
    blocker.write_text("not a directory", encoding="utf-8")
    return str(blocker / "taskboard.sqlite3")


def database_naming_a_directory(tmp_path: Path) -> str:
    """Return a database path that names an existing directory."""
    directory = tmp_path / "a-directory"
    directory.mkdir()
    return str(directory)


def database_naming_a_non_sqlite_file(tmp_path: Path) -> str:
    """Return a database path holding bytes that are not a SQLite database."""
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(NOT_A_DATABASE)
    return str(corrupt)


# The three ways a configured path is unusable, each built inside the test's own
# tmp_path: no writable folder, a directory instead of a file, and a file SQLite
# cannot read. They differ only in the path they produce, so one case list covers
# them all.
UNUSABLE_DATABASE_CASES = pytest.mark.parametrize(
    "build_database",
    [
        pytest.param(database_under_a_regular_file, id="parent-is-a-regular-file"),
        pytest.param(database_naming_a_directory, id="path-is-a-directory"),
        pytest.param(database_naming_a_non_sqlite_file, id="file-is-not-sqlite"),
    ],
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove both variables, so a default-value test cannot read the host's own."""
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_mapping_holds_the_documented_defaults() -> None:
    """With nothing configured, the mapping holds exactly the five documented keys."""
    mapping = config_mapping()

    assert set(mapping) == MAPPING_KEYS
    assert mapping["DATABASE"] == DEFAULT_DATABASE
    assert mapping["SECRET_KEY"] == DEV_SECRET_KEY
    assert mapping["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert mapping["SESSION_COOKIE_HTTPONLY"] is True
    assert mapping["TESTING"] is False


def test_mapping_reads_both_variables_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A set variable replaces its default, which is what makes the app configurable."""
    database = str(tmp_path / "environment.sqlite3")
    monkeypatch.setenv("DATABASE", database)
    monkeypatch.setenv("SECRET_KEY", "an-environment-secret")

    mapping = config_mapping()

    assert mapping["DATABASE"] == database
    assert mapping["SECRET_KEY"] == "an-environment-secret"


@pytest.mark.parametrize("variable", ENVIRONMENT_VARIABLES)
def test_mapping_treats_an_empty_variable_as_unset(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """An empty value falls back to the default, never to an empty critical value."""
    monkeypatch.setenv(variable, "")

    mapping = config_mapping()

    defaults = {"DATABASE": DEFAULT_DATABASE, "SECRET_KEY": DEV_SECRET_KEY}
    assert mapping[variable] == defaults[variable]


def test_test_config_outranks_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller's mapping beats the environment, which is how a test isolates itself."""
    monkeypatch.setenv("DATABASE", str(tmp_path / "from-environment.sqlite3"))
    override = str(tmp_path / "from-mapping.sqlite3")

    mapping = config_mapping({"DATABASE": override, "TESTING": True})

    assert mapping["DATABASE"] == override
    assert mapping["TESTING"] is True


def test_each_call_returns_a_fresh_mapping(tmp_path: Path) -> None:
    """Mutating one caller's mapping leaves the next caller's defaults untouched."""
    first = config_mapping({"DATABASE": str(tmp_path / "first.sqlite3")})
    first["DATABASE"] = "mutated-by-the-caller"

    second = config_mapping()

    assert second["DATABASE"] == DEFAULT_DATABASE


def test_resolve_database_path_joins_a_relative_value(tmp_path: Path) -> None:
    """A relative value is resolved against the repository root, not against the cwd."""
    root = str(tmp_path)

    resolved = resolve_database_path("instance/nested/taskboard.sqlite3", root)

    assert resolved == str(tmp_path / "instance" / "nested" / "taskboard.sqlite3")


def test_resolve_database_path_uses_an_absolute_value_as_given(tmp_path: Path) -> None:
    """An absolute value -- what a test's tmp_path is -- comes back unchanged."""
    database = str(tmp_path / "absolute.sqlite3")

    assert resolve_database_path(database, str(tmp_path / "elsewhere")) == database


def test_resolve_database_path_ignores_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same value resolves to the same file whatever directory the process runs in.

    The repository root is passed in, so changing the process's own working
    directory must not move the database: that is what keeps the documented
    commands pointing at one file.
    """
    root = tmp_path / "clone"
    monkeypatch.chdir(tmp_path)

    resolved = resolve_database_path(DEFAULT_DATABASE, str(root))

    assert resolved == str(root / DEFAULT_DATABASE)
    assert Path(resolved).parent.parent == root


def test_module_exports_only_its_public_names() -> None:
    """``__all__`` names the four public names, so no wildcard import leaks ``os``."""
    assert set(app.config.__all__) == PUBLIC_NAMES
    for name in app.config.__all__:
        assert hasattr(app.config, name), f"{name} is exported but not defined"
    assert "os" not in app.config.__all__


def test_create_app_logs_the_resolved_database_at_the_default_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The start-up and schema-applied records reach the log without raising logging.

    ``flask --app app run`` configures no logging of its own, so the application
    logger's level is the only thing that decides whether an operator sees which
    database the instance serves.
    """
    database = str(tmp_path / "startup.sqlite3")
    caplog.set_level(logging.INFO)

    application = create_app({"DATABASE": database, "TESTING": True})

    assert application.logger.level == logging.INFO
    messages = [record.getMessage() for record in caplog.records]
    assert f"{STARTUP_PREFIX}{database}" in messages
    assert any(
        message.startswith(SCHEMA_APPLIED_PREFIX) for message in messages
    ), "the schema bootstrap's own record must reach the log too"


@UNUSABLE_DATABASE_CASES
def test_create_app_reports_an_unusable_database_on_one_line(
    build_database: Callable[[Path], str], tmp_path: Path
) -> None:
    """A path the process cannot use is reported with the setting, the path and the fix.

    The factory is the one place the server and both ``init-db`` and ``seed-db``
    commands pass through, so this is the failure every operator path shows: a
    ``click.ClickException`` renders as a single ``Error: ...`` line and exits 1,
    where the library exception it replaces printed a traceback naming neither the
    setting nor the resolved path.
    """
    database = build_database(tmp_path)

    with pytest.raises(click.ClickException) as raised:
        create_app({"DATABASE": database, "TESTING": True})

    message = raised.value.message
    assert UNUSABLE_SETTING in message
    assert database in message
    assert RECOVERY_COMMAND in message


def test_an_unusable_database_file_is_left_untouched(tmp_path: Path) -> None:
    """Reporting an unreadable database leaves the file's bytes exactly as they were."""
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(NOT_A_DATABASE)

    with pytest.raises(click.ClickException):
        create_app({"DATABASE": str(corrupt), "TESTING": True})

    assert corrupt.read_bytes() == NOT_A_DATABASE
