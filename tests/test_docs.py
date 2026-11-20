"""SKILL.md's Reference block against the CLI: every command, option, exit code and setting, and nothing else.

Agents read the Reference block instead of --help, so a flag added to the CLI without a doc update, or a
documented flag that doesn't exist, fails here. The commands and options come from Click itself.
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

import tollbooth_cli as cli
import tollbooth_core as core
import tollbooth_guard as guard

ROOT = Path(__file__).resolve().parent.parent
GLOBAL_OPTIONS = {"--as", "--budget"}
OPTION_RE = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*")  # -X, --min-stars; not `-` inside words or numbers


def reference_block() -> str:
    """The one fenced block under `## Reference` in SKILL.md."""
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^## Reference\n(?:(?!^## ).)*?^```[a-z]*\n(.*?)^```", text, re.S | re.M)
    assert m is not None, "SKILL.md needs a `## Reference` section with a fenced block"
    return m.group(1)


def entries(block: str) -> dict[str, str]:
    """The block's entries: a line that starts at column 0, plus the indented lines that continue it.

    Keyed by the first two words for commands ("gh-tollbooth rank"), else the first word ("Exit:")."""
    out: dict[str, list[str]] = {}
    key = ""
    for line in block.splitlines():
        if line and not line[0].isspace():
            words = line.split()
            key = " ".join(words[:2]) if words[0] == "gh-tollbooth" else words[0]
            out[key] = []
        out.setdefault(key, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def options(text: str) -> set[str]:
    return set(OPTION_RE.findall(text.split(">")[0]))  # a `> RAW.jsonl` redirect is not an option


def spellings(command: click.Command) -> set[str]:
    """Every option spelling of a Click command, without --help and the global --as and --budget."""
    return {s for p in command.params if isinstance(p, click.Option) for s in p.opts + p.secondary_opts} \
        - {"-h", "--help"} - GLOBAL_OPTIONS


def exit_codes(text: str) -> set[int]:
    """The numbers in a list like `Exit codes: 0 ok; 1 no token; ...` or `Exit: 0 ok · 1 no token · ...`."""
    return {int(n) for n in re.findall(r"(?:Exit(?: codes)?:|[;·])\s*(\d+)\b", re.sub(r"\s+", " ", text))}


@pytest.fixture(scope="module")
def ref() -> dict[str, str]:
    return entries(reference_block())


def test_every_command_is_listed(ref):
    documented = {k.split()[1] for k in ref if k.startswith("gh-tollbooth ") and not k.endswith("--help")}
    assert documented == set(cli.cli.commands), "commands in SKILL.md's Reference block"


@pytest.mark.parametrize("name", sorted(cli.cli.commands))
def test_every_option_of_a_command_is_listed(ref, name):
    entry = ref.get(f"gh-tollbooth {name}")
    assert entry is not None, f"the Reference block has no line starting with 'gh-tollbooth {name}'"
    if name == "call":  # what follows `--` belongs to the guard, checked below
        entry = entry.split(" -- ")[0]
    assert options(entry) == spellings(cli.cli.commands[name]), f"options of `{name}` in SKILL.md's Reference block"


def test_the_global_options_are_listed(ref):
    assert options(ref["Global,"]) == GLOBAL_OPTIONS
    group = {s for p in cli.cli.params if isinstance(p, click.Option) for s in p.opts}
    assert group - {"-h", "--help"} == GLOBAL_OPTIONS, "the top-level options changed: update SKILL.md"


def test_call_lists_every_option_the_guard_accepts(ref):
    # `call -- api` takes the options of guard._api_args. A kind of option may have several spellings
    # (-f, --raw-field); the block names at least one per kind, and nothing the guard doesn't accept.
    after = options(ref["gh-tollbooth call"].split(" -- ", 1)[1])
    visible = [p for p in guard._api_args.params if isinstance(p, click.Option) and not p.hidden]
    accepted = {s for p in visible for s in p.opts + p.secondary_opts}
    assert after <= accepted, "call options the guard doesn't accept"
    for p in visible:
        assert after & set(p.opts), f"call option {p.opts} missing from SKILL.md's Reference block"


def test_exit_codes_match_the_help_text_and_the_code(ref):
    help_text = cli.cli.get_help(click.Context(cli.cli))
    documented = exit_codes(ref["Exit:"])
    assert documented == exit_codes(help_text[help_text.index("Exit codes:"):]), "exit codes: SKILL.md vs --help"
    assert documented == {0} | {v for k, v in vars(core).items() if k.startswith("EX_")}, "exit codes vs the code"


def test_settings_match_what_the_code_reads(ref):
    env = ref["Env:"]
    fields = {f"TOLLBOOTH_{name.upper()}" for name in core.Settings.model_fields}
    as_option = next(p for p in cli.cli.params if p.name == "label")
    assert set(re.findall(r"\bTOLLBOOTH_[A-Z_]+", env)) == fields | {as_option.envvar}, "TOLLBOOTH_ settings"
