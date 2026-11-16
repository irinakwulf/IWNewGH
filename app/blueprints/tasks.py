"""The task routes: the list, the create and edit forms, the detail view, and the
two quick actions and the comment post that hang off it.

Nine URL rules carry the nine task endpoints the interface uses -- one rule, and one
method, per endpoint -- and between them they cover every task capability the
product has: creating a task, assigning it to a team member, moving it through the
three closed statuses, giving it a deadline, discussing it in comments, and
filtering the list by status, assignee and deadline. Each capability is one
endpoint, and the two saves among them pair the read rule that renders the form with
the write rule beside it -- creating a task is ``GET /tasks/new`` for the fields and
``POST /tasks`` to store them -- so a reviewer reading this module sees the whole
HTTP surface of a task in one place.

Every view does the same small thing in the same order, which is what keeps the
layer boundary visible: it reads ``request.args`` on a read path or ``request.form``
on a write path, parses the submitted strings in :mod:`app.validation`, calls one
function in :mod:`app.services`, and then either flashes a confirmation and
redirects or re-renders its own template with the errors. No view types a submitted
value itself, the assign quick action's member id included, because that value is
resolved by the same rule the three member controls the parsers own go through.
Nothing here writes SQL, opens a connection, formats a date or decides a business
rule -- the services and the parser own all of that -- and no statement or redirect
target is ever assembled from a submitted string. The wording of every rule lives in
:mod:`app.validation` too, with the one exception named below: the two quick
actions' 400 descriptions.

**Two rejection paths, chosen by where the offending value came from.** The
data-entry posts -- create, edit, comment -- collect the parser's ``{field:
message}`` map and re-render their own form with the submitted values still in the
fields and nothing written, so a reviewer corrects one field instead of retyping the
form. The quick actions -- a status change and an assignment from a list row or from
the detail view -- and the task list's query string carry a single value that the
interface only ever offers from a closed set, so a value outside that set means the
request itself is malformed: those answer with the 400 page, naming the parameter.
Their descriptions are the exception to the wording rule above: each is the
malformed-request form of the equivalent field message -- the parameter name, then
the sentence the parser uses for the same mistake -- which is why the two sit beside
their explanation at ``_MSG_INVALID_STATUS`` and ``_MSG_INVALID_ASSIGNEE``. An
unknown task id is a 404, and it is decided before the posted value is looked at, so
an id that does not exist is a 404 whatever the payload.

**Returning a quick action to a filtered list.** A row's status or assign action is
posted from the task list and returns the reviewer to that same filtered list, so
the three filter values travel with the request as the hidden ``filter_status``,
``filter_assignee`` and ``filter_deadline`` inputs the row template renders -- that
is the shape the interface actually posts, and it is the one shape read back. One
of those filters is itself named ``status``, which the status action also uses for
its own new value, so the two never share a source: the action's value is read from
the plain body field and the filters only ever from the prefixed ``filter_*`` names,
which is why a ``status`` posted for the action cannot come back as a status filter
however the action's own URL is written. The carried filters are re-validated with
the same parser the list itself uses, before the write, so a malformed request
cannot change a row; the redirect is then rebuilt with ``url_for`` from those
validated values. No target is taken from the request, which is why the module has
no ``next`` parameter and no open-redirect surface at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.models import STATUS_LABELS, Task
from app.services import comments, members, metrics, projects
from app.services import tasks as task_service
from app.templating import reference_day
from app.validation import (
    DEFAULT_STATUS,
    is_valid_status,
    parse_comment_form,
    parse_task_filters,
    parse_task_form,
    resolve_member_id,
)

__all__ = ["bp"]

# The blueprint's name is the endpoint contract: every template builds its URLs
# with url_for("tasks.<endpoint>", ...). It is registered without a url_prefix, so
# the rules below are the whole path of each route.
bp = Blueprint("tasks", __name__)

# The three filter parameters, in the order the filter bar shows them. Named once,
# because the list reads them and the quick actions carry them back.
_FILTER_KEYS: tuple[str, ...] = ("status", "assignee", "deadline")

# What a list-origin quick action posts as its navigation hint. Any other value,
# including a missing key, sends the reviewer to the task's detail page.
_ORIGIN_LIST = "list"

# The parameter each quick action reads its own value from. Named as constants
# because the 400 description has to name the parameter the request got wrong.
_STATUS_FIELD = "status"
_ASSIGNEE_FIELD = "assignee_id"

# The messages the two quick actions quote in their 400 description. Each pairs the
# parameter name with the wording the task form shows beside the same field for the
# same mistake: app.validation owns the field messages, and these two are the
# malformed-request form of them, which the 400 page reads as its description.
_MSG_INVALID_STATUS = f"{_STATUS_FIELD}: Select a valid status."
_MSG_INVALID_ASSIGNEE = f"{_ASSIGNEE_FIELD}: Select a team member from the list."


# --------------------------------------------------------------------------
# Helpers. Each one is a shape the seven views would otherwise repeat, and none of
# them holds a business rule: the parser decides what a value means, the services
# decide what happens to it.
# --------------------------------------------------------------------------


def _bad_request(errors: Mapping[str, str]) -> NoReturn:
    """Abort with 400, naming every parameter the validation rejected.

    The filter parser reports all three parameters independently, so a request that
    gets two of them wrong is described by both rather than by whichever the parser
    happened to look at first.

    Args:
        errors: The parser's ``{parameter: message}`` map, non-empty by construction.
    """
    abort(
        400,
        description="; ".join(
            f"{field}: {message}" for field, message in errors.items()
        ),
    )


def _origin() -> str:
    """Return the navigation hint a quick action posted, or "" when it posted none.

    Read from the body first and the query string second, so either a hidden input
    or a query parameter works. The value is used only to choose between two
    redirect targets the application itself names; it is never a URL.
    """
    return request.form.get("origin") or request.args.get("origin") or ""


def _carried_filters() -> dict[str, object]:
    """Re-validate the task-list filters a list-origin quick action carried.

    The filters arrive as the row template's hidden ``filter_*`` inputs and from
    nowhere else: the form body is the only source, and only the prefixed name is
    read. One of those filters is itself called ``status``, and the status action
    uses that name for its own new value, so a body without the prefix holds the
    action's value rather than a filter -- which is why the plain name is never
    read, and why the action's value can never become a filter whatever else the
    request carries. The action's own URL is not consulted either, for the same
    reason: a query string is the list's own filter source, not this action's.

    Returns:
        The parser's ``values`` mapping: every filter key present, each one either a
        validated value or ``None`` for a filter that was not applied.

    Raises:
        werkzeug.exceptions.BadRequest: Through :func:`_bad_request`, when a carried
            value is not in its control's allowed set. That is the same 400 the list
            answers for the same value, and it happens before any write.
    """
    source = {
        key: request.form[f"filter_{key}"]
        for key in _FILTER_KEYS
        if f"filter_{key}" in request.form
    }

    filters, errors = parse_task_filters(source)
    if errors:
        _bad_request(errors)
    return filters


def _list_url(filters: Mapping[str, object]) -> str:
    """Build the task-list URL from already-validated filter values.

    A filter that is not applied is ``None`` and is left out of the query string
    entirely, so an unfiltered list is ``/tasks`` rather than ``/tasks?status=``.
    """
    return url_for(
        "tasks.list",
        **{key: value for key, value in filters.items() if value is not None},
    )


def _redirect_after_quick_action(
    task_id: int, origin: str, filters: Mapping[str, object]
) -> Response:
    """Return the reviewer to the filtered list, or to the task's detail page.

    Both targets are built here with ``url_for`` from values this module has already
    validated: the origin only selects between them, and a hint this module does not
    recognise -- a missing ``origin`` among them -- gets the task's own page.
    """
    if origin == _ORIGIN_LIST:
        return redirect(_list_url(filters))
    return redirect(url_for("tasks.detail", task_id=task_id))


def _empty_task_values() -> dict[str, object]:
    """Return the create form's field values, with the status already Pending."""
    return {
        "title": "",
        "description": "",
        "assignee_id": None,
        "status": DEFAULT_STATUS,
        "deadline": None,
    }


