"""Direct unit tests of :mod:`app.validation`, one case per rule it enforces.

``app.validation`` is the only module that decides what a submission means, and it
imports no web-framework object, so it is tested here on its own: no test client, no
request, no route and no template. Each parser is handed the plain mapping a view
hands it and both halves of its answer are asserted -- the typed ``values`` a service
call would receive and the ``errors`` mapping the form renders -- because a rule that
accepted a value it should reject, or rejected one it should accept, is a defect
whether or not some page happens to show it.

Values are asserted as whole mappings and messages as equal strings, never as
substrings: the parsers return exactly the documented keys and exactly one message
per offending field, and a test that only looked for a fragment would pass against a
rule that had grown a second, contradictory sentence.

Nothing about the sample data is written down here. Member ids are read from the
seeded database through the same service function the application uses, and every
calendar date in an assertion is derived from ``date.today()`` or from the seeded
project's own dates, so the suite keeps its meaning when the dataset or the day
changes. A few deliberately unparseable submissions are spelled out, because a shape
the product must never store is a fact about the format rather than about the data.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

import pytest
from werkzeug.datastructures import MultiDict

import seed
from app import models, validation
from app.services import members, projects

# ---------------------------------------------------------------------------
# The messages the parsers must produce, spelled out rather than imported from
# app.validation: the wording is part of what the forms render, so a reworded
# sentence should fail this suite and be reviewed instead of following the module
# silently.
# ---------------------------------------------------------------------------

MSG_ENTER_PROJECT_NAME = "Enter a project name."
MSG_PROJECT_NAME_TOO_LONG = "Project names are limited to 120 characters."
MSG_ENTER_TASK_TITLE = "Enter a task title."
MSG_TASK_TITLE_TOO_LONG = "Task titles are limited to 200 characters."
MSG_DESCRIPTION_TOO_LONG = "Descriptions are limited to 2000 characters."
MSG_ENTER_COMMENT = "Enter a comment."
MSG_COMMENT_BODY_TOO_LONG = "Comments are limited to 1000 characters."
MSG_INVALID_DATE = "Enter a valid date (YYYY-MM-DD)."
MSG_END_DATE_BEFORE_START_DATE = "End date cannot be earlier than the start date."
MSG_DEADLINE_BEFORE_PROJECT_START = (
    "Deadline cannot be earlier than the project start date."
)
MSG_SELECT_TEAM_MEMBER = "Select a team member from the list."
MSG_SELECT_COMMENT_AUTHOR = "Select a comment author."
MSG_SELECT_VALID_STATUS = "Select a valid status."
MSG_INVALID_FILTER_STATUS = "Invalid value for filter 'status'."
MSG_INVALID_FILTER_ASSIGNEE = "Invalid value for filter 'assignee'."
MSG_INVALID_FILTER_DEADLINE = "Invalid value for filter 'deadline'."

# The bounds the messages above describe, named once so each boundary case states
# the length it tests rather than a bare number.
PROJECT_NAME_LIMIT = 120
TITLE_LIMIT = 200
DESCRIPTION_LIMIT = 2000
COMMENT_BODY_LIMIT = 1000

# Submissions that are not the ``YYYY-MM-DD`` calendar form the schema stores: a
# day-first date, a month and a day that do not exist, the basic ``YYYYMMDD`` form
# ``date.fromisoformat`` also reads, and a ten-character ISO week date it reads too
# -- the last two would compare wrongly against the ISO text in SQLite.
MALFORMED_DATE_SHAPES = (
    pytest.param("31-10-2026", id="day-first"),
    pytest.param("2026-13-01", id="month-thirteen"),
    pytest.param("2026-02-30", id="february-thirtieth"),
    pytest.param("20261026", id="basic-format"),
    pytest.param("2026-W43-1", id="iso-week-date"),
)

# A required date field posts the empty string when the reviewer leaves it blank,
# and that is a missing date rather than an unreadable one. An optional date field
# -- the task deadline -- reads the same empty string as no deadline at all, so it
# is covered by its own case instead of this table.
MALFORMED_DATES = (pytest.param("", id="empty"), *MALFORMED_DATE_SHAPES)

# Values naming no member: one past any seeded key, one that is not a number at
# all. Both are what a tampered form posts, and neither may be stored.
UNKNOWN_MEMBER_ID = "999999"
UNKNOWN_MEMBER_IDS = (
    pytest.param(UNKNOWN_MEMBER_ID, id="no-such-row"),
    pytest.param("abc", id="not-a-number"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_shift(days: int) -> str:
    """Return today's date moved by a number of days, as ``YYYY-MM-DD`` text."""
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _shift(iso_date: str, days: int) -> str:
    """Return an ISO date moved by a number of days."""
    return (
        datetime.date.fromisoformat(iso_date) + datetime.timedelta(days=days)
    ).isoformat()


