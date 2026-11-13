"""Tests for the project screen: its seeded view, update, rejections and upsert.

``/project`` is the only screen that records the request's "basic project details",
and it is exercised here through the application's own HTTP surface, the way a
reviewer drives it: a ``GET`` renders the stored record -- or its empty state before
one exists -- and a ``POST`` either saves and redirects or re-renders with a message
beside the field and writes nothing. Four things are asserted that are not visible
from a single page load:

* the save is an **upsert**. ``app/schema.sql`` pins the record with ``CHECK (id = 1)``,
  so a second save must update row 1 rather than insert a second one; the row count is
  read back after two posts to prove it.
* a rejected submission writes **nothing**, and the row it did not touch is read back
  field by field before and after, so a rejection that half-wrote cannot pass.
* the submitted values **survive** a rejection in the fields, so the reviewer corrects
  one field instead of retyping the form.
* the deadline rule of the task form **follows the saved start date**, which is the one
  cross-capability consequence of editing the project: it is checked here because the
  project screen is what sets the date the rule compares against.

Every expected value is derived rather than written down. The seeded name and
description come from ``seed.SAMPLE_DATA``; each date is computed from ``date.today()``
plus the dataset's own day offsets and formatted the way the interface renders it, so
no calendar date is spelled out in this module and the suite keeps its meaning when the
dataset or the day changes. Stored state is read back through
:func:`app.db.get_db` -- the connection the application itself uses -- rather than
re-asserted from the page that was just redirected to.

The messages, the name bound and the two framings of the empty state are spelled out
here instead of imported from :mod:`app.validation` and the templates: the wording is
part of what a reviewer reads on screen, so a reworded sentence should fail this suite
and be reviewed rather than follow the module silently.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from html import unescape
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

import seed
from app.db import get_db
# The one statement of how the suite spells a month lives in ``tests/conftest.py``;
# this module imports the names from there rather than restating the rule.
from conftest import MONTH_ABBREVIATIONS

# ---------------------------------------------------------------------------
# The interface's words. Asserted as equal substrings of the rendered page, so a
# message that changed would be caught rather than accommodated.
# ---------------------------------------------------------------------------

MSG_ENTER_PROJECT_NAME = "Enter a project name."
MSG_PROJECT_NAME_TOO_LONG = "Project names are limited to 120 characters."
MSG_DESCRIPTION_TOO_LONG = "Descriptions are limited to 2000 characters."
MSG_INVALID_DATE = "Enter a valid date (YYYY-MM-DD)."
MSG_END_DATE_BEFORE_START_DATE = "End date cannot be earlier than the start date."
MSG_DEADLINE_BEFORE_PROJECT_START = (
    "Deadline cannot be earlier than the project start date."
)
MSG_PROJECT_SAVED = "Project details saved."

# The empty state's heading, rendered where the definition list would be.
EMPTY_STATE_HEADING = "No project details yet."

# The form's submit control, which is the one primary action on the screen.
SUBMIT_LABEL = "Save project details"

# The name bound app/validation.py enforces, and a name one character past it.
PROJECT_NAME_LIMIT = 120
LONG_NAME = "N" * (PROJECT_NAME_LIMIT + 1)

# The description bound the same module enforces, and a description one character
# past it: the field has no required rule, so the length is the only way it rejects.
DESCRIPTION_LIMIT = 2000
LONG_DESCRIPTION = "D" * (DESCRIPTION_LIMIT + 1)

# The screen under test, and the id of the one record the schema allows.
PROJECT_PATH = "/project"
PROJECT_ID = 1

# The names and description the update tests post. Distinct from the seeded values so
# a page that rendered the stored record instead of the submitted one would differ.
FIRST_NAME = "Website Redesign Phase Two"
SECOND_NAME = "Website Redesign Launch"
SAVED_DESCRIPTION = "Marketing site refresh, second pass, with the CMS migration done."

# A description carrying markup, which must be stored as typed and shown as text.
MARKUP_DESCRIPTION = "<script>alert(1)</script>"

# ---------------------------------------------------------------------------
# The statements the tests read stored state with. Only reads: every write in these
# tests goes through the application's own routes.
# ---------------------------------------------------------------------------

_SELECT_PROJECT_SQL = (
    "SELECT id, name, description, start_date, end_date FROM project ORDER BY id"
)
_COUNT_PROJECT_SQL = "SELECT COUNT(*) AS total FROM project"
_COUNT_TASK_TITLED_SQL = "SELECT COUNT(*) AS total FROM task WHERE title = ?"

# The rows a read returns, one mapping per row keyed by column name. Named once so
# every helper below states its shape instead of repeating the annotation.
Rows = Callable[..., list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Derivation helpers. Nothing here reads the application's formatting code: a test
# that asked app.templating to format a date would confirm the module against itself.
# ---------------------------------------------------------------------------


def offset_day(days: int) -> date:
    """Return the day ``days`` from today.

    Args:
        days: Day offset from today, negative for the past.

    Returns:
        The date the dataset's own offsets resolve to, since ``seed_database`` adds
        them to a single ``date.today()``.
    """
    return date.today() + timedelta(days=days)


def offset_iso(days: int) -> str:
    """Return the day ``days`` from today as the ``YYYY-MM-DD`` text the app stores."""
    return offset_day(days).isoformat()


def offset_rendered(days: int) -> str:
    """Return the day ``days`` from today as the project screen renders it.

    The interface shows a date as ``26 Oct 2026`` -- an unpadded day, a three-letter
    month and a four-digit year -- which is what this reproduces from the date alone.
    The month comes from ``MONTH_ABBREVIATIONS``, the English names
    ``tests/conftest.py`` states once, rather than from ``strftime``, which follows
    the host's locale.
    """
    day = offset_day(days)
    return f"{day.day} {MONTH_ABBREVIATIONS[day.month - 1]} {day.year}"


def day_first(iso_text: str) -> str:
    """Return an ISO-8601 date in the day-first ``DD-MM-YYYY`` shape the app refuses.

    Derived from a date rather than written as a literal so the malformed shape this
    suite submits stays malformed whenever it runs: the day-first form never has a
    hyphen in the position ``YYYY-MM-DD`` puts one, so it can never be mistaken for a
    date the product should accept.
    """
    year, month, day = iso_text.split("-")
    return f"{day}-{month}-{year}"


def valid_form(**overrides: Any) -> dict[str, str]:
    """Return a complete, acceptable project submission with ``overrides`` applied.

    Args:
        **overrides: Field values to replace. Every value is passed through ``str`` so
            a caller can blank a field with ``""`` and still post text.

    Returns:
        A mapping carrying all four form fields: a name distinct from the seeded one,
        a description, and a start date comfortably after today with an end date after
        it, so every case that is meant to be accepted is accepted on the date-order
        rule as well.
    """
    form = {
        "name": FIRST_NAME,
        "description": SAVED_DESCRIPTION,
        "start_date": offset_iso(3),
        "end_date": offset_iso(30),
    }
    form.update({field: str(value) for field, value in overrides.items()})
    return form


# ---------------------------------------------------------------------------
# Fixtures and the assertions they feed.
# ---------------------------------------------------------------------------


@pytest.fixture
def rows(app: Flask) -> Rows:
    """Return a function that runs a read-only statement on the test database.

    The connection is the application's own -- :func:`app.db.get_db` inside an
    application context, with the row factory the application sets -- so a test reads
    exactly what the routes wrote, from the same file and through the same handle type.
    Each row is returned as a plain ``dict`` because the connection is closed when the
    context ends.
    """

    def fetch(sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run ``sql`` with bound ``parameters`` and return its rows as mappings."""
        with app.app_context():
            cursor = get_db().execute(sql, parameters)
            return [dict(row) for row in cursor.fetchall()]

    return fetch


