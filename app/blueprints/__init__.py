"""Flask blueprints: the HTTP layer of the task-tracking application.

Each module owns one blueprint (``dashboard``, ``tasks``, ``project``), parses
the request, calls one service function and then flashes and redirects or
re-renders its template with validation errors.
"""
