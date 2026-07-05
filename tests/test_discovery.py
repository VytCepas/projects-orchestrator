"""Tests for project discovery and rendering."""

from __future__ import annotations

import pytest

from projects_orchestrator.discovery import discover, status_of
from projects_orchestrator.render import render_html, render_json, render_tui

MARKER = """# Project: demo-app

> A demo project

| | |
|---|---|
| Language | python |
| Memory stack | auto |
| MCPs | none |
"""


@pytest.fixture
def project_root(tmp_path):
    """Create a fake project-init project tree under a scan root."""
    proj = tmp_path / "demo-app" / ".claude"
    proj.mkdir(parents=True)
    (proj / "project-init.md").write_text(MARKER, encoding="utf-8")
    return tmp_path


def test_discover_finds_marker(project_root):
    assert len(discover(project_root)) == 1


def test_discover_reads_name(project_root):
    assert discover(project_root)[0].name == "demo-app"


def test_discover_reads_language(project_root):
    assert discover(project_root)[0].language == "python"


def test_discover_ignores_unmarked_dirs(tmp_path):
    (tmp_path / "plain").mkdir()
    assert discover(tmp_path) == []


def test_render_tui_includes_project_name(project_root):
    assert "demo-app" in render_tui(discover(project_root))


def test_render_html_is_a_document(project_root):
    assert render_html(discover(project_root)).startswith("<!doctype html>")


def test_render_json_is_a_list(project_root):
    assert render_json(discover(project_root)).startswith("[")


def test_status_of_unversioned_when_no_git(project_root):
    assert status_of(discover(project_root)[0]) == "unversioned"
