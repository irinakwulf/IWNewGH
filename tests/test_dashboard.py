"""Tests for the dashboard: the four metric cards, their links, and both empty states.

``GET /`` reports four numbers and nothing else, so this module is the proof that
each one is the number the dataset actually holds: that Total counts every row,
that the three status buckets are exhaustive and mutually exclusive, and that a
create or a status change moves exactly the cards it should.

Every expected value is derived from :data:`seed.SAMPLE_DATA` -- the row counts,
the buckets, the project name and description, the two dates, the id of the task
whose status a test changes -- so the dataset may grow or shrink without this
module restating one of its numbers, and a failure here means the dashboard and
the dataset disagree rather than that a fixture was edited.

The numbers are read out of the rendered cards rather than out of the database,
because the page is what the dashboard reports: a correct query behind a template
that prints the wrong key would pass a database-level assertion and fail a
reviewer. Each card's link is read the same way, which is how "every number is one
click from the rows behind it" is checked without a browser.

Deliberately not asserted: that the dashboard holds no task table, no filter bar or
a particular heading depth. Those are the template's judgement, and the only
markup this module depends on is the contract the dashboard's own docstring fixes
-- the four card labels, their counts and their hrefs, the create control and the
two empty states. Everything else in the markup may change freely.

The dashboard's failure path is asserted here too: a raise from the service the
view calls answers with the 500 page, so the delivery of "the error pages" is a
check rather than a reading of the template.

The module talks only to Flask's test client, through the ``seeded_client``,
``client`` and ``app`` fixtures of ``tests/conftest.py``. It imports no Flask module
and opens no application context; the one read of the database it needs, the id of a
seeded task, is a parameterized query over the ``db_path`` fixture.
"""

from __future__ import annotations

import contextlib
import html
import re
import sqlite3
import uuid
from datetime import date, timedelta
from typing import Any

import pytest

import seed

# --------------------------------------------------------------------------
# The interface the dashboard is contracted to render.
# --------------------------------------------------------------------------

# Each card's label, in the order ``dashboard.html`` renders it, paired with the
# ``app.services.metrics`` key its count comes from and the status its link
# filters the task list by -- ``None`` for the unfiltered Total card. The labels
# are the card vocabulary rather than the status vocabulary: "Total tasks" is not
# a status, so the two are written down here instead of being derived from one
# another.
CARD_CONTRACT: dict[str, tuple[str, str | None]] = {
    "Total tasks": ("total", None),
    "Completed": ("completed", "completed"),
    "Pending": ("pending", "pending"),
    "In progress": ("in_progress", "in_progress"),
}

# The unfiltered task list every Total card opens, and the path the three status
# cards append their query to.
TASK_LIST_PATH = "/tasks"

# Where the dashboard itself is served. It is what the 500 page's one link offers,
# and it is why that link is read from its own anchor rather than from the shell's
# navigation, which links home from every page.
DASHBOARD_PATH = "/"

# The create form the dashboard's create control opens. Written down rather than
# derived from the application, because this module imports no Flask and opens no
# application context: the control is contracted to be a link to this path, so
# plain text or a link pointing anywhere else is not the control.
TASK_CREATE_PATH = "/tasks/new"

# The task area's two states and its seed hint, and the project panel's own empty
# state. The two empty states are independent, so both are named: an empty task
# area must never displace the project details, and vice versa.
CREATE_TASK_CONTROL = "Create task"
EMPTY_TASK_AREA_HEADING = "No tasks yet."
EMPTY_TASK_AREA_ACTION = "Create the first task"
SEED_COMMAND_HINT = "flask --app app seed-db"
EMPTY_PROJECT_HEADING = "No project details yet."

# The project panel's dates read as ``D Mon YYYY`` -- "26 Oct 2026" -- so the day
# is not zero-padded and the month is one of these twelve three-letter English
# names, indexed by ``date.month - 1``. Spelled out rather than taken from
# ``strftime("%b")``, which follows the process locale: the interface ships in
# English only, so a correct render must not move with the host's ``LC_TIME`` and
# neither may the expectation derived from it.
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

