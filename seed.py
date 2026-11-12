"""The sample dataset for the task board and the loader that writes it.

``SAMPLE_DATA`` holds one project, five team members, twelve tasks and eight
comments, and :func:`seed_database` writes exactly that dataset into the SQLite
file it is handed. The ``seed-db`` command in :mod:`app.db` and the test fixtures
both call :func:`seed_database`, so the rows the application starts on and the
rows the tests assert against are the same rows by construction.

Two properties make the dataset usable on whatever day it is loaded:

* Dates are written as day *offsets* from the day the loader runs, never as
  literal calendar dates. ``seed_database`` resolves them against a single
  ``date.today()``, so the overdue, due-today and due-this-week deadline buckets
  always have matching tasks.
* A task names its assignee and a comment names its author by the member's
  ``email`` -- the one column the schema declares UNIQUE -- so the literal
  references a member unambiguously instead of by insert position.

The module deliberately imports nothing from the application package and needs no
Flask application context: it opens its own ``sqlite3`` connection to the path it
is given. The only artefact it reads from the package is the schema,
``app/schema.sql``, which stays the single home of the DDL.
"""

from __future__ import annotations

import contextlib
import datetime
import sqlite3
from pathlib import Path

__all__ = ["SAMPLE_DATA", "seed_database"]

# The one file that defines the tables. It is executed through ``executescript``
# so this module never restates the DDL and cannot drift from it.
SCHEMA_PATH = Path(__file__).resolve().parent / "app" / "schema.sql"

# The text format SQLite's own datetime('now') default produces (UTC), so seeded
# and application-written timestamps sort and render identically.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# The single project row. The schema pins it with CHECK (id = 1).
PROJECT_ID = 1

