"""SQL text scanner.

Responsibility: the one piece of code that knows how SQLite's tokenizer
pairs quotes and comments. `scan` splits SQL text into tokens so callers can
tell structure (`;`, parentheses, names) from quoted text. It does not parse
SQL and knows nothing about the database; `sql_guard` builds its text checks
on it.
"""

from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Opening quote character -> closing one, as SQLite's tokenizer pairs them:
# strings, and the three ways to quote an identifier.
_QUOTES = {"'": "'", '"': '"', "`": "`", "[": "]"}


class Token(BaseModel):
    """One lexical piece of SQL text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # "name" is a quoted identifier; "word" a keyword, bare name or number.
    kind: Literal["string", "name", "word", "comment", "symbol"]
    text: str  # as written; for a string or a quoted name, the content without its quotes


class UnterminatedQuote(ValueError):
    """A quote was opened and never closed. `quote` is the opening character."""

    def __init__(self, quote: str) -> None:
        super().__init__(f"unterminated {quote} quote")
        self.quote = quote


def scan(text: str) -> Iterator[Token]:
    """Split SQL text into tokens, pairing quotes and comments as SQLite's tokenizer does.

    Whitespace is dropped. A word is a run of letters, digits and `_`
    (keywords, names, numbers); every other character is a one-character
    symbol. Raises `UnterminatedQuote` when it reaches an unclosed quote.
    """
    position = 0
    while position < len(text):
        character = text[position]
        if character in _QUOTES:
            end = _end_of_quoted(text, position)
            close = _QUOTES[character]
            content = text[position + 1 : end - 1]
            if close != "]":
                content = content.replace(close * 2, close)
            yield Token(kind="string" if character == "'" else "name", text=content)
        elif text.startswith("--", position):
            end = text.find("\n", position)
            end = len(text) if end == -1 else end
            yield Token(kind="comment", text=text[position:end])
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            end = len(text) if end == -1 else end + 2
            yield Token(kind="comment", text=text[position:end])
        elif character.isspace():
            end = position + 1
        elif _is_word_character(character):
            end = position + 1
            while end < len(text) and _is_word_character(text[end]):
                end += 1
            yield Token(kind="word", text=text[position:end])
        else:
            end = position + 1
            yield Token(kind="symbol", text=character)
        position = end


def _is_word_character(character: str) -> bool:
    return character.isalnum() or character == "_"


def _end_of_quoted(text: str, start: int) -> int:
    """Index just past the quoted text that opens at `start`."""
    close = _QUOTES[text[start]]
    position = start + 1
    while True:
        position = text.find(close, position)
        if position == -1:
            raise UnterminatedQuote(text[start])
        position += 1
        # A doubled quote is an escaped quote, except in [bracket] names.
        if close != "]" and text.startswith(close, position):
            position += 1
            continue
        return position