def _stored_task_values(task: Task) -> dict[str, object]:
    """Return a stored task as the edit form's field values."""
    return {
        "title": task.title,
        "description": task.description,
        "assignee_id": task.assignee_id,
        "status": task.status,
        "deadline": task.deadline,
    }


def _empty_comment_values() -> dict[str, object]:
    """Return the add-comment form's field values: no author chosen, empty body."""
    return {"author_id": None, "body": ""}


def _render_task_form(
    *,
    task: Task | None,
    values: Mapping[str, object],
    errors: Mapping[str, str] | None = None,
) -> str:
    """Render ``tasks/form.html``, for both the create form and the edit form.

    The two forms are the same five fields with the same labels, so one helper
    builds the context for both, and a rejected submission re-renders through the
    same path with the parser's ``values`` and ``errors`` in place of the first
    render's. ``members`` is read here rather than in each view, because the
    Assignee select lists them on both.

    Args:
        task: The stored task on the edit form, or ``None`` while creating one. The
            template uses it to choose the heading, the form's action and where
            Cancel goes.
        values: The five field values to show: the stored task's, the create
            defaults, or what the reviewer submitted on a rejected post.
        errors: The parser's ``{field: message}`` map, empty on a first render.

    Returns:
        The rendered form as HTML.
    """
    return render_template(
        "tasks/form.html",
        task=task,
        members=members.list_members(),
        values=values,
        errors=errors or {},
    )


