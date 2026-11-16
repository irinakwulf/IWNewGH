"""Tests for the task list's three filters, and for its refusal to ignore a bad value.

``GET /tasks`` offers exactly three filters -- status, assignee and deadline -- which
apply together with ``AND``. This module drives them through the application's own
HTTP surface, one case per dimension and then in combination, and it covers the two
mistakes a filter bar makes easy to ship: silently dropping a value it does not
recognise, and reporting finished work as late.

Nothing about the sample data is written down here. Every expected row set is derived
from :data:`seed.SAMPLE_DATA` or from the rows the seed wrote -- the statuses from
:data:`app.models.TASK_STATUSES`, the assignees from the members' own ``email``
values, the deadline buckets from the deadlines the seed stored, compared against the
same single reading of ``date.today()`` the request itself evaluates them with -- and
each member's id is read from the seeded database through
:func:`app.services.members.list_members`, the same function the application uses. A
failure here therefore means the filter and the dataset disagree, never that a number
in this file went stale.

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

What the filter cases deliberately do not assert: that any select submits itself when
its value changes (the application ships no script, so there is nothing to test), the
exact shape of the page, and any literal count, member id or calendar date drawn from
the dataset -- no filter expectation holds one, because every such value is derived
from :data:`seed.SAMPLE_DATA` or from the seeded rows. The direct cases for the four
Jinja filters, at the end of this module, are the one deliberate exception: they pin the
exact text each input renders as, because they assert a format rather than a dataset
figure.

This module also carries the direct unit cases for the four Jinja filters in
:mod:`app.templating` -- ``format_date``, ``format_datetime``, ``status_label`` and
``deadline_state`` -- because those filters are pure functions whose template call
sites guard every input they receive.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from html import unescape

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.exceptions import BadRequest

import seed
from app import db, models, templating, validation
from app.services import members
from app.services import tasks as task_service

# The task list, which is the only screen these filters reach.
TASKS_URL = "/tasks"

# The last day of the "Due this week" window: today <= deadline <= today + 6. The
# same seven-day span app.services.tasks and app.templating.deadline_state use, so a
# task is never "due this week" in one place and "due soon" in another.
THIS_WEEK_END_OFFSET = 6

# The three statuses the status filter offers, in the order the model, the schema's
# CHECK constraint and the filter bar list them. Spelled out rather than taken from
# app.models, for the same reason as DEADLINE_BUCKETS below: a status the parser
# quietly stops accepting, or a fourth one the model grows, fails this suite instead
# of following the module.
STATUS_FILTERS = ("pending", "in_progress", "completed")

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

# The filter bar's two controls, each matched inside the form body: the submit button
# that sends the three selects, and the link back to the unfiltered list. Both are
# matched on their role and their target as well as their wording, because every case
# below requests the filtered URL directly instead of pressing either one -- so a
# button that lost its ``type``, or a Clear link pointing anywhere but the whole board,
# would otherwise go unnoticed.
_APPLY_CONTROL = re.compile(r'<button[^>]*\btype="submit"[^>]*>Apply filters</button>')
_CLEAR_CONTROL = re.compile(
    rf'<a[^>]*\bhref="{re.escape(TASKS_URL)}"[^>]*>Clear filters</a>'
)

# One rendered task row: the title anchor the table emits per row. No other markup on
# the page -- not the navigation, not the toolbar's Create task link, not a row's Edit
# link or status form action -- links to ``/tasks/<digits>`` and stops there, so this
# pattern counts rows and nothing else.
_ROW_LINK = re.compile(r'<a href="/tasks/(?P<task_id>\d+)">(?P<title>[^<]*)</a>')

# One rendered deadline indicator: the ``.deadline`` span the table emits inside a
# row's deadline cell. The filter bar's own "Overdue" and "Due today" option labels are
# bare text inside ``<option>`` elements and carry no such span, so this reads the
# indicators the rows show and nothing the controls say.
_DEADLINE_CELL = re.compile(
    r'<span class="deadline[^"]*">(?P<text>.*?)</span>', re.DOTALL
)


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


def stored_tasks(app: Flask) -> list[sqlite3.Row]:
    """Return the seeded task rows the deadline expectations are measured from.

    The rows are read straight out of the seeded database inside an application
    context, so the expectation depends on no filter logic of its own: the
    deadlines the tasks actually carry are what it compares, exactly as the
    application's own query will.

    Args:
        app: The application whose database the ``seeded_client`` fixture seeded.

    Returns:
        One row per seeded task, carrying ``title``, ``status``, ``deadline`` and
        ``assignee_id``, in the order the loader inserted them.
    """
    with app.app_context():
        return db.get_db().execute(
            "SELECT title, status, deadline, assignee_id FROM task ORDER BY id"
        ).fetchall()


def _in_deadline_bucket(row: sqlite3.Row, bucket: str, today: date) -> bool:
    """Return whether a stored task row falls in deadline ``bucket`` on ``today``.

    The rules are the product's own, restated over a stored row: ``overdue`` is a
    deadline before today on work that is not completed, ``today`` is a deadline of
    today, ``week`` is today through today + 6 inclusive, and ``none`` is no
    deadline at all. A task due today therefore falls in both ``today`` and
    ``week``, because the control offers alternative buckets rather than additive
    categories.

    Args:
        row: One task row, as :func:`stored_tasks` returns them.
        bucket: One of :data:`DEADLINE_BUCKETS`.
        today: The single day the whole evaluation is measured from.

    Returns:
        ``True`` when that bucket selects the row.

    Raises:
        ValueError: If ``bucket`` is not one of :data:`DEADLINE_BUCKETS`, so a case
            table that grew a bucket the product does not have fails loudly.
    """
    stored = row["deadline"]
    deadline = date.fromisoformat(stored) if stored is not None else None
    if bucket == "overdue":
        return (
            deadline is not None and deadline < today and row["status"] != "completed"
        )
    if bucket == "today":
        return deadline == today
    if bucket == "week":
        return (
            deadline is not None
            and today <= deadline <= today + timedelta(days=THIS_WEEK_END_OFFSET)
        )
    if bucket == "none":
        return deadline is None
    raise ValueError(f"Unknown deadline bucket: {bucket!r}")


def titles_in_deadline_bucket(bucket: str, app: Flask) -> set[str]:
    """Return the titles the application puts in deadline ``bucket``.

    The application evaluates every stored deadline against ``date.today()`` while
    it serves the request, so the expectation reads that day once and applies the
    product's rule to those same stored deadlines. Deriving the buckets from the
    day the seed ran instead would fail after midnight, because the request itself
    evaluates the new day.

    Args:
        bucket: One of :data:`DEADLINE_BUCKETS`.
        app: The application whose database the ``seeded_client`` fixture seeded.

    Returns:
        The titles that bucket selects.

    Raises:
        ValueError: If ``bucket`` is not one of :data:`DEADLINE_BUCKETS`, so a case
            table that grew a bucket the product does not have fails loudly.
    """
    today = date.today()
    return {
        row["title"]
        for row in stored_tasks(app)
        if _in_deadline_bucket(row, bucket, today)
    }


def titles_completed_and_past_due() -> set[str]:
    """Return the seeded titles that are finished work with a deadline behind them.

    These are the rows the ``overdue`` bucket must not return: finished work is not
    late. They exist in the dataset precisely so the exclusion is tested against real
    rows instead of being asserted about an empty set. The offsets are the dataset's
    own, and a negative one stays in the past however long afterwards it is read, so
    this needs no calendar date of its own.
    """
    return {
        task["title"]
        for task in sample_tasks()
        if task["status"] == "completed"
        and task["deadline_offset"] is not None
        and task["deadline_offset"] < 0
    }


def dataset_titles_in_deadline_bucket(bucket: str) -> set[str]:
    """Return the titles ``SAMPLE_DATA`` declares for deadline ``bucket``.

    The coverage guards assert what the dataset itself holds, so this reads the
    declared offsets in pure offset space rather than any stored deadline: negative
    is the past, zero is the day the loader runs, and the week window is zero
    through the sixth day ahead. No calendar date is involved, so the guards built
    on it hold however late they run.

    Args:
        bucket: One of :data:`DEADLINE_BUCKETS`.

    Returns:
        The titles whose declared offset the bucket covers.

    Raises:
        ValueError: If ``bucket`` is not one of :data:`DEADLINE_BUCKETS`.
    """
    titles: set[str] = set()
    for task in sample_tasks():
        offset = task["deadline_offset"]
        is_past = offset is not None and offset < 0
        if bucket == "overdue":
            matches = is_past and task["status"] != "completed"
        elif bucket == "today":
            matches = offset == 0
        elif bucket == "week":
            matches = offset is not None and 0 <= offset <= THIS_WEEK_END_OFFSET
        elif bucket == "none":
            matches = offset is None
        else:
            raise ValueError(f"Unknown deadline bucket: {bucket!r}")
        if matches:
            titles.add(task["title"])
    return titles


def probe_task_and_bucket(app: Flask) -> tuple[sqlite3.Row, str]:
    """Return a seeded task row that falls in a deadline bucket, and that bucket.

    The filters are exercised together on one task, so the task has to be one a
    deadline bucket actually holds: a completed task whose deadline has passed belongs
    to none of them -- ``overdue`` excludes finished work and it is neither due today,
    inside the coming week nor undated -- so it could not make a combination case. The
    row and the rule that selects it are the seeded rows and the product's own rule,
    measured from one reading of today, so the probe is a row the application's own
    filter agrees is in the bucket it names.

    Args:
        app: The application whose database the ``seeded_client`` fixture seeded.

    Returns:
        The first such task in dataset order and the first bucket holding it, in the
        order :data:`DEADLINE_BUCKETS` declares.

    Raises:
        AssertionError: If no seeded task falls in a bucket, which would leave the
            combination cases with nothing to prove.
    """
    today = date.today()
    for row in stored_tasks(app):
        for bucket in DEADLINE_BUCKETS:
            if _in_deadline_bucket(row, bucket, today):
                return row, bucket
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


def rendered_deadline_cells(html: str) -> list[str]:
    """Return the text of every deadline indicator the task rows rendered.

    The words "Overdue" and "Due today" are also the filter bar's own option labels, so
    a page-wide search for them would answer for the controls rather than for the rows.
    Reading the ``.deadline`` span each row carries keeps the check about what the
    deadline column says, and the text is unescaped and stripped so it compares with the
    indicator's own words.

    Args:
        html: The rendered task list.

    Returns:
        One string per rendered position of a deadline cell, in row order.
    """
    return [
        unescape(match.group("text")).strip()
        for match in _DEADLINE_CELL.finditer(html)
    ]


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


def assert_filter_controls(html: str) -> None:
    """Assert the filter bar rendered both of the controls it exists to offer.

    The bar's Apply button is the only thing that submits the three selects, and its
    Clear link is the only route back to the whole board. Neither is proved by a page
    rendering, and no case in this module presses either one -- each submits its query
    string directly -- so without this check a bar whose Apply button was deleted, or
    whose Clear link pointed somewhere else, would answer every case green while a
    reviewer could no longer filter the list at all.

    Args:
        html: A rendered task list, filtered or not.

    Raises:
        AssertionError: If the page renders no filter form, or if the form body holds no
            submit button reading "Apply filters", or no link reading "Clear filters"
            whose ``href`` is the unfiltered list.
    """
    form = _FILTER_FORM.search(html)
    assert form is not None, "the task list rendered no filter form"

    body = form.group("body")
    assert _APPLY_CONTROL.search(body), (
        "the filter form has no submit button reading 'Apply filters'"
    )
    assert _CLEAR_CONTROL.search(body), (
        f"the filter form has no 'Clear filters' link to {TASKS_URL}"
    )


def expected_result_line(matches: int, total: int, *, filtered: bool) -> str:
    """Return the sentence the list must show beside its table.

    Args:
        matches: How many tasks the filters selected.
        total: How many tasks the board holds.
        filtered: Whether at least one filter is applied. The line reads differently
            in the two cases, and a filter applied to a board that holds tasks must
            still say how many of the whole board it matched. A filter applied to a
            board with no tasks at all matches no "of" line, because there is nothing
            to be a fraction of.

    Returns:
        The count line as the template renders it, with no surrounding markup.
    """
    if filtered and total:
        return (
            f"{matches} of {total} task{'' if total == 1 else 's'}"
            f" match{'es' if matches == 1 else ''} these filters"
        )
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
    assert_filter_controls(html)

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
    assert_filter_controls(html)
    for name in FILTER_KEYS:
        assert selected_filter_values(html, name) == {""}
    return html


# The message each filter parameter is rejected with, keyed by the parameter name. The
# wording is pinned here rather than taken from app.validation -- the module under test
# -- so a reworded filter message fails in this module too, where the rendered 400 page
# is read; tests/test_validation.py pins the same three sentences for its own
# assertions. Each message names its parameter, and the 400 page's description prefixes
# it with the parameter again, so the two together are what "names the offending
# parameter" means on the rendered page.
FILTER_MESSAGES: dict[str, str] = {
    "status": "Invalid value for filter 'status'.",
    "assignee": "Invalid value for filter 'assignee'.",
    "deadline": "Invalid value for filter 'deadline'.",
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
    app: Flask, seeded_client: FlaskClient, bucket: str
) -> None:
    """One case per deadline bucket, including the one that selects no deadline."""
    _assert_filtered_list(
        seeded_client, {"deadline": bucket}, titles_in_deadline_bucket(bucket, app)
    )


def test_overdue_excludes_finished_work(app: Flask, seeded_client: FlaskClient) -> None:
    """Work that is finished is not late, so the overdue bucket leaves it out.

    The dataset holds completed tasks whose deadline has passed, so this is asserted
    about real rows; a run against a dataset without one fails here rather than passing
    on an empty set.
    """
    completed_past_due = titles_completed_and_past_due()
    assert completed_past_due, (
        "SAMPLE_DATA holds no completed task whose deadline has passed"
    )

    overdue = titles_in_deadline_bucket("overdue", app)
    assert overdue.isdisjoint(completed_past_due)

    html = _assert_filtered_list(seeded_client, {"deadline": "overdue"}, overdue)
    assert completed_past_due.isdisjoint(rendered_titles(html))


def test_the_deadline_filter_and_the_indicator_measure_one_reference_day(
    app: Flask, seeded_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket a row is selected by and the word beside its deadline share one day.

    The interface states that ``today`` is evaluated once per request, so the day the
    deadline filter selects its rows with is the day the deadline indicator beside each
    row is measured against: a request served across midnight cannot select a row as due
    today and then label it "Overdue". The patched clock hands its first reader the
    current day and every later reader the day after, and it records each reading, so a
    second reading taken anywhere in the request -- by the filter or by the indicator --
    shows up either as a second recorded read or as a row whose word contradicts the
    bucket that selected it.
    """
    reads: list[date] = []

    class SteppingDate(date):
        """A ``date`` whose ``today`` steps one day on after its first reading."""

        @classmethod
        def today(cls) -> date:
            """Return today on the first reading and the day after on any later one."""
            reads.append(date.today())
            if len(reads) == 1:
                return reads[-1]
            return reads[-1] + timedelta(days=1)

    class Clock:
        """The parts of the ``datetime`` module the deadline helper reads."""

        date = SteppingDate
        datetime = datetime
        timedelta = timedelta

    monkeypatch.setattr(templating, "datetime", Clock)
    # The filter's own reading is patched with the same stepping date. The reading this
    # case rules out is the filter taking one of its own, so a second reading has to be
    # both visible and one day ahead whichever of the two readers takes it.
    monkeypatch.setattr(task_service, "date", SteppingDate)

    html = _get_list(seeded_client, {"deadline": "today"})

    assert len(reads) == 1, (
        "the request read today more than once, so its deadline filter and its "
        "deadline indicator can be measured from different days"
    )
    measured_from = reads[0]
    expected = {
        row["title"]
        for row in stored_tasks(app)
        if _in_deadline_bucket(row, "today", measured_from)
    }
    assert expected, "the seeded dataset holds no task due today"

    assert rendered_titles(html) == expected, "the filtered list rendered other rows"
    total = len(sample_tasks())
    assert f"<p>{expected_result_line(len(expected), total, filtered=True)}</p>" in html

    cells = rendered_deadline_cells(html)
    assert len(cells) == len(expected), "one deadline cell per matching row"
    for text in cells:
        assert text.endswith("Due today"), f"a due-today row reads {text!r}"
        assert "Overdue" not in text, f"a due-today row reads {text!r}"


