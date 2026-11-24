"""Smoke test: the package installs and the entry point runs."""

import pytest

import fsm_assistant
from fsm_assistant.cli import main


def test_package_imports() -> None:
    assert fsm_assistant.__doc__


def test_cli_placeholder_runs(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert "scaffold" in capsys.readouterr().out
