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

The module talks only to Flask's test client, through the ``seeded_client`` and
``client`` fixtures of ``tests/conftest.py``. It imports no Flask module and opens
no application context; the one read of the database it needs, the id of a seeded
task, is a parameterized query over the ``db_path`` fixture.
"""

from __future__ import annotations

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

# The task area's two states and its seed hint, and the project panel's own empty
# state. The two empty states are independent, so both are named: an empty task
# area must never displace the project details, and vice versa.
CREATE_TASK_CONTROL = "Create task"
EMPTY_TASK_AREA_HEADING = "No tasks yet."
EMPTY_TASK_AREA_ACTION = "Create the first task"
SEED_COMMAND_HINT = "flask --app app seed-db"
EMPTY_PROJECT_HEADING = "No project details yet."

# The heading the 500 page renders. A dashboard that failed would answer with that
# page rather than with four cards, so its absence is asserted wherever a
# successful render is.
UNEXPECTED_FAILURE_HEADING = "Internal Server Error"

# One card: an anchor carrying the ``metric`` class. Matched by pattern rather
# than by exact markup, so attribute order and the elements inside the card are
# the template's business.
_METRIC_CARD = re.compile(r'<a\b[^>]*class="[^"]*\bmetric\b[^"]*"[^>]*>.*?</a>', re.S)

# The ``href`` of a matched anchor, and the count-then-label text the macro emits.
_ANCHOR_HREF = re.compile(r'href="([^"]*)"')
_MARKUP_TAG = re.compile(r"<[^>]+>")
_LEADING_COUNT = re.compile(r"\s*(?P<count>\d+)(?P<label>.*)", re.S)

# What the create form's post redirects to: the new task's own page.
_TASK_DETAIL_LOCATION = re.compile(r"/tasks/\d+")


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
    zero-padding on the day and a three-letter English month.
    """
    day = date.today() + timedelta(days=offset)
    return f"{day.day} {day.strftime('%b')} {day.year}"


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
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id FROM task WHERE title = ?", (title,)
        ).fetchone()
    assert row is not None, f"the seeded dataset holds no task called {title!r}"
    return int(row[0])


def count_task_rows(db_path: str) -> int:
    """Return how many rows the ``task`` table holds."""
    with sqlite3.connect(db_path) as connection:
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
    """With tasks present the task area offers the create control, not its empty state."""
    body, _ = read_dashboard(seeded_client)
    assert CREATE_TASK_CONTROL in body
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
