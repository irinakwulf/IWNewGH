"""Tests for the sample dataset in :mod:`seed`: its idempotency and its coverage.

The loader is exercised directly, with no Flask application and no application
context: ``seed`` opens its own connection to the path it is handed, and the tests
do the same to read the rows back. That independence is what lets the ``seed-db``
command and the test fixtures share it.

The one thing that is driven through the application is the pair of commands the
application registers on it -- ``init-db`` and ``seed-db`` -- through Flask's own
CLI runner, because those are the entry points the README and the plan tell a
reviewer to run and a broken registration would otherwise leave the suite green.

Every expected value about the dataset is derived from :data:`seed.SAMPLE_DATA`
-- the row counts, the summary line the loader prints, the members of each
deadline bucket -- so the dataset may grow or shrink without this module
restating one of its numbers, and a failure here means the loader and the
literal disagree rather than that a fixture was edited. The only literal
expectations are the lines the two commands themselves print, which are asserted
as the contract they are.
"""

from __future__ import annotations

import builtins
import contextlib
import sqlite3
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

import seed
from app.models import TASK_STATUSES

# The SQLite file a test seeds, inside that test's own ``tmp_path``.
DATABASE_FILENAME = "seed.sqlite3"

# ``SAMPLE_DATA["project"]`` is a single mapping by contract -- one project
# record, which the schema pins with CHECK (id = 1) -- so it contributes one row,
# and that row is the one whose id this constant names.
PROJECT_ROW_COUNT = 1

# The last day of the "due this week" window the deadline filter and the deadline
# indicator share: today <= deadline <= today + 6.
THIS_WEEK_END_OFFSET = 6

# The four deadline buckets the task list offers, named as the filter names them.
DEADLINE_BUCKETS = ("overdue", "today", "week", "none")

# The four tables the loader writes, in the order the dataset is inserted.
TABLE_NAMES = ("project", "member", "task", "comment")

# What the two documented commands report. Each is the line a reviewer reads at the
# terminal, so it is asserted as the contract it is rather than as anything derived:
# a command that stopped saying what it did would fail here.
INIT_DB_MESSAGE = "Initialized the database."
MISSING_SEED_MESSAGE = (
    "seed.py was not found at the repository root; run this command from there."
)


def sample_row_counts() -> dict[str, int]:
    """Return the rows each seeded table holds, derived from ``SAMPLE_DATA``.

    Returns:
        A mapping of table name to the number of rows the dataset inserts into
        it, so no test in this module restates a count of its own.
    """
    tasks = seed.SAMPLE_DATA["tasks"]
    return {
        "project": PROJECT_ROW_COUNT,
        "member": len(seed.SAMPLE_DATA["members"]),
        "task": len(tasks),
        "comment": sum(len(task["comments"]) for task in tasks),
    }


def expected_summary_line() -> str:
    """Return the one-line summary :func:`seed.seed_database` prints."""
    counts = sample_row_counts()
    return (
        f"Seeded {counts['project']} project, {counts['member']} members, "
        f"{counts['task']} tasks, {counts['comment']} comments."
    )


