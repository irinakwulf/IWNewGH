"""Jinja filters and globals that render the interface's dates and vocabulary.

This module is the single home for the vocabulary the interface shows. Templates
carry no formatting logic: they pipe a stored value through one of these four
filters, or render a deadline through the :func:`format_deadline` global, so the
status badge, the deadline column and the deadline indicator cannot drift apart,
and changing how a date reads is a change in one file.

The page shell and the three error pages take their links from the
:func:`app_path` global. It resolves an endpoint against the application's URL map
alone, so one of those pages still renders for a request Flask could build no URL
adapter for.

The stored shapes are the ones :mod:`app.models` documents: an ISO-8601
``YYYY-MM-DD`` string for a date, the UTC text SQLite writes with
``datetime('now')`` for a timestamp, and a lowercase member of
``app.models.TASK_STATUSES`` for a status.

Every filter returns a string for any input, and a value the filter cannot format
renders as an empty string rather than raising, because one bad row must not
replace a whole page with the error view. :func:`format_deadline` is the one
departure: it states an unreadable deadline's stored text, the way
:func:`status_label` states an unknown status, so the deadline column never
renders a blank cell.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from flask import current_app, g, has_request_context

from app import models

if TYPE_CHECKING:  # pragma: no cover - import for the annotation only
    from flask import Flask

__all__ = [
    "DEADLINE_SEPARATOR",
    "MONTHS",
    "SOON_WINDOW_DAYS",
    "app_path",
    "deadline_state",
    "format_date",
    "format_deadline",
    "format_datetime",
    "init_app",
    "reference_day",
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

# The one glyph that joins a deadline's absolute date to its relative word: a middle
# dot, written as an escape so the character in this file is unambiguous. The task
# list and the task detail page share it by both rendering :func:`format_deadline`,
# so neither can show a separator of its own.
DEADLINE_SEPARATOR = "\u00b7"

# The status that ends the working life of a task, and so the one status a
# deadline can pass without the task becoming late.
_COMPLETED_STATUS = "completed"

# The attribute of ``flask.g`` the request's reference day is cached under. A module
# constant so the helper and any reader of it cannot disagree on the spelling.
_REFERENCE_DAY_KEY = "deadline_reference_day"

# The server name :func:`app_path` binds the URL map to. A fixed literal, because the
# adapter a path is built from must not carry anything the request supplied: an
# unusable ``Host`` header is exactly the case those pages have to survive, and any
# value would do, since nothing built here is ever external.
_PATH_ONLY_SERVER_NAME = "localhost"


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


def _stored_text(value: object) -> str:
    """Return a stored value's own text, or ``""`` when it stores nothing."""
    if value is None:
        return ""
    return str(value).strip()


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


def reference_day() -> datetime.date:
    """Return the single day this request measures every deadline against.

    The interface measures every deadline on a page against one reference day, so the
    state and the day count of a rendered indicator always describe the same day, and a
    page rendered across midnight cannot label two rows of one list against different
    days.

    This is also the day :func:`app.services.tasks.list_tasks` measures its deadline
    buckets from: a request reads it here, passes it to the filter and lets the
    indicator read the same cached value, so the bucket a row is selected by and the
    word beside its deadline are decided against one day.

    Inside a request the day is read on first use and cached on ``flask.g`` for the
    rest of it; outside a request, as in a unit test calling :func:`deadline_state`
    directly, there is nothing to scope the value to and a fresh reading is returned.

    Returns:
        The day deadlines are compared with, as a :class:`datetime.date`.
    """
    if not has_request_context():
        return datetime.date.today()
    day: datetime.date | None = getattr(g, _REFERENCE_DAY_KEY, None)
    if day is None:
        day = datetime.date.today()
        setattr(g, _REFERENCE_DAY_KEY, day)
    return day


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

    The state names the deadline class a template renders. The word beside the date
    comes from :func:`format_deadline`, which states "Overdue", "Due today" or
    "Due in N days" for unfinished work and only the absolute date for anything else,
    so a task finished after its deadline never reads as late. That exclusion is the
    whole reason ``status`` is a parameter rather than a constant.

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
    # The request's one reference day, so every row of a page is measured against the
    # same day and the state always agrees with the day count beside it.
    today = reference_day()
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if due <= today + datetime.timedelta(days=SOON_WINDOW_DAYS):
        return "soon"
    return ""


