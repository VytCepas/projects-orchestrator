"""Adapters that read cross-repo state (GitHub, cloud) for the fleet.

Each adapter shells out to the owning CLI through the shared, timeout-bounded
:func:`projects_orchestrator.runner.run_command`, and degrades to ``unknown``
when that CLI is missing, unauthenticated, or offline — never raising.
"""