def _form(**values: object) -> MultiDict[str, str]:
    """Return a submission mapping holding these values, omitting the absent ones.

    A value of ``None`` means the control was not submitted at all, whereas ``""``
    is what a control the reviewer left blank posts. The two are exercised
    separately because a parser that conflates them accepts a value it should
    reject. ``MultiDict`` is the type ``request.form`` and ``request.args`` are, so
    the parsers are called with what they are given in production.
    """
    return MultiDict(
        {name: str(value) for name, value in values.items() if value is not None}
    )


def _project_form(**overrides: object) -> MultiDict[str, str]:
    """Return a valid project submission with the named fields overridden."""
    fields: dict[str, object] = {
        "name": "Website Redesign",
        "description": "Marketing site refresh with a new design system.",
        "start_date": _iso_shift(-10),
        "end_date": _iso_shift(10),
    }
    fields.update(overrides)
    return _form(**fields)


def _task_form(**overrides: object) -> MultiDict[str, str]:
    """Return a valid task submission with the named fields overridden.

    The assignee, status and deadline keys are absent unless a test supplies them,
    so every case that resolves no member runs without a database.
    """
    fields: dict[str, object] = {
        "title": "Implement the new hero component",
        "description": "Cover the new hero above the fold.",
    }
    fields.update(overrides)
    return _form(**fields)


def _comment_form(**overrides: object) -> MultiDict[str, str]:
    """Return a comment submission with the named fields overridden."""
    fields: dict[str, object] = {"body": "Looks good to me."}
    fields.update(overrides)
    return _form(**fields)


# ---------------------------------------------------------------------------
# Fixtures. The parsers reach the member table whenever a submission names an
# assignee or a comment author, which needs the request's connection and therefore
# an application context. Only the cases that do run inside one: everything else is
# context-free, so a failure there points at the rule rather than at the database.
# ---------------------------------------------------------------------------


@pytest.fixture
def app_context(seeded_client) -> Iterator[None]:
    """Run the test inside an application context on the seeded database."""
    with seeded_client.application.app_context():
        yield


@pytest.fixture
def member_ids(app_context: None) -> dict[str, int]:
    """Return each seeded member's email mapped to the id it was given."""
    return {member.email: member.id for member in members.list_members()}


@pytest.fixture
def sample_member_id(member_ids: dict[str, int]) -> int:
    """Return the stored id of the first member in the sample dataset."""
    return member_ids[seed.SAMPLE_DATA["members"][0]["email"]]


@pytest.fixture
def seeded_project(app_context: None) -> models.Project:
    """Return the recorded project, whose dates bound a submitted deadline."""
    project = projects.get_project()
    assert project is not None, "the sample data always records one project"
    return project


# ---------------------------------------------------------------------------
# The project record: four editable fields, two of them dates that must be in
# order. Nothing here resolves a member, so nothing here needs a database.
# ---------------------------------------------------------------------------