# The heading the 500 page renders. A dashboard that failed would answer with that
# page rather than with four cards, so its absence is asserted wherever a
# successful render is.
UNEXPECTED_FAILURE_HEADING = "Internal Server Error"

# The 500 page's way back to the dashboard. Matched with its own anchor so the
# classes and attribute order stay the template's business, and so a page that only
# carried the shell's navigation would not pass for offering a way back.
BACK_TO_DASHBOARD_TEXT = "Back to the dashboard"

# One card: an anchor carrying the ``metric`` class. Matched by pattern rather
# than by exact markup, so attribute order and the elements inside the card are
# the template's business.
_METRIC_CARD = re.compile(r'<a\b[^>]*class="[^"]*\bmetric\b[^"]*"[^>]*>.*?</a>', re.S)

# The ``href`` of a matched anchor, and the count-then-label text the macro emits.
_ANCHOR_HREF = re.compile(r'href="([^"]*)"')
_MARKUP_TAG = re.compile(r"<[^>]+>")
_LEADING_COUNT = re.compile(r"\s*(?P<count>\d+)(?P<label>.*)", re.S)

# Any anchor on a page, so a control is found by its wording and checked by its
# target together rather than as two unrelated substrings of the document.
_ANCHOR = re.compile(r"<a\b[^>]*>.*?</a>", re.S)

# What the create form's post redirects to: the new task's own page.
_TASK_DETAIL_LOCATION = re.compile(r"/tasks/\d+")

# The 500 page's back link: the anchor whose text is BACK_TO_DASHBOARD_TEXT, with
# its href captured so the destination is checked rather than the phrase alone.
_BACK_TO_DASHBOARD_LINK = re.compile(
    r'<a\b[^>]*href="(?P<href>[^"]*)"[^>]*>\s*' + BACK_TO_DASHBOARD_TEXT + r"\s*</a>"
)

# A heading and its text, so the failure page's visible heading is read from the
# heading rather than from the document title, which carries the same phrase: an
# error page whose title was right and whose heading was wrong would still leave
# the visitor without the sentence that says what happened.
_HEADING = re.compile(r"<h1\b[^>]*>(?P<text>[^<]*)</h1>", re.S)


# --------------------------------------------------------------------------
# Reading the dashboard. One extractor, used by every test in the module, so the
# page is read the same way whether it is expected to hold seeded numbers or zeros.
# --------------------------------------------------------------------------


def metric_cards(body: str) -> dict[str, tuple[int, str]]:
    """Return ``{label: (count, href)}`` for every metric card in ``body``.

    The anchor's accessible name is the count followed by the label, and the
    macro emits the two inside a ``<strong>`` and a ``<span>``, so the count is
    read as the leading digits of the tag-stripped text and the label as whatever
    follows them. Splitting on the digits rather than on a space keeps a
    multi-word label such as "In progress" whole, and keeps the match independent
    of the whitespace the template happens to put between the elements.

    Args:
        body: The dashboard's HTML.

    Returns:
        A mapping of card label to the count it reports and the URL it links to,
        with the href's HTML entities decoded so a query string's separators
        compare as they were built.
    """
    cards: dict[str, tuple[int, str]] = {}
    for match in _METRIC_CARD.finditer(body):
        anchor = match.group(0)
        href = _ANCHOR_HREF.search(anchor)
        text = _MARKUP_TAG.sub("", anchor)
        counted = _LEADING_COUNT.match(text)
        assert counted is not None, f"metric card without a leading count: {anchor!r}"
        cards[counted.group("label").strip()] = (
            int(counted.group("count")),
            html.unescape(href.group(1)) if href is not None else "",
        )
    return cards


