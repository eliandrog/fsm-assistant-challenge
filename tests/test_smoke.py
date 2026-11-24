"""Smoke test: the package installs and the entry point runs."""

import pytest

import fsm_assistant
from fsm_assistant.cli import main


def test_package_imports() -> None:
    assert fsm_assistant.__doc__


def test_cli_without_a_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "check-model" in capsys.readouterr().out