def test_the_filter_vocabularies_are_the_documented_ones() -> None:
    """The values these filters accept are the closed sets the product defines.

    Each set is compared with a literal spelled out in this module -- the model's
    statuses and the parser's filter vocabularies would otherwise only be compared
    with one another, which the parser's own assignment makes true by construction.
    """
    assert models.TASK_STATUSES == STATUS_FILTERS
    assert validation.STATUS_FILTERS == STATUS_FILTERS
    assert validation.DEADLINE_FILTERS == DEADLINE_BUCKETS
    assert validation.ASSIGNEE_UNASSIGNED == "unassigned"


def test_the_seeded_dataset_makes_every_case_meaningful() -> None:
    """Every bucket asserted above holds rows, and no two tasks share a title.

    The first half keeps the parameterized cases from passing vacuously; the second is
    what lets a set of titles stand for the set of rows. The deadline buckets are read
    from the offsets ``SAMPLE_DATA`` declares -- the dataset's own intent, which needs
    no calendar date -- so the guard holds however late it runs.
    """
    titles = [task["title"] for task in sample_tasks()]
    assert len(titles) == len(set(titles))

    assert all(titles_by_status(status) for status in models.TASK_STATUSES)
    assert titles_by_assignee(None)
    assert any(titles_by_assignee(email) for email in sample_member_emails())
    assert all(dataset_titles_in_deadline_bucket(bucket) for bucket in DEADLINE_BUCKETS)
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
    probe, bucket = probe_task_and_bucket(app)

    assignee_id = probe["assignee_id"]
    email_by_id = {member_id: email for email, member_id in member_ids(app).items()}
    if assignee_id is None:
        assignee = None
    else:
        assert assignee_id in email_by_id, "a seeded task names a member that is absent"
        assignee = email_by_id[assignee_id]

    params = {
        "status": probe["status"],
        "assignee": ASSIGNEE_UNASSIGNED if assignee_id is None else str(assignee_id),
        "deadline": bucket,
    }
    expected = (
        titles_by_status(probe["status"])
        & titles_by_assignee(assignee)
        & titles_in_deadline_bucket(bucket, app)
    )
    assert probe["title"] in expected

    _assert_filtered_list(seeded_client, params, expected)