def anchor_hrefs(body: str, control: str) -> list[str]:
    """Return the target of every anchor in ``body`` whose text is ``control``.

    A control that is contracted to be a link is read as the pair it is -- its
    wording and the page it opens -- rather than as a substring of the document: a
    page that printed the words as plain text, or linked them somewhere else, holds
    no anchor here at all. The anchor's text is its tag-stripped content with the
    surrounding whitespace removed, so the template may wrap or indent it freely.

    Args:
        body: A page's HTML.
        control: The anchor text the control carries, such as "Create task".

    Returns:
        The matching anchors' hrefs, in document order, with HTML entities decoded
        and an anchor that carries no href represented by an empty string. An empty
        list means the control is not a link on that page.
    """
    hrefs: list[str] = []
    for match in _ANCHOR.finditer(body):
        anchor = match.group(0)
        if _MARKUP_TAG.sub("", anchor).strip() != control:
            continue
        href = _ANCHOR_HREF.search(anchor)
        hrefs.append(html.unescape(href.group(1)) if href is not None else "")
    return hrefs


def read_dashboard(client: Any) -> tuple[str, dict[str, tuple[int, str]]]:
    """Render the dashboard and return its body with its cards.

    Args:
        client: A Flask test client, seeded or not.

    Returns:
        The rendered HTML and the ``{label: (count, href)}`` mapping read from it.

    Raises:
        AssertionError: If the page is not a 200 or does not render exactly the
            four cards the dashboard is contracted to show, because every
            assertion below would otherwise be read off a page that was not the
            dashboard at all.
    """
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    cards = metric_cards(body)
    assert set(cards) == set(CARD_CONTRACT), f"cards rendered: {sorted(cards)}"
    assert UNEXPECTED_FAILURE_HEADING not in body
    return body, cards


def counts_from(cards: dict[str, tuple[int, str]]) -> dict[str, int]:
    """Return the cards' counts keyed by the ``dashboard_metrics`` key they report."""
    return {
        CARD_CONTRACT[label][0]: count for label, (count, _) in cards.items()
    }


def expected_card_href(status: str | None) -> str:
    """Return the task-list URL the card named by ``status`` links to.

    ``None`` is the Total card, which opens the unfiltered list.
    """
    if status is None:
        return TASK_LIST_PATH
    return f"{TASK_LIST_PATH}?status={status}"


# --------------------------------------------------------------------------
# Deriving the expectations from the sample dataset.
# --------------------------------------------------------------------------


def sample_tasks() -> list[dict[str, Any]]:
    """Return the task records :data:`seed.SAMPLE_DATA` specifies."""
    return seed.SAMPLE_DATA["tasks"]


def expected_counts() -> dict[str, int]:
    """Return the four counts the dashboard must report for the seeded dataset.

    ``total`` is every task in the dataset and each status key counts the tasks
    holding it, so the mapping is what the dashboard should show and never a
    number written into this module.
    """
    tasks = sample_tasks()
    counts = {"total": len(tasks)}
    for key, status in CARD_CONTRACT.values():
        if status is not None:
            counts[key] = sum(1 for task in tasks if task["status"] == status)
    return counts


def formatted_date(offset: int) -> str:
    """Return the day ``offset`` days from today as the dashboard renders it.

    The seed writes day offsets rather than calendar dates, and the project panel
    renders each stored date through the ``format_date`` filter, so the expectation
    is the same offset resolved against today and formatted the same way: no
    zero-padding on the day, and the English month name spelled out in
    :data:`MONTHS` rather than the one ``strftime`` would take from the locale.
    """
    day = date.today() + timedelta(days=offset)
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def first_seeded_title(status: str) -> str:
    """Return the title of the first seeded task holding ``status``."""
    for task in sample_tasks():
        if task["status"] == status:
            return task["title"]
    raise AssertionError(f"the seeded dataset holds no task with status {status!r}")


def seeded_task_id(db_path: str, title: str) -> int:
    """Return the id the seed loader gave the task called ``title``.

    The ids are the database's own business, so they are read back rather than
    assumed to run from one: a parameterized lookup by title, which the schema's
    data makes unambiguous, is what a test that changes one seeded row needs.

    Args:
        db_path: The database this test's application is using.
        title: A title taken from :data:`seed.SAMPLE_DATA`.

    Returns:
        The task's integer id.
    """
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute(
            "SELECT id FROM task WHERE title = ?", (title,)
        ).fetchone()
    assert row is not None, f"the seeded dataset holds no task called {title!r}"
    return int(row[0])


