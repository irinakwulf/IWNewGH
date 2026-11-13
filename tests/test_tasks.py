"""Tests for the task screens: the list, the create and edit forms, the detail view
and the two quick actions that hang off it.

The module drives the application the way a browser does -- through Flask's test
client, with no live server and no browser -- and reads the stored rows back
through :func:`app.db.get_db`, so every write is checked twice: once in the page
the reviewer is sent to and once in the database. That is the point of the module,
because the two failure modes worth catching are a page that reports a change
nothing stored and a row that changed while the page says nothing did.

Three conventions hold throughout, and each one keeps the module honest:

* **Every expected value is derived.** Task titles, statuses, assignee emails and
  deadlines come from :data:`seed.SAMPLE_DATA`; ids are looked up by title rather
  than written down; the project's start date is resolved from
  ``SAMPLE_DATA["project"]["start_offset"]`` against ``date.today()``. A dataset
  change therefore moves the tests with it instead of failing them, and a failure
  means the application and the dataset disagree.
* **The row form's own fields are harvested, not guessed.** A row's status form
  posts the list filters it was rendered under as hidden inputs, and one of those
  filters and the action's own value are both called ``status``. The list-origin
  cases read the hidden inputs out of the rendered form and post exactly those, so
  they exercise the control a reviewer clicks rather than a naming scheme invented
  here.
* **Both rejection paths are asserted.** The data-entry posts -- create and edit --
  answer a bad field with 200 and a message beside it, writing nothing; the quick
  actions answer a value outside their closed set with the 400 page naming the
  parameter. Each is asserted together with the stored row, because a rejection
  that still wrote is the defect neither page alone would reveal.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from html import unescape
from urllib.parse import parse_qs, urlparse

import pytest
from flask import Flask, url_for

import seed
from app import templating
from app.db import get_db
from app.models import Member, STATUS_LABELS, TASK_STATUSES
from app.services import tasks as task_service
from app.services.members import list_members
# The one statement of how the suite spells a month lives in ``tests/conftest.py``;
# this module imports the names from there rather than restating the rule.
from conftest import MONTH_ABBREVIATIONS

# --------------------------------------------------------------------------
# Constants. Anything that is a property of the dataset is derived by the helpers
# below; the values here are the contract's own vocabulary -- a title no seeded
# task holds, the parser's title bound, and the deadline window the indicator and
# the filter share.
# --------------------------------------------------------------------------

# The task the create cases build. It is not one of the seeded titles, which the
# module asserts rather than trusts.
NEW_TASK_TITLE = "Write the launch announcement"
NEW_TASK_DESCRIPTION = "A first draft of the launch note for the mailing list."

# The longest title the parser accepts, and the bound its message quotes.
TITLE_LIMIT = 200

# The longest description the parser accepts. One character past it is what the
# rejected create case posts, to read back app/validation.py's own sentence,
# "Descriptions are limited to 2000 characters."
DESCRIPTION_LIMIT = 2000

# The last day of the "due this week" window the deadline filter and the deadline
# indicator share: today <= deadline <= today + 6. Mirrored here rather than
# imported so the module states the window it expects and a change to it fails a
# test instead of being followed silently.
SOON_WINDOW_DAYS = 6

# The glyph the deadline indicator puts between a date and its relative word: U+00B7,
# a middle dot with one space on each side. Spelled as an escape here rather than
# copied from the application so a separator that changes shows up as a failure.
DEADLINE_SEPARATOR = "\u00b7"

# The three status labels the interface displays, pinned as literals rather than read
# from ``app.models.STATUS_LABELS``. That mapping is what the pages render from and
# what the rest of this module's status assertions compare against, so a typo in it
# would move the page and the expectation together and no test here would fail. These
# three words are the contract's own vocabulary, so they are stated once as text.
STATUS_LABEL_TEXT = {
    "pending": "Pending",
    "in_progress": "In progress",
    "completed": "Completed",
}

# An id no task and no member holds. The routes parse ids as integers, so the value
# only has to be one nothing was inserted with.
UNKNOWN_ID = 9999

# Member ids no row could hold: one past the range SQLite's signed 64-bit key stores,
# and one long enough that converting it to an integer would raise rather than yield
# an id to look up. Neither may reach the database as a bind, and neither may leave a
# row changed.
OVER_RANGE_ASSIGNEE_IDS = (
    pytest.param("9" * 21, id="past-the-signed-64-bit-range"),
    pytest.param("9" * 4301, id="past-the-integer-digit-limit"),
)

# The largest id a SQLite INTEGER column can store, and the first one it cannot.
# Werkzeug's ``<int:>`` converter accepts any run of digits, so a route can be handed
# an id no row id could ever hold; the smaller value is in range and reaches the
# query, which is what keeps the boundary between the two cases visible.
SQLITE_INTEGER_MAX = 2**63 - 1
SQLITE_INTEGER_OVERFLOW = 2**63

# A deadline filter value outside the four the control offers, used to prove a
# carried value is re-validated rather than ignored.
UNKNOWN_DEADLINE_FILTER = "yesterday"

# The tag shapes the HTML helpers read. Attribute order is never assumed: each
# tag's attributes are parsed into a mapping first.
_ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')
_FORM = re.compile(r"<form\b[^>]*>.*?</form>", re.DOTALL | re.IGNORECASE)
_INPUT = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_OPTION = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.DOTALL | re.IGNORECASE)
_SELECTED = re.compile(r"(?:^|\s)selected(?:\s|$)")
_LABEL = re.compile(r"<label\b([^>]*)>(.*?)</label>", re.DOTALL | re.IGNORECASE)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL | re.IGNORECASE)

# The deadline indicator's span, with whatever state class modifiers it carries. One
# pattern serves both the row helper and the detail page, so neither reads its text by
# guessing at the surrounding markup.
_DEADLINE_SPAN = re.compile(r'<span class="deadline[^"]*">(.*?)</span>', re.DOTALL)


# --------------------------------------------------------------------------
# Reading the seeded dataset and the rows the views write.
# --------------------------------------------------------------------------


def query_one(
    app: Flask, statement: str, parameters: tuple[object, ...] = ()
) -> sqlite3.Row | None:
    """Run one statement in the application's context and return its row.

    Args:
        app: The application whose database is read -- the same database the
            ``seeded_client`` fixture seeded and the views write to.
        statement: The SQL to run, with any value bound rather than interpolated.
        parameters: The bound parameters, in statement order.

    Returns:
        The first ``sqlite3.Row`` the statement produced, read by column name, or
        ``None`` when it produced no row.
    """
    with app.app_context():
        return get_db().execute(statement, parameters).fetchone()


def task_count(app: Flask) -> int:
    """Return how many task rows the database currently holds."""
    return int(query_one(app, "SELECT COUNT(*) AS total FROM task")["total"])


def task_row(app: Flask, task_id: int) -> sqlite3.Row:
    """Return one stored task row, by column name.

    Raises:
        AssertionError: If no task has that id, which would mean the case asserts
            against a row that was never written.
    """
    row = query_one(app, "SELECT * FROM task WHERE id = ?", (task_id,))
    assert row is not None, f"no task row with id {task_id}"
    return row


def task_id_for(app: Flask, title: str) -> int:
    """Return the id of the stored task whose title is ``title``."""
    row = query_one(app, "SELECT id FROM task WHERE title = ?", (title,))
    assert row is not None, f"no task row titled {title!r}"
    return int(row["id"])


def project_row_id(app: Flask) -> int:
    """Return the id of the single project row."""
    row = query_one(app, "SELECT id FROM project")
    assert row is not None, "the seeded database holds no project row"
    return int(row["id"])


def member_for_email(app: Flask, email: str) -> Member:
    """Return the stored member whose email is ``email``.

    The member service is the authority on which members exist, so the ids the
    cases post are the ids the application itself offers in its selects.
    """
    with app.app_context():
        members = {member.email: member for member in list_members()}
    assert email in members, f"no member row with email {email!r}"
    return members[email]


def find_seeded_task(predicate, *, description: str) -> dict:
    """Return the first seeded task satisfying ``predicate``.

    Args:
        predicate: Called with each ``SAMPLE_DATA["tasks"]`` entry.
        description: What the case needs, in words, for the failure message.

    Returns:
        The matching entry of :data:`seed.SAMPLE_DATA`.

    Raises:
        AssertionError: If the dataset holds no such task, which is a gap in the
            dataset rather than a defect in the application.
    """
    for task in seed.SAMPLE_DATA["tasks"]:
        if predicate(task):
            return task
    raise AssertionError(f"SAMPLE_DATA holds no task {description}")


def seeded_task_in_status(status: str) -> dict:
    """Return a seeded task whose status is ``status``."""
    return find_seeded_task(
        lambda task: task["status"] == status,
        description=f"with status {status!r}",
    )


def seeded_unassigned_task() -> dict:
    """Return the seeded task nobody is assigned to.

    This is the row that proves the "Unassigned" rendering and the assign-and-clear
    round trip.
    """
    return find_seeded_task(
        lambda task: task["assignee"] is None,
        description="without an assignee",
    )


def seeded_assigned_task() -> dict:
    """Return a seeded task that does have an assignee."""
    return find_seeded_task(
        lambda task: task["assignee"] is not None,
        description="with an assignee",
    )


def seeded_task_without_deadline() -> dict:
    """Return a seeded task carrying no deadline at all."""
    return find_seeded_task(
        lambda task: task["deadline_offset"] is None,
        description="without a deadline",
    )


def seeded_task_with_deadline_state(state: str) -> dict:
    """Return a seeded task whose deadline renders as ``state``.

    The selection mirrors the deadline indicator's own rule: finished work carries
    no state, and the three states are a past deadline, today, and the seven-day
    window that starts today.

    Args:
        state: ``"overdue"``, ``"today"`` or ``"soon"``.

    Raises:
        ValueError: If ``state`` is not one of the three.
    """

    def matches(task: dict) -> bool:
        offset = task["deadline_offset"]
        if task["status"] == "completed" or offset is None:
            return False
        if state == "overdue":
            return offset < 0
        if state == "today":
            return offset == 0
        if state == "soon":
            return 0 < offset <= SOON_WINDOW_DAYS
        raise ValueError(f"Unknown deadline state: {state!r}")

    return find_seeded_task(
        matches, description=f"whose deadline renders as {state!r}"
    )


def seeded_completed_task_with_a_past_deadline() -> dict:
    """Return a finished task whose deadline has passed.

    Finished work is not late, so this is the row that must never read as overdue.
    """
    return find_seeded_task(
        lambda task: task["status"] == "completed"
        and task["deadline_offset"] is not None
        and task["deadline_offset"] < 0,
        description="that is completed with a past deadline",
    )


def iso_offset(offset: int) -> str:
    """Return ``offset`` days from today as the ``YYYY-MM-DD`` text the app stores."""
    return (date.today() + timedelta(days=offset)).isoformat()


def offset_rendered(offset: int) -> str:
    """Return the day ``offset`` days from today as the interface renders a date.

    The interface shows a date as ``26 Oct 2026`` -- an unpadded day, a three-letter
    English month and a four-digit year -- which is what this reproduces from the day
    alone, without asking ``app.templating`` to format it for the test asserting it.
    """
    day = date.today() + timedelta(days=offset)
    return f"{day.day} {MONTH_ABBREVIATIONS[day.month - 1]} {day.year}"


def relative_word(offset: int) -> str:
    """Return the relative word a deadline ``offset`` days from today renders.

    The three wordings are the interface's own: past work reads ``Overdue``, today's
    reads ``Due today``, and a deadline inside the coming week states how far away it
    is as ``Due in N days``, with the plural standing at one day. The word is derived
    from the offset rather than written down, so a dataset change moves it.
    """
    if offset < 0:
        return "Overdue"
    if offset == 0:
        return "Due today"
    return f"Due in {offset} days"


def deadline_indicator(offset: int) -> str:
    """Return the whole indicator an unfinished deadline ``offset`` days out renders.

    The date, the separator and the word are one string, which is what makes the list
    and the detail page comparable character for character.
    """
    return f"{offset_rendered(offset)} {DEADLINE_SEPARATOR} {relative_word(offset)}"


def project_start_iso() -> str:
    """Return the seeded project's start date as stored ISO-8601 text."""
    return iso_offset(seed.SAMPLE_DATA["project"]["start_offset"])


