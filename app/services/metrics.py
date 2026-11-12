"""The four numbers the dashboard reports, computed from the task rows themselves.

The dashboard asks one question of the data -- how much work sits in each state --
and this module answers it from the ``task`` table on every call rather than from a
stored counter, so a create, an assignment or a status change is reflected the next
time the page is loaded and no code path exists that could leave a stale number.

Two statements are the whole cost of a dashboard render:

* one grouped aggregate returns the pending, in-progress and completed buckets in a
  single pass, served by the index on ``task(status)``;
* one ``COUNT(*)`` returns the total.

A ``COUNT`` per card would pay a scan and a round trip for each metric, and a
materialised counter would have to be maintained on every status change and
backfilled whenever the data changed. The status set is closed by the schema's
``CHECK`` constraint, so the three buckets are exhaustive and mutually exclusive and
the total always equals their sum.
"""

from __future__ import annotations

from app.db import get_db
from app.models import TASK_STATUSES

__all__ = ["dashboard_metrics"]


def dashboard_metrics() -> dict[str, int]:
    """Return the four dashboard counts for the current database state.

    Returns:
        A mapping with exactly four keys -- ``total``, ``completed``, ``pending``
        and ``in_progress``, in that order -- whose values are the number of tasks
        in each state. Every key is always present: a status with no rows is
        reported as ``0``, never omitted and never ``None``, because the dashboard
        template reads all four unconditionally. ``total`` counts every row,
        whatever its status or assignee, so it equals the sum of the three status
        buckets.
    """
    connection = get_db()

    # One grouped pass fills all three status buckets. Keying the aggregate by
    # status and then reading only the values TASK_STATUSES names is what keeps a
    # status outside the schema's closed set out of the mapping; the total below
    # comes from its own count, so such a row cannot change it either.
    by_status = connection.execute(
        "SELECT status, COUNT(*) AS count FROM task GROUP BY status"
    )
    counted = {row["status"]: int(row["count"]) for row in by_status}

    counts: dict[str, int] = {
        "total": 0,
        "completed": 0,
        "pending": 0,
        "in_progress": 0,
    }
    for status in TASK_STATUSES:
        counts[status] = counted.get(status, 0)

    # COUNT(*) with no GROUP BY always yields exactly one row.
    total = connection.execute("SELECT COUNT(*) AS count FROM task").fetchone()
    counts["total"] = int(total["count"])
    return counts
