"""Jinja filters that render dates, timestamps, status labels and deadline states.

This module is the single home for the vocabulary the interface shows. Templates
carry no formatting logic: they pipe a stored value through one of these four
filters, so the status badge, the deadline column and the deadline indicator
cannot drift apart, and changing how a date reads is a change in one file.

The stored shapes are the ones :mod:`app.models` documents: an ISO-8601
``YYYY-MM-DD`` string for a date, the UTC text SQLite writes with
``datetime('now')`` for a timestamp, and a lowercase member of
``app.models.TASK_STATUSES`` for a status.

Every filter returns a string for any input. A malformed value renders as an
empty string rather than raising, because one bad row must not replace a whole
page with the error view.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from app import models

if TYPE_CHECKING:  # pragma: no cover - import for the annotation only
    from flask import Flask

__all__ = [
    "MONTHS",
    "SOON_WINDOW_DAYS",
    "deadline_state",
    "format_date",
    "format_datetime",
    "init_app",
    "status_label",
]

# Three-letter month names, indexed by ``date.month - 1``. A tuple rather than
# ``strftime("%b")``: that follows the process locale, and the interface ships in
# English only, so the rendered date must not change with the host's locale.
MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# How far past today the "soon" state reaches, in days. The Due this week task
# filter matches ``today <= deadline <= today + 6``, and this is the same seven
# day span, so the word beside a deadline and the deadline filter never disagree
# about which work is imminent.
SOON_WINDOW_DAYS = 6

# The status that ends the working life of a task, and so the one status a
# deadline can pass without the task becoming late.
_COMPLETED_STATUS = "completed"


def _parse_date(value: object) -> datetime.date | None:
    """Return the date a stored value holds, or ``None`` when it holds none.

    Accepts a ``date`` or ``datetime`` object as well as text; text carrying a
    time component keeps its date part, since a date column that holds a
    timestamp should still render as a date.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # "T" and a space are the two separators an ISO-8601 value can use between
    # its date and time parts.
    date_part = text.split("T", 1)[0].split(" ", 1)[0]
    try:
        return datetime.date.fromisoformat(date_part)
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime.datetime | None:
    """Return the moment a stored timestamp holds, or ``None`` when it holds none.

    ``fromisoformat`` takes the space or ``T`` separator, a trailing ``Z``, a
    numeric offset and fractional seconds, which covers what SQLite writes and
    what a reviewer might hand-place in the database.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def format_date(value: str | None) -> str:
    """Render a stored date as ``26 Oct 2026``; ``""`` when absent or unusable.

    The day is not zero-padded, so January the fifth reads ``5 Jan 2026``, which
    is how the interface states dates in prose everywhere else. A value that also
    carries a time component is accepted and its date part used.
    """
    day = _parse_date(value)
    if day is None:
        return ""
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def format_datetime(value: str | None) -> str:
    """Render a stored timestamp as ``26 Oct 2026, 14:03``; ``""`` when unusable.

    Seconds are dropped, the clock is 24-hour with a zero-padded hour and minute,
    and the day is not zero-padded, matching :func:`format_date`. The stored
    wall-clock value is shown as written -- no time-zone conversion -- so the same
    row always renders the same string wherever the application runs.
    """
    moment = _parse_datetime(value)
    if moment is None:
        return ""
    month = MONTHS[moment.month - 1]
    return f"{moment.day} {month} {moment.year}, {moment.hour:02d}:{moment.minute:02d}"


def status_label(value: str | None) -> str:
    """Render a status as ``Pending``, ``In progress`` or ``Completed``.

    An absent value renders as ``""``. A value the label mapping does not know is
    returned as its own text, so a row that somehow holds an unexpected status
    shows its stored value instead of breaking the page.
    """
    if not value:
        return ""
    text = str(value)
    return models.STATUS_LABELS.get(text, text)


def deadline_state(deadline: str | None, status: str = "pending") -> str:
    """Return ``"overdue"``, ``"today"``, ``"soon"`` or ``""`` for a deadline.

    A completed task renders no relative word: the interface adds "Overdue",
    "Due today" or "Due in N days" only for unfinished work, so a task finished
    after its deadline never reads as late. That exclusion is the whole reason
    ``status`` is a parameter rather than a constant.

    Args:
        deadline: The stored ISO-8601 deadline, or ``None`` for a task without one.
        status: The task's own status; ``"completed"`` suppresses every state. It
            defaults to an unfinished status, so a caller that cannot supply it
            still gets the deadline's state instead of silence.

    Returns:
        The state name the templates map onto their deadline classes, or ``""``
        when there is nothing to say: no deadline, a completed task, an
        unparseable value, or a date further ahead than :data:`SOON_WINDOW_DAYS`.
    """
    if not deadline or status == _COMPLETED_STATUS:
        return ""
    due = _parse_date(deadline)
    if due is None:
        return ""
    # Read inside the call, not at import time, so a process running across
    # midnight labels deadlines against the day the page is rendered.
    today = datetime.date.today()
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if due <= today + datetime.timedelta(days=SOON_WINDOW_DAYS):
        return "soon"
    return ""


def init_app(app: Flask) -> None:
    """Register the four presentation filters on ``app``'s Jinja environment.

    ``add_template_filter`` takes the callable's ``__name__`` as the filter name,
    so the function names above are the template-facing contract and none of them
    is wrapped or renamed.
    """
    app.add_template_filter(format_date)
    app.add_template_filter(format_datetime)
    app.add_template_filter(status_label)
    app.add_template_filter(deadline_state)
