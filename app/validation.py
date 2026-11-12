"""Form and filter parsing: typed values for the services, messages for the forms.

Every submission the application accepts is read here. A parser takes the plain
string mapping it is handed -- ``request.form`` for a write, ``request.args`` for
the task list -- and returns a pair ``(values, errors)``:

* ``values`` holds the typed values a service call needs: dates normalised to
  ISO-8601 ``YYYY-MM-DD``, a member id resolved to an ``int``, absent optional
  fields as ``None``.
* ``errors`` maps a field name to the one message the form shows beside that
  field. It is empty exactly when the submission was accepted.

Both are populated on both paths, and that is deliberate. A rejected submission
still returns the trimmed text the reviewer typed, so the re-rendered form comes
back with every field filled in and only the offending one flagged -- a field the
parser could not type, such as an unparseable date, contributes its raw text.
``errors`` being empty is the only signal of success, so a caller never has to
test whether ``values`` looks complete.

This module owns the wording of every message, once, as a module constant: the
same bound is described by one string whether the form is a task or a project,
and changing how a rule reads is a change in one place.

Three boundaries decide what is not here. No web-framework object is imported --
the parsers receive mappings, which is what lets the whole rule set be unit-tested
without an application or a request. No SQL is executed here: a submitted member
id is checked through :func:`app.services.members.get_member`, the same check
behind the assignee field, the assignee filter and the comment author, so those
three controls cannot disagree about who exists. That lookup is the module's only
service call, and it only reads: this module decides what a value is, never what
to do with it, so nothing here can change stored data.

The task-list parser is the only one whose errors a view answers differently: a
write re-renders its own form, whereas a filter value outside its allowed set is a
malformed request and becomes the 400 page. Its rejection messages therefore name
the parameter rather than a field a reviewer would correct.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TypeAlias

from app import models
from app.services import members

__all__ = [
    "ASSIGNEE_UNASSIGNED",
    "DEADLINE_FILTERS",
    "DEFAULT_STATUS",
    "Form",
    "MSG_COMMENT_BODY_TOO_LONG",
    "MSG_DEADLINE_BEFORE_PROJECT_START",
    "MSG_DESCRIPTION_TOO_LONG",
    "MSG_END_DATE_BEFORE_START_DATE",
    "MSG_ENTER_COMMENT",
    "MSG_ENTER_PROJECT_NAME",
    "MSG_ENTER_TASK_TITLE",
    "MSG_INVALID_DATE",
    "MSG_INVALID_FILTER_ASSIGNEE",
    "MSG_INVALID_FILTER_DEADLINE",
    "MSG_INVALID_FILTER_STATUS",
    "MSG_PROJECT_NAME_TOO_LONG",
    "MSG_SELECT_COMMENT_AUTHOR",
    "MSG_SELECT_TEAM_MEMBER",
    "MSG_SELECT_VALID_STATUS",
    "MSG_TASK_TITLE_TOO_LONG",
    "Query",
    "STATUS_FILTERS",
    "is_valid_status",
    "parse_comment_form",
    "parse_iso_date",
    "parse_project_form",
    "parse_task_filters",
    "parse_task_form",
]

# The shape a parser receives. Naming the request globals by their content rather
# than by their Flask origin is what keeps this module free of Flask, and it lets
# a test pass a plain dict.
Form: TypeAlias = Mapping[str, str]
Query: TypeAlias = Mapping[str, str]

# --------------------------------------------------------------------------
# Messages. One constant per sentence the interface can show, because a rule and
# the words that describe it must change together or not at all. Several fields
# share a message where the rule is genuinely the same one.
# --------------------------------------------------------------------------

MSG_ENTER_PROJECT_NAME = "Enter a project name."
MSG_PROJECT_NAME_TOO_LONG = "Project names are limited to 120 characters."
MSG_ENTER_TASK_TITLE = "Enter a task title."
MSG_TASK_TITLE_TOO_LONG = "Task titles are limited to 200 characters."
# A description is optional on both forms and carries the same bound, so one
# message serves the project and the task.
MSG_DESCRIPTION_TOO_LONG = "Descriptions are limited to 2000 characters."
MSG_ENTER_COMMENT = "Enter a comment."
MSG_COMMENT_BODY_TOO_LONG = "Comments are limited to 1000 characters."

# The three calendar fields -- both project dates and the task deadline -- are read
# by one rule and so are described by one message.
MSG_INVALID_DATE = "Enter a valid date (YYYY-MM-DD)."
MSG_END_DATE_BEFORE_START_DATE = "End date cannot be earlier than the start date."
MSG_DEADLINE_BEFORE_PROJECT_START = (
    "Deadline cannot be earlier than the project start date."
)

MSG_SELECT_TEAM_MEMBER = "Select a team member from the list."
MSG_SELECT_COMMENT_AUTHOR = "Select a comment author."
MSG_SELECT_VALID_STATUS = "Select a valid status."

# Filter messages name their parameter: the view turns the mapping into a 400
# page, and that page tells the reviewer which parameter the request got wrong.
MSG_INVALID_FILTER_STATUS = "Invalid value for filter 'status'."
MSG_INVALID_FILTER_ASSIGNEE = "Invalid value for filter 'assignee'."
MSG_INVALID_FILTER_DEADLINE = "Invalid value for filter 'deadline'."

# --------------------------------------------------------------------------
# Bounds and vocabularies. The lengths mirror what the schema allows and what the
# templates' maxlength attributes hint at; the schema stays the last word, and
# these are the checks the reviewer meets first.
# --------------------------------------------------------------------------

_PROJECT_NAME_LIMIT = 120
_TITLE_LIMIT = 200
_DESCRIPTION_LIMIT = 2000
_COMMENT_BODY_LIMIT = 1000

# The status a task takes when a submission names none, matching the column
# default in app/schema.sql.
DEFAULT_STATUS: str = "pending"

# The statuses the status filter offers: the closed set itself, so the filter bar
# and the dashboard counts cannot recognise different statuses.
STATUS_FILTERS: tuple[str, ...] = models.TASK_STATUSES

# The deadline buckets the deadline filter offers. Each is a predicate over
# ``deadline`` and ``status`` rather than a value to compare, so they are named
# here and interpreted in app/services/tasks.py.
DEADLINE_FILTERS: tuple[str, ...] = ("overdue", "today", "week", "none")

# What the assignee filter submits to select tasks nobody is assigned to. A word
# rather than an id, so it can never be confused with one.
ASSIGNEE_UNASSIGNED = "unassigned"

_ISO_DATE_LENGTH = 10
_ISO_DATE_SEPARATOR = "-"
# Position 4 is the month separator and 7 the day separator in YYYY-MM-DD.
_ISO_DATE_SEPARATOR_POSITIONS = frozenset({4, 7})
# ASCII digits only: str.isdigit() is true of characters date.fromisoformat cannot
# read, so membership in this set is the check that matters.
_ASCII_DIGITS = frozenset("0123456789")


# --------------------------------------------------------------------------
# Helpers the parsers share.
# --------------------------------------------------------------------------


def parse_iso_date(value: str) -> str | None:
    """Return a submitted date as normalised ``YYYY-MM-DD`` text, or None.

    The value must be exactly ten characters of calendar form: zero-padded month
    and day, hyphens in the two separator positions, nothing before or after. The
    shape is checked before anything is parsed because ``date.fromisoformat``
    accepts forms this product does not store -- the basic ``20261026`` and the ISO
    week date ``2026-W43-1``, which is ten characters long -- and a deadline that
    reached SQLite in either form would compare wrongly against the ISO text the
    schema holds.

    Args:
        value: The submitted text, exactly as it arrived.

    Returns:
        The date as ``YYYY-MM-DD`` text, in the one form every consumer stores and
        compares, or ``None`` when the value is not a calendar date in that form.
        This function never raises: a caller learns a submission was unusable from
        the ``None``, not from an exception.
    """
    if not isinstance(value, str):
        return None
    if not _is_iso_date_shaped(value):
        return None
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        # Right shape, impossible date: 2026-02-30, month 13, day 00.
        return None
    return parsed.isoformat()


def is_valid_status(value: str) -> bool:
    """Return whether a value is one of the three statuses a task can hold.

    Exported because the two quick actions -- a status change from the task list
    and from the task detail view -- answer a value outside the set with the 400
    page rather than a field message: the control only ever offers the three, so an
    out-of-set value means the request itself was malformed.

    Args:
        value: The submitted text.

    Returns:
        ``True`` when the value is a member of :data:`app.models.TASK_STATUSES`.
    """
    return value in models.TASK_STATUSES


def _is_iso_date_shaped(value: str) -> bool:
    """Return whether a value has exactly the characters ``YYYY-MM-DD`` allows."""
    if len(value) != _ISO_DATE_LENGTH:
        return False
    return (
        value[4] == _ISO_DATE_SEPARATOR
        and value[7] == _ISO_DATE_SEPARATOR
        and all(
            character in _ASCII_DIGITS
            for position, character in enumerate(value)
            if position not in _ISO_DATE_SEPARATOR_POSITIONS
        )
    )


def _trim(raw: str | None) -> str:
    """Return a submitted value without surrounding whitespace; "" when absent.

    Trimming happens before a required check and before a length check, so a field
    of spaces is empty rather than two characters long, and a title padded with
    spaces is measured without them.
    """
    if raw is None:
        return ""
    return raw.strip()


def _required_text(
    raw: str | None,
    *,
    missing_message: str,
    limit: int,
    limit_message: str,
) -> tuple[str, str | None]:
    """Return a trimmed required text field and the message it violates, if any.

    Returns:
        The trimmed value -- empty when the field was left blank -- and the message
        to show beside the field, or ``None`` when the value is acceptable. The
        value is returned on both paths so a rejected form can repopulate.
    """
    value = _trim(raw)
    if not value:
        return value, missing_message
    if len(value) > limit:
        return value, limit_message
    return value, None


def _optional_text(
    raw: str | None,
    *,
    limit: int,
    limit_message: str,
) -> tuple[str, str | None]:
    """Return a trimmed optional text field and the message it violates, if any.

    Returns:
        The trimmed value, which is the empty string when the field was left blank,
        and the message for an over-long value or ``None`` when it is acceptable.
    """
    value = _trim(raw)
    if len(value) > limit:
        return value, limit_message
    return value, None


def _date_value(raw: str | None) -> tuple[str, str | None]:
    """Return a required date as ISO-8601 text and the message if it is unusable.

    Returns:
        The normalised date when it parses, otherwise the trimmed text the reviewer
        typed -- so the re-rendered form is never blanker than the submission --
        and the message to show beside the field, or ``None`` on success.
    """
    text = _trim(raw)
    parsed = parse_iso_date(text)
    if parsed is None:
        return text, MSG_INVALID_DATE
    return parsed, None


def _resolve_member(text: str) -> models.Member | None:
    """Return the member an id names, or None when it names one that does not exist.

    This is the single existence check behind the assignee field, the assignee
    filter and the comment author: one lookup, so the three controls cannot
    disagree about who is on the team. A value that is not a run of ASCII digits
    names no member without a query being spent on it.
    """
    if not (text.isascii() and text.isdigit()):
        return None
    return members.get_member(int(text))


def _assignee_value(raw: str | None) -> tuple[int | None, str | None]:
    """Return the assignee a task submission names, and its message if unknown.

    Returns:
        The member's id, or ``None`` when the field was absent or submitted empty
        -- which is how an assignment is cleared -- plus ``None`` when the value is
        acceptable or the message for an id that names no member. An id that names
        no member also yields ``None`` rather than the raw text: the assignee select
        only offers real members, so nothing in ``values`` needs to hold a value no
        option could match.
    """
    text = _trim(raw)
    if not text:
        return None, None
    member = _resolve_member(text)
    if member is None:
        return None, MSG_SELECT_TEAM_MEMBER
    return member.id, None


def _status_value(raw: str | None) -> tuple[str, str | None]:
    """Return the status a task submission names, defaulting when the key is absent.

    An absent key means the form did not offer a status and the task takes
    :data:`DEFAULT_STATUS`. A key that is present is a deliberate choice, so an
    empty string or any other value outside the closed set is an error rather than
    a default: the form's select always submits one of the three, and silently
    defaulting would hide a malformed request.

    Returns:
        The status to store, plus ``None`` on success or the message for a value
        outside the set. On rejection the value is the trimmed text submitted, so
        the field is repopulated as it arrived.
    """
    if raw is None:
        return DEFAULT_STATUS, None
    text = _trim(raw)
    if is_valid_status(text):
        return text, None
    return text, MSG_SELECT_VALID_STATUS


def _deadline_value(
    raw: str | None, project: models.Project | None
) -> tuple[str | None, str | None]:
    """Return a task deadline as ISO-8601 text, and the message if it is unusable.

    Args:
        raw: The submitted deadline, empty when the field was left blank.
        project: The stored project record, whose start date a deadline may not
            precede. ``None`` while the project details have not been recorded
            yet, in which case there is no start date to compare against and any
            calendar date is accepted -- a task can be created before the project
            record exists. A deadline later than the project's end date is
            accepted too: the end date describes the project, not a limit on it.

    Returns:
        The normalised deadline, ``None`` when none was submitted, or the trimmed
        text that was submitted when it does not parse -- and the message to show
        beside the field, or ``None`` when the value is acceptable.
    """
    text = _trim(raw)
    if not text:
        return None, None
    deadline = parse_iso_date(text)
    if deadline is None:
        return text, MSG_INVALID_DATE
    # Parsed rather than compared as text so a stored start date in any other form
    # is simply not a constraint instead of a wrong one.
    project_start = None if project is None else parse_iso_date(project.start_date)
    if project_start is not None and deadline < project_start:
        return deadline, MSG_DEADLINE_BEFORE_PROJECT_START
    return deadline, None


def _author_value(raw: str | None) -> tuple[int | None, str | None]:
    """Return the comment author a submission names, and its message if unknown.

    Unlike an assignee, an author is required: the application has no accounts, so
    a comment with nobody attributed to it has no author at all.

    Returns:
        The member's id, or ``None`` when the field was blank or named no member,
        plus ``None`` on success or the message to show beside the field.
    """
    member = _resolve_member(_trim(raw))
    if member is None:
        return None, MSG_SELECT_COMMENT_AUTHOR
    return member.id, None


# --------------------------------------------------------------------------
# The parsers. Each returns ``(values, errors)``: the typed values a service call
# needs and the message to show beside each offending field.
# --------------------------------------------------------------------------


def parse_project_form(form: Form) -> tuple[dict[str, object], dict[str, str]]:
    """Parse a submitted project record.

    The project has exactly four editable fields, and all four are read on every
    submission whether the record exists yet or not: ``POST /project`` inserts the
    row the first time and updates it afterwards, so the same parser serves both.

    Args:
        form: The submitted mapping, holding ``name``, ``description``,
            ``start_date`` and ``end_date``.

    Returns:
        ``(values, errors)``. ``values`` always carries all four keys -- the trimmed
        name and description, and each date as ISO-8601 text -- except that a date
        which did not parse keeps the text that was submitted, so the re-rendered
        form shows what was typed. ``errors`` maps ``name``, ``description``,
        ``start_date`` or ``end_date`` to its message, and is empty when the
        submission is acceptable.

        ``end_date`` is rejected when it precedes ``start_date``, but only when both
        dates parsed: one message per field is what the form can show, and the date
        message describes an unreadable date better than the ordering rule does.
    """
    errors: dict[str, str] = {}

    name, name_error = _required_text(
        form.get("name"),
        missing_message=MSG_ENTER_PROJECT_NAME,
        limit=_PROJECT_NAME_LIMIT,
        limit_message=MSG_PROJECT_NAME_TOO_LONG,
    )
    if name_error is not None:
        errors["name"] = name_error

    description, description_error = _optional_text(
        form.get("description"),
        limit=_DESCRIPTION_LIMIT,
        limit_message=MSG_DESCRIPTION_TOO_LONG,
    )
    if description_error is not None:
        errors["description"] = description_error

    start_date, start_error = _date_value(form.get("start_date"))
    if start_error is not None:
        errors["start_date"] = start_error

    end_date, end_error = _date_value(form.get("end_date"))
    if end_error is not None:
        errors["end_date"] = end_error
    elif start_error is None and end_date < start_date:
        # Both dates parsed, so the comparison is between ISO-8601 text, which
        # orders by date. An unparseable start date is not a constraint on the end
        # date: the reviewer is already being asked to fix the start date, and a
        # spurious ordering message beside the end date would be a second problem
        # they do not have.
        errors["end_date"] = MSG_END_DATE_BEFORE_START_DATE

    values: dict[str, object] = {
        "name": name,
        "description": description,
        "start_date": start_date,
        "end_date": end_date,
    }
    return values, errors


def parse_task_form(
    form: Form, project: models.Project | None
) -> tuple[dict[str, object], dict[str, str]]:
    """Parse a submitted task, for either the create or the edit form.

    Args:
        form: The submitted mapping, holding ``title``, ``description``,
            ``assignee_id``, ``status`` and ``deadline``. Every key is optional to
            the parser, and only the title is required of the reviewer.
        project: The stored project record, whose start date a deadline may not
            precede, or ``None`` while the project details have not been recorded.

    Returns:
        ``(values, errors)``. ``values`` always carries all five keys -- the trimmed
        title and description, ``assignee_id`` as an ``int`` or ``None``, the status
        (``pending`` when the form named none) and the deadline as ISO-8601 text or
        ``None`` -- with unparseable or unreadable text left as it was submitted so
        the form repopulates. ``errors`` maps a field name to its message and is
        empty when the submission is acceptable.

        No ``project_id`` is produced: which project a task belongs to is the
        service's decision, read from the current record at insert time, not a
        value a form can influence.
    """
    errors: dict[str, str] = {}

    title, title_error = _required_text(
        form.get("title"),
        missing_message=MSG_ENTER_TASK_TITLE,
        limit=_TITLE_LIMIT,
        limit_message=MSG_TASK_TITLE_TOO_LONG,
    )
    if title_error is not None:
        errors["title"] = title_error

    description, description_error = _optional_text(
        form.get("description"),
        limit=_DESCRIPTION_LIMIT,
        limit_message=MSG_DESCRIPTION_TOO_LONG,
    )
    if description_error is not None:
        errors["description"] = description_error

    assignee_id, assignee_error = _assignee_value(form.get("assignee_id"))
    if assignee_error is not None:
        errors["assignee_id"] = assignee_error

    status, status_error = _status_value(form.get("status"))
    if status_error is not None:
        errors["status"] = status_error

    deadline, deadline_error = _deadline_value(form.get("deadline"), project)
    if deadline_error is not None:
        errors["deadline"] = deadline_error

    values: dict[str, object] = {
        "title": title,
        "description": description,
        "assignee_id": assignee_id,
        "status": status,
        "deadline": deadline,
    }
    return values, errors


def parse_comment_form(form: Form) -> tuple[dict[str, object], dict[str, str]]:
    """Parse a submitted comment.

    Args:
        form: The submitted mapping, holding ``author_id`` and ``body``.

    Returns:
        ``(values, errors)``. ``values`` always carries both keys -- ``author_id``
        as the id of an existing member or ``None``, and the trimmed ``body`` --
        so the comment form comes back filled in after a rejection. ``errors`` maps
        ``author_id`` or ``body`` to its message, and is empty when the submission
        is acceptable.
    """
    errors: dict[str, str] = {}

    author_id, author_error = _author_value(form.get("author_id"))
    if author_error is not None:
        errors["author_id"] = author_error

    body, body_error = _required_text(
        form.get("body"),
        missing_message=MSG_ENTER_COMMENT,
        limit=_COMMENT_BODY_LIMIT,
        limit_message=MSG_COMMENT_BODY_TOO_LONG,
    )
    if body_error is not None:
        errors["body"] = body_error

    values: dict[str, object] = {"author_id": author_id, "body": body}
    return values, errors


def parse_task_filters(args: Query) -> tuple[dict[str, object], dict[str, str]]:
    """Parse the task list's three filters.

    A filter that is absent or submitted empty is not applied -- the empty string
    is what the "All" option of each select submits, so it is a legitimate
    submission and yields ``None``. A value that is not empty and not in its
    control's allowed set is rejected instead of being silently dropped: the
    request itself is malformed, and the view answers ``errors`` with the 400 page
    naming the parameter. The three are independent, so a request that gets two of
    them wrong reports both.

    Args:
        args: The request's query mapping, holding ``status``, ``assignee`` and
            ``deadline``.

    Returns:
        ``(values, errors)``. ``values`` always carries all three keys:
        ``status`` as one of :data:`STATUS_FILTERS` or ``None``; ``assignee`` as a
        member id, :data:`ASSIGNEE_UNASSIGNED`, or ``None``; ``deadline`` as one of
        :data:`DEADLINE_FILTERS` or ``None``. A rejected value leaves its key
        ``None`` -- the request produces the 400 page, so no filter list is
        rendered from it -- and ``errors`` maps ``status``, ``assignee`` or
        ``deadline`` to the message naming it.
    """
    values: dict[str, object] = {"status": None, "assignee": None, "deadline": None}
    errors: dict[str, str] = {}

    status = _trim(args.get("status"))
    if status:
        if is_valid_status(status):
            values["status"] = status
        else:
            errors["status"] = MSG_INVALID_FILTER_STATUS

    assignee = _trim(args.get("assignee"))
    if assignee:
        if assignee == ASSIGNEE_UNASSIGNED:
            values["assignee"] = ASSIGNEE_UNASSIGNED
        else:
            member = _resolve_member(assignee)
            if member is None:
                errors["assignee"] = MSG_INVALID_FILTER_ASSIGNEE
            else:
                values["assignee"] = member.id

    deadline = _trim(args.get("deadline"))
    if deadline:
        if deadline in DEADLINE_FILTERS:
            values["deadline"] = deadline
        else:
            errors["deadline"] = MSG_INVALID_FILTER_DEADLINE

    return values, errors