def count_task_rows(db_path: str) -> int:
    """Return how many rows the ``task`` table holds."""
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute("SELECT COUNT(*) FROM task").fetchone()
    return int(row[0])


def post_new_task(client: Any) -> int:
    """Create a task through the create form and return its id.

    The post names only a title: no assignee, no status and no deadline, so the
    row is unassigned, undated and takes the default Pending status. That is the
    task the dashboard has to count as one of its own, and the one that proves the
    defaults apply.

    Args:
        client: A Flask test client.

    Returns:
        The id of the created task, read from the redirect's target.
    """
    response = client.post(
        "/tasks", data={"title": f"Dashboard probe {uuid.uuid4().hex[:12]}"}
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert _TASK_DETAIL_LOCATION.fullmatch(location), location
    return int(location.rsplit("/", 1)[-1])


# --------------------------------------------------------------------------
# The seeded dashboard: every card, the identity behind the total, the create
# control and the project panel.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("label", list(CARD_CONTRACT))
def test_seeded_card_reports_its_derived_count_and_link(seeded_client, label):
    """Every card shows its dataset-derived count and links to the rows behind it."""
    _, cards = read_dashboard(seeded_client)
    key, status = CARD_CONTRACT[label]
    count, href = cards[label]
    assert count == expected_counts()[key]
    assert href == expected_card_href(status)


def test_seeded_total_is_the_sum_of_its_status_buckets(seeded_client):
    """The three status buckets are exhaustive, so they add up to the total.

    The identity alone would survive two swapped buckets, so it is asserted beside
    the derived counts: together they fail if the aggregate mixes the buckets up,
    drops one, or reports a total that is not the number of rows.
    """
    _, cards = read_dashboard(seeded_client)
    counts = counts_from(cards)
    assert counts["total"] == len(sample_tasks())
    assert counts["total"] == (
        counts["pending"] + counts["in_progress"] + counts["completed"]
    )
    assert counts == expected_counts()


def test_seeded_dashboard_offers_the_create_control(seeded_client):
    """The task area offers the create control rather than its empty state.

    Tasks exist, so the empty state's own heading and button are absent.
    """
    body, _ = read_dashboard(seeded_client)
    # The control is a link into the create form, so plain text reading "Create
    # task" or a link pointing anywhere else does not satisfy it, and there is
    # exactly one of them.
    assert anchor_hrefs(body, CREATE_TASK_CONTROL) == [TASK_CREATE_PATH]
    assert EMPTY_TASK_AREA_HEADING not in body
    assert EMPTY_TASK_AREA_ACTION not in body


def test_seeded_dashboard_shows_the_project_details(seeded_client):
    """The project panel shows the stored name, description and both dates."""
    body, _ = read_dashboard(seeded_client)
    project = seed.SAMPLE_DATA["project"]
    assert html.escape(project["name"], quote=False) in body
    assert html.escape(project["description"], quote=False) in body
    assert formatted_date(project["start_offset"]) in body
    assert formatted_date(project["end_offset"]) in body
    assert EMPTY_PROJECT_HEADING not in body


# --------------------------------------------------------------------------
# The numbers move: a create and a status change are reflected the next time the
# page is loaded, because nothing about them is stored.
# --------------------------------------------------------------------------


def test_creating_a_task_moves_the_total_and_pending_cards(seeded_client):
    """A create raises Total and Pending by one and leaves the other two alone."""
    _, before_cards = read_dashboard(seeded_client)
    before = counts_from(before_cards)
    assert before == expected_counts()

    created = post_new_task(seeded_client)
    assert created > 0

    _, after_cards = read_dashboard(seeded_client)
    after = counts_from(after_cards)
    assert after["total"] == before["total"] + 1
    assert after["pending"] == before["pending"] + 1
    assert after["completed"] == before["completed"]
    assert after["in_progress"] == before["in_progress"]


def test_completing_a_task_moves_one_row_and_leaves_the_total(
    seeded_client, db_path
):
    """Completing a pending task moves one row between buckets and keeps Total.

    This is the property the per-request aggregate exists to guarantee: a status
    change alters two of the three buckets and no other card, so the total of a
    finished task is the total it already had.
    """
    task_id = seeded_task_id(db_path, first_seeded_title("pending"))
    response = seeded_client.post(
        f"/tasks/{task_id}/status",
        data={"status": "completed", "origin": "detail"},
    )
    assert response.status_code == 302

    _, cards = read_dashboard(seeded_client)
    counts = counts_from(cards)
    expected = expected_counts()
    assert counts["completed"] == expected["completed"] + 1
    assert counts["pending"] == expected["pending"] - 1
    assert counts["in_progress"] == expected["in_progress"]
    assert counts["total"] == expected["total"]


# --------------------------------------------------------------------------
# The zero state: no tasks and no project row. Each area answers it in its own
# words, and neither displaces the other.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("label", list(CARD_CONTRACT))
def test_empty_dashboard_reports_zero_for_every_card(client, label):
    """With no rows at all every card reads 0 and keeps its link into the list."""
    _, cards = read_dashboard(client)
    _, status = CARD_CONTRACT[label]
    count, href = cards[label]
    assert count == 0
    assert href == expected_card_href(status)


def test_empty_dashboard_shows_both_empty_states(client):
    """The task area and the project panel each render their own empty state."""
    body, cards = read_dashboard(client)
    assert set(counts_from(cards).values()) == {0}
    assert EMPTY_TASK_AREA_HEADING in body
    assert EMPTY_TASK_AREA_ACTION in body
    assert SEED_COMMAND_HINT in body
    assert EMPTY_PROJECT_HEADING in body
    # The empty state takes the create control's place rather than sitting beside
    # it, so exactly one way to create the first task is on screen.
    assert CREATE_TASK_CONTROL not in body


# --------------------------------------------------------------------------
# The mixed states: one area holds rows while the other is empty. The two empty
# states are independent, so neither may be suppressed by the other area having
# something to show.
# --------------------------------------------------------------------------

# Where the project form lives. The second mixed state records the project row
# through it, so the project panel has details of its own while no task exists.
PROJECT_PATH = "/project"


def post_project_details(client: Any) -> str:
    """Record a project row through the project form and return the name it stores.

    All four fields are submitted, because the project has no optional one. Both
    dates are resolved against today rather than written down: a start date behind
    today and an end date ahead of it satisfy the date-order rule whenever the test
    runs. The name is unique per call and shares no wording with either empty
    state, so finding it on the dashboard cannot be mistaken for a heading.

    Args:
        client: A Flask test client whose database holds no project row yet.

    Returns:
        The name the form was submitted with, as the dashboard renders it.
    """
    name = f"Dashboard project {uuid.uuid4().hex[:12]}"
    response = client.post(
        PROJECT_PATH,
        data={
            "name": name,
            "description": "Recorded to give the project panel a row of its own.",
            "start_date": (date.today() - timedelta(days=7)).isoformat(),
            "end_date": (date.today() + timedelta(days=30)).isoformat(),
        },
    )
    assert response.status_code == 302
    return name


def test_task_without_project_row_keeps_the_project_empty_state(client):
    """Tasks present and no project row: the project panel still offers its form.

    A task may be recorded before the project details are, and this is that state:
    the task area shows its create control while the project panel shows its own
    empty state, so the panel's empty state survives the board holding work.
    """
    post_new_task(client)

    body, cards = read_dashboard(client)
    # The premise rather than a dashboard claim: the create above succeeded, so the
    # task area really is populated while no project row exists.
    assert counts_from(cards)["total"] == 1
    assert anchor_hrefs(body, CREATE_TASK_CONTROL) == [TASK_CREATE_PATH]
    assert EMPTY_TASK_AREA_HEADING not in body
    assert EMPTY_TASK_AREA_ACTION not in body
    assert EMPTY_PROJECT_HEADING in body


def test_project_without_tasks_keeps_the_task_area_empty_states(client):
    """A project row present and no tasks: the task area still offers the first task.

    The mirror of the case above. Both halves of the task area's empty state -- its
    heading and its action -- must survive a stored project row, which a template
    that gated them on the project existing would fail while every other case
    stayed green.
    """
    name = post_project_details(client)

    body, cards = read_dashboard(client)
    assert set(counts_from(cards).values()) == {0}
    assert EMPTY_TASK_AREA_HEADING in body
    assert EMPTY_TASK_AREA_ACTION in body
    assert html.escape(name, quote=False) in body
    assert EMPTY_PROJECT_HEADING not in body
    assert CREATE_TASK_CONTROL not in body


# --------------------------------------------------------------------------
# What the total counts: every row, assigned or not, dated or not.
# --------------------------------------------------------------------------


def test_unassigned_and_undated_tasks_are_counted_like_any_other(
    seeded_client, db_path
):
    """Total counts every row, so it is not a count of assigned or dated work.

    The dataset is asserted to hold both cases before the dashboard is read, so
    the test cannot pass by describing a dataset that never exercises them.
    """
    assert any(task["assignee"] is None for task in sample_tasks())
    assert any(task["deadline_offset"] is None for task in sample_tasks())

    _, cards = read_dashboard(seeded_client)
    counts = counts_from(cards)
    assert counts["total"] == len(sample_tasks())
    assert counts["total"] == count_task_rows(db_path)

    # A task with neither an assignee nor a deadline, created through the form
    # that leaves both unset, is counted exactly like the rest.
    post_new_task(seeded_client)
    _, grown = read_dashboard(seeded_client)
    assert counts_from(grown)["total"] == counts["total"] + 1


# --------------------------------------------------------------------------
# The failure path: an unexpected error answers with the 500 page, inside the
# shell and with nothing of the exception behind it.
# --------------------------------------------------------------------------


def test_an_unhandled_failure_renders_the_500_page_inside_the_shell(
    app, monkeypatch
):
    """An unhandled failure answers with the 500 page, and with nothing of itself.

    The dashboard reads its numbers through one service call, so a raise there is
    the unexpected failure the 500 handler exists for. The response is checked for
    the page's own heading, its one way back to the dashboard and the shell around
    it, and then for the absence of the failure that produced it: a page that
    answered 500 while printing the exception's message would still be handing the
    visitor the application's internals.
    """
    failure = f"dashboard metrics failed {uuid.uuid4().hex}"

    def failing_metrics():
        """Stand in for the service by raising, as an unexpected bug would."""
        raise RuntimeError(failure)

    # The view calls the service through the name the blueprint module imported, so
    # that binding is the one replaced: patching app.services.metrics would leave
    # the dashboard pointed at the working function.
    monkeypatch.setattr("app.blueprints.dashboard.dashboard_metrics", failing_metrics)
    # The ``app`` fixture builds a TESTING application, and a testing application
    # hands an unhandled exception to its caller instead of rendering the page for
    # it. A served application does the opposite, which is the case asserted here.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    response = app.test_client().get(DASHBOARD_PATH)
    body = response.get_data(as_text=True)

    assert response.status_code == 500
    # The heading is built from the exception Flask handled -- its name and its
    # code -- so it is checked the same way rather than against a copied phrase,
    # and it is read from the single <h1> the page renders: the <title> carries
    # the same words, so the phrase alone would pass for a page whose heading had
    # broken.
    headings = [match.group("text").strip() for match in _HEADING.finditer(body)]
    assert headings == [f"{UNEXPECTED_FAILURE_HEADING} (500)"]
    assert BACK_TO_DASHBOARD_TEXT in body
    back_link = _BACK_TO_DASHBOARD_LINK.search(body)
    assert back_link is not None, "the 500 page offers no way back to the dashboard"
    assert back_link.group("href") == DASHBOARD_PATH
    # Inside the shell, which is what keeps every other screen one click away: the
    # navigation's Tasks link is rendered by the shell alone.
    assert f'href="{TASK_LIST_PATH}"' in body
    # And the failure itself stays out of the response, so the page says what
    # happened without naming what broke.
    assert failure not in body
    assert "RuntimeError" not in body
