"""Tests for the task list's three filters, and for its refusal to ignore a bad value.

``GET /tasks`` offers exactly three filters -- status, assignee and deadline -- which
apply together with ``AND``. This module drives them through the application's own
HTTP surface, one case per dimension and then in combination, and it covers the two
mistakes a filter bar makes easy to ship: silently dropping a value it does not
recognise, and reporting finished work as late.

Nothing about the sample data is written down here. Every expected row set is derived
from :data:`seed.SAMPLE_DATA` -- the statuses from :data:`app.models.TASK_STATUSES`,
the assignees from the members' own ``email`` values, the deadline buckets from each
task's ``deadline_offset`` measured against ``date.today()`` -- and each member's id
is read from the seeded database through :func:`app.services.members.list_members`,
the same function the application uses. A failure here therefore means the filter and
the dataset disagree, never that a number in this file went stale.

Row exactness is asserted from the markup the list is built from: every rendered row
links its title to ``/tasks/<id>``, and nothing else on the page produces that shape,
so the set of titles behind those links *is* the set of rows and the number of links
is the number of rows. The count line beside the table is asserted against the same
derived figures, which is what makes the "N of M tasks match these filters" line a
checked sentence rather than a caption.

A filter value that is not in its control's allowed set is a malformed request, not a
field to correct, so the list answers it with HTTP 400 naming the parameter. Those
cases assert the parameter's own message, the absence of the messages belonging to the
parameters that were fine, and the absence of a rendered list -- a page that returned
400 while showing every task anyway would pass a status-code-only check.

What is deliberately not asserted: that any select submits itself when its value
changes (the application ships no script, so there is nothing to test), the exact
shape of the page, and any literal count, member id or calendar date.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, timedelta
from html import unescape

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.exceptions import BadRequest

import seed
from app import models, validation
from app.services import members
from app.services import tasks as task_service

# The task list, which is the only screen these filters reach.
TASKS_URL = "/tasks"

# The last day of the "Due this week" window: today <= deadline <= today + 6. The
# same seven-day span app.services.tasks and app.templating.deadline_state use, so a
# task is never "due this week" in one place and "due soon" in another.
THIS_WEEK_END_OFFSET = 6

# The four deadline buckets the filter bar offers, named as the filter names them.
# Spelled out rather than taken from app.validation, so a bucket the parser quietly
# stops accepting, or a fifth one it starts accepting, fails this suite instead of
# following the module.
DEADLINE_BUCKETS = ("overdue", "today", "week", "none")

# The three query parameters, in the order the filter bar shows them.
FILTER_KEYS = ("status", "assignee", "deadline")

# The word the Assignee control submits to select the rows nobody is assigned to. It
# is a word rather than an id, which is what lets the filter select ``assignee_id IS
# NULL`` without a member row to point at.
ASSIGNEE_UNASSIGNED = "unassigned"

# The heading the table partial renders instead of a table when a filtered list
# matched nothing, and the caption it renders when there is at least one row. The
# pair is what distinguishes "nothing matched" from "the page did not render a list
# at all", which is the difference between a correct 400 and a degraded one.
EMPTY_MATCH_HEADING = "No tasks match these filters."
TABLE_CAPTION = "<caption>Task list</caption>"

# The list's own controls: the filter form is the only place a filter select lives,
# and "Apply filters" appears nowhere on the 400 page. A row's own status form also
# carries a ``name="status"`` select, which is why every select is located inside the
# filter form rather than by name across the whole document.
_FILTER_FORM = re.compile(
    r'<form[^>]*class="filters"[^>]*>(?P<body>.*?)</form>', re.DOTALL
)
_OPTION = re.compile(r"<option(?P<attributes>[^>]*)>", re.DOTALL)
_OPTION_VALUE = re.compile(r'value="(?P<value>[^"]*)"')
_SELECTED = re.compile(r"\bselected\b")

# One rendered task row: the title anchor the table emits per row. No other markup on
# the page -- not the navigation, not the toolbar's Create task link, not a row's Edit
# link or status form action -- links to ``/tasks/<digits>`` and stops there, so this
# pattern counts rows and nothing else.
_ROW_LINK = re.compile(r'<a href="/tasks/(?P<task_id>\d+)">(?P<title>[^<]*)</a>')


# ---------------------------------------------------------------------------
# Deriving what each filter must return, from the dataset the application seeds.
# ---------------------------------------------------------------------------


def sample_tasks() -> list[dict]:
    """Return the tasks ``SAMPLE_DATA`` declares, in the order it inserts them."""
    return seed.SAMPLE_DATA["tasks"]


def sample_member_emails() -> list[str]:
    """Return every seeded member's email, the value a task's assignee names."""
    return [member["email"] for member in seed.SAMPLE_DATA["members"]]


def titles_by_status(status: str) -> set[str]:
    """Return the titles of the seeded tasks holding ``status``.

    Args:
        status: One of :data:`app.models.TASK_STATUSES`.

    Returns:
        The titles whose ``status`` equals ``status``, as a set so two derived
        buckets can be intersected directly.
    """
    return {task["title"] for task in sample_tasks() if task["status"] == status}


def titles_by_assignee(email: str | None) -> set[str]:
    """Return the titles assigned to ``email``, or to nobody when it is ``None``.

    Args:
        email: A seeded member's address, or ``None`` for the "unassigned" bucket --
            the rows the filter selects with ``assignee_id IS NULL``.

    Returns:
        The titles whose ``assignee`` equals ``email``.
    """
    return {task["title"] for task in sample_tasks() if task["assignee"] == email}


def _deadline_of(task: dict, today: date) -> date | None:
    """Return a seeded task's deadline as a date, resolved against ``today``.

    The dataset stores signed day offsets rather than calendar dates, and the loader
    resolves them against a single ``date.today()`` when it runs. Resolving them here
    against the same clock is what keeps the derived buckets comparable with the ones
    the request computes.
    """
    offset = task["deadline_offset"]
    if offset is None:
        return None
    return today + timedelta(days=offset)


def titles_in_deadline_bucket(bucket: str) -> set[str]:
    """Return the titles the seeded tasks put in deadline ``bucket``.

    The rules are the ones the product states, applied here to the dataset rather than
    to SQL: ``overdue`` is a deadline before today on work that is not completed,
    ``today`` is a deadline of today, ``week`` is today through today + 6 inclusive,
    and ``none`` is no deadline at all. A task due today therefore falls in both
    ``today`` and ``week``, because the control offers alternative buckets rather than
    additive categories.

    Args:
        bucket: One of :data:`DEADLINE_BUCKETS`.

    Returns:
        The titles that bucket selects.

    Raises:
        ValueError: If ``bucket`` is not one of :data:`DEADLINE_BUCKETS`, so a case
            table that grew a bucket the product does not have fails loudly.
    """
    today = date.today()
    week_end = today + timedelta(days=THIS_WEEK_END_OFFSET)

    titles: set[str] = set()
    for task in sample_tasks():
        deadline = _deadline_of(task, today)
        if bucket == "overdue":
            matches = (
                deadline is not None
                and deadline < today
                and task["status"] != "completed"
            )
        elif bucket == "today":
            matches = deadline == today
        elif bucket == "week":
            matches = deadline is not None and today <= deadline <= week_end
        elif bucket == "none":
            matches = deadline is None
        else:
            raise ValueError(f"Unknown deadline bucket: {bucket!r}")
        if matches:
            titles.add(task["title"])
    return titles


def titles_completed_and_past_due() -> set[str]:
    """Return the seeded titles that are finished work with a deadline behind them.

    These are the rows the ``overdue`` bucket must not return: finished work is not
    late. They exist in the dataset precisely so the exclusion is tested against real
    rows instead of being asserted about an empty set.
    """
    today = date.today()
    titles: set[str] = set()
    for task in sample_tasks():
        deadline = _deadline_of(task, today)
        if task["status"] == "completed" and deadline is not None and deadline < today:
            titles.add(task["title"])
    return titles


def probe_task_and_bucket() -> tuple[dict, str]:
    """Return a seeded task that falls in a deadline bucket, and that bucket.

    The filters are exercised together on one task, so the task has to be one a
    deadline bucket actually holds: a completed task whose deadline has passed belongs
    to none of them -- ``overdue`` excludes finished work and it is neither due today,
    inside the coming week nor undated -- so it could not make a combination case.

    Returns:
        The first such task in dataset order and the first bucket holding it, in the
        order :data:`DEADLINE_BUCKETS` declares.

    Raises:
        AssertionError: If no seeded task falls in a bucket, which would leave the
            combination cases with nothing to prove.
    """
    for task in sample_tasks():
        for bucket in DEADLINE_BUCKETS:
            if task["title"] in titles_in_deadline_bucket(bucket):
                return task, bucket
    raise AssertionError("no seeded task falls in a deadline bucket")


def member_ids(app: Flask) -> dict[str, int]:
    """Return each seeded member's id, keyed by the email their tasks name them with.

    Read through :func:`app.services.members.list_members` inside an application
    context, which is how the application itself reads them, so the ids this module
    submits are the ids the rows actually hold rather than a guess about the insert
    order.

    Args:
        app: The application whose database the ``seeded_client`` fixture seeded.

    Returns:
        A mapping of member email to member id.
    """
    with app.app_context():
        return {member.email: member.id for member in members.list_members()}


# ---------------------------------------------------------------------------
# Reading the rendered list.
# ---------------------------------------------------------------------------


def rendered_rows(html: str) -> list[tuple[str, str]]:
    """Return ``(task id, title)`` for every task row the list rendered.

    The title is unescaped, so a task titled with a character Jinja escapes is compared
    against the text the dataset holds rather than against its entity form.
    """
    return [
        (match.group("task_id"), unescape(match.group("title")))
        for match in _ROW_LINK.finditer(html)
    ]


def rendered_titles(html: str) -> set[str]:
    """Return the titles of the task rows the list rendered."""
    return {title for _, title in rendered_rows(html)}


def selected_filter_values(html: str, name: str) -> set[str]:
    """Return the values of the filter select ``name`` that are marked selected.

    Every select in the filter bar renders its options with a literal ``selected``
    attribute on the one that is current, so the marked value is read from that
    attribute however the template spells it. Exactly one option per select is
    expected to carry it, which is why the answer is a set: a select that marks two
    options, or none, is a defect the assertion upstream will catch.

    Args:
        html: The rendered task list.
        name: The control's ``name`` -- ``status``, ``assignee`` or ``deadline``.

    Returns:
        The ``value`` of each option carrying ``selected``, with ``""`` for the "All"
        option.

    Raises:
        AssertionError: If the page renders no filter form, or no select with that
            name inside it, in which case the control cannot be showing any state.
    """
    form = _FILTER_FORM.search(html)
    assert form is not None, "the task list rendered no filter form"

    select = re.search(
        rf'<select[^>]*\bname="{re.escape(name)}"[^>]*>(?P<options>.*?)</select>',
        form.group("body"),
        re.DOTALL,
    )
    assert select is not None, f"the filter bar has no {name!r} select"

    selected: set[str] = set()
    for attributes in _OPTION.findall(select.group("options")):
        value = _OPTION_VALUE.search(attributes)
        if _SELECTED.search(attributes):
            selected.add(value.group("value") if value is not None else "")
    return selected


def expected_result_line(matches: int, total: int, *, filtered: bool) -> str:
    """Return the sentence the list must show beside its table.

    Args:
        matches: How many tasks the filters selected.
        total: How many tasks the board holds.
        filtered: Whether at least one filter is applied. The line reads differently
            in the two cases, and a filter applied but empty must still say how many
            of the whole board it matched.

    Returns:
        The count line as the template renders it, with no surrounding markup.
    """
    if filtered:
        return f"{matches} of {total} tasks match these filters"
    return "1 task" if total == 1 else f"{total} tasks"


def _get_list(client: FlaskClient, params: Mapping[str, str] | None = None) -> str:
    """Return the task list rendered for a request carrying ``params``.

    A filtered list is a 200 whatever it matched -- an empty result is a state of the
    page, not a failure -- so the status is checked here once for every case that
    expects a list.
    """
    response = client.get(TASKS_URL, query_string=params or {})
    assert response.status_code == 200, (
        f"GET {TASKS_URL} with {params} answered {response.status_code}"
    )
    return response.get_data(as_text=True)


def _assert_filtered_list(
    client: FlaskClient, params: Mapping[str, str], expected: set[str]
) -> str:
    """Assert a filtered list shows exactly ``expected``, and the derived count line.

    Args:
        client: The seeded test client.
        params: The query string to submit, as the filter form would.
        expected: The titles the derived bucket holds, which may legitimately be empty.

    Returns:
        The rendered page, for the caller's own assertions about its markup.
    """
    total = len(sample_tasks())
    html = _get_list(client, params)

    assert rendered_titles(html) == expected, "the filtered list rendered other rows"
    assert len(rendered_rows(html)) == len(expected), "one row per matching task"
    assert f"<p>{expected_result_line(len(expected), total, filtered=True)}</p>" in html

    if expected:
        assert TABLE_CAPTION in html
        assert EMPTY_MATCH_HEADING not in html
    else:
        # A filter that matched nothing is a different situation from a board with no
        # tasks at all, and the list answers it in its own words rather than by
        # rendering an empty table.
        assert EMPTY_MATCH_HEADING in html
        assert "Clear filters" in html
        assert TABLE_CAPTION not in html
    return html


def _assert_whole_list(client: FlaskClient, params: Mapping[str, str]) -> str:
    """Assert the request applied no filter: every task, and no control marked."""
    total = len(sample_tasks())
    html = _get_list(client, params)

    assert rendered_titles(html) == {task["title"] for task in sample_tasks()}
    assert len(rendered_rows(html)) == total
    assert f"<p>{expected_result_line(total, total, filtered=False)}</p>" in html
    for name in FILTER_KEYS:
        assert selected_filter_values(html, name) == {""}
    return html


# The message each filter parameter is rejected with, keyed by the parameter name. The
# message names the parameter itself, and the 400 page's description prefixes it with
# the parameter again, so the two together are what "names the offending parameter"
# means on the rendered page.
FILTER_MESSAGES: dict[str, str] = {
    "status": validation.MSG_INVALID_FILTER_STATUS,
    "assignee": validation.MSG_INVALID_FILTER_ASSIGNEE,
    "deadline": validation.MSG_INVALID_FILTER_DEADLINE,
}


def _assert_rejected(
    client: FlaskClient, params: Mapping[str, str], named: set[str]
) -> str:
    """Assert the request was answered with the 400 page naming ``named``.

    Args:
        client: The seeded test client.
        params: The malformed query string.
        named: The parameters whose message the page must carry. Every other
            parameter's message must be absent, which is how a valid value beside an
            invalid one is proved not to be reported as a second mistake.

    Returns:
        The rendered 400 page.
    """
    response = client.get(TASKS_URL, query_string=params)
    html = response.get_data(as_text=True)

    assert response.status_code == 400, f"{params} was not rejected"
    # The page's heading is built from the exception it was raised with, so it is
    # checked against the same name and code rather than a copied phrase. The visible
    # text is read unescaped, because the description quotes the parameter's name in
    # single quotes and Jinja escapes those in the markup.
    page_text = unescape(html)
    bad_request = BadRequest()
    assert f"{bad_request.name} ({bad_request.code})" in page_text

    for field, message in FILTER_MESSAGES.items():
        if field in named:
            assert f"{field}: {message}" in page_text, f"page did not name {field!r}"
        else:
            assert message not in page_text, f"the page reported {field!r} as well"

    # Inside the application shell, and never a list: a 400 that rendered the rows
    # anyway would have ignored the value it had just refused.
    assert "Back to the dashboard" in page_text
    assert "Apply filters" not in page_text
    assert rendered_rows(html) == []
    return html


# ---------------------------------------------------------------------------
# Each filter on its own.
# ---------------------------------------------------------------------------

# The assignee filter's cases: the "Unassigned" word first, then one case per seeded
# member. Named by the address's local part, so a failure identifies the member without
# turning a full address into a test id.
ASSIGNEE_FILTER_CASES = [
    pytest.param(None, id="unassigned"),
    *[
        pytest.param(email, id=email.split("@")[0])
        for email in sample_member_emails()
    ],
]


@pytest.mark.parametrize("status", models.TASK_STATUSES)
def test_status_filter_returns_exactly_that_statuses_tasks(
    seeded_client: FlaskClient, status: str
) -> None:
    """One case per status, against the rows the dataset gives that status."""
    _assert_filtered_list(seeded_client, {"status": status}, titles_by_status(status))


@pytest.mark.parametrize("email", ASSIGNEE_FILTER_CASES)
def test_assignee_filter_returns_exactly_that_members_tasks(
    app: Flask, seeded_client: FlaskClient, email: str | None
) -> None:
    """One case per member, plus the unassigned bucket that selects ``NULL``.

    The unassigned case is the one that proves the filter's ``IS NULL`` path: a
    comparison against ``NULL`` matches nothing, so a filter that bound the word as an
    id would return no rows at all and this case would fail.
    """
    if email is None:
        params = {"assignee": ASSIGNEE_UNASSIGNED}
    else:
        params = {"assignee": str(member_ids(app)[email])}

    expected = titles_by_assignee(email)
    html = _assert_filtered_list(seeded_client, params, expected)

    if email is None and expected:
        assert "<td>Unassigned</td>" in html, "an unassigned row names no one"


@pytest.mark.parametrize("bucket", DEADLINE_BUCKETS)
def test_deadline_filter_returns_exactly_that_bucket(
    seeded_client: FlaskClient, bucket: str
) -> None:
    """One case per deadline bucket, including the one that selects no deadline."""
    _assert_filtered_list(
        seeded_client, {"deadline": bucket}, titles_in_deadline_bucket(bucket)
    )


def test_overdue_excludes_finished_work(seeded_client: FlaskClient) -> None:
    """Work that is finished is not late, so the overdue bucket leaves it out.

    The dataset holds completed tasks whose deadline has passed, so this is asserted
    about real rows; a run against a dataset without one skips rather than passing on
    an empty set.
    """
    completed_past_due = titles_completed_and_past_due()
    if not completed_past_due:
        pytest.skip("SAMPLE_DATA holds no completed task whose deadline has passed")

    overdue = titles_in_deadline_bucket("overdue")
    assert overdue.isdisjoint(completed_past_due)

    html = _assert_filtered_list(seeded_client, {"deadline": "overdue"}, overdue)
    assert completed_past_due.isdisjoint(rendered_titles(html))


def test_the_filter_vocabularies_are_the_documented_ones() -> None:
    """The values these filters accept are the closed sets the product defines."""
    assert validation.STATUS_FILTERS == models.TASK_STATUSES
    assert validation.DEADLINE_FILTERS == DEADLINE_BUCKETS
    assert validation.ASSIGNEE_UNASSIGNED == ASSIGNEE_UNASSIGNED


def test_the_seeded_dataset_makes_every_case_meaningful() -> None:
    """Every bucket asserted above holds rows, and no two tasks share a title.

    The first half keeps the parameterized cases from passing vacuously; the second is
    what lets a set of titles stand for the set of rows.
    """
    titles = [task["title"] for task in sample_tasks()]
    assert len(titles) == len(set(titles))

    assert all(titles_by_status(status) for status in models.TASK_STATUSES)
    assert titles_by_assignee(None)
    assert any(titles_by_assignee(email) for email in sample_member_emails())
    assert all(titles_in_deadline_bucket(bucket) for bucket in DEADLINE_BUCKETS)
    assert titles_completed_and_past_due()


# ---------------------------------------------------------------------------
# Filters in combination, and the two states a list can be in.
# ---------------------------------------------------------------------------

# Every status with every member: the pairs that share rows and the pairs that do not,
# so the intersection case covers both a populated answer and an empty one.
STATUS_AND_MEMBER_CASES = [
    pytest.param(status, email, id=f"{status}-{email.split('@')[0]}")
    for status in models.TASK_STATUSES
    for email in sample_member_emails()
]


@pytest.mark.parametrize(("status", "email"), STATUS_AND_MEMBER_CASES)
def test_status_and_assignee_together_return_their_intersection(
    app: Flask, seeded_client: FlaskClient, status: str, email: str
) -> None:
    """Two filters apply together with AND, so the rows are the overlap of the two."""
    params = {"status": status, "assignee": str(member_ids(app)[email])}
    expected = titles_by_status(status) & titles_by_assignee(email)

    _assert_filtered_list(seeded_client, params, expected)


def test_three_filters_together_return_their_intersection(
    app: Flask, seeded_client: FlaskClient
) -> None:
    """All three filters at once select the intersection of the three buckets.

    The three values are taken from one seeded task -- its status, its assignee and
    the deadline bucket it falls in -- so the intersection provably holds that task
    and the case is not vacuous, while the expectation itself is still derived from
    the dataset rather than from the task that picked the values.
    """
    probe, bucket = probe_task_and_bucket()

    assignee = probe["assignee"]
    params = {
        "status": probe["status"],
        "assignee": (
            ASSIGNEE_UNASSIGNED if assignee is None else str(member_ids(app)[assignee])
        ),
        "deadline": bucket,
    }
    expected = (
        titles_by_status(probe["status"])
        & titles_by_assignee(assignee)
        & titles_in_deadline_bucket(bucket)
    )
    assert probe["title"] in expected

    _assert_filtered_list(seeded_client, params, expected)


def test_a_combination_that_cannot_match_shows_the_no_matches_state(
    seeded_client: FlaskClient,
) -> None:
    """A filtered list that matches nothing is its own state, not an empty table.

    ``status=completed`` with ``deadline=overdue`` is empty by definition -- finished
    work is not late -- so the empty state is asserted without depending on the day or
    on the dataset holding any particular row.
    """
    expected = titles_by_status("completed") & titles_in_deadline_bucket("overdue")
    assert expected == set()

    _assert_filtered_list(
        seeded_client, {"status": "completed", "deadline": "overdue"}, expected
    )


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="absent"),
        pytest.param(
            {"status": "", "assignee": "", "deadline": ""}, id="empty-strings"
        ),
    ],
)
def test_a_filter_that_is_not_applied_returns_every_task(
    seeded_client: FlaskClient, params: dict[str, str]
) -> None:
    """Return the whole board when no filter is applied.

    An absent parameter and the empty string the "All" option submits both mean "no
    filter", so the request returns every task and the unfiltered count line.
    """
    _assert_whole_list(seeded_client, params)


# ---------------------------------------------------------------------------
# Invalid values are rejected, never ignored.
# ---------------------------------------------------------------------------

# Malformed query strings, and the parameters each one must be reported against. The
# fourth case proves a valid value beside an invalid one is not reported as a mistake,
# and the fifth that two mistakes are both described.
INVALID_FILTER_CASES = [
    pytest.param({"status": "unexpected"}, {"status"}, id="status-out-of-set"),
    pytest.param({"deadline": "yesterday"}, {"deadline"}, id="deadline-out-of-set"),
    pytest.param({"assignee": "abc"}, {"assignee"}, id="assignee-not-a-number"),
    pytest.param(
        {"status": "pending", "deadline": "yesterday"},
        {"deadline"},
        id="valid-status-beside-invalid-deadline",
    ),
    pytest.param(
        {"status": "unexpected", "deadline": "yesterday"},
        {"status", "deadline"},
        id="two-values-out-of-set",
    ),
]


@pytest.mark.parametrize(("params", "named"), INVALID_FILTER_CASES)
def test_an_invalid_filter_value_is_rejected_naming_its_parameter(
    seeded_client: FlaskClient, params: dict[str, str], named: set[str]
) -> None:
    """A value outside its control's set is a malformed request, answered with 400."""
    _assert_rejected(seeded_client, params, named)


