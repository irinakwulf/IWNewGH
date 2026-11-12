"""Configuration defaults for the taskboard application.

This module owns the default configuration mapping that the application factory
applies, plus the path helper the factory uses to place the SQLite file under
the repository's ``instance`` directory. It creates no directory and no file:
creating ``instance/`` is the factory's job.

Only two environment variables are read, and both have working defaults, so the
application starts with neither set:

* ``DATABASE`` -- overrides the SQLite database path.
* ``SECRET_KEY`` -- signs the session cookie that carries flash messages; the
  built-in fallback is for development only.

The module imports nothing from Flask and nothing from elsewhere in the package.
It is the lowest layer ``create_app()`` reads first, so a dependency on any other
module would risk an import cycle.
"""

import os

# Development-only session key, used when SECRET_KEY is not set in the
# environment. Visibly fake on purpose: it must never be mistaken for a real
# secret, and no real key is ever committed to the repository.
DEV_SECRET_KEY = "dev-only-insecure-secret-key"

# Default SQLite path, relative to the repository root.
DEFAULT_DATABASE = "instance/taskboard.sqlite3"


def config_mapping(
    test_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return the default configuration merged with an optional test override.

    Args:
        test_config: Values a caller wants layered over the defaults, in practice
            a test supplying a temporary database path and ``TESTING``. ``None``
            leaves the defaults untouched.

    Returns:
        A new dict holding the five default keys, with every key in
        ``test_config`` replacing its default. Each call returns a fresh dict, so
        no caller can mutate the defaults another caller reads.
    """
    mapping: dict[str, object] = {
        "DATABASE": os.environ.get("DATABASE") or DEFAULT_DATABASE,
        "SECRET_KEY": os.environ.get("SECRET_KEY") or DEV_SECRET_KEY,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_HTTPONLY": True,
        "TESTING": False,
    }
    if test_config is not None:
        mapping.update(test_config)
    return mapping


def resolve_database_path(
    value: str | os.PathLike[str],
    repository_root: str | os.PathLike[str],
) -> str:
    """Return an absolute SQLite path, joining a relative value onto repository_root.

    Flask's own ``app.instance_path`` is deliberately not the base: for a source
    package it resolves to ``<repo>/app/instance``, while the documented database
    location is ``<repo>/instance/taskboard.sqlite3``. Resolving against the
    repository root keeps the default path, the ``.gitignore`` entry and the
    documented commands pointing at the same file.

    Args:
        value: The configured database path, absolute or relative to
            ``repository_root``. An absolute value -- which is what a test passing
            a ``tmp_path`` database supplies -- is returned unchanged.
        repository_root: Directory a relative ``value`` is resolved against.

    Returns:
        The path as an absolute ``str``. No directory is created and no file is
        touched, and ``~`` is left unexpanded because nothing in the product
        implies home-directory expansion.
    """
    path = os.fspath(value)
    if os.path.isabs(path):
        return path
    return os.path.join(os.fspath(repository_root), path)