def format_deadline(deadline: str | None, status: str = "pending") -> str:
    """Render a deadline as its absolute date and, when it applies, its word.

    The whole indicator is built here so the task list and the task detail page
    cannot describe the same deadline differently: each renders this string inside
    the element whose modifier class :func:`deadline_state` supplies.

    Args:
        deadline: The stored ISO-8601 deadline, or ``None`` for a task without one.
        status: The task's own status; ``"completed"`` drops the relative word,
            because finished work is not late. It defaults to an unfinished status,
            matching :func:`deadline_state`.

    Returns:
        ``"13 Nov 2026 · Due today"``, ``"11 Nov 2026 · Overdue"`` or
        ``"16 Nov 2026 · Due in 3 days"`` for unfinished work inside the window
        :func:`deadline_state` defines; the formatted date alone for a completed task
        or a deadline further out than :data:`SOON_WINDOW_DAYS`; the stored value's own
        text when no date can be read from it, so the column states what the row holds
        instead of falling blank; and ``""`` only when the deadline is absent or blank,
        which each caller states in its own words.
    """
    due = _parse_date(deadline)
    if due is None:
        # A value that holds no date is stated as it is stored, matching
        # :func:`status_label` on an unknown status: the cell names what the row
        # carries rather than leaving the reviewer with nothing to read.
        return _stored_text(deadline)
    date_text = format_date(deadline)
    state = deadline_state(deadline, status)
    if state == "overdue":
        word = "Overdue"
    elif state == "today":
        word = "Due today"
    elif state == "soon":
        # "soon" means the deadline is ahead of today and inside the window, so the
        # count runs from 1 to SOON_WINDOW_DAYS. The plural stands at one day because
        # "Due in N days" is the wording the interface states.
        days = (due - reference_day()).days
        word = f"Due in {days} days"
    else:
        return date_text
    return f"{date_text} {DEADLINE_SEPARATOR} {word}"


def app_path(endpoint: str, **values: object) -> str:
    """Build the path an endpoint is served at, without reading the request's Host.

    :func:`flask.url_for` builds through the request's own URL adapter, and a
    request whose ``Host`` header cannot be encoded -- one carrying a label longer
    than the 63 characters a DNS label allows, for instance -- is answered by
    werkzeug with a ``BadHost`` (400) before any adapter is bound, and Flask leaves
    that request with no adapter at all. The page shell and the three error pages
    still owe that request a rendered page, so the links and the stylesheet they
    carry are built here instead: from the application's own URL map, bound to
    :data:`_PATH_ONLY_SERVER_NAME`, in its relative form. For a request served at
    the application's root the result is the same path ``url_for`` returns.

    Args:
        endpoint: The endpoint to build, such as ``"dashboard.index"``.
        **values: The endpoint's variable arguments, such as ``filename`` for the
            ``"static"`` endpoint.

    Returns:
        The path that endpoint is served at, beginning with ``/``.

    Raises:
        werkzeug.routing.BuildError: If ``values`` does not fit the endpoint, the
            same failure ``url_for`` raises for that call.
    """
    adapter = current_app.url_map.bind(_PATH_ONLY_SERVER_NAME, url_scheme="http")
    return adapter.build(endpoint, values)


def init_app(app: Flask) -> None:
    """Register the presentation filters and the two globals on ``app``.

    ``add_template_filter`` takes the callable's ``__name__`` as the filter name, so
    the function names above are the template-facing contract and none of them is
    wrapped or renamed. :func:`format_deadline` and :func:`app_path` are globals
    rather than filters because the filter set is fixed at four; a global is called
    by name in a template exactly as a filter is, without widening that set.
    """
    app.add_template_filter(format_date)
    app.add_template_filter(format_datetime)
    app.add_template_filter(status_label)
    app.add_template_filter(deadline_state)
    app.add_template_global(format_deadline)
    app.add_template_global(app_path)