def test_an_assignee_naming_no_member_is_rejected(
    app: Flask, seeded_client: FlaskClient
) -> None:
    """A well-formed id that names nobody is as invalid as a word that is not an id.

    The id is derived -- one past the highest the seeded rows hold -- so no particular
    member id is written down here.
    """
    unknown = max(member_ids(app).values()) + 1

    _assert_rejected(seeded_client, {"assignee": str(unknown)}, {"assignee"})


def test_the_query_refuses_a_deadline_bucket_outside_the_four(app: Flask) -> None:
    """An unrecognised bucket raises instead of returning the unfiltered table.

    The HTTP surface cannot reach this: ``parse_task_filters`` answers 400 for the
    same value before the list is built, which the cases above assert. The guard is
    asserted directly because its purpose is the caller that does not exist yet --
    with no final branch an unknown bucket would append no clause and quietly widen
    the statement into the unfiltered full-table read, the failure the status filter
    cannot have.
    """
    with app.app_context(), pytest.raises(ValueError):
        task_service.list_tasks({"deadline": "yesterday"})


# ---------------------------------------------------------------------------
# The controls keep the applied filters.
# ---------------------------------------------------------------------------

# One case per filter that needs no derived id: the control's name and a value it
# offers. The member-id case needs the seeded rows and has its own test below.
SELECTED_FILTER_CASES = [
    pytest.param("status", "pending", id="status-pending"),
    pytest.param("status", "completed", id="status-completed"),
    pytest.param("assignee", ASSIGNEE_UNASSIGNED, id="assignee-unassigned"),
    pytest.param("deadline", "overdue", id="deadline-overdue"),
    pytest.param("deadline", "none", id="deadline-none"),
]