def test_a_combination_that_cannot_match_shows_the_no_matches_state(
    app: Flask, seeded_client: FlaskClient
) -> None:
    """A filtered list that matches nothing is its own state, not an empty table.

    ``status=completed`` with ``deadline=overdue`` is empty by definition -- finished
    work is not late -- so the empty state is asserted without depending on the day or
    on the dataset holding any particular row.
    """
    expected = titles_by_status("completed") & titles_in_deadline_bucket(
        "overdue", app
    )
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


# Assignee values no stored member id could occupy: one past the range SQLite's
# signed 64-bit key holds, and one long enough that converting it to an integer would
# raise rather than produce an id to look up. Both are malformed in the same way, so
# both take the same 400.
OVER_RANGE_ASSIGNEES = (
    pytest.param("9" * 21, id="past-the-signed-64-bit-range"),
    pytest.param("9" * 4301, id="past-the-integer-digit-limit"),
)


@pytest.mark.parametrize("assignee", OVER_RANGE_ASSIGNEES)
def test_an_assignee_outside_the_id_range_is_rejected(
    seeded_client: FlaskClient, assignee: str
) -> None:
    """An id bigger than the key column is a bad value, not a server error.

    The value cannot be stored in a member's key, so no lookup can resolve it and the
    list answers it with the same 400 that names the parameter.
    """
    _assert_rejected(seeded_client, {"assignee": assignee}, {"assignee"})


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
    probe, bucket = probe_task_and_bucket(app)
    assignee_id = probe["assignee_id"]
    submitted = {
        "status": probe["status"],
        "assignee": ASSIGNEE_UNASSIGNED if assignee_id is None else str(assignee_id),
        "deadline": bucket,
    }

    html = _get_list(seeded_client, submitted)

    for control, value in submitted.items():
        assert selected_filter_values(html, control) == {value}


