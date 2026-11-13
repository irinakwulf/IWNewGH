"""Read and write the tasks, which is the entity every capability acts on.

A task carries its title, description, optional assignee, one of the three closed
statuses and an optional deadline. This module serves the whole task surface:

* :func:`list_tasks` is the task list, and it owns the three filters -- status,
  assignee and deadline bucket -- so filtering happens in one ``WHERE`` clause in
  the database rather than in Python over a fetched table.
* :func:`get_task` is the task detail view's read, and its ``None`` for an unknown
  id is the entire mechanism by which a route reaches its 404: nothing here raises
  a framework exception, returns a page or sets a confirmation message.
* :func:`create_task`, :func:`update_task`, :func:`set_status` and
  :func:`set_assignee` are the four mutations. Each refreshes ``updated_at``,
  commits, and returns the row re-read from the database, so the caller shows
  stored values rather than values invented at the call site.

Two shapes are shared rather than repeated, because a reader and a re-read that
disagree would hand the interface two different tasks. :data:`_TASK_SELECT` is the
one projection every read uses: it joins the assignee's name and counts the task's
comments in a correlated subquery, so the list needs no per-row query and each
:class:`app.models.Task` arrives with its ``assignee_name`` and ``comment_count``
filled. The join is a ``LEFT JOIN`` on purpose -- an unassigned task must stay in
the list with ``assignee_name`` of ``None``, which the templates render as
"Unassigned", instead of disappearing from it.

That one projection serves the single-row reads as well -- :func:`get_task` and the
re-read every mutation performs -- although only the task list displays the comment
count. Splitting the count out of them was considered and rejected: a second,
count-less projection would have to be kept in step with the first, and the same
task would then read a ``comment_count`` of 0 through :func:`get_task` and 2 through
:func:`list_tasks`, which is precisely the disagreement the shared shape prevents.
What the count costs a single-row read is one index seek, served by the
``comment(task_id)`` index, and that was weighed and accepted at this scale.

Boundaries owned by other layers are deliberately absent. Parsing, trimming and
the character bounds live in :mod:`app.validation`, which also parses the ISO-8601
dates, so this module stores exactly the values it is handed and never re-parses a
date into a ``date`` object -- SQLite holds ``TEXT`` and the round trip would change
its type. Whether a status or a member id is legal is decided by the same parser
and by :mod:`app.services.members` before a quick action reaches these functions.
Where the task's project id comes from is :mod:`app.services.projects`, read here
so a task created before the project details are recorded still works: the column
is nullable, and ``None`` is the honest value for "no project yet".

Every statement binds its values as parameters and runs on the connection
:func:`app.db.get_db` caches for the current application context, so this module
neither opens nor closes a connection and no user string can reach SQL as text.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from app.db import get_db
from app.models import Task
from app.services import projects

__all__ = [
    "create_task",
    "get_task",
    "list_tasks",
    "set_assignee",
    "set_status",
    "update_task",
]

# The schema's own default for a task's status, applied in Python as well because it
# is the value both write paths bind when a caller omits the status: an INSERT then
# stores the pending task the column default would have given it, and an UPDATE
# writes the same value over the stored one rather than leaving that status alone.
_DEFAULT_STATUS = "pending"

# Finished work is not late, so the overdue bucket excludes it. This is the same
# rule the deadline indicator applies when it suppresses its relative word.
_COMPLETED_STATUS = "completed"

# The assignee filter value that means "no one". It is spelled out rather than
# derived from a member id, because it selects the rows a NULL assignee_id marks.
_UNASSIGNED = "unassigned"

# How far past today the "Due this week" bucket reaches. Seven days inclusive --
# today through today + 6 -- which is the same window app.templating.deadline_state
# uses for its "soon" state, so the filter and the word beside a deadline cannot
# disagree about which work is imminent.
_WEEK_WINDOW_DAYS = 6

# The bounds of the signed 64-bit INTEGER a SQLite column stores, so no row id can
# lie outside them. An id beyond them cannot be a task, and it must not reach a
# bind: sqlite3 raises OverflowError for such a value rather than matching no row,
# which would turn the route's 404 into a 500.
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1

# The projection and join every task read shares, so the list, the detail view and
# the re-read after a write cannot disagree about which columns a Task needs. Only
# constants are concatenated onto it; no submitted value ever is.
#
# The correlated count is deliberately part of this one projection rather than a
# second, count-less one for the single-row reads: get_task and list_tasks must
# return the same row shape, or the same task would read comment_count 0 through
# get_task and 2 through list_tasks. The count is displayed only by the task list,
# so what a single-row read pays for the shared shape is one index seek on
# comment(task_id) -- a cost weighed and accepted at this scale.
_TASK_SELECT = (
    "SELECT t.id, t.project_id, t.title, t.description, t.assignee_id, t.status,"
    " t.deadline, t.created_at, t.updated_at,"
    " m.name AS assignee_name,"
    " (SELECT COUNT(*) FROM comment c WHERE c.task_id = t.id) AS comment_count"
    " FROM task t"
    " LEFT JOIN member m ON m.id = t.assignee_id"
)

# get_task's shape: the shared projection narrowed to one row.
_SELECT_TASK = _TASK_SELECT + " WHERE t.id = ?"

# created_at and updated_at are left to the column defaults, which the schema
# defines as datetime('now') in UTC, so no Python timestamp is ever written.
_INSERT_TASK = (
    "INSERT INTO task (project_id, title, description, assignee_id, status, deadline)"
    " VALUES (?, ?, ?, ?, ?, ?)"
)

# Every mutation refreshes updated_at; created_at keeps the moment of the insert.
_UPDATE_TASK = (
    "UPDATE task SET title = ?, description = ?, assignee_id = ?, status = ?,"
    " deadline = ?, updated_at = datetime('now') WHERE id = ?"
)

_SET_TASK_STATUS = (
    "UPDATE task SET status = ?, updated_at = datetime('now') WHERE id = ?"
)

# The assignee is bound as the value itself, so clearing the assignment is
# member_id=None rather than a second statement with a different WHERE clause.
_SET_TASK_ASSIGNEE = (
    "UPDATE task SET assignee_id = ?, updated_at = datetime('now') WHERE id = ?"
)


def list_tasks(filters: Mapping[str, Any] | None = None) -> list[Task]:
    """Return the tasks matching the validated filters, ordered by id.

    The three filters combine with ``AND``, and a filter whose value is ``None``
    is not applied at all -- that is how the filter bar's "All" option and an
    absent query argument both arrive here. Unknown keys in ``filters`` are
    ignored, so a caller may pass the parser's mapping straight through.

    Args:
        filters: The mapping :func:`app.validation.parse_task_filters` returns for
            an accepted query string, or ``None`` for the unfiltered list. The
            keys are ``status`` (one of :data:`app.models.TASK_STATUSES`),
            ``assignee`` (a member id, the string ``"unassigned"``, or ``None``)
            and ``deadline`` (``"overdue"``, ``"today"``, ``"week"``, ``"none"``
            or ``None``). Values are bound as parameters and never interpolated.

    Returns:
        One :class:`app.models.Task` per match, each carrying its assignee's name
        and its comment count, ordered by ``id`` so the rendered list and the
        result count line are stable. An empty list when nothing matches and when
        no task exists yet.

    Raises:
        ValueError: If ``filters`` carries a ``deadline`` value that is not one of
            the four buckets. The chain fails closed rather than open, so an
            unrecognised value can never leave the query unfiltered; the callers
            reach this function only through
            :func:`app.validation.parse_task_filters`, which answers HTTP 400 for
            such a value before the list is built.
    """
    # Read once, here: the overdue, today and week buckets must all measure from
    # the same instant, and a second call inside the clauses could straddle
    # midnight and put one task in two different buckets.
    today = date.today()

    # An absent mapping and a mapping whose values are all None describe the same
    # request -- the unfiltered list -- so one empty mapping serves both.
    applied = filters or {}

    clauses: list[str] = []
    parameters: list[Any] = []

    status = applied.get("status")
    if status is not None:
        clauses.append("t.status = ?")
        parameters.append(status)

    assignee = applied.get("assignee")
    if assignee == _UNASSIGNED:
        # No parameter: the bucket selects the rows a NULL assignee_id marks, and
        # a NULL never compares equal to anything.
        clauses.append("t.assignee_id IS NULL")
    elif assignee is not None:
        clauses.append("t.assignee_id = ?")
        parameters.append(assignee)

    deadline = applied.get("deadline")
    if deadline == "overdue":
        # Completed work is never late, which is why the status test is part of
        # the bucket rather than something the caller filters afterwards.
        clauses.append("t.deadline IS NOT NULL AND t.deadline < ? AND t.status <> ?")
        parameters.extend((today.isoformat(), _COMPLETED_STATUS))
    elif deadline == "today":
        clauses.append("t.deadline = ?")
        parameters.append(today.isoformat())
    elif deadline == "week":
        clauses.append(
            "t.deadline IS NOT NULL AND t.deadline >= ? AND t.deadline <= ?"
        )
        parameters.extend(
            (
                today.isoformat(),
                (today + timedelta(days=_WEEK_WINDOW_DAYS)).isoformat(),
            )
        )
    elif deadline == "none":
        clauses.append("t.deadline IS NULL")
    elif deadline is not None:
        # Both filter branches fail closed, and this one must raise rather than fall
        # through: with no final branch an unrecognised bucket would append no
        # clause at all, quietly widening this statement into the unfiltered
        # full-table read. Nothing reachable over HTTP arrives here --
        # app.validation.parse_task_filters answers 400 for a deadline outside the
        # four buckets before the list is built.
        raise ValueError(f"unknown deadline filter: {deadline!r}")

    # The only string assembly in this module: fixed clause fragments joined with
    # AND. Every value remains a bound parameter in the order it was appended.
    statement = _TASK_SELECT
    if clauses:
        statement += " WHERE " + " AND ".join(clauses)
    statement += " ORDER BY t.id"

    rows = get_db().execute(statement, parameters).fetchall()
    return [Task.from_row(row) for row in rows]


def get_task(task_id: int) -> Task | None:
    """Return one task, or ``None`` when the id is unknown.

    Args:
        task_id: The task's primary key, already parsed to an ``int`` by the caller.
            An id outside SQLite's signed 64-bit integer range is not a task: no row
            can hold it, so it is answered without being bound to a statement.

    Returns:
        The matching :class:`app.models.Task`, carrying its ``assignee_name``
        (``None`` when unassigned) and its ``comment_count``, or ``None`` when no
        task has that id. An id outside SQLite's integer range returns ``None``
        exactly like a missing row, which is what keeps the route on its 404 path
        instead of letting an unrepresentable value reach the bind. Returned as
        ``None`` rather than raised, because the route is what decides whether an
        unknown id is a 404.
    """
    # Refuse an id no SQLite INTEGER can hold before the statement is prepared: the
    # bind would raise OverflowError, and an id that cannot be a row is simply not a
    # task -- the same answer as a missing one.
    if not _SQLITE_INTEGER_MIN <= task_id <= _SQLITE_INTEGER_MAX:
        return None
    row = get_db().execute(_SELECT_TASK, (task_id,)).fetchone()
    if row is None:
        return None
    return Task.from_row(row)


def create_task(values: Mapping[str, Any]) -> Task:
    """Insert a task and return it as stored.

    Args:
        values: The mapping :func:`app.validation.parse_task_form` returns for an
            accepted submission: ``title``, ``description``, ``assignee_id`` (an
            ``int`` or ``None``), ``status`` (one of :data:`app.models.TASK_STATUSES`)
            and ``deadline`` (ISO-8601 ``YYYY-MM-DD`` or ``None``). A missing or
            empty ``status`` is stored as ``pending``.

    Returns:
        The stored :class:`app.models.Task`, re-read from the database so it
        carries the ``id``, the timestamps and the joined ``assignee_name``.

    Raises:
        RuntimeError: If the row cannot be read back after a successful insert,
            which would mean it was removed mid-write. The row exists whenever the
            statement above succeeded, so this only makes a broken invariant
            visible instead of returning ``None`` from a function that promises a
            task.
    """
    # A task may exist before the project details are recorded, so no project is
    # required and none is created here: the nullable column carries that state.
    project = projects.get_project()
    project_id = project.id if project is not None else None

    connection = get_db()
    cursor = connection.execute(
        _INSERT_TASK,
        (
            project_id,
            values.get("title"),
            values.get("description", ""),
            values.get("assignee_id"),
            values.get("status") or _DEFAULT_STATUS,
            values.get("deadline"),
        ),
    )
    connection.commit()

    task = get_task(int(cursor.lastrowid))
    if task is None:  # pragma: no cover - the insert above just created the row
        raise RuntimeError("task row missing after insert")
    return task


def update_task(task_id: int, values: Mapping[str, Any]) -> Task | None:
    """Update a task from a full set of field values and return it as stored.

    Args:
        task_id: The task to update.
        values: The mapping :func:`app.validation.parse_task_form` returns for an
            accepted edit -- the same keys :func:`create_task` takes, describing
            the task as it should now stand. An omitted or empty ``status`` is
            written as ``pending`` and overwrites whatever status the row holds:
            SQLite applies a column ``DEFAULT`` on an ``INSERT`` only, never on an
            ``UPDATE``, so leaving the value out of the statement would not leave
            the stored status untouched. An omitted ``description`` is written as
            empty for the same reason.

    Returns:
        The updated :class:`app.models.Task` with its refreshed ``updated_at``, or
        ``None`` when no task has ``task_id``, in which case nothing was written.
        The row count of the statement is deliberately not consulted: an edit that
        changes no value is still a successful edit.
    """
    connection = get_db()
    connection.execute(
        _UPDATE_TASK,
        (
            values.get("title"),
            values.get("description", ""),
            values.get("assignee_id"),
            values.get("status") or _DEFAULT_STATUS,
            values.get("deadline"),
            task_id,
        ),
    )
    connection.commit()
    return get_task(task_id)


def set_status(task_id: int, status: str) -> Task | None:
    """Set a task's status and return it as stored.

    Args:
        task_id: The task to update.
        status: The new status, already checked against
            :data:`app.models.TASK_STATUSES` by the caller. It is stored as given:
            this layer does not re-validate, and a status the schema's ``CHECK``
            rejects raises ``sqlite3.IntegrityError`` rather than being swallowed.

    Returns:
        The updated :class:`app.models.Task` with its refreshed ``updated_at``, or
        ``None`` when no task has ``task_id``, in which case nothing was written.
    """
    connection = get_db()
    connection.execute(_SET_TASK_STATUS, (status, task_id))
    connection.commit()
    return get_task(task_id)


def set_assignee(task_id: int, member_id: int | None) -> Task | None:
    """Set or clear a task's assignee and return it as stored.

    Args:
        task_id: The task to update.
        member_id: The member to assign, already checked against the member table
            by the caller, or ``None`` to clear the assignment -- which the
            interface renders as "Unassigned".

    Returns:
        The updated :class:`app.models.Task` with its refreshed ``updated_at`` and
        its ``assignee_name`` reflecting the new assignment, or ``None`` when no
        task has ``task_id``, in which case nothing was written.
    """
    connection = get_db()
    connection.execute(_SET_TASK_ASSIGNEE, (member_id, task_id))
    connection.commit()
    return get_task(task_id)
