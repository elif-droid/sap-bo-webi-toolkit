"""Every page renders without a BO server (no network calls happen until a button is pressed)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = str(Path(__file__).resolve().parents[1])
PAGES = [("convert", "render"), ("pending", "render"), ("rls", "render"), ("rollback", "render"),
         ("history", "render_log"), ("history", "render_errors"), ("inventory", "render"),
         ("security", "render"), ("sql_pull", "render"), ("explorer", "render")]


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.mark.parametrize("module,func", PAGES)
def test_page_renders(module, func):
    script = f"""
import sys
sys.path.insert(0, {ROOT!r})
from views import {module}
{module}.{func}()
"""
    at = AppTest.from_string(script, default_timeout=30).run()
    assert not at.exception, at.exception


def test_app_boots():
    at = AppTest.from_file(str(Path(ROOT) / "app.py"), default_timeout=30).run()
    assert not at.exception, at.exception
