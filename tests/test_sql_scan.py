"""sql_scan: tokens, with quotes and comments paired as SQLite pairs them."""

import pytest
from pydantic import ValidationError

from fsm_assistant.sql_scan import Token, UnterminatedQuote, scan


def test_scan_pairs_quotes_and_comments() -> None:
    sql = "SELECT 'it''s; )', \"a \"\"b\"\"\", [c d], `e` -- note\n/* x */ date(1.5)"
    tokens = [(token.kind, token.text) for token in scan(sql)]
    assert tokens == [
        ("word", "SELECT"),
        ("string", "it's; )"),
        ("symbol", ","),
        ("name", 'a "b"'),
        ("symbol", ","),
        ("name", "c d"),
        ("symbol", ","),
        ("name", "e"),
        ("comment", "-- note"),
        ("comment", "/* x */"),
        ("word", "date"),
        ("symbol", "("),
        ("word", "1"),
        ("symbol", "."),
        ("word", "5"),
        ("symbol", ")"),
    ]
    with pytest.raises(UnterminatedQuote):
        list(scan("SELECT 'abc"))


@pytest.mark.parametrize(("sql", "quote"), [("SELECT 'abc", "'"), ('"id = 1', '"'), ("[id", "["), ("`id", "`")])
def test_an_unclosed_quote_names_its_opening_character(sql: str, quote: str) -> None:
    with pytest.raises(UnterminatedQuote) as raised:
        list(scan(sql))
    assert raised.value.quote == quote


def test_an_unclosed_block_comment_runs_to_the_end() -> None:
    assert [(token.kind, token.text) for token in scan("1 /* open")] == [("word", "1"), ("comment", "/* open")]


def test_tokens_are_immutable() -> None:
    token = Token(kind="word", text="SELECT")
    with pytest.raises(ValidationError):
        token.text = "DELETE"