def test_project_form_accepts_a_complete_record() -> None:
    """A complete submission is accepted, trimmed and returned as ISO text."""
    start = _iso_shift(-10)
    end = _iso_shift(10)

    values, errors = validation.parse_project_form(
        _form(
            name="  Website Redesign  ",
            description="  Marketing site refresh.  ",
            start_date=start,
            end_date=end,
        )
    )

    assert errors == {}
    assert values == {
        "name": "Website Redesign",
        "description": "Marketing site refresh.",
        "start_date": start,
        "end_date": end,
    }


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
    ],
)
def test_project_name_is_required(name: str | None) -> None:
    """A name that is absent, empty or only spaces is rejected after trimming."""
    values, errors = validation.parse_project_form(_project_form(name=name))

    assert errors == {"name": MSG_ENTER_PROJECT_NAME}
    assert values["name"] == ""


@pytest.mark.parametrize(
    ("length", "expected_error"),
    [
        pytest.param(PROJECT_NAME_LIMIT, None, id="at-the-limit"),
        pytest.param(PROJECT_NAME_LIMIT + 1, MSG_PROJECT_NAME_TOO_LONG, id="over"),
    ],
)
def test_project_name_length_boundary(
    length: int, expected_error: str | None
) -> None:
    """120 characters is accepted and 121 is rejected."""
    name = "N" * length

    values, errors = validation.parse_project_form(_project_form(name=name))

    assert values["name"] == name
    if expected_error is None:
        assert errors == {}
    else:
        assert errors == {"name": expected_error}


@pytest.mark.parametrize(
    ("description", "accepted"),
    [
        pytest.param(None, True, id="absent"),
        pytest.param("", True, id="empty"),
        pytest.param("D" * DESCRIPTION_LIMIT, True, id="at-the-limit"),
        pytest.param("D" * (DESCRIPTION_LIMIT + 1), False, id="over"),
    ],
)
def test_project_description_is_optional_and_bounded(
    description: str | None, accepted: bool
) -> None:
    """A description may be left out, and may be up to 2000 characters."""
    values, errors = validation.parse_project_form(
        _project_form(description=description)
    )

    assert (errors == {}) is accepted
    assert values["description"] == (description or "")
    if not accepted:
        assert errors == {"description": MSG_DESCRIPTION_TOO_LONG}


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("submitted", MALFORMED_DATES)
def test_project_dates_must_parse(field: str, submitted: str) -> None:
    """An unusable date is rejected, and its raw text is kept for the re-render.

    Exactly one field is flagged: an unreadable start date is not also reported
    beside the end date, because the reviewer is already being asked to fix it.
    """
    values, errors = validation.parse_project_form(
        _project_form(**{field: submitted})
    )

    assert errors == {field: MSG_INVALID_DATE}
    assert values[field] == submitted


def test_project_end_date_cannot_precede_the_start_date() -> None:
    """An end date one day before the start date is rejected on the end date."""
    start = _iso_shift(0)
    end = _shift(start, -1)

    values, errors = validation.parse_project_form(
        _project_form(start_date=start, end_date=end)
    )

    assert errors == {"end_date": MSG_END_DATE_BEFORE_START_DATE}
    assert values["start_date"] == start
    assert values["end_date"] == end


def test_project_end_date_may_equal_the_start_date() -> None:
    """A one-day project is a valid project: equal dates are accepted."""
    same_day = _iso_shift(0)

    values, errors = validation.parse_project_form(
        _project_form(start_date=same_day, end_date=same_day)
    )

    assert errors == {}
    assert values["start_date"] == same_day
    assert values["end_date"] == same_day


def test_a_rejected_project_form_keeps_the_submitted_text() -> None:
    """Every field comes back as submitted, so the re-rendered form is not blank."""
    values, errors = validation.parse_project_form(
        _project_form(
            name="  Website Redesign  ",
            description="  Marketing site refresh.  ",
            start_date="",
            end_date="31-10-2026",
        )
    )

    assert errors == {
        "start_date": MSG_INVALID_DATE,
        "end_date": MSG_INVALID_DATE,
    }
    assert values == {
        "name": "Website Redesign",
        "description": "Marketing site refresh.",
        "start_date": "",
        "end_date": "31-10-2026",
    }


# ---------------------------------------------------------------------------
# The task form: title required, description optional, assignee and deadline
# nullable, status defaulted. Only the assignee cases resolve a member.
# ---------------------------------------------------------------------------