# ---------------------------------------------------------------------------
# The four Jinja filters, exercised directly.
#
# These cases are direct because the filters are pure functions of
# :mod:`app.templating` whose template call sites guard every input: a task without a
# deadline reaches no date filter, a row without a timestamp renders no cell, and a
# status always arrives from the closed set. Their defensive paths -- no value, blank
# text, an unparseable value, a ``date`` or ``datetime`` object where text was
# expected -- are therefore unreachable through HTTP, so nothing at that level could
# assert them. What they must render is the format the interface states: ``26 Oct
# 2026`` for a date, ``26 Oct 2026, 14:03`` for a timestamp, ``Pending`` / ``In
# progress`` / ``Completed`` for a status, and the overdue, today and soon states of a
# deadline.
# ---------------------------------------------------------------------------

# Every accepted shape ``format_date`` renders, with the string it must produce. ISO
# text is the stored form; the ``date`` and ``datetime`` objects are what a column read
# with a type converter yields; the two time-carrying strings are what a hand-written
# row holds. January and December prove the month names come from ``MONTHS`` rather
# than from a locale-sensitive ``strftime``.
FORMAT_DATE_CASES = [
    pytest.param("2026-10-26", "26 Oct 2026", id="iso-text"),
    pytest.param("2026-01-05", "5 Jan 2026", id="january-day-not-zero-padded"),
    pytest.param("2026-12-31", "31 Dec 2026", id="december"),
    pytest.param("  2026-10-26  ", "26 Oct 2026", id="surrounding-whitespace"),
    pytest.param("2026-10-26T14:03:07", "26 Oct 2026", id="iso-text-with-t-separator"),
    pytest.param(
        "2026-10-26 14:03:07", "26 Oct 2026", id="iso-text-with-space-separator"
    ),
    pytest.param(date(2026, 10, 26), "26 Oct 2026", id="date-object"),
    pytest.param(datetime(2026, 10, 26, 14, 3, 7), "26 Oct 2026", id="datetime-object"),
]