def open_connection(db_path: str) -> sqlite3.Connection:
    """Return a connection to the SQLite file at ``db_path`` yielding named rows.

    Args:
        db_path: Path of the database file to read.

    Returns:
        An open connection whose rows are indexed by column name. The caller owns
        it and closes it.
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def count_rows(connection: sqlite3.Connection, table: str) -> tuple[int, int]:
    """Return ``(COUNT(*), MAX(id))`` for ``table``.

    ``table`` is interpolated rather than bound because SQLite has no placeholder
    for an identifier; it comes from this module's own :data:`TABLE_NAMES`, never
    from input.
    """
    row = connection.execute(
        f"SELECT COUNT(*) AS total, MAX(id) AS max_id FROM {table}"
    ).fetchone()
    return int(row["total"]), int(row["max_id"])


def _expected_bucket_titles(bucket: str) -> list[str]:
    """Return the sorted titles ``SAMPLE_DATA`` puts in deadline ``bucket``.

    Raises:
        ValueError: If ``bucket`` is not one of :data:`DEADLINE_BUCKETS`.
    """
    titles: list[str] = []
    for task in seed.SAMPLE_DATA["tasks"]:
        offset = task["deadline_offset"]
        is_past = offset is not None and offset < 0
        if bucket == "overdue":
            matches = is_past and task["status"] != "completed"
        elif bucket == "today":
            matches = offset == 0
        elif bucket == "week":
            matches = offset is not None and 0 <= offset <= THIS_WEEK_END_OFFSET
        elif bucket == "none":
            matches = offset is None
        else:
            raise ValueError(f"Unknown deadline bucket: {bucket!r}")
        if matches:
            titles.append(task["title"])
    return sorted(titles)


def seed_day(connection: sqlite3.Connection) -> date:
    """Return the day :func:`seed.seed_database` resolved its offsets against.

    The loader resolves every ``SAMPLE_DATA`` offset against the single
    ``date.today()`` it reads when it runs, and writes the project's dates as that
    day plus the project's own offsets. Recovering the day from the stored row --
    its start date minus ``start_offset`` -- therefore takes no clock reading of
    its own, which is what lets a run whose seeding and assertions straddle
    midnight still compare the rows and the expectations of one and the same day.

    Args:
        connection: A named-row connection to a freshly seeded database.

    Returns:
        The date the loader measured the dataset's day offsets from.

    Raises:
        AssertionError: If the database holds no project row, which would leave
            the offsets unresolvable.
    """
    row = connection.execute("SELECT start_date FROM project").fetchone()
    assert row is not None, "the seeded database holds no project row"
    return date.fromisoformat(row["start_date"]) - timedelta(
        days=seed.SAMPLE_DATA["project"]["start_offset"]
    )


def _bucket_query(bucket: str, today: date) -> tuple[str, tuple[str, ...]]:
    """Return the SQL, and its parameters, selecting ``bucket`` from a seeded file.

    The rules are the ones the task list applies, stated against the dates the
    loader resolved: overdue excludes finished work, and the week window runs from
    ``today`` to ``today + 6`` inclusive.
    """
    start = today.isoformat()
    if bucket == "overdue":
        return (
            "SELECT title FROM task WHERE deadline IS NOT NULL"
            " AND deadline < ? AND status <> 'completed'",
            (start,),
        )
    if bucket == "today":
        return "SELECT title FROM task WHERE deadline = ?", (start,)
    if bucket == "week":
        end = (today + timedelta(days=THIS_WEEK_END_OFFSET)).isoformat()
        return (
            "SELECT title FROM task WHERE deadline IS NOT NULL"
            " AND deadline >= ? AND deadline <= ?",
            (start, end),
        )
    if bucket == "none":
        return "SELECT title FROM task WHERE deadline IS NULL", ()
    raise ValueError(f"Unknown deadline bucket: {bucket!r}")


@pytest.fixture
def db_file(tmp_path: Path) -> str:
    """Return the path of this test's own SQLite file, which nothing has written."""
    return str(tmp_path / DATABASE_FILENAME)


@pytest.fixture
def file_connection(db_file: str) -> Iterator[sqlite3.Connection]:
    """Yield a named-row connection to a freshly seeded database."""
    seed.seed_database(db_file)
    with contextlib.closing(open_connection(db_file)) as connection:
        yield connection


def test_seed_database_is_idempotent(db_file: str) -> None:
    """Two runs leave one dataset, not two, because the tables are rebuilt each run.

    Each run is inspected before the next one starts, so a difference between
    them cannot be masked. ``MAX(id)`` restates the count because ids are assigned
    from one in a freshly created table: a table that had been appended to instead
    of recreated would count twice the rows and hold an id beyond the count.
    """
    expected = sample_row_counts()
    observed: list[dict[str, tuple[int, int]]] = []
    for _ in range(2):
        seed.seed_database(db_file)
        with contextlib.closing(open_connection(db_file)) as connection:
            observed.append(
                {table: count_rows(connection, table) for table in TABLE_NAMES}
            )

    for run, counts in enumerate(observed, start=1):
        for table in TABLE_NAMES:
            total, max_id = counts[table]
            assert total == expected[table], f"{table} row count after run {run}"
            assert max_id == expected[table], f"{table} MAX(id) after run {run}"