def _render_task_detail(
    task: Task,
    *,
    values: Mapping[str, object] | None = None,
    errors: Mapping[str, str] | None = None,
) -> str:
    """Render ``tasks/detail.html``: the task, its status and assign controls, its
    comments and the add-comment form.

    Args:
        task: The stored task the page is about.
        values: The add-comment form's field values; the empty ones when the page is
            rendered by its own GET, and the submitted ones when a comment was
            rejected so the typed body survives.
        errors: The comment parser's ``{field: message}`` map, empty on a first
            render.

    Returns:
        The rendered detail page as HTML.
    """
    return render_template(
        "tasks/detail.html",
        task=task,
        comments=comments.list_comments(task.id),
        members=members.list_members(),
        values=_empty_comment_values() if values is None else values,
        errors=errors or {},
    )


# --------------------------------------------------------------------------
# The routes. Seven views serve the nine endpoints: each one has exactly one rule
# and one method, a read rule for the list, the two forms and the detail page and a
# write rule for the two saves and the three actions that hang off the detail page.
# --------------------------------------------------------------------------


@bp.route("/tasks", methods=["GET"], endpoint="list")
def task_list():
    """Render the task list, filtered by the query string.

    ``app.validation.parse_task_filters`` reads ``status``, ``assignee`` and
    ``deadline`` and ``app.services.tasks.list_tasks`` applies them; the members are
    read for the filter bar's Assignee select, and ``total_count`` comes from
    ``app.services.metrics.total_task_count`` so the "2 of 12 tasks match these
    filters" line counts the whole table with one ``COUNT(*)``: the result line needs
    the unfiltered total and none of the status buckets a dashboard render computes,
    so none of them is computed here.

    A filter value outside its control's allowed set is a malformed request rather
    than a field to correct, so it is answered with the 400 page naming the
    parameter instead of a list that silently ignores it.

    The request's single reference day is read here and handed to the filter, so the
    deadline buckets and the per-row deadline indicator are measured from one day.

    Returns:
        The rendered task list with the default 200 status, or the 400 page naming
        every filter parameter the parser rejected.
    """
    filters, errors = parse_task_filters(request.args)
    if errors:
        _bad_request(errors)

    # One reading of today for the whole render: the per-row deadline indicator reads
    # the same cached day, which is why a row this filter selects as due today cannot
    # be labelled "Overdue" by its own indicator.
    today = reference_day()

    return render_template(
        "tasks/list.html",
        tasks=task_service.list_tasks(filters, today=today),
        members=members.list_members(),
        filters=filters,
        total_count=metrics.total_task_count(),
    )