@pytest.mark.parametrize(("name", "value"), SELECTED_FILTER_CASES)
def test_an_applied_filter_stays_selected_in_its_own_control(
    seeded_client: FlaskClient, name: str, value: str
) -> None:
    """The control shows the value that was applied, and the other two show "All".

    Exactly one option per select carries the mark, so a select that marked two
    options -- or none -- fails here rather than looking plausible.
    """
    html = _get_list(seeded_client, {name: value})

    for control in FILTER_KEYS:
        expected = {value} if control == name else {""}
        assert selected_filter_values(html, control) == expected


@pytest.mark.parametrize("email", sample_member_emails())
def test_a_member_assignee_filter_stays_selected(
    app: Flask, seeded_client: FlaskClient, email: str
) -> None:
    """The Assignee control marks the member whose id was submitted."""
    member_id = member_ids(app)[email]

    html = _get_list(seeded_client, {"assignee": str(member_id)})

    assert selected_filter_values(html, "assignee") == {str(member_id)}
    assert selected_filter_values(html, "status") == {""}
    assert selected_filter_values(html, "deadline") == {""}


def test_every_applied_filter_stays_selected_together(
    app: Flask, seeded_client: FlaskClient
) -> None:
    """All three controls show their applied value at once, not only the last one."""
    probe, bucket = probe_task_and_bucket()
    assignee = probe["assignee"]
    submitted = {
        "status": probe["status"],
        "assignee": (
            ASSIGNEE_UNASSIGNED if assignee is None else str(member_ids(app)[assignee])
        ),
        "deadline": bucket,
    }

    html = _get_list(seeded_client, submitted)

    for control, value in submitted.items():
        assert selected_filter_values(html, control) == {value}
