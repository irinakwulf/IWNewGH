"""Read access to the team members tasks are assigned to and comments cite.

The request names no member administration, so this module reads the ``member``
table and never writes it: the five members arrive with the sample data, and the
application's only uses for them are the assignee select, the assignee filter and
the comment-author select -- all of which need the list in one predictable order
-- plus the existence check every one of those fields is validated against.

Both functions return :class:`app.models.Member`, never a raw ``sqlite3.Row``, so
no template resolves a column itself. An id that names no member yields ``None``
rather than an exception: the caller decides whether that is a field error on a
form or a 400 on a quick action, and this layer holds no HTTP concept.
"""

from __future__ import annotations

from app.db import get_db, is_sqlite_integer
from app.models import Member

__all__ = ["get_member", "list_members"]


def list_members() -> list[Member]:
    """Return every team member ordered by name.

    Ordering is ``name, id`` so two members who share a name still come back in
    the same order on every call, which keeps the generated selects stable.

    Returns:
        The ``member`` rows as :class:`app.models.Member` objects, ordered by
        name. An empty list when no members exist yet.
    """
    rows = get_db().execute(
        "SELECT id, name, role, email, created_at FROM member ORDER BY name, id"
    ).fetchall()
    return [Member.from_row(row) for row in rows]


def get_member(member_id: int | None) -> Member | None:
    """Return the member with this id, or None when the id is unknown.

    Args:
        member_id: The member's primary key, already parsed to an ``int`` by the
            caller. ``None`` means the field was absent or submitted empty, which
            is answered here without a query: binding ``None`` into ``id = ?``
            matches nothing and would cost a statement to learn that. An id
            outside SQLite's signed 64-bit range names no member either, and is
            answered the same way: the column cannot hold it, so no row can carry
            it, and binding it would raise rather than match nothing.

    Returns:
        The matching :class:`app.models.Member`, or ``None`` when no member has
        that id -- including an id no row could hold, which is decided without a
        query being spent on it.
    """
    if member_id is None:
        return None
    if not is_sqlite_integer(member_id):
        return None
    row = get_db().execute(
        "SELECT id, name, role, email, created_at FROM member WHERE id = ?",
        (member_id,),
    ).fetchone()
    if row is None:
        return None
    return Member.from_row(row)
