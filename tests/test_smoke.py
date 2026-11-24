"""Smoke test: the package installs and the entry point runs."""

import fsm_assistant
from fsm_assistant.cli import main


def test_package_imports():
    assert fsm_assistant.__doc__


def test_cli_placeholder_runs(capsys):
    main()
    assert "scaffold" in capsys.readouterr().out