# One read rule and one write rule for this endpoint: `/tasks/new` renders the form
# and `POST /tasks` is the only path that creates a task. `url_for("tasks.create",
# _method="POST")` asks for the write method, which only one rule accepts, so the
# task form's action is the contract's collection path while a plain `url_for` --
# the "Create task" link -- builds the form's own read path.
@bp.route("/tasks/new", methods=["GET"], endpoint="create")
@bp.route("/tasks", methods=["POST"], endpoint="create")
def task_create():
    """Render the empty create form, or create a task from a submitted one.

    On a POST, ``app.validation.parse_task_form`` parses the submission -- with
    ``app.services.projects.get_project`` supplying the project start date the
    deadline rule compares against -- and ``app.services.tasks.create_task`` is the
    one call that writes. A rejected submission re-renders the form at 200 with the
    submitted values and the parser's messages, and writes nothing.

    The GET render defaults the Status select to Pending, which is the same status
    the parser and the schema apply when a submission names none.

    Returns:
        On ``GET``, the empty create form with the default 200 status. On a
        rejected ``POST``, the same form at 200 with the submitted values and the
        parser's messages, having written nothing. On an accepted ``POST``, a 302
        to the new task's detail page.
    """
    # Tested for the write method rather than for GET, because Flask adds HEAD to
    # every GET rule: a HEAD probe must take this branch and receive the form's
    # response without its body, not the write branch's re-render.
    if request.method != "POST":
        return _render_task_form(task=None, values=_empty_task_values())

    values, errors = parse_task_form(request.form, projects.get_project())
    if errors:
        return _render_task_form(task=None, values=values, errors=errors)

    task = task_service.create_task(values)
    flash("Task created.", "success")
    return redirect(url_for("tasks.detail", task_id=task.id))


@bp.route("/tasks/<int:task_id>", methods=["GET"], endpoint="detail")
def task_detail(task_id):
    """Render one task with its status and assign controls and its comments.

    ``app.services.tasks.get_task`` is the read, and its ``None`` is what turns an
    unknown id into the 404 page; ``app.services.comments.list_comments`` supplies
    the conversation, oldest first, and ``app.services.members.list_members`` the
    options for the assign and comment-author selects.

    Returns:
        The rendered detail page with the default 200 status, or the 404 page when
        no task has that id.
    """
    task = task_service.get_task(task_id)
    if task is None:
        abort(404)
    return _render_task_detail(task)


# The edit endpoint follows the same shape: `/tasks/<id>/edit` renders the form and
# `POST /tasks/<id>` is the only path that saves it, so the form's action built with
# `url_for("tasks.edit", task_id=..., _method="POST")` lands on the item path while
# the "Edit task" link builds the read path.
@bp.route("/tasks/<int:task_id>/edit", methods=["GET"], endpoint="edit")
@bp.route("/tasks/<int:task_id>", methods=["POST"], endpoint="edit")
def task_edit(task_id):
    """Render the edit form for one task, or save an edited one.

    The task is read first on both methods, because an unknown id is a 404 whether
    the reviewer is opening the form or posting a corrected one.
    ``app.services.tasks.update_task`` is the one call that writes; a rejected
    submission re-renders the form at 200 with the submitted values, the parser's
    messages and the stored task still behind the heading, and writes nothing.

    Returns:
        On ``GET``, the edit form with the default 200 status. On a rejected
        ``POST``, the same form at 200 with the submitted values and the parser's
        messages, having written nothing. On an accepted ``POST``, a 302 to the
        task's detail page. An unknown id is the 404 page on both methods.
    """
    task = task_service.get_task(task_id)
    if task is None:
        abort(404)

    # As in task_create: HEAD is implicit on the GET rule, so only the POST rule
    # reaches the write branch and a HEAD probe gets the form a GET renders.
    if request.method != "POST":
        return _render_task_form(task=task, values=_stored_task_values(task))

    values, errors = parse_task_form(request.form, projects.get_project())
    if errors:
        return _render_task_form(task=task, values=values, errors=errors)

    task_service.update_task(task_id, values)
    flash("Task updated.", "success")
    return redirect(url_for("tasks.detail", task_id=task_id))