def stored_project(rows: Rows) -> list[dict[str, Any]]:
    """Return every row of the project table, ordered by id."""
    return rows(_SELECT_PROJECT_SQL)


def project_row_count(rows: Rows) -> int:
    """Return how many rows the project table holds."""
    return int(rows(_COUNT_PROJECT_SQL)[0]["total"])


def task_count_titled(rows: Rows, title: str) -> int:
    """Return how many tasks carry ``title``."""
    return int(rows(_COUNT_TASK_TITLED_SQL, (title,))[0]["total"])


def assert_stored_project(rows: Rows, expected: Mapping[str, str]) -> None:
    """Assert the project table holds one row -- row 1 -- carrying ``expected``.

    Both halves matter: the field values prove the save wrote what was submitted, and
    the single row proves it updated the record the schema allows instead of adding a
    second one.

    Args:
        rows: The read helper.
        expected: Column name to expected value, for the four editable fields.
    """
    stored = stored_project(rows)
    assert len(stored) == 1, "the project table must hold exactly one row"
    row = stored[0]
    assert row["id"] == PROJECT_ID
    assert {field: row[field] for field in expected} == dict(expected)


def assert_empty_project_form(html: str) -> None:
    """Assert the four project controls render with nothing in them.

    The three inputs are checked for an empty ``value`` attribute and the textarea for
    an empty body, which is what a first render on a database with no project row
    produces.
    """
    for control_id in ("name", "start_date", "end_date"):
        assert re.search(rf'id="{control_id}"[^>]*\bvalue=""', html), control_id
    assert re.search(r'id="description"[^>]*></textarea>', html)