# The dataset. ``project`` is one dict, ``members`` and ``tasks`` are lists in the
# order they are inserted, and each task carries its own ``comments`` list so a
# comment's parent needs no id lookup. Offsets are day offsets from ``today``.
SAMPLE_DATA = {
    "project": {
        "name": "Website Redesign",
        "description": (
            "Marketing site refresh with a new design system and CMS migration."
        ),
        "start_offset": -14,
        "end_offset": 45,
    },
    "members": [
        {
            "name": "Amina Haddad",
            "role": "Product Owner",
            "email": "amina.haddad@example.com",
        },
        {
            "name": "Daniel Rivera",
            "role": "Frontend Engineer",
            "email": "daniel.rivera@example.com",
        },
        {
            "name": "Priya Nair",
            "role": "Backend Engineer",
            "email": "priya.nair@example.com",
        },
        {
            "name": "Marcus Bell",
            "role": "Designer",
            "email": "marcus.bell@example.com",
        },
        {
            "name": "Sofia Lindqvist",
            "role": "QA Engineer",
            "email": "sofia.lindqvist@example.com",
        },
    ],
    "tasks": [
        {
            "title": "Draft homepage wireframes",
            "description": (
                "Three low-fidelity layouts for the new homepage, ready for review."
            ),
            "assignee": "marcus.bell@example.com",
            "status": "completed",
            "deadline_offset": -9,
            "comments": [
                {
                    "author": "daniel.rivera@example.com",
                    "body": (
                        "Wireframes look good; the hero section needs one more pass."
                    ),
                    "age_days": 9,
                    "time_of_day": "09:15:00",
                },
            ],
        },
        {
            "title": "Set up CI for the site build",
            "description": (
                "Build and test pipeline for the marketing site's static bundle."
            ),
            "assignee": "priya.nair@example.com",
            "status": "completed",
            "deadline_offset": -5,
            "comments": [
                {
                    "author": "sofia.lindqvist@example.com",
                    "body": (
                        "Build ran green on the first attempt, so I am closing this."
                    ),
                    "age_days": 12,
                    "time_of_day": "16:40:00",
                },
            ],
        },
        {
            "title": "Audit the current content inventory",
            "description": (
                "Catalogue every page on the current site with its owner and traffic."
            ),
            "assignee": "amina.haddad@example.com",
            "status": "completed",
            "deadline_offset": None,
            "comments": [],
        },
        {
            "title": "Implement the new hero component",
            "description": (
                "Responsive hero section built from the approved design tokens."
            ),
            "assignee": "daniel.rivera@example.com",
            "status": "in_progress",
            "deadline_offset": 0,
            "comments": [
                {
                    "author": "marcus.bell@example.com",
                    "body": "I left spacing notes on the second frame for you.",
                    "age_days": 4,
                    "time_of_day": "11:22:00",
                },
                {
                    "author": "daniel.rivera@example.com",
                    "body": "Spacing is applied; ready for another look.",
                    "age_days": 1,
                    "time_of_day": "14:03:00",
                },
            ],
        },
        {
            "title": "Migrate blog posts to the new CMS",
            "description": (
                "Move the existing blog entries across without breaking inbound links."
            ),
            "assignee": "priya.nair@example.com",
            "status": "in_progress",
            "deadline_offset": 3,
            "comments": [
                {
                    "author": "amina.haddad@example.com",
                    "body": "Please keep the old permalinks intact while migrating.",
                    "age_days": 7,
                    "time_of_day": "08:05:00",
                },
            ],
        },
        {
            "title": "Review accessibility of the navigation",
            "description": (
                "Keyboard and screen-reader pass over the primary navigation."
            ),
            "assignee": "sofia.lindqvist@example.com",
            "status": "in_progress",
            "deadline_offset": 6,
            "comments": [],
        },
        {
            "title": "Write the deployment runbook",
            "description": (
                "Step-by-step notes for releasing the new site to production."
            ),
            "assignee": "priya.nair@example.com",
            "status": "in_progress",
            "deadline_offset": None,
            "comments": [],
        },
        {
            "title": "Collect stakeholder feedback on the copy",
            "description": (
                "Gather comments on the marketing copy from each stakeholder group."
            ),
            "assignee": "amina.haddad@example.com",
            "status": "pending",
            "deadline_offset": -2,
            "comments": [
                {
                    "author": "priya.nair@example.com",
                    "body": "Feedback from the sales team is in the shared folder.",
                    "age_days": 2,
                    "time_of_day": "10:12:00",
                },
                {
                    "author": "sofia.lindqvist@example.com",
                    "body": (
                        "Two stakeholders want a shorter form; noted in the summary."
                    ),
                    "age_days": 5,
                    "time_of_day": "15:47:00",
                },
            ],
        },
        {
            "title": "Build the pricing page",
            "description": (
                "Pricing table with the three plans and their comparison rows."
            ),
            "assignee": "daniel.rivera@example.com",
            "status": "pending",
            "deadline_offset": -1,
            "comments": [],
        },
        {
            "title": "Define the redirect map",
            "description": "Map every retired URL to its replacement before launch.",
            "assignee": "priya.nair@example.com",
            "status": "pending",
            "deadline_offset": 4,
            "comments": [
                {
                    "author": "amina.haddad@example.com",
                    "body": "Start with the top twenty URLs by traffic.",
                    "age_days": 3,
                    "time_of_day": "13:30:00",
                },
            ],
        },
        {
            "title": "Prepare the launch checklist",
            "description": "Everything that has to be true before the site goes live.",
            "assignee": "sofia.lindqvist@example.com",
            "status": "pending",
            "deadline_offset": None,
            "comments": [],
        },
        {
            "title": "Optimise the hero images",
            "description": "Compress and serve the hero artwork at responsive sizes.",
            # Deliberately due beyond the coming week: with three tasks carrying no
            # deadline at all, this is the one row that exercises the rendering of
            # a date that is neither overdue, due today nor due within the week.
            "assignee": None,
            "status": "pending",
            "deadline_offset": 10,
            "comments": [],
        },
    ],
}


def _iso_offset(today: datetime.date, offset: int | None) -> str | None:
    """Return ``offset`` days from ``today`` as ISO-8601 text, or ``None``.

    Args:
        today: The single date the whole dataset is resolved against.
        offset: Day offset, negative for the past, or ``None`` for no date.

    Returns:
        ``YYYY-MM-DD`` text, which is the format the schema's deadline check and
        the deadline filters both accept, or ``None`` when ``offset`` is ``None``.
    """
    if offset is None:
        return None
    return (today + datetime.timedelta(days=offset)).isoformat()


