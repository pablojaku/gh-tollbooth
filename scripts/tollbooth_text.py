"""tollbooth_text: the rules for text gh-tollbooth doesn't trust, in one place.

Text comes in from agents (the arguments of `call`) and goes out to a terminal (headers, the call log,
messages). An agent's argument may not carry control or invisible characters: has_hidden() finds them
and is_utf8() finds argv bytes that weren't UTF-8; the guard refuses both. Printed text goes through
printable(), so nothing gh-tollbooth prints can drive the terminal. LABEL_RE is the form of a Label.
Not here: what a request may ask GitHub (tollbooth_guard).
"""
from __future__ import annotations

import re

LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # always use .fullmatch: "$" would accept a trailing newline

# Printed text: C0 and C1 control characters, which could drive the terminal.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# An agent's arguments: control chars (except tab and newline), CR (a GraphQL line terminator), NEL,
# zero-width and bidi-control characters, Unicode line/paragraph separators and the BOM. Built from code
# points so the source file itself contains no invisible characters.
_BAD_CODEPOINTS = [*range(0x00, 0x09), *range(0x0B, 0x20), 0x7F, 0x85, *range(0x200B, 0x2010),
                   0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF]
_BAD_CHARS_RE = re.compile("[" + re.escape("".join(map(chr, _BAD_CODEPOINTS))) + "]")
# Lone surrogates: how Python decodes argv bytes that aren't UTF-8 (U+DC80-U+DCFF); no request can carry them.
_NOT_UTF8_RE = re.compile("[\ud800-\udfff]")


def printable(text: str) -> str:
    """`text` with control characters as "?", so nothing printed can drive a terminal."""
    return _CONTROL_RE.sub("?", text)


def has_hidden(text: str) -> bool:
    """Whether an agent's argument carries a control or invisible character (tab and newline are allowed)."""
    return _BAD_CHARS_RE.search(text) is not None


def is_utf8(text: str) -> bool:
    """Whether `text` came from valid UTF-8: Python decodes other argv bytes to lone surrogates."""
    return _NOT_UTF8_RE.search(text) is None