# ---------------------------------------------------------------------------
# Reading the rendered page. Attribute order is never assumed: a control's
# attributes are parsed into a mapping before any one of them is read.
# ---------------------------------------------------------------------------

_ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')
_CONTROL_TAGS = ("input", "select", "textarea")
_LABEL = re.compile(r"<label\b([^>]*)>(.*?)</label>", re.DOTALL | re.IGNORECASE)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL | re.IGNORECASE)


def control_attributes(html: str, control_id: str) -> dict[str, str]:
    """Return the attributes of the form control carrying ``control_id``.

    Args:
        html: The rendered page.
        control_id: The value of the control's ``id`` attribute.

    Returns:
        The control's attributes as a mapping, so a case reads ``name``,
        ``aria-invalid`` or ``aria-describedby`` by name instead of matching markup
        in a fixed order.

    Raises:
        AssertionError: If the page renders no input, select or textarea with that
            id, which would mean the control the label names does not exist.
    """
    for tag in _CONTROL_TAGS:
        pattern = re.compile(
            rf'<{tag}\b[^>]*\bid="{re.escape(control_id)}"[^>]*>', re.IGNORECASE
        )
        match = pattern.search(html)
        if match is not None:
            return dict(_ATTRIBUTE.findall(match.group(0)))
    raise AssertionError(f"no form control with id={control_id!r} in the page")


def label_text(html: str, control_id: str) -> str:
    """Return the text of the ``<label>`` that points at one control.

    A label is tied to its control by the ``for`` attribute and by nothing else, so
    this reads the label whose ``for`` names ``control_id`` rather than the first
    label the page happens to render.

    Args:
        html: The rendered page.
        control_id: The value of the labelled control's ``id`` attribute.

    Returns:
        The label's text, unescaped and stripped of surrounding whitespace, so an
        empty return is a label that names the control but says nothing.

    Raises:
        AssertionError: If no label names that control, which leaves the control
            with no accessible name at all.
    """
    for attributes, text in _LABEL.findall(html):
        if dict(_ATTRIBUTE.findall(attributes)).get("for") == control_id:
            return unescape(text).strip()
    raise AssertionError(f"no <label for={control_id!r}> in the page")


def current_links(html: str) -> list[str]:
    """Return the text of every anchor the page marks ``aria-current="page"``.

    The navigation states which of its three sections the visitor is in, so the
    caller asserts both the mark's presence and that exactly one anchor carries it.

    Args:
        html: The rendered page.

    Returns:
        The marked anchors' text, unescaped and stripped, in document order.
    """
    return [
        unescape(text).strip()
        for attributes, text in _ANCHOR.findall(html)
        if dict(_ATTRIBUTE.findall(attributes)).get("aria-current") == "page"
    ]


# ---------------------------------------------------------------------------
# Phase A -- the seeded view.
# ---------------------------------------------------------------------------


