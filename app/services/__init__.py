"""HTTP-free business rules for the taskboard application.

This package is the domain layer. The views in ``app.blueprints`` parse a
request and then call one function here; these modules reach SQLite only
through :func:`app.db.get_db`, and they return the dataclasses declared in
``app.models`` rather than raw rows. Nothing here touches ``request``,
``flash``, ``redirect`` or a template.

The package marker imports nothing on purpose. ``app.services.tasks`` imports
its siblings, so importing them eagerly here would leave the package partially
initialised and invite import cycles; callers import a module directly with
``from app.services import tasks``.
"""
