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
        "project_context.domain.projects",
        "project_context.domain.audit",
        "project_context.domain.sources",
        "project_context.domain.evidence",
        "project_context.db",
        "project_context.db.connection",
        "project_context.db.migrations",
        "project_context.db.health",
        "project_context.db.projects_repository",
        "project_context.db.audit_repository",
        "project_context.db.sources_repository",
        "project_context.db.evidence_repository",
        "project_context.services",
        "project_context.services.projects",
        "project_context.services.evidence",
        "project_context.evidence_store",
        "project_context.spans",
        "project_context.chunking",
        "project_context.connectors",
        "project_context.parsers",
        "project_context.parsers.kinds",
        "project_context.parsers.models",
        "project_context.parsers.registry",
        "project_context.parsers.txt_parser",
        "project_context.parsers.docx_parser",
        "project_context.parsers.pdf_parser",
        "project_context.parsers.vtt_parser",
        "project_context.llm",
        "project_context.retrieval",
        "project_context.ui",
        "project_context.ui.session",
        "project_context.ui.chrome",
        "project_context.ui.db",
        "project_context.ui.project_scope",
        "project_context.ui.pages",
        "project_context.ui.pages.projects",
        "project_context.ui.pages.project_overview",
        "project_context.ui.pages.activity",
        "project_context.ui.pages.ledger",
        "project_context.ui.pages.evidence",
        "project_context.ui.pages.briefs",
        "project_context.ui.pages.sources_settings",
        "project_context.ui.navigation",
    ]
    for name in submodules:
        importlib.import_module(name)

    assert list(tmp_path.iterdir()) == []
