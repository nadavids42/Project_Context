"""Smoke test: the application package must import cleanly, with no side
effects (no filesystem writes, no network calls, no directory creation)."""

from __future__ import annotations

import importlib


def test_import_project_context_package():
    module = importlib.import_module("project_context")
    assert module.__version__


def test_import_all_submodules_without_side_effects(tmp_path, monkeypatch):
    # If any submodule created files/directories merely on import, they
    # would land in the (empty) cwd we chdir into here.
    monkeypatch.chdir(tmp_path)

    submodules = [
        "project_context.config",
        "project_context.observability",
        "project_context.ids",
        "project_context.timeutil",
        "project_context.domain",
        "project_context.db",
        "project_context.db.connection",
        "project_context.db.migrations",
        "project_context.db.health",
        "project_context.services",
        "project_context.connectors",
        "project_context.parsers",
        "project_context.llm",
        "project_context.retrieval",
        "project_context.ui",
    ]
    for name in submodules:
        importlib.import_module(name)

    assert list(tmp_path.iterdir()) == []