@bp.route("/tasks/<int:task_id>/status", methods=["POST"], endpoint="set_status")
def task_set_status(task_id):
    """Change one task's status, then return the reviewer to where they acted.

    The order of the three checks is deliberate. The task is read first, so an
    unknown id is a 404 whatever the body says; the submitted status is checked
    against the closed set next, so a value the interface never offers is a 400
    naming ``status``; and the filters a list-origin action carried are validated
    last but still before ``app.services.tasks.set_status`` writes, so a malformed
    request about where to return cannot leave a row changed.

    Returns:
        A 302 to the filtered list when the action carried ``origin=list``, and to
        the task's detail page otherwise. An unknown id is the 404 page and a
        rejected status or carried filter the 400 page, both raised before
        ``app.services.tasks.set_status`` writes.
    """
    if task_service.get_task(task_id) is None:
        abort(404)

    status = request.form.get(_STATUS_FIELD, "")
    if not is_valid_status(status):
        abort(400, description=_MSG_INVALID_STATUS)

    origin = _origin()
    filters = _carried_filters() if origin == _ORIGIN_LIST else {}

    task_service.set_status(task_id, status)
    flash(f"Status updated to {STATUS_LABELS[status]}.", "success")
    return _redirect_after_quick_action(task_id, origin, filters)


@bp.route("/tasks/<int:task_id>/assign", methods=["POST"], endpoint="set_assignee")
def task_set_assignee(task_id):
    """Assign one task to a team member, or clear its assignment, then return.

    Same order as the status action: the task is read first (404), the posted
    assignee is resolved next, and the carried filters are validated before
    ``app.services.tasks.set_assignee`` writes. An empty ``assignee_id`` is how the
    interface clears an assignment, and it is the only value that is not a member
    id: everything else must name a member, which
    :func:`app.validation.resolve_member_id` is the authority on -- the same rule
    the task form's Assignee field, the assignee filter and the comment author go
    through, so this control accepts exactly their spellings rather than whatever
    ``int()`` would coerce.

    Returns:
        A 302 to the filtered list when the action carried ``origin=list``, and to
        the task's detail page otherwise. An unknown id is the 404 page and a
        rejected assignee or carried filter the 400 page, both raised before
        ``app.services.tasks.set_assignee`` writes.
    """
    if task_service.get_task(task_id) is None:
        abort(404)

    submitted = (request.form.get(_ASSIGNEE_FIELD) or "").strip()
    member_id: int | None = None
    if submitted:
        member_id = resolve_member_id(submitted)
        if member_id is None:
            abort(400, description=_MSG_INVALID_ASSIGNEE)

    origin = _origin()
    filters = _carried_filters() if origin == _ORIGIN_LIST else {}

    task_service.set_assignee(task_id, member_id)
    flash("Task unassigned." if member_id is None else "Assignee updated.", "success")
    return _redirect_after_quick_action(task_id, origin, filters)


@bp.route("/tasks/<int:task_id>/comments", methods=["POST"], endpoint="add_comment")
def task_add_comment(task_id):
    """Add a comment to one task, attributed to the member the form names.

    The task is read first, so an unknown id is a 404 before the body is parsed.
    ``app.validation.parse_comment_form`` parses the submission and
    ``app.services.comments.add_comment`` is the one call that writes; a rejected
    submission re-renders the detail page at 200 with the conversation, the typed
    body and the parser's messages in place, and writes nothing.

    Returns:
        On an accepted ``POST``, a 302 to the task's detail page. On a rejected
        one, the same page at 200 with the typed body and the parser's messages,
        having written nothing; an unknown id is the 404 page.
    """
    task = task_service.get_task(task_id)
    if task is None:
        abort(404)

    values, errors = parse_comment_form(request.form)
    if errors:
        return _render_task_detail(task, values=values, errors=errors)

    comments.add_comment(task_id, values)
    flash("Comment added.", "success")
    return redirect(url_for("tasks.detail", task_id=task_id))