def test_task_form_accepts_a_complete_submission(
    app_context: None, sample_member_id: int, seeded_project: models.Project
) -> None:
    """A complete submission is accepted with exactly the five documented keys.

    Which project a task belongs to is the service's decision rather than a value a
    form can influence, so ``values`` carries no ``project_id``.
    """
    deadline = _shift(seeded_project.start_date, 5)

    values, errors = validation.parse_task_form(
        _task_form(
            title="  Implement the new hero component  ",
            description="  Cover the new hero above the fold.  ",
            assignee_id=str(sample_member_id),
            status="in_progress",
            deadline=deadline,
        ),
        seeded_project,
    )

    assert errors == {}
    assert values == {
        "title": "Implement the new hero component",
        "description": "Cover the new hero above the fold.",
        "assignee_id": sample_member_id,
        "status": "in_progress",
        "deadline": deadline,
    }


@pytest.mark.parametrize(
    "title",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
    ],
)
def test_task_title_is_required(title: str | None) -> None:
    """A title that is absent, empty or only spaces is rejected after trimming."""
    values, errors = validation.parse_task_form(_task_form(title=title), None)

    assert errors == {"title": MSG_ENTER_TASK_TITLE}
    assert values["title"] == ""


@pytest.mark.parametrize(
    ("length", "expected_error"),
    [
        pytest.param(TITLE_LIMIT, None, id="at-the-limit"),
        pytest.param(TITLE_LIMIT + 1, MSG_TASK_TITLE_TOO_LONG, id="over"),
    ],
)
def test_task_title_length_boundary(length: int, expected_error: str | None) -> None:
    """200 characters is accepted and 201 is rejected."""
    title = "T" * length

    values, errors = validation.parse_task_form(_task_form(title=title), None)

    assert values["title"] == title
    if expected_error is None:
        assert errors == {}
    else:
        assert errors == {"title": expected_error}


@pytest.mark.parametrize(
    ("description", "accepted"),
    [
        pytest.param(None, True, id="absent"),
        pytest.param("D" * DESCRIPTION_LIMIT, True, id="at-the-limit"),
        pytest.param("D" * (DESCRIPTION_LIMIT + 1), False, id="over"),
    ],
)
def test_task_description_is_optional_and_bounded(
    description: str | None, accepted: bool
) -> None:
    """A description may be left out, and may be up to 2000 characters."""
    values, errors = validation.parse_task_form(
        _task_form(description=description), None
    )

    assert (errors == {}) is accepted
    assert values["description"] == (description or "")
    if not accepted:
        assert errors == {"description": MSG_DESCRIPTION_TOO_LONG}


@pytest.mark.parametrize(
    "assignee_id",
    [pytest.param(None, id="absent"), pytest.param("", id="empty")],
)
def test_task_assignee_may_be_cleared(assignee_id: str | None) -> None:
    """An absent or empty assignee clears the assignment without an error."""
    values, errors = validation.parse_task_form(
        _task_form(assignee_id=assignee_id), None
    )

    assert errors == {}
    assert values["assignee_id"] is None


def test_task_assignee_is_resolved_to_a_member_id(
    app_context: None, sample_member_id: int
) -> None:
    """A submitted member id arrives at the service as the stored ``int`` id."""
    values, errors = validation.parse_task_form(
        _task_form(assignee_id=str(sample_member_id)), None
    )

    assert errors == {}
    assert values["assignee_id"] == sample_member_id
    assert isinstance(values["assignee_id"], int)


@pytest.mark.parametrize("assignee_id", UNKNOWN_MEMBER_IDS)
def test_task_assignee_must_name_an_existing_member(
    app_context: None, assignee_id: str
) -> None:
    """An id that names no member is rejected, and no value is handed to the service."""
    values, errors = validation.parse_task_form(
        _task_form(assignee_id=assignee_id), None
    )

    assert errors == {"assignee_id": MSG_SELECT_TEAM_MEMBER}
    assert values["assignee_id"] is None


def test_task_status_defaults_to_pending_when_the_form_names_none() -> None:
    """A form that offers no status creates a pending task."""
    values, errors = validation.parse_task_form(_task_form(), None)

    assert errors == {}
    assert values["status"] == "pending"


