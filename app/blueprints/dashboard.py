"""The dashboard: four task counts and the project details, served at ``GET /``.

This blueprint owns one route and does one thing with it. The four numbers are
read from :func:`app.services.metrics.dashboard_metrics` and the project panel
from :func:`app.services.projects.get_project`, and both results are handed to
``dashboard.html`` unchanged: the view writes no SQL, computes nothing, reads
nothing from the request, and holds no business rule. That keeps the layer rule
of the application intact -- blueprints parse a request and call a service -- and
it is why this module has no ``get_db``, no ``request``, no ``flash`` and no
``redirect``.

Both service calls sit inside the view body rather than at module level or
behind a cache, so the counts are recomputed on every request. A dashboard
number is derived data: creating a task, assigning it or changing its status is
reflected the next time the page loads, and no code path exists that could leave
a stale figure on screen.

The blueprint is registered without a ``url_prefix``, which is what puts the
dashboard on ``/``. Its name is the endpoint contract: ``dashboard.index`` is
what ``url_for("dashboard.index")`` resolves everywhere the interface links home
-- the navigation in ``base.html`` and the "Back to the dashboard" link on the
error pages -- so the blueprint name and the view name are both load-bearing.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from app.services.metrics import dashboard_metrics
from app.services.projects import get_project

__all__ = ["bp"]

bp = Blueprint("dashboard", __name__)


@bp.route("/", methods=["GET"])
def index():
    """Render the dashboard from the metric and the project service.

    ``app.services.metrics.dashboard_metrics()`` supplies the counts and
    ``app.services.projects.get_project()`` the project panel. ``dashboard.html``
    is written against exactly those two names: ``metrics`` carries the ``total``,
    ``completed``, ``pending`` and ``in_progress`` counts, whose ``total`` chooses
    between the create control and the dashboard's own empty state, and
    ``project`` is the stored :class:`app.models.Project` or ``None`` while no
    project details have been recorded yet.

    Returns:
        The rendered dashboard with the default 200 status. The route accepts no
        input and changes nothing, so it has no error page of its own.
    """
    return render_template(
        "dashboard.html",
        metrics=dashboard_metrics(),
        project=get_project(),
    )