# Values no date can be read from. Each must render as an empty string rather than
# raise, because one unreadable row must not replace a whole page with the error view.
UNUSABLE_DATE_CASES = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("not-a-date", id="not-a-date"),
    pytest.param("31-10-2026", id="wrong-field-order"),
    pytest.param("2026-13-01", id="month-out-of-range"),
]

# The same two tables for ``format_datetime``: the clock is 24-hour and zero-padded,
# the seconds are dropped, and a bare date renders as midnight of that day.
FORMAT_DATETIME_CASES = [
    pytest.param("2026-10-26 14:03:07", "26 Oct 2026, 14:03", id="sqlite-text"),
    pytest.param(
        "2026-10-26T14:03:07", "26 Oct 2026, 14:03", id="iso-text-with-t-separator"
    ),
    pytest.param(
        "2026-01-05 09:07:59", "5 Jan 2026, 09:07", id="january-clock-zero-padded"
    ),
    pytest.param("2026-12-31 23:59:59", "31 Dec 2026, 23:59", id="december"),
    pytest.param(
        datetime(2026, 10, 26, 14, 3, 7), "26 Oct 2026, 14:03", id="datetime-object"
    ),
    pytest.param(date(2026, 10, 26), "26 Oct 2026, 00:00", id="date-object-midnight"),
]