def project_end_iso() -> str:
    """Return the seeded project's end date as stored ISO-8601 text."""
    return iso_offset(seed.SAMPLE_DATA["project"]["end_offset"])


def rendered_date(iso_text: str) -> str:
    """Return a stored ``YYYY-MM-DD`` date in the shape the interface renders it.

    The page shows a date as ``26 Oct 2026`` -- an unpadded day, a three-letter month
    and a four-digit year -- which is what this reproduces from the stored text alone.
    The month comes from ``MONTH_ABBREVIATIONS``, the English names
    ``tests/conftest.py`` states once, never from ``strftime('%b')``, which would
    follow the host's locale.
    The ``format_date`` filter the page calls is deliberately not used: a case that
    asked the application to format the value would confirm that filter against itself
    and prove nothing about what the page shows.
    """
    day = date.fromisoformat(iso_text)
    return f"{day.day} {MONTH_ABBREVIATIONS[day.month - 1]} {day.year}"


def valid_task_form(app: Flask) -> dict[str, str]:
    """Return a submission the create form accepts, as the form would post it.

    Every value is derived: the assignee is a member the service reports, and the
    deadline is the project's own start date, which the deadline rule accepts
    because it is not earlier than itself. A case overrides only the field it is
    about, so a rejection can only be caused by that field.
    """
    member = member_for_email(app, seed.SAMPLE_DATA["members"][0]["email"])
    return {
        "title": NEW_TASK_TITLE,
        "description": NEW_TASK_DESCRIPTION,
        "assignee_id": str(member.id),
        "status": "pending",
        "deadline": project_start_iso(),
    }


def task_screen_path(app, screen: str) -> str:
    """Return the path of one of the four task screens.

    Args:
        app: The application whose database holds the task the two id-bearing
            screens open on.
        screen: ``"list"``, ``"create"``, ``"detail"`` or ``"edit"``.

    Returns:
        The path of that screen. The id ``detail`` and ``edit`` need is looked up
        through :func:`task_id_for`, so no task id is written down here.

    Raises:
        ValueError: If ``screen`` is not one of the four.
    """
    if screen == "list":
        return "/tasks"
    if screen == "create":
        return "/tasks/new"
    task_id = task_id_for(app, seeded_task_in_status("pending")["title"])
    if screen == "detail":
        return f"/tasks/{task_id}"
    if screen == "edit":
        return f"/tasks/{task_id}/edit"
    raise ValueError(f"Unknown task screen: {screen!r}")


def task_form_url(app, screen: str) -> str:
    """Return the read path of one of the two task-form screens.

    The edit form needs a stored task, so the path is built from a seeded row's own
    id rather than written down, and both screens are reached through the path the
    contract gives them.

    Args:
        app: The application whose database is read.
        screen: ``"create"`` for the empty form, ``"edit"`` for a stored task's.
    """
    if screen == "create":
        return "/tasks/new"
    task = seeded_task_in_status("pending")
    return f"/tasks/{task_id_for(app, task['title'])}/edit"


def task_form_action(app, screen: str) -> str:
    """Return the URL the task form on ``screen`` must post to.

    ``_method="POST"`` asks the endpoint for the rule that accepts a write, which is
    the rule whose handler stores the form, so the expected action is built by the
    application's own URL builder rather than written down and a renamed route takes
    the expectation with it. That builder needs a request context to produce a
    relative path, which is why one is pushed for the call.

    Args:
        app: The application whose URL map is asked, and whose database holds the
            task the edit form posts to.
        screen: ``"create"`` for the empty form, ``"edit"`` for a stored task's.

    Returns:
        The path the rendered form's ``action`` must declare.

    Raises:
        ValueError: If ``screen`` is not one of the two ``TASK_FORM_SCREENS``.
    """
    if screen not in TASK_FORM_SCREENS:
        raise ValueError(f"Unknown task form screen: {screen!r}")
    task_id = (
        task_id_for(app, seeded_task_in_status("pending")["title"])
        if screen == "edit"
        else None
    )
    with app.test_request_context():
        if task_id is None:
            return url_for("tasks.create", _method="POST")
        return url_for("tasks.edit", task_id=task_id, _method="POST")


# --------------------------------------------------------------------------
# Reading the rendered page. These helpers exist so no case has to guess a field
# name, assume an attribute order or compare a whole HTML block.
# --------------------------------------------------------------------------


def tag_attributes(body: str, tag: str, element_id: str) -> dict:
    """Return the attributes of the ``tag`` element carrying ``element_id``.

    Args:
        body: The rendered page.
        tag: The element name, such as ``"input"`` or ``"select"``.
        element_id: The value of the element's ``id`` attribute.

    Returns:
        The element's attributes as a mapping, so a case reads ``value`` or ``type``
        by name instead of matching markup in a fixed order.

    Raises:
        AssertionError: If the page holds no such element.
    """
    pattern = re.compile(
        rf'<{tag}\b[^>]*\bid="{re.escape(element_id)}"[^>]*>', re.IGNORECASE
    )
    match = pattern.search(body)
    assert match is not None, f"no <{tag} id={element_id!r}> in the page"
    return dict(_ATTRIBUTE.findall(match.group(0)))


def label_text(body: str, control_id: str) -> str:
    """Return the text of the ``<label>`` that points at one control.

    A label is tied to its control by the ``for`` attribute and by nothing else, so
    this reads the label whose ``for`` names ``control_id`` rather than the first
    label the page happens to render.

    Args:
        body: The rendered page.
        control_id: The value of the labelled control's ``id`` attribute.

    Returns:
        The label's text, unescaped and stripped of surrounding whitespace, so an
        empty return is a label that names the control but says nothing.

    Raises:
        AssertionError: If no label names that control, which leaves the control
            with no accessible name at all.
    """
    for attributes, text in _LABEL.findall(body):
        if dict(_ATTRIBUTE.findall(attributes)).get("for") == control_id:
            return unescape(text).strip()
    raise AssertionError(f"no <label for={control_id!r}> in the page")


def current_links(body: str) -> list[str]:
    """Return the text of every anchor the page marks ``aria-current="page"``.

    The navigation states which of its three sections the visitor is in, so the
    caller asserts both the mark's presence and that exactly one anchor carries it.

    Args:
        body: The rendered page.

    Returns:
        The marked anchors' text, unescaped and stripped, in document order.
    """
    return [
        unescape(text).strip()
        for attributes, text in _ANCHOR.findall(body)
        if dict(_ATTRIBUTE.findall(attributes)).get("aria-current") == "page"
    ]


def input_value(body: str, element_id: str) -> str:
    """Return the ``value`` an input was rendered with, unescaped."""
    return unescape(tag_attributes(body, "input", element_id).get("value", ""))