@pytest.mark.parametrize("status", models.TASK_STATUSES)
def test_task_status_accepts_each_value_of_the_closed_set(status: str) -> None:
    """Each of the three statuses is echoed back unchanged."""
    values, errors = validation.parse_task_form(_task_form(status=status), None)

    assert errors == {}
    assert values["status"] == status


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("", id="empty"),
        pytest.param("done", id="outside-the-set"),
        pytest.param("in progress", id="label-instead-of-value"),
    ],
)
def test_task_status_outside_the_closed_set_is_rejected(status: str) -> None:
    """A present status outside the set is an error rather than a default.

    Only an absent key means "the form did not offer a status". An empty string is
    a submitted value, so silently defaulting it would hide a malformed request.
    """
    values, errors = validation.parse_task_form(_task_form(status=status), None)

    assert errors == {"status": MSG_SELECT_VALID_STATUS}
    assert values["status"] == status


@pytest.mark.parametrize(
    "deadline",
    [pytest.param(None, id="absent"), pytest.param("", id="empty")],
)
def test_task_deadline_may_be_absent(deadline: str | None) -> None:
    """A task without a deadline is stored with ``None``."""
    values, errors = validation.parse_task_form(
        _task_form(deadline=deadline), project=None
    )

    assert errors == {}
    assert values["deadline"] is None


@pytest.mark.parametrize("submitted", MALFORMED_DATE_SHAPES)
def test_task_deadline_must_parse(submitted: str) -> None:
    """A deadline that is not a calendar date is rejected and kept as submitted."""
    values, errors = validation.parse_task_form(
        _task_form(deadline=submitted), project=None
    )

    assert errors == {"deadline": MSG_INVALID_DATE}
    assert values["deadline"] == submitted


def test_task_deadline_cannot_precede_the_project_start_date(
    seeded_project: models.Project,
) -> None:
    """A deadline one day before the project starts is rejected."""
    too_early = _shift(seeded_project.start_date, -1)

    values, errors = validation.parse_task_form(
        _task_form(deadline=too_early), seeded_project
    )

    assert errors == {"deadline": MSG_DEADLINE_BEFORE_PROJECT_START}
    assert values["deadline"] == too_early


def test_task_deadline_may_equal_the_project_start_date(
    seeded_project: models.Project,
) -> None:
    """Work may be due on the day the project starts."""
    values, errors = validation.parse_task_form(
        _task_form(deadline=seeded_project.start_date), seeded_project
    )

    assert errors == {}
    assert values["deadline"] == seeded_project.start_date


def test_task_deadline_after_the_project_end_date_is_accepted(
    seeded_project: models.Project,
) -> None:
    """The end date describes the project, so it is not a limit on a task."""
    later = _shift(seeded_project.end_date, 30)

    values, errors = validation.parse_task_form(
        _task_form(deadline=later), seeded_project
    )

    assert errors == {}
    assert values["deadline"] == later


def test_task_deadline_ignores_the_project_start_date_without_a_project() -> None:
    """Before the project details are recorded there is no start date to compare.

    A task can be created first -- ``task.project_id`` is nullable for exactly that
    reason -- so a deadline that a later project start date would forbid is accepted
    here.
    """
    long_ago = _iso_shift(-3650)

    values, errors = validation.parse_task_form(_task_form(deadline=long_ago), None)

    assert errors == {}
    assert values["deadline"] == long_ago


# ---------------------------------------------------------------------------
# The comment form: an author chosen from the team and a bounded body. The author
# is required, unlike an assignee, because a comment with nobody attributed to it
# has no author at all.
# ---------------------------------------------------------------------------


def test_comment_is_trimmed_and_resolved_to_its_author(
    app_context: None, sample_member_id: int
) -> None:
    """An accepted comment carries the author's ``int`` id and the trimmed body."""
    values, errors = validation.parse_comment_form(
        _comment_form(
            author_id=str(sample_member_id),
            body="  One more pass on the hero.  ",
        )
    )

    assert errors == {}
    assert values == {
        "author_id": sample_member_id,
        "body": "One more pass on the hero.",
    }


