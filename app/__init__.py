"""The application factory: the single entry point that builds the taskboard app.

:func:`create_app` is the one place the application is assembled, and it wires its
parts in an order the rest depends on: configuration first, then the resolved
database path and the folder holding it, then the data layer, then the template
filters, then the schema bootstrap, then the blueprints and the error handlers.
Nothing runs at import time beyond defining the function, so ``flask --app app``
and each test build an application on demand. A test hands this function a
temporary database path and gets an isolated application, which a module-level
application object would make impossible.

Two constraints here are not obvious and both are load-bearing:

* The database path is resolved against ``os.path.dirname(app.root_path)`` -- the
  repository root -- rather than against ``app.instance_path``, which follows
  wherever the package is resolved from: for an installed distribution it shifts to
  ``<prefix>/var/app-instance``, off the documented location. The repository root is
  what :data:`app.config.DEFAULT_DATABASE`, the ``.gitignore`` entry and the
  documented commands are fixed relative to.
* The ``init-db`` and ``seed-db`` commands are registered by
  :func:`app.db.init_app` and must not be added again here: a second registration
  makes ``flask --app app`` fail with "Command already registered".
"""

from __future__ import annotations

import logging
import os
import sqlite3

from flask import Flask, Request, Response, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from app import config, db, templating
from app.blueprints import dashboard, project, tasks
from app.models import DEADLINE_FILTER_LABELS, TASK_STATUSES
from app.validation import ASSIGNEE_UNASSIGNED, DEADLINE_FILTERS, FIELD_LIMITS

__all__ = ["create_app"]

# The single start-up line the application logs, naming where its data lives. The
# application writes nothing else at start-up: this project runs no structured
# logging layer, and the resolved path is the one fact worth having in the log.
_STARTUP_MESSAGE = "Taskboard ready; database at %s"

# The endpoints that answer a form post and no read request: the two quick actions
# and the comment post on the task detail page. A read of one of them belongs on the
# task page the action hangs off, which is what the 405 handler below sends it to.
_WRITE_ONLY_TASK_ENDPOINTS = frozenset(
    {"tasks.add_comment", "tasks.set_assignee", "tasks.set_status"}
)


