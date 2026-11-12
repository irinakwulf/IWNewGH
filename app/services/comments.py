"""Comment reads and writes for the task board.

A comment belongs to one task and is attributed to one member, so both functions
here join ``member`` and hand back a :class:`app.models.Comment` carrying its
``author_name``: the list never pays a second query per row, and no caller has to
resolve an author id itself.

The conversation reads oldest first, and that order is expressed in SQL rather
than by sorting in Python, with the id as a tie-break so two comments written in
the same second still come back in a stable order.

Two boundaries belong to other layers and are deliberately absent here. Parsing
and the 1-1000 character bound on a body live in :mod:`app.validation`, so this
module stores exactly the body it is given. An unknown task becomes ``None``
instead of an exception -- turning that into a 404 is the blueprint's decision --
which also means the existence check runs before the insert rather than letting
the foreign key raise.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db import get_db
from app.models import Comment

__all__ = ["add_comment", "list_comments"]

# The projection and join every comment read shares, so the conversation listing
# and the re-read after an insert cannot disagree about which columns a Comment
# needs. Only constants are concatenated onto it; no submitted value ever is.
_COMMENT_SELECT = (
    "SELECT c.id, c.task_id, c.author_id, c.body, c.created_at,"
    " m.name AS author_name"
    " FROM comment c JOIN member m ON m.id = c.author_id"
)

# The explicit ORDER BY is what makes a conversation read in the order it
# happened; the id tie-break keeps that order deterministic within one second.
_LIST_COMMENTS = (
    _COMMENT_SELECT + " WHERE c.task_id = ? ORDER BY c.created_at ASC, c.id ASC"
)

# Reads back the row just inserted, through the same join, so the returned value
# carries the joined author name and the created_at the column default wrote.
_SELECT_COMMENT = _COMMENT_SELECT + " WHERE c.id = ?"

# Checked before the insert: with foreign keys enforced, a comment on a missing
# task would raise sqlite3.IntegrityError, and this module reports an unknown
# task as None instead.
_TASK_EXISTS = "SELECT id FROM task WHERE id = ?"

_INSERT_COMMENT = "INSERT INTO comment (task_id, author_id, body) VALUES (?, ?, ?)"


def list_comments(task_id: int) -> list[Comment]:
    """Return the comments on a task, oldest first, with each author's name.

    Args:
        task_id: The task whose conversation to read.

    Returns:
        One :class:`app.models.Comment` per comment on the task, ordered by
        ``created_at`` and then by ``id``, each carrying its ``author_name``. The
        list is empty for a task with no comments and for a task id that does not
        exist, because this function does not decide whether the task is there --
        the caller's existence check is what turns an unknown id into a 404.
    """
    rows = get_db().execute(_LIST_COMMENTS, (task_id,)).fetchall()
    return [Comment.from_row(row) for row in rows]


def add_comment(task_id: int, values: Mapping[str, Any]) -> Comment | None:
    """Insert a comment and return it, or return None when the task does not exist.

    Args:
        task_id: The task the comment is written on.
        values: The mapping :func:`app.validation.parse_comment_form` returns for
            an accepted submission: ``author_id``, already checked against the
            member table, and the trimmed ``body``. Both reach SQLite as bound
            parameters.

    Returns:
        The stored :class:`app.models.Comment`, re-read from the database so it
        carries the ``id`` and the ``created_at`` that SQLite's column default
        wrote, along with its joined ``author_name``. ``None`` when no task has
        ``task_id``, in which case nothing is written.
    """
    db = get_db()
    if db.execute(_TASK_EXISTS, (task_id,)).fetchone() is None:
        return None

    cursor = db.execute(
        _INSERT_COMMENT, (task_id, values["author_id"], values["body"])
    )
    db.commit()

    # The row was committed a line ago, so this read always finds it; reading it
    # back is what supplies the database-written timestamp and the author's name.
    row = db.execute(_SELECT_COMMENT, (cursor.lastrowid,)).fetchone()
    return Comment.from_row(row)
