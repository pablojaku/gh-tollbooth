"""tollbooth_text: the rules for text gh-tollbooth doesn't trust, in one place.

Text comes in from agents (the arguments of `call`) and goes out to a terminal (headers, the call log,
messages). One set of hidden characters serves both ways: characters a person reading a terminal can't
see but a program or model still reads. An agent's argument may not carry one (has_hidden(); tab and
newline are allowed), nor argv bytes that weren't UTF-8 (is_utf8()); the guard refuses both. Everything
gh-tollbooth prints goes through printable(), which shows each hidden character as "?", tab and newline
included. LABEL_RE is the form of a Label; RESOURCE_RE the form of a resource name a message may repeat.
Not here: what a request may ask GitHub (tollbooth_guard).
"""
from __future__ import annotations

import re
import unicodedata

LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")  # always use .fullmatch: "$" would accept a trailing newline
RESOURCE_RE = re.compile(r"[a-z_]{1,40}")       # GitHub's rate-limit resources: core, graphql, dependency_sbom, ...

# Hidden: controls (Cc), format characters such as zero-width, bidi and tag characters (Cf), line and
# paragraph separators (Zl, Zp) and lone surrogates (Cs), as each Python's Unicode version files them...
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
# ...and the variation selectors, which Unicode files as combining marks (Mn): "emoji smuggling" hides one
# byte in each after a visible character. Mongolian U+180B-180D and U+180F (U+180E is Cf), U+FE00-FE0F,
# U+E0100-E01EF. Built from code points, so the source file itself holds no hidden characters.
_SELECTORS = frozenset([*range(0x180B, 0x180E), 0x180F, *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)])
_LET_IN = frozenset("\t\n")  # an agent's argument may carry these: a GraphQL document has lines
# Lone surrogates: how Python decodes argv bytes that aren't UTF-8 (U+DC80-U+DCFF); no request can carry them.
_NOT_UTF8_RE = re.compile("[\ud800-\udfff]")


def _hidden(ch: str) -> bool:
    return ord(ch) in _SELECTORS or unicodedata.category(ch) in _HIDDEN_CATEGORIES


def printable(text: str) -> str:
    """`text` with each hidden character as "?", tab and newline included, so nothing printed can drive a
    terminal or hide text from the person reading it."""
    return "".join("?" if _hidden(c) else c for c in text)


def has_hidden(text: str) -> bool:
    """Whether an agent's argument carries a hidden character (tab and newline are allowed)."""
    return any(_hidden(c) and c not in _LET_IN for c in text)


def is_utf8(text: str) -> bool:
    """Whether `text` came from valid UTF-8: Python decodes other argv bytes to lone surrogates."""
    return _NOT_UTF8_RE.search(text) is None