def _task_id_for_write_action(app: Flask, request_obj: Request) -> int | None:
    """Return the task a write-only action path names, or None for any other path.

    The path is matched against the application's own URL map as the post it would
    answer, so the endpoint and the task id are the ones Flask itself binds rather
    than anything read back out of the request's text.

    Args:
        app: The application whose URL map is matched against.
        request_obj: The request whose path is being read.

    Returns:
        The task id behind a route in :data:`_WRITE_ONLY_TASK_ENDPOINTS`, or
        ``None`` when the path is not one such route.
    """
    try:
        endpoint, arguments = app.url_map.bind_to_environ(request_obj.environ).match(
            method="POST"
        )
    except HTTPException:
        return None
    if endpoint not in _WRITE_ONLY_TASK_ENDPOINTS:
        return None
    task_id = arguments.get("task_id")
    return task_id if isinstance(task_id, int) else None


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    """Build and configure the application; ``test_config`` overrides the defaults.

    Args:
        test_config: Values layered over the defaults from
            :func:`app.config.config_mapping`, in practice a temporary ``DATABASE``
            path and ``TESTING``. ``None`` -- what the Flask CLI passes -- leaves
            the documented defaults untouched.

    Returns:
        A ready :class:`flask.Flask` application: configured, with its database
        folder present and its schema applied, the four template filters
        registered, the five template globals published -- ``TASK_STATUSES`` and
        ``DEADLINE_FILTER_LABELS`` from :mod:`app.models`, and ``DEADLINE_FILTERS``,
        ``ASSIGNEE_UNASSIGNED`` and ``FIELD_LIMITS`` from :mod:`app.validation`, so
        a template reads the shared vocabulary rather than restating it -- the three
        blueprints mounted at the paths their routes declare, the two database
        commands available to the CLI, the 400, 404 and 500 handlers rendering the
        application's own error pages, and the handler that keeps a read of a
        write-only task action inside the shell.

    Raises:
        click.ClickException: When the configured ``DATABASE`` cannot be used:
            either its folder cannot be created, or the file cannot be opened or
            read as SQLite. The message names the setting, the resolved path, the
            library's reason and the fix, so the CLI reports a misconfiguration
            on one line instead of as a traceback. Nothing is created, repaired,
            retried or deleted on the operator's behalf beyond the database's own
            folder.
    """
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(config.config_mapping(test_config))

    # Flask leaves `app.logger` at NOTSET, so its effective level is the root logger's
    # WARNING and the INFO records this factory and the schema bootstrap emit -- the
    # start-up line naming the served database among them -- would be discarded at the
    # documented run command. Raising this logger's level is all it takes: the handler
    # and the propagation stay Flask's own, so no logging configuration is added.
    app.logger.setLevel(logging.INFO)

    # Absolute by the time this returns, so `makedirs` always has a real parent and
    # a test's tmp_path database is used exactly as given. Both failure paths below
    # name this one value, so their messages cannot disagree with what is used.
    repository_root = os.path.dirname(app.root_path)
    database_path = config.resolve_database_path(
        app.config["DATABASE"], repository_root
    )
    app.config["DATABASE"] = database_path
    try:
        os.makedirs(os.path.dirname(database_path), exist_ok=True)
    except OSError as error:
        raise db.unusable_database_error(database_path, error) from error

    # Registers the connection teardown and the init-db and seed-db commands.
    db.init_app(app)
    templating.init_app(app)

    # Template vocabulary no view may restate: the closed status set and the deadline
    # buckets with the wording each shows, all owned by app/models.py, the sentinel
    # and the bounds app/validation.py enforces, and the buckets that module accepts.
    # A template iterating one of those sets, or reading a maxlength from
    # FIELD_LIMITS, cannot drift from the rule behind it.
    app.jinja_env.globals.update(
        ASSIGNEE_UNASSIGNED=ASSIGNEE_UNASSIGNED,
        DEADLINE_FILTERS=DEADLINE_FILTERS,
        DEADLINE_FILTER_LABELS=DEADLINE_FILTER_LABELS,
        FIELD_LIMITS=FIELD_LIMITS,
        TASK_STATUSES=TASK_STATUSES,
    )

    # A fresh checkout must serve the dashboard, the task list and the project
    # screen -- empty states, zero counts -- without anyone running init-db first.
    # An unusable file (a directory, or bytes that are not SQLite) fails here, and
    # a corrupt database is exactly what `seed-db` is meant to rebuild, so the
    # failure is translated rather than left to abort the CLI before the command
    # body runs.
    with app.app_context():
        try:
            db.ensure_schema()
        except sqlite3.Error as error:
            raise db.unusable_database_error(database_path, error) from error

    # Registered without a url_prefix: each blueprint declares its full paths, and
    # its name and view names are what every template's url_for resolves against.
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(tasks.bp)
    app.register_blueprint(project.bp)

    # On the application object, not a blueprint: an error raised while building a
    # URL, or before a blueprint has been chosen, must still reach these pages.
    @app.errorhandler(400)
    def bad_request(error: HTTPException) -> tuple[str, int]:
        """Render the 400 page for a malformed filter, status or assignee value."""
        return render_template("errors/400.html", error=error), 400

    @app.errorhandler(404)
    def not_found(error: HTTPException) -> tuple[str, int]:
        """Render the 404 page for a task id that does not exist."""
        return render_template("errors/404.html", error=error), 404

    @app.errorhandler(500)
    def server_error(error: HTTPException) -> tuple[str, int]:
        """Render the 500 page so an unexpected failure is never a traceback."""
        return render_template("errors/500.html", error=error), 500

    @app.errorhandler(405)
    def method_not_allowed(error: HTTPException) -> Response | HTTPException:
        """Send a read of a write-only task action to the task page it belongs to.

        A rejected data-entry post re-renders its own form at 200, so the address bar
        keeps the path it posted to. A GET of one of the three task actions -- a
        bookmark, a hand-typed URL, a reload whose resubmission was cancelled --
        would otherwise be answered by Werkzeug's own 405 page, which sits outside
        the application's shell. The route that would have answered the post names
        the task, so the reviewer is sent to that task's page through ``url_for``;
        every other method mismatch keeps the default response, because no page for
        it is part of this product.
        """
        if request.method in ("GET", "HEAD"):
            task_id = _task_id_for_write_action(app, request)
            if task_id is not None:
                return redirect(url_for("tasks.detail", task_id=task_id))
        return error

    app.logger.info(_STARTUP_MESSAGE, app.config["DATABASE"])
    return app