def test_project_screen_shows_the_seeded_details(seeded_client: FlaskClient) -> None:
    """The stored record's name, description and both dates render on the screen.

    The dates are asserted in the interface's own format, so this also proves the
    definition list pipes them through the ``format_date`` filter rather than showing
    the ISO text the database holds.
    """
    project = seed.SAMPLE_DATA["project"]

    response = seeded_client.get(PROJECT_PATH)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert project["name"] in html
    assert project["description"] in html
    assert offset_rendered(project["start_offset"]) in html
    assert offset_rendered(project["end_offset"]) in html
    assert EMPTY_STATE_HEADING not in html


def test_project_form_is_prefilled_with_the_stored_values(
    seeded_client: FlaskClient,
) -> None:
    """The edit form opens on the stored record, with the dates in ISO form.

    The two date controls carry the raw ``YYYY-MM-DD`` text because that is the only
    form an ``<input type="date">`` reads, which is why the form and the definition
    list above it show the same dates two different ways.
    """
    project = seed.SAMPLE_DATA["project"]

    html = seeded_client.get(PROJECT_PATH).get_data(as_text=True)

    assert f'value="{project["name"]}"' in html
    assert f'>{project["description"]}</textarea>' in html
    assert f'value="{offset_iso(project["start_offset"])}"' in html
    assert f'value="{offset_iso(project["end_offset"])}"' in html
    assert SUBMIT_LABEL in html


# The four controls the project form declares, in the order the screen lists them. Each
# id is the field name app.validation.parse_project_form reads, and it is also what the
# label's ``for`` and every ``<field>-error`` id are built from.
PROJECT_CONTROLS = ("name", "description", "start_date", "end_date")


@pytest.mark.parametrize("control_id", PROJECT_CONTROLS)
def test_project_form_labels_every_control(
    seeded_client: FlaskClient, control_id: str
) -> None:
    """Every control on the project form carries a visible, non-empty label.

    The label's ``for`` is the only thing that gives the control its accessible name,
    so a label that names no control, or a control that no label names, fails here.
    The control must also post under the field name the parser reads, which is what
    keeps the label's target and the field it fills the same thing.
    """
    html = seeded_client.get(PROJECT_PATH).get_data(as_text=True)

    assert label_text(html, control_id), f"{control_id} has an empty label"
    assert control_attributes(html, control_id).get("name") == control_id


def test_the_project_screen_marks_the_project_nav_link_current(
    seeded_client: FlaskClient,
) -> None:
    """The project screen marks the Project link in the navigation, and only that link.

    The mark is how the shell says which of its three sections the visitor is in, so a
    page carrying none leaves the navigation without a position and a page carrying two
    answers the question with a contradiction.
    """
    html = seeded_client.get(PROJECT_PATH).get_data(as_text=True)

    assert current_links(html) == ["Project"]


# ---------------------------------------------------------------------------
# Phase B -- the update, and the upsert behind it.
# ---------------------------------------------------------------------------


def test_valid_project_post_updates_the_row_and_redirects(
    seeded_client: FlaskClient, rows: Rows
) -> None:
    """An accepted submission saves all four fields and returns to the screen.

    The redirect is the contract's: the reviewer lands on a page that reads the
    database again, so what is shown after a write is what is stored.
    """
    form = valid_form(start_date=offset_iso(2))

    response = seeded_client.post(PROJECT_PATH, data=form)

    assert response.status_code == 302
    assert response.headers["Location"].endswith(PROJECT_PATH)
    page = seeded_client.get(PROJECT_PATH).get_data(as_text=True)
    assert MSG_PROJECT_SAVED in page
    assert SAVED_DESCRIPTION in page
    assert_stored_project(rows, form)
    assert project_row_count(rows) == 1


