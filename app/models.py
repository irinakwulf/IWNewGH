"""Value objects for the task board's four entities and its status vocabulary.

Services build these frozen dataclasses from ``sqlite3.Row`` results and templates
read them, so no template ever indexes a raw database row. Field types are the
Python types the application passes around: ``deadline`` is an ISO-8601
``YYYY-MM-DD`` string or ``None``, timestamps are the UTC text SQLite writes with
``datetime('now')``, and ``status`` is one of ``TASK_STATUSES``.

Label and deadline wording deliberately live in :mod:`app.templating`, not here:
presentation has one home, which keeps the status badge, the deadline indicator
and the deadline filter from disagreeing with each other.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Comment",
    "Member",
    "Project",
    "STATUS_LABELS",
    "TASK_STATUSES",
    "Task",
]

# The closed status set, in the same order as the schema's CHECK constraint.
TASK_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")

# Display labels keyed by the database value, which is what every query stores.
STATUS_LABELS: dict[str, str] = {
    "pending": "Pending",
    "in_progress": "In progress",
    "completed": "Completed",
}


def _column(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    """Return ``row[name]`` when the query selected that column, else ``default``.

    Queries that join an assignee or count comments carry extra columns while
    others select the plain table columns only, so the joined values are read
    through this helper and a missing one yields ``default`` instead of raising
    ``IndexError``.
    """
    return row[name] if name in row.keys() else default


@dataclass(frozen=True)
class Project:
    """The single project record: name, description, start date and end date."""

    id: int
    name: str
    description: str
    start_date: str
    end_date: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Project:
        """Build a Project from a project-table row."""
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class Member:
    """A team member tasks are assigned to and comments are attributed to."""

    id: int
    name: str
    role: str
    email: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Member:
        """Build a Member from a member-table row."""
        return cls(
            id=row["id"],
            name=row["name"],
            role=row["role"],
            email=row["email"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Task:
    """A unit of work, optionally assigned to a member and optionally due.

    ``assignee_name`` and ``comment_count`` are joined display values: the
    services that select them fill them in, and the ones that do not leave the
    defaults, so templates never resolve an assignee id or count comments.
    """

    id: int
    project_id: int | None
    title: str
    description: str
    assignee_id: int | None
    status: str
    deadline: str | None
    created_at: str
    updated_at: str
    assignee_name: str | None = None
    comment_count: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        """Build a Task from a row, filling the joined columns when selected."""
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            description=row["description"],
            assignee_id=row["assignee_id"],
            status=row["status"],
            deadline=row["deadline"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            assignee_name=_column(row, "assignee_name"),
            comment_count=int(_column(row, "comment_count", 0) or 0),
        )


@dataclass(frozen=True)
class Comment:
    """A comment on a task, attributed to a team member at a moment in time.

    ``author_name`` is a joined display value, defaulted so a plain comment row
    constructs too.
    """

    id: int
    task_id: int
    author_id: int
    body: str
    created_at: str
    author_name: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Comment:
        """Build a Comment from a row, filling the joined author name when selected."""
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            author_id=row["author_id"],
            body=row["body"],
            created_at=row["created_at"],
            author_name=_column(row, "author_name") or "",
        )