def _timestamp(today: datetime.date, age_days: int, time_of_day: str) -> str:
    """Return ``age_days`` before ``today`` at ``time_of_day`` as stored text.

    Args:
        today: The single date the whole dataset is resolved against.
        age_days: Days before ``today`` the comment was written, at least 1.
        time_of_day: ``HH:MM:SS`` time, fixed per comment so seeded timestamps are
            distinct by construction and sort deterministically.

    Returns:
        ``YYYY-MM-DD HH:MM:SS`` text, the format SQLite's own ``datetime('now')``
        default writes.
    """
    day = today - datetime.timedelta(days=age_days)
    moment = datetime.datetime.combine(day, datetime.time.fromisoformat(time_of_day))
    return moment.strftime(TIMESTAMP_FORMAT)


def _member_id(member_ids: dict[str, int], email: str) -> int:
    """Return the id of the member with ``email``.

    Args:
        member_ids: Mapping of member email to the id it was inserted with.
        email: The email a task's assignee or a comment's author names.

    Returns:
        The member's integer id.

    Raises:
        ValueError: If the dataset names an email the member list does not hold,
            which keeps a typo in the literal from surfacing as a bare KeyError.
    """
    try:
        return member_ids[email]
    except KeyError:
        raise ValueError(
            f"SAMPLE_DATA references an unknown member email: {email!r}"
        ) from None


def seed_database(db_path: str | Path) -> None:
    """Write the sample dataset into the SQLite database at ``db_path``.

    The database is rebuilt on every call: the four tables are dropped and the
    schema is re-applied from ``app/schema.sql`` before the rows go in, so running
    this twice leaves exactly the documented dataset rather than duplicates.

    Args:
        db_path: Path of the SQLite file to write, as a ``str`` or a path-like
            object. Any missing parent directory is created, so the command works
            from a fresh checkout.

    Returns:
        ``None``. The rows are committed to ``db_path`` and a one-line summary of
        what was written is printed to standard output.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today()
    project = SAMPLE_DATA["project"]
    members = SAMPLE_DATA["members"]
    tasks = SAMPLE_DATA["tasks"]

    # closing() guarantees the connection is shut even if an insert fails; the
    # connection's own context manager commits on success and rolls back on
    # error, so a half-written dataset is never left behind.
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        # Off by default in SQLite; without it a comment could name a task or a
        # member that does not exist and the integrity the schema declares would
        # simply not be checked.
        connection.execute("PRAGMA foreign_keys = ON")

        # Dropped child first so the statements hold with foreign keys enforced.
        # The schema file drops the tables too; these stay so idempotency does not
        # depend on another file's contents.
        connection.execute("DROP TABLE IF EXISTS comment")
        connection.execute("DROP TABLE IF EXISTS task")
        connection.execute("DROP TABLE IF EXISTS member")
        connection.execute("DROP TABLE IF EXISTS project")
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

        connection.execute(
            "INSERT INTO project (id, name, description, start_date, end_date)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                PROJECT_ID,
                project["name"],
                project["description"],
                _iso_offset(today, project["start_offset"]),
                _iso_offset(today, project["end_offset"]),
            ),
        )

        member_ids: dict[str, int] = {}
        for member in members:
            cursor = connection.execute(
                "INSERT INTO member (name, role, email) VALUES (?, ?, ?)",
                (member["name"], member["role"], member["email"]),
            )
            member_ids[member["email"]] = int(cursor.lastrowid)

        task_ids: dict[str, int] = {}
        for task in tasks:
            assignee = task["assignee"]
            cursor = connection.execute(
                "INSERT INTO task"
                " (project_id, title, description, assignee_id, status, deadline)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    PROJECT_ID,
                    task["title"],
                    task["description"],
                    _member_id(member_ids, assignee) if assignee else None,
                    task["status"],
                    _iso_offset(today, task["deadline_offset"]),
                ),
            )
            task_ids[task["title"]] = int(cursor.lastrowid)

        comment_count = 0
        for task in tasks:
            task_id = task_ids[task["title"]]
            for comment in task["comments"]:
                connection.execute(
                    "INSERT INTO comment (task_id, author_id, body, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        task_id,
                        _member_id(member_ids, comment["author"]),
                        comment["body"],
                        _timestamp(
                            today, comment["age_days"], comment["time_of_day"]
                        ),
                    ),
                )
                comment_count += 1

    print(
        f"Seeded {PROJECT_ID} project, {len(members)} members, "
        f"{len(tasks)} tasks, {comment_count} comments."
    )