def test_second_project_post_updates_the_same_row(
    seeded_client: FlaskClient, rows: Rows
) -> None:
    """Two accepted submissions leave one row, and the newest values win.

    This is the case that proves "an insert when no row exists, an update of row 1
    otherwise": a second insert would break the schema's ``CHECK (id = 1)`` or leave a
    second project behind, and a save that kept the older values would show up as a
    stale name on the screen.
    """
    seeded_client.post(PROJECT_PATH, data=valid_form())
    second = valid_form(name=SECOND_NAME, description=SAVED_DESCRIPTION)

    response = seeded_client.post(PROJECT_PATH, data=second)

    assert response.status_code == 302
    assert_stored_project(rows, second)
    assert project_row_count(rows) == 1
    assert SECOND_NAME in seeded_client.get(PROJECT_PATH).get_data(as_text=True)


# ---------------------------------------------------------------------------
# Phase C -- rejections. Each one re-renders the form at 200 with a message beside
# the field, keeps the stored row untouched and leaves every submitted value in place.
# ---------------------------------------------------------------------------

# One case per rule the project form enforces. The value replaced is named by the
# mapping itself, so a case states only what it changes; the message it must produce is
# spelled out beside it.
REJECTED_PROJECT_FORMS = (
    pytest.param({"name": ""}, MSG_ENTER_PROJECT_NAME, id="blank-name"),
    pytest.param({"name": "   "}, MSG_ENTER_PROJECT_NAME, id="whitespace-name"),
    pytest.param({"name": LONG_NAME}, MSG_PROJECT_NAME_TOO_LONG, id="name-too-long"),
    pytest.param(
        {"description": LONG_DESCRIPTION},
        MSG_DESCRIPTION_TOO_LONG,
        id="description-too-long",
    ),
    pytest.param({"start_date": ""}, MSG_INVALID_DATE, id="blank-start-date"),
    pytest.param(
        {"start_date": day_first(offset_iso(5))},
        MSG_INVALID_DATE,
        id="day-first-start-date",
    ),
    pytest.param(
        {"end_date": day_first(offset_iso(20))},
        MSG_INVALID_DATE,
        id="day-first-end-date",
    ),
)


@pytest.mark.parametrize("overrides, message", REJECTED_PROJECT_FORMS)
def test_rejected_project_post_changes_nothing(
    seeded_client: FlaskClient,
    rows: Rows,
    overrides: dict[str, str],
    message: str,
) -> None:
    """A rejected submission renders its message, writes nothing and keeps the values.

    The three assertions are the three things a reviewer depends on: the page comes back
    with the problem explained, the record they were editing is exactly as it was --
    compared field by field, so a partial write cannot pass -- and the values they typed
    are still in the fields rather than lost.
    """
    before = stored_project(rows)
    form = valid_form(**overrides)

    response = seeded_client.post(PROJECT_PATH, data=form)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert message in html
    assert stored_project(rows) == before
    assert project_row_count(rows) == 1
    for submitted in form.values():
        if submitted.strip():
            assert submitted.strip() in html, "a submitted value should survive"


def test_a_rejected_project_field_is_flagged_for_assistive_technology(
    seeded_client: FlaskClient, rows: Rows
) -> None:
    """The name field's message is tied to the control that caused it.

    The paragraph carrying the ``name-error`` id is the one the control points at, and
    ``aria-invalid="true"`` is what tells assistive technology the value was refused, so
    the whole association is asserted rather than the sentence alone. The two date hints
    are read on a clean render beside it, because a hint describes its control before
    anything is wrong -- that is when the format it states is needed.
    """
    clean = seeded_client.get(PROJECT_PATH).get_data(as_text=True)
    for control_id in ("start_date", "end_date"):
        described_by = control_attributes(clean, control_id).get("aria-describedby")
        assert described_by == f"{control_id}-hint"
        assert f'id="{control_id}-hint"' in clean

    before = stored_project(rows)

    response = seeded_client.post(PROJECT_PATH, data=valid_form(name=""))
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    error = f'<p class="field-error" id="name-error">{MSG_ENTER_PROJECT_NAME}</p>'
    assert error in html
    name = control_attributes(html, "name")
    assert name.get("aria-invalid") == "true"
    assert name.get("aria-describedby") == "name-error"
    assert stored_project(rows) == before


