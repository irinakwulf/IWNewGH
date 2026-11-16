"""Read and write the single project record.

The application tracks exactly one project -- its name, description, start date
and end date -- and ``app/schema.sql`` enforces that with ``CHECK (id = 1)`` rather
than by convention. This module is the only code that reads or writes that row,
and it exposes the two operations the views need:

* :func:`get_project` returns the stored row, or ``None`` while the project
  details have not been recorded yet. That ``None`` is a signal, not an error:
  the dashboard's project panel and the project screen turn it into their "No
  project details yet." empty state, and ``app.services.tasks.create_task`` turns
  it into a ``NULL`` ``task.project_id``.
* :func:`save_project` is an upsert. It inserts row 1 when the record does not
  exist yet and updates row 1 otherwise, so ``POST /project`` has one call to make
  whether the reviewer is recording the details for the first time or editing them.

Three conventions this module follows, and the reason for each:

* Dates stay ISO-8601 ``YYYY-MM-DD`` text. SQLite stores them that way, and
  ``app.validation`` and ``app.templating`` already agree on the format, so the
  values pass through unchanged instead of being parsed and reformatted here.
* The return value is the row that was just written, re-read from the database,
  not a dataclass assembled from the input. The write itself is one statement, so
  that re-read is the only read a save makes. The caller confirms the save and
  sends the reviewer to a page that reads the database again, so the timestamps it
  shows must be the stored ones rather than values invented at the call site.
* Nothing here validates and nothing here decides wording. The length limits and
  the rule that ``end_date`` cannot precede ``start_date`` live in
  ``app.validation``, which also owns the messages; this layer receives accepted
  values only. Likewise no web-framework object is imported: the service is
  HTTP-free, and the view alone chooses the confirmation wording and where the
  follow-up lands.

Every statement binds its values as parameters and runs on the connection
:func:`app.db.get_db` caches for the current application context, so this module
neither opens nor closes a connection and no user string can reach SQL as text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db import get_db
from app.models import Project

__all__ = ["get_project", "save_project"]

# The one project row. The schema pins it with CHECK (id = 1), so the id is named
# here rather than left to INTEGER PRIMARY KEY auto-assignment: the single-project
# rule then reads at every call site instead of only inside the DDL.
_PROJECT_ID = 1

# The columns Project.from_row reads, named once so the read after a write cannot
# select a different shape from the read in get_project().
_SELECT_PROJECT_SQL = (
    "SELECT id, name, description, start_date, end_date, created_at, updated_at"
    " FROM project WHERE id = ?"
)

# One statement writes the record: it inserts row 1 and, when that row is already
# there, updates the four editable columns instead. created_at is absent from the
# DO UPDATE list, so it keeps the moment the record was first written while
# updated_at moves; the insert still relies on the column defaults. Because no
# read decides between an INSERT and an UPDATE, a save cannot observe an empty
# table and then collide with a concurrent save on the primary key.
# ON CONFLICT ... DO UPDATE requires SQLite 3.24 or later (3.45.1 here).
_UPSERT_PROJECT_SQL = (
    "INSERT INTO project (id, name, description, start_date, end_date)"
    " VALUES (?, ?, ?, ?, ?)"
    " ON CONFLICT(id) DO UPDATE SET name = excluded.name,"
    " description = excluded.description, start_date = excluded.start_date,"
    " end_date = excluded.end_date, updated_at = datetime('now')"
)


def get_project() -> Project | None:
    """Return the single project row, or ``None`` before one has been recorded.

    Returns:
        The stored :class:`app.models.Project`, or ``None`` when no project row
        exists yet. Callers use that ``None`` to render their empty state or to
        leave ``task.project_id`` unset; it is never an error condition.
    """
    row = get_db().execute(_SELECT_PROJECT_SQL, (_PROJECT_ID,)).fetchone()
    if row is None:
        return None
    return Project.from_row(row)


def save_project(values: Mapping[str, Any]) -> Project:
    """Insert the project row, or update row 1 when it already exists.

    One statement does either -- ``INSERT ... ON CONFLICT (id) DO UPDATE`` -- so
    nothing reads the table first, and two concurrent saves cannot both observe an
    empty project and then collide on the primary key. ``created_at`` keeps the
    moment the record was first written, because the conflict clause does not name
    it.

    Args:
        values: The mapping ``app.validation.parse_project_form`` produces on
            success: the keys ``name``, ``description``, ``start_date`` and
            ``end_date``, all already trimmed strings with the two dates in
            ISO-8601 ``YYYY-MM-DD`` form. They are bound as parameters in the
            order the statement names them.

    Returns:
        The saved :class:`app.models.Project`, re-read after the commit so its
        ``created_at`` and ``updated_at`` are the stored values.

    Raises:
        RuntimeError: If row 1 cannot be read back after a successful write, which
            would mean something removed it mid-save. The row exists whenever the
            statement above succeeded, so this only makes a broken invariant
            visible instead of returning ``None`` from a function that promises a
            project.
    """
    connection = get_db()
    parameters = (
        values["name"],
        values["description"],
        values["start_date"],
        values["end_date"],
    )
    connection.execute(_UPSERT_PROJECT_SQL, (_PROJECT_ID, *parameters))
    connection.commit()

    row = connection.execute(_SELECT_PROJECT_SQL, (_PROJECT_ID,)).fetchone()
    if row is None:  # pragma: no cover - the write above just created the row
        raise RuntimeError("project row missing after save")
    return Project.from_row(row)