@pytest.mark.parametrize(
    "author_id",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        *UNKNOWN_MEMBER_IDS,
    ],
)
def test_comment_author_is_required(app_context: None, author_id: str | None) -> None:
    """A blank author, or one naming no member, is rejected."""
    values, errors = validation.parse_comment_form(_comment_form(author_id=author_id))

    assert errors == {"author_id": MSG_SELECT_COMMENT_AUTHOR}
    assert values["author_id"] is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
    ],
)
def test_comment_body_is_required(
    app_context: None, sample_member_id: int, body: str | None
) -> None:
    """A body that is absent, empty or only spaces is rejected after trimming."""
    values, errors = validation.parse_comment_form(
        _comment_form(author_id=str(sample_member_id), body=body)
    )

    assert errors == {"body": MSG_ENTER_COMMENT}
    assert values["body"] == ""


@pytest.mark.parametrize(
    ("length", "expected_error"),
    [
        pytest.param(COMMENT_BODY_LIMIT, None, id="at-the-limit"),
        pytest.param(COMMENT_BODY_LIMIT + 1, MSG_COMMENT_BODY_TOO_LONG, id="over"),
    ],
)
def test_comment_body_length_boundary(
    app_context: None,
    sample_member_id: int,
    length: int,
    expected_error: str | None,
) -> None:
    """1000 characters is accepted and 1001 is rejected."""
    body = "B" * length

    values, errors = validation.parse_comment_form(
        _comment_form(author_id=str(sample_member_id), body=body)
    )

    assert values["body"] == body
    if expected_error is None:
        assert errors == {}
    else:
        assert errors == {"body": expected_error}


def test_comment_reports_every_offending_field(app_context: None) -> None:
    """A blank body does not mask an author that names no member."""
    values, errors = validation.parse_comment_form(
        _comment_form(author_id=UNKNOWN_MEMBER_ID, body="   ")
    )

    assert errors == {
        "author_id": MSG_SELECT_COMMENT_AUTHOR,
        "body": MSG_ENTER_COMMENT,
    }
    assert values == {"author_id": None, "body": ""}


# ---------------------------------------------------------------------------
# The task-list filters. An absent parameter and an empty one both mean "this
# filter is not applied" -- the empty string is what each "All" option submits --
# while a non-empty value outside its set is reported and never applied. The
# parser reports; the 400 page is the view's answer, not this layer's.
# ---------------------------------------------------------------------------

NO_FILTERS: dict[str, object] = {"status": None, "assignee": None, "deadline": None}


def test_filters_absent_from_the_query_are_not_applied() -> None:
    """A task list with no query string shows every task."""
    values, errors = validation.parse_task_filters(MultiDict())

    assert errors == {}
    assert values == NO_FILTERS


@pytest.mark.parametrize("parameter", ["status", "assignee", "deadline"])
def test_an_empty_filter_value_is_the_same_as_an_absent_one(parameter: str) -> None:
    """The empty string each "All" option submits leaves that filter unapplied."""
    values, errors = validation.parse_task_filters(MultiDict({parameter: ""}))

    assert errors == {}
    assert values == NO_FILTERS


@pytest.mark.parametrize("status", models.TASK_STATUSES)
def test_status_filter_accepts_each_status(status: str) -> None:
    """Each of the three statuses is echoed as the filter's value."""
    values, errors = validation.parse_task_filters(MultiDict({"status": status}))

    assert errors == {}
    assert values == {**NO_FILTERS, "status": status}


def test_status_filter_rejects_a_value_outside_the_closed_set() -> None:
    """An out-of-set status is reported and leaves no status to filter on."""
    values, errors = validation.parse_task_filters(
        MultiDict({"status": "unexpected"})
    )

    assert errors == {"status": MSG_INVALID_FILTER_STATUS}
    assert values == NO_FILTERS