# The two date controls, one case each. The text they are given is derived in the test
# rather than written here, because it is a real date in a shape the product refuses.
UNSHOWABLE_PROJECT_DATES = (
    pytest.param("start_date", id="start-date"),
    pytest.param("end_date", id="end-date"),
)


@pytest.mark.parametrize("control_id", UNSHOWABLE_PROJECT_DATES)
def test_a_rejected_project_date_is_echoed_beside_the_field(
    seeded_client: FlaskClient, rows: Rows, control_id: str
) -> None:
    """A date the control cannot display comes back stated beside the field.

    HTML defines a date input's value as empty unless it is a valid date string, so the
    text a rejected post carries for a date that did not parse reaches the control and
    renders blank: the one field whose entry a re-render would otherwise lose. The form
    therefore states the entry back in its own paragraph, gives that paragraph an id,
    and names that id in the control's ``aria-describedby`` after the message, so what
    was typed is still on the page and announced with the field it belongs to.

    The value attribute is asserted as well: it is left empty for this text, because a
    date input is required to ignore a value it cannot display, so the paragraph is the
    one place the entry is carried instead of an attribute the control would discard.
    """
    before = stored_project(rows)
    submitted = day_first(offset_iso(5))

    response = seeded_client.post(
        PROJECT_PATH, data=valid_form(**{control_id: submitted})
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert MSG_INVALID_DATE in html
    assert stored_project(rows) == before
    control = control_attributes(html, control_id)
    assert control.get("value") == ""
    echoed = re.search(rf'<p id="{control_id}-submitted">(.*?)</p>', html, re.DOTALL)
    assert echoed is not None, "the rejected date was not echoed beside the field"
    assert submitted in unescape(echoed.group(1))
    assert control.get("aria-describedby") == (
        f"{control_id}-error {control_id}-submitted {control_id}-hint"
    )
    assert control.get("aria-invalid") == "true"


def test_end_date_before_start_date_is_rejected(
    seeded_client: FlaskClient, rows: Rows
) -> None:
    """An end date one day before the start date is refused, and neither is written.

    Both dates are echoed back into their controls, so the reviewer corrects the one
    that is wrong instead of re-entering the pair.
    """
    before = stored_project(rows)
    start = offset_iso(10)
    end = offset_iso(9)
    form = valid_form(start_date=start, end_date=end)

    response = seeded_client.post(PROJECT_PATH, data=form)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert MSG_END_DATE_BEFORE_START_DATE in html
    assert f'value="{start}"' in html
    assert f'value="{end}"' in html
    assert stored_project(rows) == before


def test_equal_start_and_end_dates_are_accepted(
    seeded_client: FlaskClient, rows: Rows
) -> None:
    """A one-day project is a project: only an end date before the start is refused."""
    same_day = offset_iso(12)
    form = valid_form(start_date=same_day, end_date=same_day)

    response = seeded_client.post(PROJECT_PATH, data=form)

    assert response.status_code == 302
    assert_stored_project(rows, form)


def test_description_with_markup_is_stored_verbatim_and_rendered_escaped(
    seeded_client: FlaskClient, rows: Rows
) -> None:
    """Markup in a description is text: stored as typed, shown escaped.

    The record is not altered on the way in -- the reviewer's words are their own --
    and the page renders them as characters, which is the autoescaping every ``.html``
    template gets and nothing in the template tree bypasses.
    """
    form = valid_form(description=MARKUP_DESCRIPTION)

    response = seeded_client.post(PROJECT_PATH, data=form)

    assert response.status_code == 302
    assert_stored_project(rows, form)
    html = seeded_client.get(PROJECT_PATH).get_data(as_text=True)
    assert MARKUP_DESCRIPTION not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# Phase D -- the empty state and the first save, on a database with no project row.
# ---------------------------------------------------------------------------


def test_project_screen_shows_its_empty_state_without_a_row(
    client: FlaskClient, rows: Rows
) -> None:
    """With nothing recorded the screen still answers 200: an empty form and a heading.

    ``None`` is a state, not an error: saving that form is what creates the record, so
    the empty state has to be a page a reviewer can act on.
    """
    response = client.get(PROJECT_PATH)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert EMPTY_STATE_HEADING in html
    assert SUBMIT_LABEL in html
    assert_empty_project_form(html)
    assert stored_project(rows) == []


def test_first_project_post_creates_the_single_row(
    client: FlaskClient, rows: Rows
) -> None:
    """The first accepted submission inserts row 1, which is what the schema pins.

    The dashboard's project panel is read afterwards because it renders the same row: a
    panel showing the details it does not store itself is the proof that the record
    lives in one place.
    """
    form = valid_form()

    response = client.post(PROJECT_PATH, data=form)

    assert response.status_code == 302
    assert response.headers["Location"].endswith(PROJECT_PATH)
    assert MSG_PROJECT_SAVED in client.get(PROJECT_PATH).get_data(as_text=True)
    assert_stored_project(rows, form)
    assert form["name"] in client.get("/").get_data(as_text=True)


def test_rejected_first_project_post_creates_no_row(
    client: FlaskClient, rows: Rows
) -> None:
    """A rejected submission on an empty database leaves it empty.

    The rejection path is the same code either way, and this is the case where getting
    it wrong would invent a project record out of a mistake.
    """
    response = client.post(PROJECT_PATH, data=valid_form(name=""))

    assert response.status_code == 200
    assert MSG_ENTER_PROJECT_NAME in response.get_data(as_text=True)
    assert stored_project(rows) == []


def test_second_project_post_without_a_seeded_row_keeps_one_row(
    client: FlaskClient, rows: Rows
) -> None:
    """Creating and then editing the record leaves one row, not two.

    The insert path and the update path are both exercised here, on a database that
    started with no project at all.
    """
    client.post(PROJECT_PATH, data=valid_form())
    second = valid_form(name=SECOND_NAME)

    response = client.post(PROJECT_PATH, data=second)

    assert response.status_code == 302
    assert_stored_project(rows, second)
    assert project_row_count(rows) == 1


# ---------------------------------------------------------------------------
# Phase E -- the deadline rule follows the saved start date.
# ---------------------------------------------------------------------------

# The task the two deadline checks are attempted with. A title no seeded task carries,
# so counting the rows that hold it counts only what this suite created.
DEADLINE_PROBE_TITLE = "Announce the redesign to customers"


@pytest.fixture
def saved_project_start(seeded_client: FlaskClient) -> str:
    """Save a project start date later than today and return its ISO-8601 text.

    The seeded start date is already in the past, so a deadline before it would prove
    nothing; pushing the start date forward is what makes the boundary visible.
    """
    start = offset_iso(5)
    form = valid_form(start_date=start, end_date=offset_iso(30))

    response = seeded_client.post(PROJECT_PATH, data=form)

    assert response.status_code == 302
    return start


def test_task_deadline_before_the_saved_project_start_is_rejected(
    seeded_client: FlaskClient, rows: Rows, saved_project_start: str
) -> None:
    """A deadline earlier than the start date is refused, and no task is written.

    The deadline is derived from the start date that was just saved rather than from a
    literal, so this asserts the relationship the rule describes: move the start date
    and the boundary moves with it.
    """
    deadline = (date.fromisoformat(saved_project_start) - timedelta(days=1)).isoformat()

    response = seeded_client.post(
        "/tasks", data={"title": DEADLINE_PROBE_TITLE, "deadline": deadline}
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert MSG_DEADLINE_BEFORE_PROJECT_START in html
    assert task_count_titled(rows, DEADLINE_PROBE_TITLE) == 0


def test_task_deadline_after_the_saved_project_start_is_accepted(
    seeded_client: FlaskClient, rows: Rows, saved_project_start: str
) -> None:
    """One day later the same submission is saved, which is the boundary working.

    Paired with the rejection above, the two cases show the comparison is against the
    saved start date rather than a fixed one.
    """
    deadline = (date.fromisoformat(saved_project_start) + timedelta(days=1)).isoformat()

    response = seeded_client.post(
        "/tasks", data={"title": DEADLINE_PROBE_TITLE, "deadline": deadline}
    )

    assert response.status_code == 302
    assert task_count_titled(rows, DEADLINE_PROBE_TITLE) == 1