def textarea_value(body: str, element_id: str) -> str:
    """Return the text a textarea was rendered with, unescaped.

    Included beside :func:`input_value` because a value carried in an element's body
    cannot be read from a ``value`` attribute, and the task form's description is a
    textarea.
    """
    pattern = re.compile(
        rf'<textarea\b[^>]*\bid="{re.escape(element_id)}"[^>]*>(.*?)</textarea>',
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(body)
    assert match is not None, f"no <textarea id={element_id!r}> in the page"
    return unescape(match.group(1))


def selected_option_text(body: str, select_id: str) -> str:
    """Return the text of the option a select renders as selected.

    Args:
        body: The rendered page.
        select_id: The value of the select's ``id`` attribute.

    Returns:
        The selected option's label, with surrounding whitespace removed.

    Raises:
        AssertionError: If the page holds no such select, or the select marks no
            option as selected.
    """
    pattern = re.compile(
        rf'<select\b[^>]*\bid="{re.escape(select_id)}"[^>]*>.*?</select>',
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(body)
    assert match is not None, f"no <select id={select_id!r}> in the page"
    for attributes, label in _OPTION.findall(match.group(0)):
        if _SELECTED.search(attributes):
            return unescape(label).strip()
    raise AssertionError(f"the <select id={select_id!r}> marks no option selected")


def form_markup(body: str, action_fragment: str) -> str:
    """Return the markup of the form whose action contains ``action_fragment``.

    Args:
        body: The rendered page.
        action_fragment: Part of the form's ``action`` URL, such as
            ``"/tasks/<id>/status"`` for a row's status form, which picks one
            control out of a page holding several forms.

    Returns:
        The whole ``<form>...</form>`` block.

    Raises:
        AssertionError: If no form's action contains the fragment.
    """
    for block in _FORM.findall(body):
        attributes = dict(_ATTRIBUTE.findall(block.split(">", 1)[0]))
        if action_fragment in attributes.get("action", ""):
            return block
    raise AssertionError(f"no form posting to {action_fragment!r} in the page")


def opening_tag(body: str, marker: str) -> str:
    """Return the opening tag of ``body`` that carries the literal ``marker``.

    A form or a control is named by a slice of its own markup -- the action it posts
    to, or its ``id`` -- rather than by the position it occupies, so a case reads as
    the element it asserts on. The match stops at the tag's own ``>``, so one
    element's attribute is never read off the next: the task form holds five labelled
    fields and only the tag the marker names is returned.

    Args:
        body: The rendered page.
        marker: The literal markup identifying the element, such as
            ``'action="/tasks"'`` or ``'id="title"'``.

    Returns:
        The element's opening tag, which is what a boolean attribute has to be read
        from: :func:`tag_attributes` maps only the attributes written with a value.

    Raises:
        AssertionError: If no opening tag carries the marker.
    """
    match = re.search(rf"<[^>]*{re.escape(marker)}[^>]*>", body)
    assert match is not None, f"no opening tag carries {marker!r}"
    return match.group(0)


def declares_attribute(tag: str, name: str) -> bool:
    """Return whether the opening tag ``tag`` declares the attribute ``name``.

    A boolean attribute is written with no value at all, so it is absent from the
    quoted-value mapping :func:`tag_attributes` returns and has to be read off the tag
    itself. The name must stand as a whole word inside the tag, which is what tells a
    control's ``required`` apart from any longer attribute containing those letters.

    Args:
        tag: An element's opening tag.
        name: The attribute's name.

    Returns:
        Whether the tag declares it.
    """
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", tag) is not None


def hidden_inputs(body: str, action_fragment: str) -> dict:
    """Return the hidden ``name``/``value`` pairs of the form posting to a path.

    This is how a list-origin quick action is exercised: the values posted are the
    ones the rendered row form carries. A filter the form carries as the empty
    string stays empty, which is what the "All" option submits and what the parser
    reads as "not applied".

    Args:
        body: The rendered page.
        action_fragment: Part of the form's ``action`` URL.

    Returns:
        A mapping of hidden field name to its rendered value, unescaped.

    Raises:
        AssertionError: If the form holds no hidden input, which would mean the
            control cannot express the filters it is rendered under.
    """
    fields: dict[str, str] = {}
    for tag in _INPUT.findall(form_markup(body, action_fragment)):
        attributes = dict(_ATTRIBUTE.findall(tag))
        if attributes.get("type") != "hidden" or "name" not in attributes:
            continue
        fields[attributes["name"]] = unescape(attributes.get("value", ""))
    assert fields, f"the form posting to {action_fragment!r} carries no hidden input"
    return fields


def table_row(body: str, task_id: int) -> str:
    """Return the task table's row for one task.

    The row is located by the link its title carries, which is the one place the
    row's own URL appears with nothing after it -- the status form's action and the
    Edit link both continue past the id.

    Args:
        body: The rendered task list.
        task_id: The id of the task whose row to return.

    Returns:
        The ``<tr>`` segment for that task, as text.

    Raises:
        AssertionError: If the table holds no row for that task.
    """
    marker = f'/tasks/{task_id}"'
    for segment in body.split("<tr>"):
        if marker in segment:
            return segment
    raise AssertionError(f"the task list holds no row linking to /tasks/{task_id}")


def deadline_span_text(markup: str, *, description: str) -> str:
    """Return the deadline indicator's text inside ``markup``, unescaped.

    The indicator is the markup's only ``.deadline`` span, and it carries the state
    class modifier as well as the words, so reading its text is what lets the list and
    the detail page be compared character for character.

    Args:
        markup: Rendered markup to read -- one task's list row, or a task's Deadline
            definition value from the detail page.
        description: Where the markup came from, for the failure message.

    Returns:
        The span's text, with surrounding whitespace removed.

    Raises:
        AssertionError: If the markup renders no deadline indicator at all.
    """
    match = _DEADLINE_SPAN.search(markup)
    assert match is not None, f"{description} renders no deadline indicator"
    return unescape(match.group(1)).strip()


def definition_value(body: str, term: str) -> str:
    """Return the value a definition list gives the term ``term``.

    The detail view states the task's description, assignee, status and deadline in
    a ``<dl>``, so this reads the row a reviewer reads instead of matching the whole
    block.

    Args:
        body: The rendered page.
        term: The ``<dt>`` text, such as ``"Assignee"``.

    Returns:
        The matching ``<dd>`` as text, with surrounding whitespace removed.

    Raises:
        AssertionError: If the page carries no such definition.
    """
    pattern = re.compile(
        rf"<dt>\s*{re.escape(term)}\s*</dt>\s*<dd>(.*?)</dd>", re.DOTALL
    )
    match = pattern.search(body)
    assert match is not None, f"the page defines no {term!r}"
    return unescape(match.group(1)).strip()


def location_path(response) -> str:
    """Return the path of a redirect's ``Location``, without its query string."""
    return urlparse(response.headers["Location"]).path


def location_query(response) -> dict:
    """Return a redirect's query string as a mapping of parameter to its values."""
    return parse_qs(urlparse(response.headers["Location"]).query)


def assert_redirects_to(response, path: str) -> None:
    """Assert a response is a 302 to ``path``, ignoring any query string."""
    assert response.status_code == 302, response.get_data(as_text=True)[:500]
    assert location_path(response) == path


# --------------------------------------------------------------------------
# The task list.
# --------------------------------------------------------------------------

# The six columns the list's table declares, each a scoped header.
COLUMNS = ("Title", "Assignee", "Status", "Deadline", "Comments", "Actions")


def test_task_list_renders_every_seeded_task(seeded_client, app) -> None:
    """The unfiltered list shows one row per seeded task and counts them.

    Each seeded title is found through the link that opens its detail page and that
    link is counted, so a task rendered twice fails here; the create control is
    counted for the same reason, because this screen carries exactly one.
    """
    titles = [task["title"] for task in seed.SAMPLE_DATA["tasks"]]

    response = seeded_client.get("/tasks")
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert f"<p>{len(titles)} tasks</p>" in body
    for title in titles:
        assert title in body, f"the list omits the seeded task {title!r}"
        task_id = task_id_for(app, title)
        assert body.count(f'<a href="/tasks/{task_id}">') == 1, (
            f"the list does not link the seeded task {title!r} exactly once"
        )
    assert body.count("Create task") == 1


@pytest.mark.parametrize("column", COLUMNS)
def test_task_list_columns_are_scoped_headers(seeded_client, column: str) -> None:
    """Every column header is a ``<th scope="col">``, which is what ties a cell to
    its column for a screen reader."""
    body = seeded_client.get("/tasks").get_data(as_text=True)

    pattern = re.compile(rf'<th\b[^>]*scope="col"[^>]*>\s*{column}\s*</th>')
    assert pattern.search(body) is not None, f"{column!r} is not a scoped header"


def test_filtered_task_list_counts_its_matches(seeded_client) -> None:
    """A filtered list says how many of the whole table matched."""
    status = "completed"
    matches = [task for task in seed.SAMPLE_DATA["tasks"] if task["status"] == status]
    total = len(seed.SAMPLE_DATA["tasks"])

    response = seeded_client.get("/tasks", query_string={"status": status})
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert f"<p>{len(matches)} of {total} tasks match these filters</p>" in body
    for task in matches:
        assert task["title"] in body


def test_unseeded_task_list_offers_to_create_the_first_task(client) -> None:
    """With no task at all the list replaces its table with the empty state, and
    that state carries the screen's only create control."""
    response = client.get("/tasks")
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert "No tasks yet." in body
    assert body.count("Create task") == 1
    assert "<table" not in body


# The three task screens the navigation covers: the list, the create form and one
# task's detail view. Every one of them is a ``tasks.*`` endpoint, which is what the
# shell reads to decide which link it marks.
TASK_SCREENS = ("list", "create", "detail")


@pytest.mark.parametrize("screen", TASK_SCREENS)
def test_the_task_screens_mark_the_tasks_nav_link_current(
    seeded_client, app, screen: str
) -> None:
    """Every task screen marks the Tasks link in the navigation, and only that link.

    The mark is how the shell says which of its three sections the visitor is in, so
    a page carrying none leaves the navigation without a position and a page carrying
    two answers the question with a contradiction. The detail screen's id is looked up
    rather than written down, so all three screens are read on the seeded dataset.
    """
    body = seeded_client.get(task_screen_path(app, screen)).get_data(as_text=True)

    assert current_links(body) == ["Tasks"]


# --------------------------------------------------------------------------
# Creating a task.
# --------------------------------------------------------------------------


def test_create_form_defaults_the_status_to_pending(seeded_client) -> None:
    """A new task starts Pending, and starts unassigned."""
    response = seeded_client.get("/tasks/new")
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert "New task" in body
    assert selected_option_text(body, "status") == STATUS_LABELS["pending"]
    assert selected_option_text(body, "assignee_id") == "Unassigned"


# The five controls the task form declares, each paired with the element that renders
# it. The id is the field name app.validation.parse_task_form reads, and it is also
# what the label's ``for`` and every ``<field>-error`` id are built from.
TASK_FORM_CONTROLS = (
    ("title", "input"),
    ("description", "textarea"),
    ("assignee_id", "select"),
    ("status", "select"),
    ("deadline", "input"),
)

# The element each field name is rendered by, for the assertion that flags a control.
TASK_FORM_CONTROL_TAGS = dict(TASK_FORM_CONTROLS)

# The two screens one template serves, so a label that renders on the create form but
# not on the edit form cannot pass.
TASK_FORM_SCREENS = ("create", "edit")


@pytest.mark.parametrize("screen", TASK_FORM_SCREENS)
@pytest.mark.parametrize(("control_id", "tag"), TASK_FORM_CONTROLS)
def test_task_form_labels_every_control(
    seeded_client, app, screen: str, control_id: str, tag: str
) -> None:
    """Every control on the task form carries a visible, non-empty label.

    ``GET /tasks/new`` and ``GET /tasks/<id>/edit`` render the same template, so both
    are read, with the edit screen's id looked up rather than written down. The label
    is tied to its control by ``for`` alone, and the control posts under the field name
    the parser reads, so a label pointing at a wrong id, or at nothing, fails here.
    """
    response = seeded_client.get(task_screen_path(app, screen))
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert label_text(body, control_id), f"{control_id} has an empty label"
    assert tag_attributes(body, tag, control_id).get("name") == control_id


@pytest.mark.parametrize("screen", TASK_FORM_SCREENS)
def test_the_task_form_posts_its_write_rule_and_marks_its_required_fields(
    seeded_client, app, screen: str
) -> None:
    """Both form screens post the endpoint's write rule, not the path they were read on.

    The action is built in the template and nowhere else, and every case that saves
    posts to the contract path directly, so nothing else in this module would notice
    an action that reverted to the form's own read path: from a browser the save
    would answer 405 -- a post to a path only the read rule serves -- while the whole
    suite stayed green. The expected action is the application's own build of the
    endpoint's write rule, and the marker carries the attribute's closing quote, so
    ``/tasks/new`` cannot satisfy the create form's ``/tasks``.

    ``novalidate`` and the title's ``required`` are asserted as a pair, for the reason
    the comment form's cases give: ``required`` tells a screen-reader user up front
    what app.validation refuses, and only ``novalidate`` lets a submission that
    bypasses it reach the parser's own sentence beside the field instead of the
    browser's bubble.
    """
    action = task_form_action(app, screen)

    response = seeded_client.get(task_form_url(app, screen))
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    form = opening_tag(body, f'action="{action}"')
    assert declares_attribute(form, "novalidate"), form

    title = opening_tag(body, 'id="title"')
    assert declares_attribute(title, "required"), title


# The two screens that render the task form, by the read path each is served on.
FORM_SCREENS = ("create", "edit")


@pytest.mark.parametrize("screen", FORM_SCREENS)
def test_a_head_request_answers_the_form_a_get_renders(
    seeded_client, app, screen: str
) -> None:
    """A HEAD probe takes the read branch and carries the GET's own headers.

    Flask adds HEAD to every GET rule, which is why the two form views branch on the
    write method rather than on GET: an equal Content-Length is what proves the
    probe did not fall through to the write branch, whose re-render of the form is a
    different and longer page.
    """
    url = task_form_url(app, screen)

    get = seeded_client.get(url)
    head = seeded_client.head(url)

    assert get.status_code == 200
    assert head.status_code == 200
    assert head.headers["Content-Length"] == get.headers["Content-Length"]
    assert head.get_data() == b""


@pytest.mark.parametrize("screen", FORM_SCREENS)
def test_a_form_path_accepts_no_write(seeded_client, app, screen: str) -> None:
    """Each endpoint has one write rule, and it is not the form's own path.

    The create form posts to ``POST /tasks`` and the edit form to
    ``POST /tasks/<id>``, so a post to either read path is a method that rule does
    not offer -- the same read path is not a second write door -- and no row is
    written.
    """
    before = task_count(app)

    response = seeded_client.post(
        task_form_url(app, screen), data=valid_task_form(app)
    )

    assert response.status_code == 405
    assert task_count(app) == before


# The three task actions that answer a form post and no read request, by the path
# segment each is served on.
WRITE_ONLY_ACTION_PATHS = ("status", "assign", "comments")


@pytest.mark.parametrize("action", WRITE_ONLY_ACTION_PATHS)
def test_a_read_of_a_write_only_action_lands_on_its_task(
    seeded_client, app, action: str
) -> None:
    """A GET of a write-only action answers with the task page, not a bare 405.

    A rejected data-entry post re-renders its own form at 200, so the address bar
    keeps the path it posted to. A read of that path -- a bookmark, a hand-typed URL,
    a reload whose resubmission was cancelled -- is sent to the task the action
    belongs to, inside the application's shell, rather than to Werkzeug's own 405
    page, which names neither the task nor a way back.
    """
    task = seeded_task_in_status("pending")
    task_id = task_id_for(app, task["title"])

    response = seeded_client.get(f"/tasks/{task_id}/{action}")

    assert_redirects_to(response, f"/tasks/{task_id}")


def test_a_method_mismatch_outside_the_task_actions_keeps_the_405(app, client) -> None:
    """Only the three write-only task actions are redirected; anything else is 405.

    The redirect exists because those paths are what a rejected post leaves in the
    address bar. Answering every method mismatch with a redirect would promise a
    route the contract does not serve, so a post-only path elsewhere keeps the
    default response, and so does a path that offers neither method at all.
    """

    @app.route("/probe/<int:task_id>/post-only", methods=["POST"])
    def probe_post_only(task_id):
        return "written"

    @app.route("/probe/put-only", methods=["PUT"])
    def probe_put_only():
        return "written"

    assert client.get("/probe/1/post-only").status_code == 405
    assert client.get("/probe/put-only").status_code == 405


def test_create_task_writes_the_row_and_confirms_it(seeded_client, app) -> None:
    """A valid submission writes every posted value and lands on the new task."""
    form = valid_task_form(app)
    before = task_count(app)

    response = seeded_client.post("/tasks", data=form)

    created_id = task_id_for(app, form["title"])
    assert_redirects_to(response, f"/tasks/{created_id}")
    assert task_count(app) == before + 1

    row = task_row(app, created_id)
    assert row["title"] == form["title"]
    assert row["description"] == form["description"]
    assert int(row["assignee_id"]) == int(form["assignee_id"])
    assert row["status"] == form["status"]
    assert row["deadline"] == form["deadline"]
    assert int(row["project_id"]) == project_row_id(app)

    page = seeded_client.get(response.headers["Location"])
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Task created." in body
    assert form["title"] in body


# One rejecting submission per rule the create form enforces, each paired with the
# message the parser renders beside the offending field.
REJECTED_CREATE_CASES = (
    pytest.param({"title": "   "}, "Enter a task title.", id="blank-title"),
    pytest.param(
        {"title": "T" * (TITLE_LIMIT + 1)},
        "Task titles are limited to 200 characters.",
        id="title-too-long",
    ),
    pytest.param(
        {"description": "D" * (DESCRIPTION_LIMIT + 1)},
        "Descriptions are limited to 2000 characters.",
        id="description-too-long",
    ),
    pytest.param({"status": "done"}, "Select a valid status.", id="unknown-status"),
    pytest.param(
        {"assignee_id": str(UNKNOWN_ID)},
        "Select a team member from the list.",
        id="unknown-member",
    ),
    pytest.param(
        {"deadline": "not-a-date"},
        "Enter a valid date (YYYY-MM-DD).",
        id="unreadable-deadline",
    ),
)


@pytest.mark.parametrize(("overrides", "message"), REJECTED_CREATE_CASES)
def test_rejected_create_rerenders_with_its_message(
    seeded_client, app, overrides: dict, message: str
) -> None:
    """A rejected create answers 200 with the field's message and writes nothing.

    The message is asserted as the whole paragraph a screen reader reaches, not as a
    bare sentence: partials/_field_errors.html gives it the ``<field>-error`` id, the
    flagged control points at that id with ``aria-describedby`` and states
    ``aria-invalid="true"``, and the control the submission left alone keeps neither
    attribute. The flag therefore cannot drift from the message it describes.

    The submission the case posts is kept in ``data`` so the description read back out
    of the textarea is the one this case sent: the fixed description for the cases that
    leave it alone, and the over-long one for the case about the description bound.
    """
    before = task_count(app)
    flagged = next(iter(overrides))
    data = {**valid_task_form(app), **overrides}

    response = seeded_client.post("/tasks", data=data)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert message in body
    assert f'<p class="field-error" id="{flagged}-error">{message}</p>' in body
    assert task_count(app) == before
    # The rest of the form comes back filled in, so the reviewer corrects one field
    # instead of retyping the submission.
    assert textarea_value(body, "description") == data["description"]

    described = tag_attributes(body, TASK_FORM_CONTROL_TAGS[flagged], flagged)
    assert described.get("aria-invalid") == "true"
    assert f"{flagged}-error" in described.get("aria-describedby", "").split()
    if flagged != "description":
        untouched = tag_attributes(body, "textarea", "description")
        assert "aria-invalid" not in untouched
        assert "aria-describedby" not in untouched


@pytest.mark.parametrize("assignee_id", OVER_RANGE_ASSIGNEE_IDS)
def test_create_rejects_an_assignee_id_no_member_row_could_hold(
    seeded_client, app, assignee_id: str
) -> None:
    """An assignee id past the key column is a field message, and nothing is written.

    The create form is a data-entry control, so a value naming no member is reported
    beside the field with the form still filled in -- the same outcome a shorter
    unknown id gets, however long the submitted run is.
    """
    before = task_count(app)

    response = seeded_client.post(
        "/tasks", data={**valid_task_form(app), "assignee_id": assignee_id}
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Select a team member from the list." in body
    assert task_count(app) == before
    assert textarea_value(body, "description") == NEW_TASK_DESCRIPTION


def test_create_rejects_a_deadline_before_the_project_start(seeded_client, app) -> None:
    """A deadline may not precede the project's start date."""
    before = task_count(app)
    start = date.fromisoformat(project_start_iso())
    deadline = (start - timedelta(days=1)).isoformat()

    response = seeded_client.post(
        "/tasks", data={**valid_task_form(app), "deadline": deadline}
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Deadline cannot be earlier than the project start date." in body
    assert task_count(app) == before


def test_the_deadline_field_is_described_by_its_hint(seeded_client, app) -> None:
    """The deadline's hint is part of the control's description on every render.

    The hint is the only place the field's format is stated, so it is wired with
    ``aria-describedby`` even when nothing is wrong -- the state a screen-reader user
    needs before typing rather than after being refused. A rejection prepends the
    error's id and flags the control, and both ids it then names resolve on the page,
    so the message and the hint are each announced with the field they belong to.
    """
    clean = seeded_client.get("/tasks/new").get_data(as_text=True)
    clean_deadline = tag_attributes(clean, "input", "deadline")
    assert clean_deadline.get("aria-describedby") == "deadline-hint"
    assert 'id="deadline-hint"' in clean

    start = date.fromisoformat(project_start_iso())
    deadline = (start - timedelta(days=1)).isoformat()

    response = seeded_client.post(
        "/tasks", data={**valid_task_form(app), "deadline": deadline}
    )

    assert response.status_code == 200
    rejected = response.get_data(as_text=True)
    rejected_deadline = tag_attributes(rejected, "input", "deadline")
    assert rejected_deadline.get("aria-describedby") == "deadline-error deadline-hint"
    assert rejected_deadline.get("aria-invalid") == "true"
    assert 'id="deadline-error"' in rejected
    assert 'id="deadline-hint"' in rejected


# Deadline text that no date input can display, one shape per kind: text that does
# not parse at all, and three forms ``date.fromisoformat`` reads but the product does
# not store -- the basic form, an ISO week date, a date with a time part. The last
# three are what makes the echo's own condition worth testing: the parser rejects them
# by their shape alone, so the field is both flagged and unable to hold the entry.
UNSHOWABLE_DEADLINES = (
    pytest.param("not-a-date", id="unreadable"),
    pytest.param("20261113", id="basic-form"),
    pytest.param("2026-W43-1", id="iso-week-date"),
    pytest.param("2026-11-13 14:00", id="date-with-a-time-part"),
)


@pytest.mark.parametrize("deadline", UNSHOWABLE_DEADLINES)
def test_a_rejected_deadline_is_echoed_beside_the_field(
    seeded_client, app, deadline: str
) -> None:
    """A deadline the date control cannot display comes back stated beside the field.

    HTML defines a date input's value as empty unless it is a valid date string, so the
    submitted text a rejection carries reaches the control and renders blank -- the one
    field of the five whose entry a re-render would otherwise lose. The form therefore
    states the entry back in its own paragraph, gives that paragraph an id, and adds the
    id to the control's ``aria-describedby`` after the message, so what was typed is
    still on the page and announced with the field it belongs to.

    The value attribute is asserted as well: it is left empty for this text, because a
    date input is required to ignore a value it cannot display, so the paragraph is the
    one place the entry is carried instead of an attribute the control would discard.
    """
    before = task_count(app)
    data = {**valid_task_form(app), "deadline": deadline}

    response = seeded_client.post("/tasks", data=data)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert task_count(app) == before
    assert input_value(body, "deadline") == ""

    echoed = re.search(r'<p id="deadline-submitted">(.*?)</p>', body, re.DOTALL)
    assert echoed is not None, "the rejected deadline was not echoed beside the field"
    assert deadline in unescape(echoed.group(1))

    described = tag_attributes(body, "input", "deadline")
    assert (
        described.get("aria-describedby")
        == "deadline-error deadline-submitted deadline-hint"
    )
    assert described.get("aria-invalid") == "true"


def test_create_accepts_a_deadline_after_the_project_end(seeded_client, app) -> None:
    """The project's end date describes the project, not a limit on its tasks."""
    end = date.fromisoformat(project_end_iso())
    deadline = (end + timedelta(days=1)).isoformat()

    response = seeded_client.post(
        "/tasks", data={**valid_task_form(app), "deadline": deadline}
    )

    assert response.status_code == 302
    row = task_row(app, task_id_for(app, NEW_TASK_TITLE))
    assert row["deadline"] == deadline


def test_task_can_be_created_before_the_project_details_exist(client, app) -> None:
    """Only a title is required, and a task may exist before the project record."""
    title = "Book the launch review"

    response = client.post("/tasks", data={"title": title})

    assert response.status_code == 302
    row = task_row(app, task_id_for(app, title))
    assert row["title"] == title
    assert row["status"] == "pending"
    assert row["project_id"] is None
    assert row["assignee_id"] is None
    assert row["deadline"] is None


def test_a_task_title_is_escaped_when_rendered(seeded_client, app) -> None:
    """A title holding markup is text on the page, which is the stored-XSS guard."""
    title = "<script>alert(1)</script>"

    response = seeded_client.post(
        "/tasks", data={**valid_task_form(app), "title": title}
    )

    assert response.status_code == 302
    body = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert title not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_a_posted_nul_in_the_title_is_not_stored(seeded_client, app) -> None:
    """A NUL in a posted title is dropped, so the stored title is the accepted one.

    The body is posted as raw bytes, because a NUL cannot survive the form encoding
    the client builds from a mapping. SQLite ends a text value at a NUL for its own
    text functions, so a title kept with one inside would be stored as text no read
    can return in full; the write reports success, so the row must hold the value
    the page then shows.
    """
    accepted_title = "QA nulbyte title"
    before = task_count(app)

    response = seeded_client.post(
        "/tasks",
        data=b"title=QA nul\x00byte title&description=",
        content_type="application/x-www-form-urlencoded",
    )

    created_id = task_id_for(app, accepted_title)
    assert_redirects_to(response, f"/tasks/{created_id}")
    assert task_count(app) == before + 1

    row = task_row(app, created_id)
    assert row["title"] == accepted_title
    stored_length = query_one(
        app, "SELECT length(title) AS chars FROM task WHERE id = ?", (created_id,)
    )
    assert int(stored_length["chars"]) == len(accepted_title)


# --------------------------------------------------------------------------
# The task detail view and the edit form.
# --------------------------------------------------------------------------


def test_the_new_task_title_is_not_a_seeded_title() -> None:
    """The title the write cases create is distinct from every seeded one, so a
    created row can always be found by it."""
    seeded_titles = {task["title"] for task in seed.SAMPLE_DATA["tasks"]}

    assert NEW_TASK_TITLE not in seeded_titles


@pytest.mark.parametrize("status", TASK_STATUSES)
def test_task_detail_shows_the_stored_task(seeded_client, app, status: str) -> None:
    """The detail page heads with the title and shows the task's own status."""
    task = seeded_task_in_status(status)
    task_id = task_id_for(app, task["title"])

    response = seeded_client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"<h1>{task['title']}</h1>" in body
    assert f"badge--{status.replace('_', '-')}" in body
    assert STATUS_LABEL_TEXT[status] in body
    assert definition_value(body, "Description") == task["description"].strip()


# The four controls the task detail page renders, each paired with the element that
# renders it: the update-status select, the assign select, the comment author select
# and the comment body.
DETAIL_CONTROLS = (
    ("status", "select"),
    ("assignee_id", "select"),
    ("author_id", "select"),
    ("body", "textarea"),
)


@pytest.mark.parametrize(("control_id", "tag"), DETAIL_CONTROLS)
def test_task_detail_labels_every_control(
    seeded_client, app, control_id: str, tag: str
) -> None:
    """Every control on the task detail page carries a visible, non-empty label.

    The label's ``for`` is the only thing giving these controls their accessible name,
    because the three action forms on this page are told apart by their own names
    rather than by anything the controls inside them say. The task's id is looked up
    rather than written down, and each control posts under the field name the parser
    reads, so a label pointing at a wrong id, or at nothing, fails here.
    """
    response = seeded_client.get(task_screen_path(app, "detail"))
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    assert label_text(body, control_id), f"{control_id} has an empty label"
    assert tag_attributes(body, tag, control_id).get("name") == control_id


@pytest.mark.parametrize("status", TASK_STATUSES)
def test_task_list_row_shows_the_task_status(seeded_client, app, status: str) -> None:
    """Each row carries a badge whose wording and class match the stored status."""
    task = seeded_task_in_status(status)
    task_id = task_id_for(app, task["title"])

    row = table_row(seeded_client.get("/tasks").get_data(as_text=True), task_id)

    assert f"badge--{status.replace('_', '-')}" in row
    assert STATUS_LABELS[status] in row


def test_unassigned_task_reads_as_unassigned_on_both_views(seeded_client, app) -> None:
    """A task with no assignee says so on the detail page and in its list row."""
    task = seeded_unassigned_task()
    task_id = task_id_for(app, task["title"])

    detail = seeded_client.get(f"/tasks/{task_id}").get_data(as_text=True)
    listing = seeded_client.get("/tasks").get_data(as_text=True)

    assert definition_value(detail, "Assignee") == "Unassigned"
    assert "Unassigned" in table_row(listing, task_id)


def test_task_without_a_deadline_says_so_on_both_views(seeded_client, app) -> None:
    """A null deadline is stated in words rather than left as a blank cell."""
    task = seeded_task_without_deadline()
    task_id = task_id_for(app, task["title"])

    detail = seeded_client.get(f"/tasks/{task_id}").get_data(as_text=True)
    listing = seeded_client.get("/tasks").get_data(as_text=True)

    assert definition_value(detail, "Deadline") == (
        '<span class="deadline">No deadline</span>'
    )
    assert "No deadline" in table_row(listing, task_id)


@pytest.mark.parametrize("state", ("overdue", "today", "soon"))
def test_deadline_indicator_states_its_state_in_words(
    seeded_client, app, state: str
) -> None:
    """The deadline carries both the state's class and the word that says it.

    The word matters as much as the colour: a deadline conveyed by colour alone is
    unreadable to part of the audience. The expected word is the one the seeded task's
    own offset produces, so the "soon" case states the day count the dataset implies
    rather than a number written down here.
    """
    task = seeded_task_with_deadline_state(state)
    task_id = task_id_for(app, task["title"])

    body = seeded_client.get(f"/tasks/{task_id}").get_data(as_text=True)

    assert f"deadline--{state}" in body
    assert relative_word(task["deadline_offset"]) in body


@pytest.mark.parametrize("days_ahead", range(1, SOON_WINDOW_DAYS + 1))
def test_deadline_indicator_counts_the_days_inside_the_soon_window(
    days_ahead: int,
) -> None:
    """Imminent work states how many days away it is, not just that it is imminent.

    The window runs from tomorrow to the sixth day ahead, and every day inside it needs
    its own count: "Due in N days" is what tells a reviewer whether to act now or at
    the end of the week. The wording keeps the plural at one day, so the first day of
    the window reads "Due in 1 days".
    """
    rendered = templating.format_deadline(iso_offset(days_ahead), "pending")

    assert rendered == deadline_indicator(days_ahead)
    assert rendered.endswith(f"Due in {days_ahead} days")


@pytest.mark.parametrize("state", ("overdue", "today", "soon"))
def test_one_deadline_reads_the_same_on_the_list_and_the_detail_page(
    seeded_client, app, state: str
) -> None:
    """A deadline is one component, so both screens word and punctuate it alike.

    The list and the detail page describe the same task for the same reviewer, so a
    date, a separator glyph or a word that differs between them is the drift that makes
    neither screen trustworthy. Both texts are compared with the derived indicator, and
    with each other, which pins the glyph as well as the words.
    """
    task = seeded_task_with_deadline_state(state)
    task_id = task_id_for(app, task["title"])

    listing = seeded_client.get("/tasks").get_data(as_text=True)
    detail = seeded_client.get(f"/tasks/{task_id}").get_data(as_text=True)

    expected = deadline_indicator(task["deadline_offset"])
    listed = deadline_span_text(
        table_row(listing, task_id), description=f"the row for /tasks/{task_id}"
    )
    shown = deadline_span_text(
        definition_value(detail, "Deadline"), description="the detail Deadline value"
    )

    assert listed == expected
    assert shown == expected
    assert listed == shown
    assert "\u2014" not in listed


@pytest.mark.parametrize("deadline", (None, "", "not-a-date"))
def test_deadline_indicator_says_nothing_without_a_usable_date(
    deadline: str | None,
) -> None:
    """A missing or unreadable deadline renders no indicator text at all.

    Both views state that case in their own words ("No deadline") rather than through
    the indicator, so the formatter returning an empty string is what keeps a blank
    date from being rendered as if it were a deadline.
    """
    assert templating.format_deadline(deadline) == ""


def test_the_deadline_reference_day_is_read_once_per_request(app, monkeypatch) -> None:
    """Every deadline on one page is measured against one day.

    The interface states that ``today`` is evaluated once per request, so the state and
    the day count of a rendered indicator always describe the same day: a page rendered
    across midnight cannot label two rows of one list against different days. The
    patched clock steps forward on every read, so a second read inside the same request
    would show up as a different day count in the rendered text.
    """
    days = [date(2026, 11, 13), date(2026, 11, 14), date(2026, 11, 15)]
    reads: list[int] = []

    class SteppingDate(date):
        """A ``date`` whose ``today`` hands out the next day on every read."""

        @classmethod
        def today(cls) -> date:
            reads.append(len(reads))
            return cls.fromordinal(days[min(len(reads) - 1, len(days) - 1)].toordinal())

    class Clock:
        """The parts of the ``datetime`` module the deadline formatters read."""

        date = SteppingDate
        datetime = datetime
        timedelta = timedelta

    monkeypatch.setattr(templating, "datetime", Clock)

    deadline_day = days[0] + timedelta(days=3)

    with app.test_request_context("/tasks"):
        first = templating.format_deadline(deadline_day.isoformat(), "pending")
        second = templating.format_deadline(deadline_day.isoformat(), "pending")

    expected = (
        f"{deadline_day.day} {MONTH_ABBREVIATIONS[deadline_day.month - 1]}"
        f" {deadline_day.year}"
        f" {DEADLINE_SEPARATOR} Due in 3 days"
    )
    assert first == second == expected
    assert len(reads) == 1


def test_a_completed_task_never_reads_as_overdue(seeded_client, app) -> None:
    """A finished task renders its deadline but no overdue state.

    The dataset holds completed tasks whose deadlines have passed, which is the only
    way this rule is visible at all. The page-rendered facts come first -- the response
    is a 200 carrying the task's own heading, and its deadline block shows the stored
    date in the interface's format -- so the case proves the deadline was stored and
    rendered before it proves the overdue wording is withheld; without that, a 404, a
    500 or a detail view with the deadline block removed would satisfy the absences
    alone. The same two facts are then checked on the row the task list renders.
    """
    task = seeded_completed_task_with_a_past_deadline()
    task_id = task_id_for(app, task["title"])
    expected_date = rendered_date(task_row(app, task_id)["deadline"])

    response = seeded_client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"<h1>{task['title']}</h1>" in body
    deadline_markup = definition_value(body, "Deadline")
    assert expected_date in deadline_markup
    assert "deadline--overdue" not in deadline_markup
    assert "Overdue" not in body

    row = table_row(seeded_client.get("/tasks").get_data(as_text=True), task_id)
    assert expected_date in row
    assert "deadline--overdue" not in row
    assert "Overdue" not in row


# Every way of naming a task that does not exist. The id is decided before the
# posted value is read, so each is a 404 rather than a validation message.
UNKNOWN_TASK_REQUESTS = (
    pytest.param("GET", f"/tasks/{UNKNOWN_ID}", None, id="detail"),
    pytest.param("GET", f"/tasks/{UNKNOWN_ID}/edit", None, id="edit-form"),
    pytest.param(
        "POST",
        f"/tasks/{UNKNOWN_ID}",
        {"title": "A task that cannot be saved"},
        id="edit-post",
    ),
    pytest.param(
        "POST",
        f"/tasks/{UNKNOWN_ID}/status",
        {"status": "completed", "origin": "detail"},
        id="status",
    ),
    pytest.param(
        "POST",
        f"/tasks/{UNKNOWN_ID}/assign",
        {"assignee_id": "", "origin": "detail"},
        id="assign",
    ),
)


@pytest.mark.parametrize(("method", "path", "data"), UNKNOWN_TASK_REQUESTS)
def test_an_unknown_task_id_is_a_404(
    seeded_client, method: str, path: str, data
) -> None:
    """An id no task holds reaches the 404 page, whatever the request asks for."""
    response = seeded_client.open(path, method=method, data=data or {})

    assert response.status_code == 404
    assert "Back to the dashboard" in response.get_data(as_text=True)


# The same requests, aimed at an id above SQLite's integer range. An id no row could
# hold is not a task, so it answers exactly like a missing one instead of raising at
# the statement's bind.
OVERSIZED_TASK_REQUESTS = (
    pytest.param("GET", f"/tasks/{SQLITE_INTEGER_OVERFLOW}", None, id="detail"),
    pytest.param(
        "GET", f"/tasks/{SQLITE_INTEGER_OVERFLOW}/edit", None, id="edit-form"
    ),
    pytest.param(
        "POST",
        f"/tasks/{SQLITE_INTEGER_OVERFLOW}",
        {"title": "A task that cannot be saved"},
        id="edit-post",
    ),
    pytest.param(
        "POST",
        f"/tasks/{SQLITE_INTEGER_OVERFLOW}/status",
        {"status": "completed", "origin": "detail"},
        id="status",
    ),
    pytest.param(
        "POST",
        f"/tasks/{SQLITE_INTEGER_OVERFLOW}/assign",
        {"assignee_id": "", "origin": "detail"},
        id="assign",
    ),
)


@pytest.mark.parametrize(("method", "path", "data"), OVERSIZED_TASK_REQUESTS)
def test_an_id_beyond_the_integer_range_is_a_404(
    seeded_client, app, method: str, path: str, data
) -> None:
    """An id SQLite cannot store is a missing task, never a server error.

    The route reads the task before it looks at the body, so the value reaches the
    read: an id outside SQLite's signed 64-bit range has to be answered with the
    same 404 an unknown id gets, because a bind would raise instead and turn the
    request into a 500. The row count is asserted with the page, so a rejection that
    still wrote would fail here.
    """
    count_before = task_count(app)

    response = seeded_client.open(path, method=method, data=data or {})

    assert response.status_code == 404
    assert "Back to the dashboard" in response.get_data(as_text=True)
    assert task_count(app) == count_before


def test_the_largest_representable_id_still_reaches_the_lookup(
    seeded_client,
) -> None:
    """The boundary inside the range is an ordinary unknown id.

    It binds successfully and matches no row, which is what shows the range check
    guards the unrepresentable values only instead of replacing the lookup.
    """
    response = seeded_client.get(f"/tasks/{SQLITE_INTEGER_MAX}")

    assert response.status_code == 404


# The three mutations that bind the id they are handed, each with the arguments it
# needs beyond that id. Every route reads the task before it writes, so no request
# reaches one of these with an id the read did not already accept; calling them
# directly is the only way to exercise the range guard each one holds.
DIRECT_TASK_MUTATIONS = (
    pytest.param(
        lambda task_id: task_service.update_task(task_id, {"title": NEW_TASK_TITLE}),
        id="update_task",
    ),
    pytest.param(
        lambda task_id: task_service.set_status(task_id, "completed"),
        id="set_status",
    ),
    pytest.param(
        lambda task_id: task_service.set_assignee(task_id, None),
        id="set_assignee",
    ),
)


@pytest.mark.parametrize("mutate", DIRECT_TASK_MUTATIONS)
def test_a_mutation_on_an_id_beyond_the_integer_range_writes_nothing(
    app, db_path: str, mutate: Callable[[int], object]
) -> None:
    """A mutation handed an id SQLite cannot store answers ``None`` and writes nothing.

    The route reads the task before it writes, so the guard inside each mutation is
    unreachable over HTTP; a direct call is the only way to prove it is there. Without
    it the id would reach the bind, sqlite3 would raise ``OverflowError`` and the
    ``None`` that leads every caller to its 404 would surface as a server error. The
    row counts are asserted with the result, because a guard that answered ``None``
    after writing is the defect neither check reveals on its own.
    """
    seed.seed_database(db_path)
    tasks_before = task_count(app)
    comments_before = int(
        query_one(app, "SELECT COUNT(*) AS total FROM comment")["total"]
    )

    with app.app_context():
        assert mutate(SQLITE_INTEGER_OVERFLOW) is None

    assert task_count(app) == tasks_before
    assert int(
        query_one(app, "SELECT COUNT(*) AS total FROM comment")["total"]
    ) == comments_before


def test_edit_form_is_prefilled_with_the_stored_values(seeded_client, app) -> None:
    """The edit form opens on the stored task, which is what makes editing it a
    correction rather than a retype."""
    task = find_seeded_task(
        lambda entry: entry["assignee"] is not None
        and entry["deadline_offset"] is not None,
        description="with an assignee and a deadline",
    )
    task_id = task_id_for(app, task["title"])
    member = member_for_email(app, task["assignee"])

    response = seeded_client.get(f"/tasks/{task_id}/edit")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Edit task" in body
    assert input_value(body, "title") == task["title"]
    assert textarea_value(body, "description") == task["description"].strip()
    assert input_value(body, "deadline") == iso_offset(task["deadline_offset"])
    assert selected_option_text(body, "assignee_id") == member.name
    assert selected_option_text(body, "status") == STATUS_LABELS[task["status"]]


def test_edit_writes_the_changed_values(seeded_client, app) -> None:
    """A valid edit stores every posted field and confirms it on the detail page."""
    task = seeded_task_in_status("pending")
    task_id = task_id_for(app, task["title"])
    changed = {
        "title": NEW_TASK_TITLE,
        "description": "Revised scope, agreed with the team.",
        "assignee_id": "",
        "status": "in_progress",
        "deadline": "",
    }

    response = seeded_client.post(f"/tasks/{task_id}", data=changed)

    assert_redirects_to(response, f"/tasks/{task_id}")
    row = task_row(app, task_id)
    assert row["title"] == changed["title"]
    assert row["description"] == changed["description"]
    assert row["status"] == changed["status"]
    assert row["assignee_id"] is None
    assert row["deadline"] is None

    body = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Task updated." in body
    assert changed["title"] in body


def test_rejected_edit_leaves_the_stored_task_unchanged(seeded_client, app) -> None:
    """A rejected edit re-renders the form at 200 and writes nothing."""
    task = seeded_task_in_status("pending")
    task_id = task_id_for(app, task["title"])
    before = task_row(app, task_id)

    response = seeded_client.post(
        f"/tasks/{task_id}",
        data={
            "title": "   ",
            "description": "An edit that must not be saved.",
            "assignee_id": "",
            "status": "completed",
            "deadline": "",
        },
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Enter a task title." in body
    assert "Edit task" in body

    after = task_row(app, task_id)
    assert after["title"] == before["title"]
    assert after["description"] == before["description"]
    assert after["status"] == before["status"]


# --------------------------------------------------------------------------
# Updating a task's status, from the detail view and from the task list.
# --------------------------------------------------------------------------


def test_status_update_from_the_detail_view(seeded_client, app) -> None:
    """A status change from the detail page is stored and confirmed there.

    The detail status select is read back before the action, so a control that never
    pre-selects the task's stored status fails here rather than passing on the post
    alone.
    """
    task = seeded_task_in_status("pending")
    task_id = task_id_for(app, task["title"])

    stored = seeded_client.get(f"/tasks/{task_id}")
    assert stored.status_code == 200
    stored_body = stored.get_data(as_text=True)
    assert selected_option_text(stored_body, "status") == STATUS_LABEL_TEXT["pending"]

    response = seeded_client.post(
        f"/tasks/{task_id}/status", data={"status": "completed", "origin": "detail"}
    )

    assert_redirects_to(response, f"/tasks/{task_id}")
    assert task_row(app, task_id)["status"] == "completed"

    body = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Status updated to Completed." in body
    assert "badge--completed" in body


def test_a_completed_task_can_be_reopened(seeded_client, app) -> None:
    """Transitions are unrestricted: finished work can go back to Pending."""
    task = seeded_task_in_status("completed")
    task_id = task_id_for(app, task["title"])

    response = seeded_client.post(
        f"/tasks/{task_id}/status", data={"status": "pending", "origin": "detail"}
    )

    assert_redirects_to(response, f"/tasks/{task_id}")
    assert task_row(app, task_id)["status"] == "pending"

    body = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert f"Status updated to {STATUS_LABELS['pending']}." in body
    assert "badge--pending" in body


def test_status_update_without_an_origin_returns_to_the_detail_view(
    seeded_client, app
) -> None:
    """A navigation hint the view does not recognise still lands somewhere real: a
    target the view names itself, never one the request supplies."""
    task = seeded_task_in_status("pending")
    task_id = task_id_for(app, task["title"])

    response = seeded_client.post(
        f"/tasks/{task_id}/status", data={"status": "in_progress"}
    )

    assert_redirects_to(response, f"/tasks/{task_id}")
    body = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert f"Status updated to {STATUS_LABELS['in_progress']}." in body


def test_status_update_from_a_filtered_list_keeps_the_filters(
    seeded_client, app
) -> None:
    """A row's status action returns the reviewer to the list they acted on.

    The posted fields are the hidden inputs the rendered row form carries, so this
    exercises the control a reviewer clicks. One of those inputs and the action's
    own value are both called ``status``; posting the harvested mapping plus the
    new value is what the browser sends. The row's status select is read back before
    that post, so a control that never pre-selects the stored status fails here.
    """
    filtered_status = "pending"
    task = seeded_task_in_status(filtered_status)
    task_id = task_id_for(app, task["title"])
    total = len(seed.SAMPLE_DATA["tasks"])

    listing = seeded_client.get("/tasks", query_string={"status": filtered_status})
    assert listing.status_code == 200
    listing_body = listing.get_data(as_text=True)
    row_select = selected_option_text(listing_body, f"task-status-{task_id}")
    assert row_select == STATUS_LABEL_TEXT[filtered_status]
    carried = hidden_inputs(listing_body, f"/tasks/{task_id}/status")
    assert carried["origin"] == "list"

    response = seeded_client.post(
        f"/tasks/{task_id}/status", data={**carried, "status": "completed"}
    )

    assert response.status_code == 302
    query = location_query(response)
    for name, value in carried.items():
        if not name.startswith("filter_") or not value:
            continue
        assert query.get(name.removeprefix("filter_")) == [value], (
            f"the redirect dropped the {name} filter"
        )
    assert task_row(app, task_id)["status"] == "completed"

    page = seeded_client.get(response.headers["Location"])
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert f"Status updated to {STATUS_LABELS['completed']}." in body

    remaining = [
        entry
        for entry in seed.SAMPLE_DATA["tasks"]
        if entry["status"] == filtered_status and entry["title"] != task["title"]
    ]
    assert f"<p>{len(remaining)} of {total} tasks match these filters</p>" in body
    assert task["title"] not in body
    for entry in remaining:
        assert entry["title"] in body


def test_an_invalid_status_on_a_quick_action_is_a_400(seeded_client, app) -> None:
    """A status the interface never offers is a malformed request, not a field to
    correct, so the quick action answers with the 400 page naming the parameter."""
    task = seeded_task_in_status("pending")
    task_id = task_id_for(app, task["title"])
    before = task_row(app, task_id)["status"]

    response = seeded_client.post(
        f"/tasks/{task_id}/status", data={"status": "done", "origin": "detail"}
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "status" in body
    assert "Select a valid status." in body
    assert task_row(app, task_id)["status"] == before


def test_list_origin_status_update_rejects_a_carried_filter_out_of_set(
    seeded_client, app
) -> None:
    """A filter the action carries back is re-validated before the write.

    A value outside the filter's own set is the same 400 the task list answers for
    it, and it happens before the row is touched.
    """
    filtered_status = "pending"
    task = seeded_task_in_status(filtered_status)
    task_id = task_id_for(app, task["title"])

    listing = seeded_client.get("/tasks", query_string={"status": filtered_status})
    carried = hidden_inputs(listing.get_data(as_text=True), f"/tasks/{task_id}/status")
    carried["filter_deadline"] = UNKNOWN_DEADLINE_FILTER

    response = seeded_client.post(
        f"/tasks/{task_id}/status", data={**carried, "status": "completed"}
    )

    assert response.status_code == 400
    # The page's description is autoescaped, so the quoted parameter reads through
    # an escaping pass rather than as typed.
    body = unescape(response.get_data(as_text=True))
    assert "deadline" in body
    assert "Invalid value for filter 'deadline'." in body
    assert task_row(app, task_id)["status"] == filtered_status


def test_status_action_does_not_read_its_own_query_string_as_a_filter(
    seeded_client, app
) -> None:
    """The carried filters are the row form's hidden inputs, and nothing else.

    A row's status form is built with no query string on its action, so a ``status``
    there can only be the new value the action is asked for -- the same name one of
    the three filters uses. Reading it as a filter instead would send the reviewer
    back to ``/tasks?status=<the new status>`` rather than the list they acted on, so
    this posts both: the body's ``filter_status`` is the filter the redirect must
    carry, and the action's own value must never appear in it.
    """
    filtered_status = "pending"
    action_status = "completed"
    task = seeded_task_in_status(filtered_status)
    task_id = task_id_for(app, task["title"])

    listing = seeded_client.get("/tasks", query_string={"status": filtered_status})
    assert listing.status_code == 200
    carried = hidden_inputs(listing.get_data(as_text=True), f"/tasks/{task_id}/status")
    assert carried["filter_status"] == filtered_status

    response = seeded_client.post(
        f"/tasks/{task_id}/status",
        query_string={"status": action_status},
        data={**carried, "status": action_status},
    )

    assert response.status_code == 302
    assert location_path(response) == "/tasks"
    assert location_query(response).get("status") == [filtered_status]
    assert task_row(app, task_id)["status"] == action_status


# --------------------------------------------------------------------------
# Assigning a task to a team member, and clearing the assignment.
# --------------------------------------------------------------------------


def test_assigning_a_member_from_the_detail_view(seeded_client, app) -> None:
    """An assignment is stored and the member's name appears on both views."""
    task = seeded_unassigned_task()
    task_id = task_id_for(app, task["title"])
    member = member_for_email(app, seed.SAMPLE_DATA["members"][0]["email"])

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": str(member.id), "origin": "detail"},
    )

    assert_redirects_to(response, f"/tasks/{task_id}")
    assert int(task_row(app, task_id)["assignee_id"]) == member.id

    detail = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Assignee updated." in detail
    assert definition_value(detail, "Assignee") == member.name

    listing = seeded_client.get("/tasks").get_data(as_text=True)
    assert member.name in table_row(listing, task_id)


def test_clearing_the_assignee_reads_as_unassigned_on_both_views(
    seeded_client, app
) -> None:
    """An empty selection is how an assignment is cleared, and both views say so."""
    task = seeded_assigned_task()
    task_id = task_id_for(app, task["title"])

    response = seeded_client.post(
        f"/tasks/{task_id}/assign", data={"assignee_id": "", "origin": "detail"}
    )

    assert_redirects_to(response, f"/tasks/{task_id}")
    assert task_row(app, task_id)["assignee_id"] is None

    detail = seeded_client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Task unassigned." in detail
    assert definition_value(detail, "Assignee") == "Unassigned"

    listing = seeded_client.get("/tasks").get_data(as_text=True)
    assert "Unassigned" in table_row(listing, task_id)


def test_assigning_an_unknown_member_is_a_400(seeded_client, app) -> None:
    """A value that names no member is malformed for a closed-set quick action, so
    the assign form answers 400 here where the task form answers 200 and a field
    message for the same value."""
    task = seeded_assigned_task()
    task_id = task_id_for(app, task["title"])
    before = task_row(app, task_id)["assignee_id"]

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": str(UNKNOWN_ID), "origin": "detail"},
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "assignee_id" in body
    assert "Select a team member from the list." in body
    assert task_row(app, task_id)["assignee_id"] == before


# The spellings of a member id that ``int()`` would have coerced. The assignee
# field, the assignee filter and the comment author all refuse each one, so the
# assign quick action must refuse it too instead of resolving it to a real member.
# ``{id}`` stands for a stored member's id, which the case fills in.
REJECTED_ASSIGNEE_SPELLINGS = (
    pytest.param("+{id}", id="leading-plus"),
    pytest.param("\u0663", id="arabic-indic-digit"),
    pytest.param("\uff15", id="fullwidth-digit"),
    pytest.param("{id}_0", id="pep-515-underscore"),
)

# The ASCII digit runs every member control accepts: a member's own id, and the same
# id zero-padded.
ACCEPTED_ASSIGNEE_SPELLINGS = (
    pytest.param("{id}", id="plain"),
    pytest.param("0{id}", id="zero-padded"),
)


@pytest.mark.parametrize("assignee_id", OVER_RANGE_ASSIGNEE_IDS)
def test_assigning_an_out_of_range_member_is_a_400(
    seeded_client, app, assignee_id: str
) -> None:
    """An assignee id no member row could hold gets the quick action's 400.

    The value cannot name a member, so the closed-set control answers it with the 400
    page naming the parameter rather than a server error, and the stored assignee is
    left as it was.
    """
    task = seeded_assigned_task()
    task_id = task_id_for(app, task["title"])
    before = task_row(app, task_id)["assignee_id"]

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": assignee_id, "origin": "detail"},
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "assignee_id" in body
    assert "Select a team member from the list." in body
    assert task_row(app, task_id)["assignee_id"] == before


@pytest.mark.parametrize("spelling", REJECTED_ASSIGNEE_SPELLINGS)
def test_assign_rejects_a_member_id_spelling_the_field_rejects(
    seeded_client, app, spelling: str
) -> None:
    """A spelling the shared member rule refuses is a 400, not an assignment.

    The four member controls accept exactly the same spellings, so a signed or
    non-ASCII digit run that the task form answers with a field message is a
    malformed request here, and the stored assignee is left alone.
    """
    task = seeded_assigned_task()
    task_id = task_id_for(app, task["title"])
    before = task_row(app, task_id)["assignee_id"]
    member = member_for_email(app, seed.SAMPLE_DATA["members"][0]["email"])

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": spelling.format(id=member.id), "origin": "detail"},
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "assignee_id" in body
    assert "Select a team member from the list." in body
    assert task_row(app, task_id)["assignee_id"] == before


@pytest.mark.parametrize("spelling", ACCEPTED_ASSIGNEE_SPELLINGS)
def test_assign_resolves_an_ascii_digit_run_to_its_member(
    seeded_client, app, spelling: str
) -> None:
    """A run of ASCII digits still assigns the member it names."""
    task = seeded_unassigned_task()
    task_id = task_id_for(app, task["title"])
    member = member_for_email(app, seed.SAMPLE_DATA["members"][0]["email"])

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": spelling.format(id=member.id), "origin": "detail"},
    )

    assert_redirects_to(response, f"/tasks/{task_id}")
    assert int(task_row(app, task_id)["assignee_id"]) == member.id


@pytest.mark.parametrize("carried", OVER_RANGE_ASSIGNEE_IDS)
def test_list_origin_assign_rejects_a_carried_assignee_out_of_range(
    seeded_client, app, carried: str
) -> None:
    """An assignee filter the action carries back is re-validated before the write.

    The carried value is the form field a row posts its filters in, and it cannot
    name a member, so the request is answered with the same 400 the task list gives
    it, naming the filter parameter, and the task's stored assignee is untouched.
    """
    task = seeded_unassigned_task()
    task_id = task_id_for(app, task["title"])
    member = member_for_email(app, seed.SAMPLE_DATA["members"][1]["email"])

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={
            "assignee_id": str(member.id),
            "origin": "list",
            "filter_assignee": carried,
        },
    )

    assert response.status_code == 400
    # The page's description is autoescaped, so the quoted parameter reads through an
    # escaping pass rather than as typed.
    body = unescape(response.get_data(as_text=True))
    assert "assignee: Invalid value for filter 'assignee'." in body
    assert task_row(app, task_id)["assignee_id"] is None


# Digit runs no stored member can have, so that every member control refuses them
# instead of raising: a twenty-digit run past SQLite's signed 64-bit INTEGER, the
# nineteen-digit value just past it, where binding the value would raise
# OverflowError, and a run longer than the 4300 digits CPython's int() converts at
# all, where the conversion itself raises.
OVERLARGE_ASSIGNEE_IDS = (
    pytest.param(str(2**63), id="past-the-64-bit-bound"),
    pytest.param("1" + "0" * 19, id="twenty-digits"),
    pytest.param("9" * 4301, id="past-the-conversion-limit"),
)


@pytest.mark.parametrize("submitted", OVERLARGE_ASSIGNEE_IDS)
def test_assign_refuses_an_assignee_id_no_row_could_hold(
    seeded_client, app, submitted: str
) -> None:
    """An id past the stored range is the quick action's 400, and nothing is written.

    The assign control answers a value that cannot name a member the way it answers
    one that names no member: the 400 page naming ``assignee_id``. The stored
    assignee is left as it was, so neither the length of the run nor its magnitude
    can reach the database bind.
    """
    task = seeded_assigned_task()
    task_id = task_id_for(app, task["title"])
    before = task_row(app, task_id)["assignee_id"]

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": submitted, "origin": "detail"},
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "assignee_id" in body
    assert "Select a team member from the list." in body
    assert task_row(app, task_id)["assignee_id"] == before


@pytest.mark.parametrize("submitted", OVERLARGE_ASSIGNEE_IDS)
def test_create_form_refuses_an_assignee_id_no_row_could_hold(
    seeded_client, app, submitted: str
) -> None:
    """The same value is a field message on the form, and no row is written.

    The task form is a data-entry control, so the value the quick action rejects
    with a 400 comes back as the Assignee field's own message at 200, with the
    submission still in the other fields and nothing stored.
    """
    before = task_count(app)

    response = seeded_client.post(
        "/tasks", data={**valid_task_form(app), "assignee_id": submitted}
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Select a team member from the list." in body
    assert task_count(app) == before
    assert textarea_value(body, "description") == NEW_TASK_DESCRIPTION


def test_assigning_from_a_filtered_list_keeps_the_filters(seeded_client, app) -> None:
    """The assign action carries the list filters back the way the status action
    does, and re-validates them with the same parser."""
    task = seeded_unassigned_task()
    task_id = task_id_for(app, task["title"])
    member = member_for_email(app, seed.SAMPLE_DATA["members"][1]["email"])

    listing = seeded_client.get("/tasks", query_string={"assignee": "unassigned"})
    assert listing.status_code == 200
    carried = hidden_inputs(listing.get_data(as_text=True), f"/tasks/{task_id}/status")
    assert carried["filter_assignee"] == "unassigned"

    response = seeded_client.post(
        f"/tasks/{task_id}/assign", data={**carried, "assignee_id": str(member.id)}
    )

    assert response.status_code == 302
    assert location_query(response).get("assignee") == ["unassigned"]
    assert int(task_row(app, task_id)["assignee_id"]) == member.id

    page = seeded_client.get(response.headers["Location"])
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Assignee updated." in body
    # The filter survived, and the task it just assigned no longer matches it.
    assert "No tasks match these filters." in body


# A plain field name carrying a value outside that filter's allowed set: the shape a
# body takes when a filter is posted under the parameter's own name rather than as the
# row form's ``filter_*`` input. No control in this application renders either one, so
# neither may be read as a filter -- reading it would answer this action with a 400
# before the write and leave the assignment unrecorded.
PLAIN_FILTER_FIELDS = (
    pytest.param("deadline", UNKNOWN_DEADLINE_FILTER, id="plain-deadline"),
    pytest.param("status", "done", id="plain-status"),
)


@pytest.mark.parametrize(("field", "value"), PLAIN_FILTER_FIELDS)
def test_assign_from_the_list_does_not_read_a_plain_filter_name(
    seeded_client, app, field: str, value: str
) -> None:
    """A body field named like a filter but not prefixed is not a filter.

    The row form posts ``filter_assignee`` and its two neighbours; a bare ``deadline``
    or ``status`` in the same body belongs to no control the list renders, and the
    status action's own value is the one posted under exactly the bare name. So the
    plain name is ignored: the action redirects to the unfiltered list and the
    assignment is stored, where reading this value -- outside its set -- would have
    answered 400 and written nothing.
    """
    task = seeded_unassigned_task()
    task_id = task_id_for(app, task["title"])
    member = member_for_email(app, seed.SAMPLE_DATA["members"][1]["email"])

    response = seeded_client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee_id": str(member.id), "origin": "list", field: value},
    )

    assert response.status_code == 302
    assert location_path(response) == "/tasks"
    assert location_query(response) == {}
    assert int(task_row(app, task_id)["assignee_id"]) == member.id
