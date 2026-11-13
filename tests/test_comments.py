"""Tests for the comment workflow: the seeded conversation, adding a comment, every
rejection, an unknown task, and the escaping of a body on the way out.

A comment is posted to ``POST /tasks/<task_id>/comments`` with two fields, an author
chosen from the member list and a body of at most 1000 characters. A valid post
redirects back to the task with a confirmation and writes exactly one row; a rejected
post re-renders the detail page at 200 with the message beside the field, the typed
body still in the textarea and nothing written; and a post aimed at a task that does
not exist is a 404 before the body is parsed at all. Each of those is its own test,
and each asserts the row count as well as the response, because a rejection that
wrote a row and one that wrote nothing are indistinguishable from the page alone.

Every expected value is derived rather than written down. Task ids are read from the
seeded database by title, member ids and names come from the same service function the
views call, comment counts are read live and asserted as a difference, and the
oldest-first order the page must show is derived from the dataset's own ``age_days``
offsets. The only literals here are the sentences the forms must render, spelled out
rather than imported from :mod:`app.validation`: that wording is part of what the page
shows, so a reworded sentence should fail this suite and be reviewed instead of
following the module silently.

The service is exercised directly once, for the branch no HTTP request can reach: the
comment route turns an unknown task into a 404 before it calls
:func:`app.services.comments.add_comment`, so only a direct call proves that the
service still reports a missing parent as ``None`` rather than raising or writing an
orphan row.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

import seed
from app.db import get_db
from app.models import Member
from app.services import comments as comment_service
from app.services import members as member_service

# ---------------------------------------------------------------------------
# The sentences the pages must show, and the flash a successful post leaves behind.
# Spelled out rather than imported from app.validation for the reason the module
# docstring gives: the wording is part of the observable contract.
# ---------------------------------------------------------------------------

MSG_ENTER_COMMENT = "Enter a comment."
MSG_COMMENT_BODY_TOO_LONG = "Comments are limited to 1000 characters."
MSG_SELECT_COMMENT_AUTHOR = "Select a comment author."
FLASH_COMMENT_ADDED = "Comment added."

# The empty state the comments block shows when a task has no conversation yet.
EMPTY_COMMENTS_TEXT = "No comments yet."

# The link the 404 page offers, which is how a test tells the application's own
# failure page apart from a bare werkzeug error response.
BACK_TO_DASHBOARD_TEXT = "Back to the dashboard"

# The bound the body rule enforces, named once so the boundary case states the
# length it tests instead of a bare number.
COMMENT_BODY_LIMIT = 1000

# The body a rejection case posts and then expects to find preserved in the
# textarea: one sentence, carrying no markup, so it matches the rendered page
# exactly as it was typed.
SURVIVING_BODY = "The hero spacing still needs one more pass."


# ---------------------------------------------------------------------------
# Reading the seeded database. Each helper opens its own application context, so a
# test can read a row, post a request and read again without holding a connection
# open across the request -- which is what the application itself does per request.
# ---------------------------------------------------------------------------


def query_one(
    app: Flask, sql: str, parameters: tuple[object, ...] = ()
) -> sqlite3.Row | None:
    """Return the first row ``sql`` selects from the seeded database, or ``None``.

    Args:
        app: The application whose ``DATABASE`` to read.
        sql: A statement with bound placeholders only; nothing here is interpolated
            from a submitted value.
        parameters: The values to bind, in the statement's own order.

    Returns:
        The row as a :class:`sqlite3.Row`, whose columns stay readable after the
        connection closes, or ``None`` when the statement selected no row.
    """
    with app.app_context():
        return get_db().execute(sql, parameters).fetchone()


def query_scalar(app: Flask, sql: str, parameters: tuple[object, ...] = ()) -> Any:
    """Return the single value ``sql`` selects from the seeded database.

    Raises:
        AssertionError: If the statement selected no row, which always means the
            fixture and the query disagree rather than that a value was empty.
    """
    row = query_one(app, sql, parameters)
    assert row is not None, f"query selected no row: {sql}"
    return row[0]


def task_id_for(app: Flask, title: str) -> int:
    """Return the id the seeded task with ``title`` was inserted with.

    Resolving ids by title rather than assuming them keeps every request below
    independent of the order the loader inserts its rows in.
    """
    return int(query_scalar(app, "SELECT id FROM task WHERE title = ?", (title,)))


def missing_id(app: Flask, table: str) -> int:
    """Return an id that no row of ``table`` holds.

    One past the highest key, so it names nothing whatever the dataset grows to --
    which is what a stale link or a tampered form posts. ``table`` is interpolated
    rather than bound because SQLite has no placeholder for an identifier; it comes
    from this module's own constants, never from input.
    """
    return int(query_scalar(app, f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}"))


def comment_count(app: Flask) -> int:
    """Return how many comment rows exist, for asserting a difference, not a total."""
    return int(query_scalar(app, "SELECT COUNT(*) FROM comment"))


def list_members(app: Flask) -> list[Member]:
    """Return the team members as the application's own service lists them."""
    with app.app_context():
        listed = member_service.list_members()
    assert listed, "the seeded member table is empty"
    return listed


