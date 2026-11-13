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

**One snapshot per render.** Both statements of a render run inside a single
explicit deferred read transaction. The sqlite3 module opens no transaction of its
own around a ``SELECT``, so two bare reads would each take a fresh snapshot and a
write committed by another connection between them would be counted by the total and
missed by the buckets -- a dashboard rendering a total one larger than its own three
counts, which is exactly the identity the closed status set is meant to guarantee. A
caller that has already opened a transaction keeps it: the two statements then join
that transaction, which observes one snapshot by definition, and the caller is the
one that ends it.

:func:`total_task_count` is the same ``COUNT(*)`` on its own, for the task list,
whose result line needs the unfiltered total and none of the buckets.
"""

from __future__ import annotations

from app.db import get_db
from app.models import TASK_STATUSES

__all__ = ["dashboard_metrics", "total_task_count"]

# The two statements a render runs, written once so the total the task list reads and
# the total the dashboard reports cannot drift apart.
_STATUS_AGGREGATE_SQL = "SELECT status, COUNT(*) AS count FROM task GROUP BY status"
_TOTAL_COUNT_SQL = "SELECT COUNT(*) AS count FROM task"


def total_task_count() -> int:
    """Return the number of task rows, whatever their status or assignee.

    The unfiltered total on its own. The task list's result line needs the number
    of rows the whole table holds and none of the status buckets, so it reads this
    single ``COUNT(*)`` rather than a whole dashboard render that would compute
    three counts it throws away. It is also the ``total`` of
    :func:`dashboard_metrics`, so the list and the dashboard count the same rows
    with the same statement.

    Returns:
        The row count as an ``int``, ``0`` for an empty table. ``COUNT(*)`` with no
        ``GROUP BY`` always yields exactly one row, so there is no row to miss.
    """
    row = get_db().execute(_TOTAL_COUNT_SQL).fetchone()
    return int(row["count"])


def dashboard_metrics() -> dict[str, int]:
    """Return the four dashboard counts for the current database state.

    The grouped aggregate and the total are read inside one deferred transaction,
    so the four numbers describe a single snapshot of the table even while another
    connection is committing a write.

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

    # One explicit deferred transaction is what makes the two statements below one
    # snapshot. Only a transaction started here is one ended here: a caller that
    # already has a transaction open (a write in progress, say) keeps it, because
    # SQLite rejects a second BEGIN and reads inside the open transaction already
    # see one snapshot.
    started_transaction = not connection.in_transaction
    if started_transaction:
        connection.execute("BEGIN DEFERRED")
    try:
        # One grouped pass fills all three status buckets. Keying the aggregate by
        # status and then reading only the values TASK_STATUSES names is what keeps a
        # status outside the schema's closed set out of the mapping; the total below
        # comes from its own count, so such a row cannot change it either.
        by_status = connection.execute(_STATUS_AGGREGATE_SQL)
        counted = {row["status"]: int(row["count"]) for row in by_status}

        counts: dict[str, int] = {
            "total": 0,
            "completed": 0,
            "pending": 0,
            "in_progress": 0,
        }
        for status in TASK_STATUSES:
            counts[status] = counted.get(status, 0)

        # The same COUNT(*) the task list reads, taken inside this transaction so it
        # counts the snapshot the buckets came from.
        counts["total"] = total_task_count()
        return counts
    finally:
        # Rollback rather than commit: these statements only read, and rolling back
        # is what releases the read lock without a write. It runs on the error path
        # too, so a failed render leaves no transaction open on the connection.
        if started_transaction:
            connection.rollback()