def test_seed_database_prints_the_documented_summary(
    db_file: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loader prints the dataset it wrote, built from the dataset itself."""
    seed.seed_database(db_file)

    assert capsys.readouterr().out.strip() == expected_summary_line()


def test_seed_module_carries_no_application_wiring() -> None:
    """The loader imports nothing from Flask or the app package.

    That independence is what lets the ``seed-db`` command and the test fixtures
    call it without an application or an application context.
    """
    foreign = sorted(
        name
        for name, value in vars(seed).items()
        if getattr(value, "__module__", "").split(".")[0] in {"app", "flask"}
    )

    assert foreign == []


def test_seeded_tasks_cover_every_status(file_connection: sqlite3.Connection) -> None:
    """The dataset, and the rows it writes, use the whole closed status set."""
    declared = {task["status"] for task in seed.SAMPLE_DATA["tasks"]}
    rows = file_connection.execute("SELECT DISTINCT status FROM task")
    stored = {row["status"] for row in rows}

    assert declared == set(TASK_STATUSES)
    assert stored == set(TASK_STATUSES)


def test_seeded_dataset_holds_an_unassigned_task(
    file_connection: sqlite3.Connection,
) -> None:
    """One task carries no assignee, which is what the "Unassigned" rendering needs."""
    declared = sorted(
        task["title"]
        for task in seed.SAMPLE_DATA["tasks"]
        if task["assignee"] is None
    )
    stored = sorted(
        row["title"]
        for row in file_connection.execute(
            "SELECT title FROM task WHERE assignee_id IS NULL"
        )
    )

    assert declared, "SAMPLE_DATA holds no task without an assignee"
    assert stored == declared


def test_seeded_dataset_holds_a_task_without_a_deadline(
    file_connection: sqlite3.Connection,
) -> None:
    """Some work carries no deadline, so the nullable column is exercised."""
    declared = sorted(
        task["title"]
        for task in seed.SAMPLE_DATA["tasks"]
        if task["deadline_offset"] is None
    )
    stored = sorted(
        row["title"]
        for row in file_connection.execute(
            "SELECT title FROM task WHERE deadline IS NULL"
        )
    )

    assert declared, "SAMPLE_DATA holds no task without a deadline"
    assert stored == declared


def test_overdue_bucket_excludes_completed_work(
    file_connection: sqlite3.Connection,
) -> None:
    """Finished work whose deadline has passed is not late, so Overdue omits it."""
    finished = sorted(
        task["title"]
        for task in seed.SAMPLE_DATA["tasks"]
        if task["deadline_offset"] is not None
        and task["deadline_offset"] < 0
        and task["status"] == "completed"
    )
    sql, parameters = _bucket_query("overdue", seed_day(file_connection))
    overdue = sorted(
        row["title"] for row in file_connection.execute(sql, parameters)
    )

    assert finished, "SAMPLE_DATA holds no completed task whose deadline has passed"
    assert overdue, "the seeded database has no overdue task to compare against"
    assert set(overdue).isdisjoint(finished)
    assert overdue == _expected_bucket_titles("overdue")


@pytest.mark.parametrize("bucket", DEADLINE_BUCKETS)
def test_deadline_bucket_matches_the_sample_data(
    file_connection: sqlite3.Connection, bucket: str
) -> None:
    """Each deadline query returns exactly the tasks the dataset puts in its bucket."""
    expected = _expected_bucket_titles(bucket)
    sql, parameters = _bucket_query(bucket, seed_day(file_connection))
    stored = sorted(
        row["title"] for row in file_connection.execute(sql, parameters)
    )

    if bucket != "none":
        assert expected, f"SAMPLE_DATA holds no task in the {bucket} deadline bucket"
    assert stored == expected


def test_seeded_project_row_is_the_single_project(
    file_connection: sqlite3.Connection,
) -> None:
    """One project row exists, with a start date that precedes its end date."""
    project = seed.SAMPLE_DATA["project"]
    stored = file_connection.execute(
        "SELECT id, start_date, end_date FROM project"
    ).fetchone()
    total = file_connection.execute("SELECT COUNT(*) AS total FROM project").fetchone()

    assert int(total["total"]) == PROJECT_ROW_COUNT
    assert stored["id"] == PROJECT_ROW_COUNT
    assert project["start_offset"] < project["end_offset"]
    assert stored["start_date"] < stored["end_date"]


def test_seeded_comments_match_the_sample_data(
    file_connection: sqlite3.Connection,
) -> None:
    """Every comment is written, attributed to a member and stamped distinctly."""
    expected_comments = sample_row_counts()["comment"]
    stored = file_connection.execute("SELECT COUNT(*) AS total FROM comment").fetchone()

    assert int(stored["total"]) == expected_comments

    # A LEFT JOIN that matches nothing is the portable way to ask SQLite for the
    # rows a foreign key would have rejected, without depending on the pragma
    # being on in this connection.
    orphan_authors = file_connection.execute(
        "SELECT comment.id FROM comment"
        " LEFT JOIN member ON member.id = comment.author_id"
        " WHERE member.id IS NULL"
    ).fetchall()
    orphan_tasks = file_connection.execute(
        "SELECT comment.id FROM comment"
        " LEFT JOIN task ON task.id = comment.task_id"
        " WHERE task.id IS NULL"
    ).fetchall()

    assert orphan_authors == []
    assert orphan_tasks == []

    # Distinct timestamps keep the oldest-first comment list deterministic instead
    # of depending on the order rows happened to be inserted in.
    timestamps = [
        row["created_at"]
        for row in file_connection.execute("SELECT created_at FROM comment")
    ]
    assert len(set(timestamps)) == expected_comments


def test_each_comment_is_attached_to_the_task_that_declares_it(
    file_connection: sqlite3.Connection,
) -> None:
    """Every comment's parent is the task whose own ``comments`` list holds it.

    The total count above cannot see a comment written against the wrong task, and
    ``task.title`` is not a column the schema declares UNIQUE, so attribution is
    asserted per task here: two tasks sharing a title must still each keep their
    own conversation.
    """
    declared = {
        task["title"]: sorted(comment["body"] for comment in task["comments"])
        for task in seed.SAMPLE_DATA["tasks"]
        if task["comments"]
    }
    stored: dict[str, list[str]] = {}
    for row in file_connection.execute(
        "SELECT task.title AS title, comment.body AS body FROM comment"
        " JOIN task ON task.id = comment.task_id"
    ):
        stored.setdefault(row["title"], []).append(row["body"])

    assert declared, "SAMPLE_DATA holds no task with a comment"
    assert {title: sorted(bodies) for title, bodies in stored.items()} == declared


@pytest.mark.parametrize(("length", "accepted"), [(80, True), (81, False), (0, False)])
@pytest.mark.parametrize("column", ("name", "role"))
def test_the_schema_carries_the_member_length_bounds(
    file_connection: sqlite3.Connection, column: str, length: int, accepted: bool
) -> None:
    """``member.name`` and ``member.role`` carry their 1-80 bound in the schema.

    Those two are the only length-bounded fields no form posts, so the schema is the
    one layer that can hold the rule: exactly 80 characters is accepted at the
    boundary, and one character more -- or none at all -- is refused by the column
    itself rather than by a caller that happens not to be listening.
    """
    values = {
        "name": "Amina Haddad",
        "role": "Product Owner",
        "email": "extra.member@example.com",
    }
    values[column] = "x" * length

    statement = "INSERT INTO member (name, role, email) VALUES (:name, :role, :email)"
    if accepted:
        file_connection.execute(statement, values)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            file_connection.execute(statement, values)


# --------------------------------------------------------------------------
# The two commands the application registers: the documented way in, driven
# through Flask's own CLI runner rather than by calling the loader directly.
# --------------------------------------------------------------------------


def refuse_the_import_of_seed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``from seed import seed_database`` fail, naming the module ``name``.

    ``seed`` is dropped from ``sys.modules`` as well as refused, because the
    command's lazy import would otherwise find the copy this module already
    imported and succeed. Only the top-level ``seed`` name is refused, so every
    other import the command makes still resolves, and ``monkeypatch`` restores
    both the module and ``__import__`` when the test ends.

    Args:
        monkeypatch: The test's patcher.
        name: The module name the failure reports, which is ``"seed"`` for a
            checkout that holds no ``seed.py`` and anything else for an import
            that breaks inside it.
    """
    real_import = builtins.__import__

    def import_refusing_seed(module: str, *args: object, **kwargs: object) -> object:
        if module == "seed":
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(module, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "seed", raising=False)
    monkeypatch.setattr(builtins, "__import__", import_refusing_seed)


def test_init_db_command_clears_the_database_to_an_empty_schema(app) -> None:
    """The documented ``init-db`` leaves every table present and holding no rows.

    The database is seeded first, so the command has data to clear: a registration
    that had broken, or a body that did nothing, would leave those rows behind and
    fail here. The four tables are asserted present as well, because rebuilding the
    schema is the other half of what the command promises.
    """
    database = app.config["DATABASE"]
    seed.seed_database(database)

    result = app.test_cli_runner().invoke(args=["init-db"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == INIT_DB_MESSAGE
    with contextlib.closing(open_connection(database)) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(TABLE_NAMES) <= tables, f"tables left behind: {sorted(tables)}"
        for table in TABLE_NAMES:
            total = connection.execute(
                f"SELECT COUNT(*) AS total FROM {table}"
            ).fetchone()
            assert int(total["total"]) == 0, f"{table} rows survived init-db"


def test_seed_db_command_loads_the_sample_dataset(app) -> None:
    """The documented ``seed-db`` writes the whole dataset and prints its summary."""
    database = app.config["DATABASE"]
    expected = sample_row_counts()

    result = app.test_cli_runner().invoke(args=["seed-db"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == expected_summary_line()
    with contextlib.closing(open_connection(database)) as connection:
        for table in TABLE_NAMES:
            counted = count_rows(connection, table)
            assert counted == (expected[table], expected[table]), (
                f"{table} after seed-db"
            )


def test_seed_db_command_is_idempotent(app) -> None:
    """Running the command twice leaves one dataset and repeats the same summary.

    The rows are read back after each run, so the second run is compared against
    the first rather than against a figure that could hide an append: ``MAX(id)``
    equals the count because ids restart from one in a recreated table.
    """
    database = app.config["DATABASE"]
    expected = sample_row_counts()
    runner = app.test_cli_runner()
    observed: list[dict[str, tuple[int, int]]] = []

    for _ in range(2):
        result = runner.invoke(args=["seed-db"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == expected_summary_line()
        with contextlib.closing(open_connection(database)) as connection:
            observed.append(
                {table: count_rows(connection, table) for table in TABLE_NAMES}
            )

    for run, counts in enumerate(observed, start=1):
        for table in TABLE_NAMES:
            assert counts[table] == (expected[table], expected[table]), (
                f"{table} after run {run}"
            )


def test_seed_db_command_reports_a_missing_seed_module(app, monkeypatch) -> None:
    """A command run with no ``seed.py`` names the fix instead of a traceback.

    The failure is the missing ``seed`` module itself, which is what a checkout
    without ``seed.py`` looks like from inside the command. The command turns it
    into the message a reviewer can act on, so the exit is non-zero and the
    terminal reads the remedy rather than a stack trace.
    """
    refuse_the_import_of_seed(monkeypatch, "seed")

    result = app.test_cli_runner().invoke(args=["seed-db"])

    assert result.exit_code == 1
    assert MISSING_SEED_MESSAGE in result.output
    assert "Traceback" not in result.output


def test_seed_db_command_keeps_a_broken_seed_import_a_real_error(
    app, monkeypatch
) -> None:
    """Only a missing ``seed`` module is reported as a wrong working directory.

    An import that breaks for any other reason -- a dependency inside ``seed.py``
    it cannot find, say -- is a real error, and answering it with "run this from
    the repository root" would send a reviewer looking in the wrong place. The
    failure is raised from inside the import of ``seed`` but names another module,
    which is exactly that case, and it is then the exception the test sees rather
    than a converted command error.
    """
    missing = "seed_dependency_that_is_not_installed"
    refuse_the_import_of_seed(monkeypatch, missing)
    runner = app.test_cli_runner()

    with pytest.raises(ModuleNotFoundError) as raised:
        runner.invoke(args=["seed-db"], catch_exceptions=False)

    assert raised.value.name == missing
    assert MISSING_SEED_MESSAGE not in str(raised.value)