UNUSABLE_DATETIME_CASES = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("not-a-timestamp", id="not-a-timestamp"),
    pytest.param("2026-13-01 00:00", id="month-out-of-range"),
]

# The label each value of the closed status set renders, read from the mapping itself so
# a status added without a label fails here rather than rendering blank on a page.
STATUS_LABEL_CASES = [
    pytest.param(status, label, id=status)
    for status, label in models.STATUS_LABELS.items()
]

# Statuses the mapping does not know, including its own values in the wrong case: each
# must be shown as stored, so an unexpected row still says something.
UNKNOWN_STATUS_CASES = ["archived", "PENDING", "in progress"]

# A deadline's state by its distance in days from today and by the task's status. The
# last day of the "soon" window is the same THIS_WEEK_END_OFFSET the filter cases above
# use, so the word beside a deadline and the Due this week filter cannot drift apart.
DEADLINE_STATE_CASES = [
    pytest.param(-1, "pending", "overdue", id="yesterday-unfinished"),
    pytest.param(-1, "completed", "", id="yesterday-completed"),
    pytest.param(0, "pending", "today", id="today"),
    *[
        pytest.param(offset, "pending", "soon", id=f"in-{offset}-days")
        for offset in range(1, THIS_WEEK_END_OFFSET + 1)
    ],
    pytest.param(THIS_WEEK_END_OFFSET + 1, "pending", "", id="a-day-past-the-window"),
    pytest.param(30, "pending", "", id="a-month-ahead"),
]