def first_member(app: Flask) -> Member:
    """Return the first member the application lists, so the author is deterministic.

    ``app.services.members.list_members`` orders by name, so this is the same member
    on every run and on every machine without the test naming one.
    """
    return list_members(app)[0]


def members_by_email(app: Flask) -> dict[str, Member]:
    """Return every member keyed by email.

    The sample data names a comment's author by email -- the one column the schema
    declares UNIQUE -- so this is how a seeded comment's expected author name is
    resolved without the test knowing any id.
    """
    return {member.email: member for member in list_members(app)}


# ---------------------------------------------------------------------------
# Reading the sample dataset, which is the same literal the fixtures seed from.
# ---------------------------------------------------------------------------


def task_carrying_two_comments() -> dict[str, Any]:
    """Return a seeded task with two comments, the conversation this module extends.

    Two comments are what makes the ordering assertion meaningful: the page has to
    put them in one order rather than show a single line.

    Raises:
        AssertionError: If no task carries two comments, in which case the ordering
            case would silently stop testing anything.
    """
    for task in seed.SAMPLE_DATA["tasks"]:
        if len(task["comments"]) == 2:
            return task
    raise AssertionError("SAMPLE_DATA holds no task with two comments")


def task_without_comments() -> dict[str, Any]:
    """Return a seeded task carrying no comments, for the empty-state case."""
    for task in seed.SAMPLE_DATA["tasks"]:
        if not task["comments"]:
            return task
    raise AssertionError("SAMPLE_DATA holds no task without comments")


def bodies_oldest_first(task: dict[str, Any]) -> list[str]:
    """Return the task's comment bodies in the order the detail page must show them.

    The loader writes ``created_at`` as ``today - age_days`` at the comment's own
    time of day, and ``app.services.comments.list_comments`` orders by that column
    ascending. So ascending ``created_at`` -- the order asserted here -- is
    descending ``age_days``, and ascending time of day within one day. The seed
    gives the comments distinct timestamps, so this order is the page's order rather
    than a guess between equals.
    """
    comments = sorted(
        task["comments"], key=lambda item: (-item["age_days"], item["time_of_day"])
    )
    return [comment["body"] for comment in comments]


# ---------------------------------------------------------------------------
# Reading the rendered page.
# ---------------------------------------------------------------------------


def position_of(html: str, text: str) -> int:
    """Return the offset of ``text`` in ``html``, failing the test when it is absent.

    Used for the ordering assertion, where the offset itself is the measurement; a
    missing string has to fail with the string named rather than as a bare
    ``ValueError`` from ``str.index``.
    """
    assert text in html, f"expected {text!r} in the rendered page"
    return html.index(text)


