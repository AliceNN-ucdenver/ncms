"""Architectural fitness functions.

Tests here enforce *structural* properties of the codebase — not
behavior — so that the architecture we landed in Phase 0 can't
silently regress:

- ``test_complexity_gate.py`` — no D+ cyclomatic complexity in
  application code.
- ``test_import_boundaries.py`` — pipeline packages don't import each
  other; domain layer has no infrastructure dependencies.

Run with ``pytest tests/architecture/`` or as part of the full suite.
"""