@pytest.mark.parametrize(("value", "expected"), FORMAT_DATE_CASES)
def test_format_date_renders_a_stored_date(value: object, expected: str) -> None:
    """A date renders as "26 Oct 2026", with the day not zero-padded."""
    assert templating.format_date(value) == expected


@pytest.mark.parametrize("value", UNUSABLE_DATE_CASES)
def test_format_date_renders_nothing_for_an_unusable_value(value: object) -> None:
    """An absent or malformed date renders as an empty string, never as an error."""
    assert templating.format_date(value) == ""


@pytest.mark.parametrize(("value", "expected"), FORMAT_DATETIME_CASES)
def test_format_datetime_renders_a_stored_timestamp(
    value: object, expected: str
) -> None:
    """A timestamp renders as "26 Oct 2026, 14:03", its seconds dropped."""
    assert templating.format_datetime(value) == expected


@pytest.mark.parametrize("value", UNUSABLE_DATETIME_CASES)
def test_format_datetime_renders_nothing_for_an_unusable_value(value: object) -> None:
    """An absent or malformed timestamp renders as an empty string."""
    assert templating.format_datetime(value) == ""


@pytest.mark.parametrize(("status", "expected"), STATUS_LABEL_CASES)
def test_status_label_renders_the_label_of_every_status(
    status: str, expected: str
) -> None:
    """Each status of the closed set renders its own label."""
    assert templating.status_label(status) == expected


@pytest.mark.parametrize("value", UNKNOWN_STATUS_CASES)
def test_status_label_shows_an_unknown_status_as_its_own_text(value: str) -> None:
    """A status outside the mapping is shown as stored instead of blanked."""
    assert templating.status_label(value) == value


@pytest.mark.parametrize("value", [None, ""])
def test_status_label_renders_nothing_for_a_missing_value(value: str | None) -> None:
    """A task with no status renders no label rather than the word None."""
    assert templating.status_label(value) == ""


@pytest.mark.parametrize(("offset", "status", "expected"), DEADLINE_STATE_CASES)
def test_deadline_state_reads_the_deadline_and_the_status(
    offset: int, status: str, expected: str
) -> None:
    """A deadline's state follows its distance from today, unless the work is done."""
    deadline = (date.today() + timedelta(days=offset)).isoformat()

    assert templating.deadline_state(deadline, status) == expected


def test_a_completed_task_is_never_overdue() -> None:
    """Finished work carries no relative word, even with its deadline behind it.

    This exclusion is the whole reason ``status`` is a parameter rather than a constant,
    so it is asserted on its own line as well as in the table above: the same deadline
    reads as overdue for unfinished work and as silence for finished work.
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    assert templating.deadline_state(yesterday, "completed") == ""
    assert templating.deadline_state(yesterday, "pending") == "overdue"


def test_deadline_state_defaults_to_an_unfinished_task() -> None:
    """Called with the deadline alone, the state is the deadline's own."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    assert templating.deadline_state(yesterday) == "overdue"


@pytest.mark.parametrize("deadline", [None, "", "   ", "not-a-date", "2026-13-01"])
def test_deadline_state_renders_nothing_for_an_unusable_deadline(
    deadline: str | None,
) -> None:
    """No deadline, or one no date can be read from, carries no relative word."""
    assert templating.deadline_state(deadline) == ""
