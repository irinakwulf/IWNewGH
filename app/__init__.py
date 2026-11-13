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
  repository root -- rather than against ``app.instance_path``, because Flask's
  automatic instance path for this source package is ``<repo>/app/instance`` while
  the documented database lives under ``<repo>/instance/``, which is where
  :data:`app.config.DEFAULT_DATABASE` points.
* The ``init-db`` and ``seed-db`` commands are registered by
  :func:`app.db.init_app` and must not be added again here: a second registration
  makes ``flask --app app`` fail with "Command already registered".
"""

from __future__ import annotations

import os

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from app import config, db, templating
from app.blueprints import dashboard, project, tasks

__all__ = ["create_app"]

# The single start-up line the application logs, naming where its data lives. The
# application writes nothing else at start-up: this project runs no structured
# logging layer, and the resolved path is the one fact worth having in the log.
_STARTUP_MESSAGE = "Taskboard ready; database at %s"


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
        registered, the three blueprints mounted at the paths their routes declare,
        the two database commands available to the CLI, and the 400, 404 and 500
        handlers rendering the application's own error pages.

    Raises:
        OSError: When the database's folder cannot be created. An error from
            :mod:`app.db` while the schema is applied propagates unchanged: a
            half-built database fails loudly rather than silently.
    """
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(config.config_mapping(test_config))

    # Absolute by the time this returns, so `makedirs` always has a real parent and
    # a test's tmp_path database is used exactly as given.
    repository_root = os.path.dirname(app.root_path)
    app.config["DATABASE"] = config.resolve_database_path(
        app.config["DATABASE"], repository_root
    )
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    # Registers the connection teardown and the init-db and seed-db commands.
    db.init_app(app)
    templating.init_app(app)

    # A fresh checkout must serve the dashboard, the task list and the project
    # screen -- empty states, zero counts -- without anyone running init-db first.
    with app.app_context():
        db.ensure_schema()

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

    app.logger.info(_STARTUP_MESSAGE, app.config["DATABASE"])
    return app