def row_markup(html: str, title: str) -> str:
    """Return the task-table row markup for the task titled ``title``.

    A row starts at the row's own title -- which appears once per page, inside the
    link to the detail view -- and runs to the row's closing tag, so every cell of
    that row is inside the returned slice and no other row's is.
    """
    start = position_of(html, title)
    end = html.find("</tr>", start)
    assert end != -1, f"the row for {title!r} has no closing tag"
    return html[start:end]


def detail_html(client: FlaskClient, task_id: int) -> str:
    """Return the rendered detail page for ``task_id``, asserting it was served."""
    response = client.get(f"/tasks/{task_id}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def list_html(client: FlaskClient) -> str:
    """Return the rendered task list, asserting it was served."""
    response = client.get("/tasks")
    assert response.status_code == 200
    return response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The seeded conversation. What a task's page shows before anything is written:
# every comment, its author, its timestamp, and the order they happened in.
# ---------------------------------------------------------------------------


def test_detail_lists_the_seeded_conversation_oldest_first(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """A task's page shows its comments in the order they were written.

    The two bodies are asserted by position rather than by presence alone, because a
    page that rendered them newest first would pass a presence check. The author
    name and the date of each comment are asserted too, which is what proves the
    ``format_datetime`` filter is doing the rendering: the expected day is derived
    from the dataset's ``age_days`` offset against today, so no month is hardcoded.
    """
    task = task_carrying_two_comments()
    task_id = task_id_for(app, task["title"])

    html = detail_html(seeded_client, task_id)

    expected_order = bodies_oldest_first(task)
    assert len(expected_order) == 2
    positions = [position_of(html, body) for body in expected_order]
    assert len(set(positions)) == len(positions), "each body is rendered once"
    assert positions == sorted(positions), "the older comment is rendered first"

    authors = members_by_email(app)
    for comment in task["comments"]:
        assert authors[comment["author"]].name in html
        written = date.today() - timedelta(days=comment["age_days"])
        assert f"{written.day} {written.strftime('%b %Y')}" in html

    assert EMPTY_COMMENTS_TEXT not in html


def test_task_without_comments_shows_the_empty_state(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """A task nobody has commented on says so instead of rendering an empty list."""
    task = task_without_comments()

    html = detail_html(seeded_client, task_id_for(app, task["title"]))

    assert EMPTY_COMMENTS_TEXT in html


def test_task_list_shows_each_tasks_comment_count(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """Every list row shows the number of comments on its task.

    Each row is checked against the count the dataset declares for it, so the case
    covers a task with comments and a task with none in the same request, and it
    fails if the column ever starts counting something other than that task's own
    rows.
    """
    html = list_html(seeded_client)

    counts = {len(task["comments"]) for task in seed.SAMPLE_DATA["tasks"]}
    assert 0 in counts and max(counts) >= 2, (
        "the dataset must cover both a task with comments and one without"
    )

    for task in seed.SAMPLE_DATA["tasks"]:
        row = row_markup(html, task["title"])
        expected_count = len(task["comments"])
        assert f"<td>{expected_count}</td>" in row, task["title"]


# ---------------------------------------------------------------------------
# Adding a comment. The one write this module makes, and everything observable
# about it: where it lands, what it says, where it is listed and what it stored.
# ---------------------------------------------------------------------------


def test_a_valid_comment_is_stored_and_listed_last(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """A valid post redirects, confirms, appends to the conversation and stores the row.

    Three things are asserted beyond the response: the row count rose by exactly
    one, the stored row carries the posted author, body and parent task, and the new
    comment is rendered after every comment that was already there. The last of
    those is the append behaviour a reviewer checks by hand, and it is what proves
    the ordering is by ``created_at`` rather than by nothing at all.
    """
    task = task_carrying_two_comments()
    task_id = task_id_for(app, task["title"])
    author = first_member(app)
    body = "Read through the notes; happy with the spacing now."
    count_before = comment_count(app)

    response = seeded_client.post(
        f"/tasks/{task_id}/comments",
        data={"author_id": str(author.id), "body": body},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/tasks/{task_id}")

    html = detail_html(seeded_client, task_id)
    assert html.count(FLASH_COMMENT_ADDED) == 1
    new_position = position_of(html, body)
    for earlier_body in bodies_oldest_first(task):
        assert new_position > position_of(html, earlier_body)

    assert comment_count(app) == count_before + 1
    stored = query_one(
        app,
        "SELECT task_id, author_id, body FROM comment WHERE body = ?",
        (body,),
    )
    assert stored is not None, "the posted comment was not stored"
    assert stored["task_id"] == task_id
    assert stored["author_id"] == author.id
    assert stored["body"] == body


def test_the_list_comment_count_rises_by_one(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """Adding a comment moves that task's row count on the list page, and only it."""
    task = task_carrying_two_comments()
    task_id = task_id_for(app, task["title"])
    author = first_member(app)
    expected_before = len(task["comments"])

    html_before = list_html(seeded_client)
    assert f"<td>{expected_before}</td>" in row_markup(html_before, task["title"])

    seeded_client.post(
        f"/tasks/{task_id}/comments",
        data={"author_id": str(author.id), "body": "Counted on the list page too."},
    )

    html_after = list_html(seeded_client)
    assert (
        f"<td>{expected_before + 1}</td>" in row_markup(html_after, task["title"])
    )
    untouched = next(
        other
        for other in seed.SAMPLE_DATA["tasks"]
        if other["title"] != task["title"]
    )
    assert (
        f"<td>{len(untouched['comments'])}</td>"
        in row_markup(html_after, untouched["title"])
    )


# ---------------------------------------------------------------------------
# Rejections. Every one of them is a data-entry mistake the reviewer corrects in
# place, so each answers 200 with its message, keeps the rest of the page, and
# writes nothing.
# ---------------------------------------------------------------------------

# The five ways a comment submission can be refused, each with the message the
# detail page must show beside the field. ``keeps_body`` says whether the submitted
# body is one the reviewer typed and must get back: a blank body trims to nothing,
# so there is nothing to preserve for that case.
REJECTION_CASES = (
    pytest.param("blank-body", MSG_ENTER_COMMENT, False, id="blank-body"),
    pytest.param(
        "over-long-body", MSG_COMMENT_BODY_TOO_LONG, True, id="over-long-body"
    ),
    pytest.param(
        "missing-author", MSG_SELECT_COMMENT_AUTHOR, True, id="missing-author"
    ),
    pytest.param("blank-author", MSG_SELECT_COMMENT_AUTHOR, True, id="blank-author"),
    pytest.param(
        "unknown-author", MSG_SELECT_COMMENT_AUTHOR, True, id="unknown-author"
    ),
)


def rejected_submission(case: str, author_id: int, app: Flask) -> dict[str, str]:
    """Return the submitted form a rejection ``case`` posts.

    Args:
        case: One of the ids in :data:`REJECTION_CASES`.
        author_id: The id of a member who does exist, which every case but the two
            author cases names.
        app: The application, read for the id that names no member.

    Returns:
        The form fields to post. The ``author_id`` key is absent for
        ``missing-author`` -- a control that was never submitted -- and the empty
        string for ``blank-author``, which is what the select's "Select an author"
        option submits; the two are exercised separately because a parser that
        conflates them accepts a value it should reject.

    Raises:
        AssertionError: If ``case`` is not one of the parametrized cases, so a case
            added to the table without a submission here fails loudly.
    """
    submission = {"author_id": str(author_id), "body": SURVIVING_BODY}
    if case == "blank-body":
        submission["body"] = "   "
    elif case == "over-long-body":
        submission["body"] = "x" * (COMMENT_BODY_LIMIT + 1)
    elif case == "missing-author":
        del submission["author_id"]
    elif case == "blank-author":
        submission["author_id"] = ""
    elif case == "unknown-author":
        submission["author_id"] = str(missing_id(app, "member"))
    else:  # pragma: no cover - guards the table against an unhandled case id
        raise AssertionError(f"unhandled rejection case: {case!r}")
    return submission


@pytest.mark.parametrize(("case", "message", "keeps_body"), REJECTION_CASES)
def test_a_rejected_comment_writes_nothing(
    seeded_client: FlaskClient,
    app: Flask,
    case: str,
    message: str,
    keeps_body: bool,
) -> None:
    """A refused submission re-renders the page with its message and stores no row.

    The response is 200 rather than a redirect, the message is on the page, the
    comment count is unchanged -- a rejection that wrote a row and one that wrote
    nothing look identical from the HTML alone, so the count is the assertion that
    matters -- and the conversation the page already had is still listed beneath the
    form, so a rejection costs the reviewer the comment they wrote and not the
    discussion they were reading.
    Where the body was one the reviewer typed, it is back in the textarea so the
    correction costs one field rather than a retyped comment.
    """
    task = task_carrying_two_comments()
    task_id = task_id_for(app, task["title"])
    author = first_member(app)
    data = rejected_submission(case, author.id, app)
    count_before = comment_count(app)

    response = seeded_client.post(f"/tasks/{task_id}/comments", data=data)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert message in html
    assert FLASH_COMMENT_ADDED not in html
    assert comment_count(app) == count_before
    for earlier_body in bodies_oldest_first(task):
        assert earlier_body in html
    if keeps_body:
        assert data["body"] in html


# ---------------------------------------------------------------------------
# An unknown task, and the escaping a rendered body must go through.
# ---------------------------------------------------------------------------


def test_a_comment_on_an_unknown_task_is_a_404(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """A post aimed at a task that does not exist is the 404 page, and writes nothing.

    The id is derived as one past the highest key rather than written down, so it
    stays unknown whatever the dataset grows to. The count assertion is the one that
    catches an implementation that let the foreign key raise after inserting, or
    inserted a row whose parent it never checked.
    """
    task_id = missing_id(app, "task")
    author = first_member(app)
    count_before = comment_count(app)

    response = seeded_client.post(
        f"/tasks/{task_id}/comments",
        data={"author_id": str(author.id), "body": "A comment on nothing."},
    )

    assert response.status_code == 404
    assert BACK_TO_DASHBOARD_TEXT in response.get_data(as_text=True)
    assert comment_count(app) == count_before


def test_add_comment_reports_an_unknown_task_as_none(app: Flask, db_path: str) -> None:
    """The service returns ``None`` for a missing parent instead of raising.

    The route answers 404 before it ever calls this function, so this branch is
    unreachable through HTTP; calling the service directly is the only way to prove
    it still declines to write an orphan row, which is what the route's 404 is
    standing in front of. The database is seeded here directly rather than through
    the client fixture, because no request is made in this test.
    """
    seed.seed_database(db_path)
    task_id = missing_id(app, "task")
    author = first_member(app)
    count_before = comment_count(app)

    with app.app_context():
        result = comment_service.add_comment(
            task_id, {"author_id": author.id, "body": "A comment on nothing."}
        )
        assert result is None
        assert comment_service.list_comments(task_id) == []

    assert comment_count(app) == count_before


def test_a_comment_body_is_escaped_when_rendered(
    seeded_client: FlaskClient, app: Flask
) -> None:
    """Markup in a body is stored as typed and rendered as text, never executed.

    Jinja's autoescaping is the defence, so the assertion is the pair: the literal
    tag is absent from the response and its escaped form is present. The stored row
    still holds the original characters, which is the evidence that escaping happens
    on the way out -- a comment is not silently rewritten by the write path.
    """
    task = task_carrying_two_comments()
    task_id = task_id_for(app, task["title"])
    author = first_member(app)
    body = "<script>alert(1)</script>"

    response = seeded_client.post(
        f"/tasks/{task_id}/comments",
        data={"author_id": str(author.id), "body": body},
        follow_redirects=True,
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert body not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    stored_body = query_scalar(
        app,
        "SELECT body FROM comment WHERE task_id = ? AND author_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, author.id),
    )
    assert stored_body == body