def test_assignee_filter_accepts_the_unassigned_word() -> None:
    """``unassigned`` selects the tasks nobody holds, and no member lookup is spent."""
    values, errors = validation.parse_task_filters(
        MultiDict({"assignee": "unassigned"})
    )

    assert errors == {}
    assert values == {**NO_FILTERS, "assignee": "unassigned"}


def test_assignee_filter_resolves_a_member_id(
    app_context: None, sample_member_id: int
) -> None:
    """A member's id arrives as the stored ``int`` id."""
    values, errors = validation.parse_task_filters(
        MultiDict({"assignee": str(sample_member_id)})
    )

    assert errors == {}
    assert values == {**NO_FILTERS, "assignee": sample_member_id}


@pytest.mark.parametrize("assignee", UNKNOWN_MEMBER_IDS)
def test_assignee_filter_rejects_an_unknown_member(
    app_context: None, assignee: str
) -> None:
    """An id that names no member is reported rather than silently dropped."""
    values, errors = validation.parse_task_filters(MultiDict({"assignee": assignee}))

    assert errors == {"assignee": MSG_INVALID_FILTER_ASSIGNEE}
    assert values == NO_FILTERS


@pytest.mark.parametrize("deadline", ["overdue", "today", "week", "none"])
def test_deadline_filter_accepts_each_bucket(deadline: str) -> None:
    """Each of the four deadline buckets is echoed as the filter's value."""
    values, errors = validation.parse_task_filters(MultiDict({"deadline": deadline}))

    assert errors == {}
    assert values == {**NO_FILTERS, "deadline": deadline}


def test_deadline_filter_rejects_a_value_outside_the_buckets() -> None:
    """A bucket the control does not offer is reported."""
    values, errors = validation.parse_task_filters(
        MultiDict({"deadline": "yesterday"})
    )

    assert errors == {"deadline": MSG_INVALID_FILTER_DEADLINE}
    assert values == NO_FILTERS


def test_every_invalid_filter_value_is_reported(app_context: None) -> None:
    """One bad value must not mask another: each offender is named."""
    values, errors = validation.parse_task_filters(
        MultiDict(
            {"status": "unexpected", "assignee": "abc", "deadline": "yesterday"}
        )
    )

    assert errors == {
        "status": MSG_INVALID_FILTER_STATUS,
        "assignee": MSG_INVALID_FILTER_ASSIGNEE,
        "deadline": MSG_INVALID_FILTER_DEADLINE,
    }
    assert values == NO_FILTERS


def test_filters_apply_together(app_context: None, sample_member_id: int) -> None:
    """All three filters in one query are each parsed and each applied."""
    values, errors = validation.parse_task_filters(
        MultiDict(
            {
                "status": "completed",
                "assignee": str(sample_member_id),
                "deadline": "week",
            }
        )
    )

    assert errors == {}
    assert values == {
        "status": "completed",
        "assignee": sample_member_id,
        "deadline": "week",
    }


# ---------------------------------------------------------------------------
# The two exported helpers the parsers and the quick actions share.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("submitted", MALFORMED_DATES)
def test_parse_iso_date_rejects_anything_but_a_calendar_date(submitted: str) -> None:
    """Only the ``YYYY-MM-DD`` calendar form parses; every other shape is ``None``."""
    assert validation.parse_iso_date(submitted) is None


def test_parse_iso_date_returns_the_normalised_form() -> None:
    """A date in the stored form comes back unchanged, ready to bind into SQL."""
    today = _iso_shift(0)

    assert validation.parse_iso_date(today) == today


def test_parse_iso_date_answers_a_non_text_value_instead_of_raising() -> None:
    """Callers hand it text, but an unusable type is a ``None`` and not an error."""
    assert validation.parse_iso_date(20261026) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("status", models.TASK_STATUSES)
def test_is_valid_status_accepts_the_closed_set(status: str) -> None:
    """Each of the three statuses is valid."""
    assert validation.is_valid_status(status) is True


@pytest.mark.parametrize(
    "status", ["", "done", "Pending", "in progress", "pending "]
)
def test_is_valid_status_rejects_anything_else(status: str) -> None:
    """A label, a padded value or any other word is not a status."""
    assert validation.is_valid_status(status) is False
