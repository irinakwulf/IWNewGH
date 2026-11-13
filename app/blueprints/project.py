"""The project screen: the single project record, served at ``GET|POST /project``.

The application tracks exactly one project, and this blueprint is the only place
its details are shown and edited. The stored record comes from
:func:`app.services.projects.get_project`, a submission is read by
:func:`app.validation.parse_project_form`, and an accepted one is written by
:func:`app.services.projects.save_project`. Nothing else happens here: the view
writes no SQL, holds no length or date rule, invents no message, and reads no
redirect target from the request. That keeps the layer rule of the application
intact -- a blueprint parses a request, calls one service and chooses where the
response goes -- which is why this module has no ``get_db`` and no second route.

``POST`` is one of the form's own data-entry writes, so a rejected submission is
not an error page. The submitted values and the parser's ``{field: message}``
errors are handed straight back to ``project.html`` with the default 200 status,
no row is written, and the reviewer corrects one field instead of retyping the
form. An accepted submission flashes one confirmation and redirects, so the page
the reviewer lands on reads the database again and a refresh cannot resubmit.

Two states the view must serve without special-casing. Before the details have
ever been recorded ``get_project()`` returns ``None``, and the GET still renders
a 200 with an empty form, because the first save is what creates the row. After
that the same form edits it: ``save_project`` decides between insert and update,
so this view has one path for both.

The blueprint name and the view name are both load-bearing: no ``url_prefix``
puts the screen on ``/project`` as the API contract specifies, and the endpoint
``project.view`` is what ``url_for("project.view")`` resolves in the navigation,
on the dashboard's "Edit project details" button and in this view's own redirect.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.services.projects import get_project, save_project
from app.validation import parse_project_form

__all__ = ["bp"]

bp = Blueprint("project", __name__)


def _values_from(project) -> dict[str, str]:
    """Return the ``values`` mapping the project form renders from a stored project.

    Args:
        project: The record :func:`app.services.projects.get_project` returned, or
            ``None`` while no project details have been recorded yet. It is not
            annotated with :class:`app.models.Project` because this module's import
            surface is the two modules it delegates to; the attributes it reads --
            ``name``, ``description``, ``start_date`` and ``end_date`` -- are the
            four fields the form edits.

    Returns:
        A mapping with exactly the four form field names. Each value is the stored
        text, or the empty string when there is no record, so a first render shows
        blank fields rather than the literal ``"None"`` and the template needs no
        condition around the input values.
    """
    if project is None:
        return {"name": "", "description": "", "start_date": "", "end_date": ""}
    return {
        "name": project.name,
        "description": project.description,
        "start_date": project.start_date,
        "end_date": project.end_date,
    }


@bp.route("/project", methods=["GET", "POST"])
def view():
    """Render ``project.html``; a valid post delegates to ``projects.save_project()``.

    The stored record is read only on the two paths that render it: a rejected post
    re-reads it, so the details list still shows what is in the database while the
    form shows what the reviewer typed, and the ``GET`` needs it for the same
    details list and for the form's prefilled values. An accepted post reads no
    project row at all -- it confirms the save and leaves the reading to the page
    it redirects to. A successful post flashes "Project details saved." and
    redirects to this same endpoint -- a target named by :func:`flask.url_for`,
    never taken from the request.

    Returns:
        On ``GET``, the rendered project screen with the stored project (or
        ``None``), the values its form shows and an empty error mapping. On an
        accepted ``POST``, a 302 to ``/project``. On a rejected ``POST``, the same
        screen with the submitted values and the parser's field messages, carrying
        the default 200 status and having written nothing.
    """
    if request.method == "POST":
        values, errors = parse_project_form(request.form)
        if errors:
            project = get_project()
            return render_template(
                "project.html", project=project, values=values, errors=errors
            )
        save_project(values)
        flash("Project details saved.", "success")
        return redirect(url_for("project.view"))
    project = get_project()
    return render_template(
        "project.html", project=project, values=_values_from(project), errors={}
    )
