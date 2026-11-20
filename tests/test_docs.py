"""SKILL.md's Reference block against the code: every command, option, exit code and setting, and nothing else.

Agents read the Reference block instead of --help, so a flag added to the CLI without a doc update, or a
documented flag that doesn't exist, fails here.
"""
import argparse
import re
import unittest
from typing import Dict, List

from _fixtures import SCRIPTS, core, load_cli
import tollbooth_guard as guard  # noqa: E402  # pyright: ignore[reportMissingImports]

ROOT = SCRIPTS.parent
GLOBAL_OPTIONS = {"--as", "--budget"}
OPTION_RE = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*")  # -X, --min-stars; not `-` inside words or numbers


def reference_block() -> str:
    """The one fenced block under `## Reference` in SKILL.md."""
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^## Reference\n(?:(?!^## ).)*?^```[a-z]*\n(.*?)^```", text, re.S | re.M)
    if m is None:
        raise AssertionError("SKILL.md needs a `## Reference` section with a fenced block")
    return m.group(1)


def groups(block: str) -> Dict[str, str]:
    """The block's entries: a line that starts at column 0, plus the indented lines that continue it.

    Keyed by the first two words for commands ("gh-tollbooth rank"), else the first word ("Exit:")."""
    out: Dict[str, List[str]] = {}
    key = ""
    for line in block.splitlines():
        if line and not line[0].isspace():
            words = line.split()
            key = " ".join(words[:2]) if words[0] == "gh-tollbooth" else words[0]
            out[key] = []
        out.setdefault(key, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def options(text: str) -> set:
    return set(OPTION_RE.findall(text.split(">")[0]))  # a `> RAW.jsonl` redirect is not an option


class ReferenceBlock(unittest.TestCase):
    def setUp(self):
        parser = load_cli("cli_docs").build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        self.commands = {name: {s for a in p._actions for s in a.option_strings} - {"-h", "--help"} - GLOBAL_OPTIONS
                         for name, p in sub.choices.items()}
        self.entries = groups(reference_block())

    def entry(self, key: str) -> str:
        self.assertIn(key, self.entries, f"the Reference block has no line starting with {key!r}")
        return self.entries[key]

    def test_every_command_is_listed_with_exactly_its_options(self):
        for name, opts in self.commands.items():
            listed = options(self.entry(f"gh-tollbooth {name}"))
            if name == "call":  # what follows `--` is for the guard, checked below
                listed = options(self.entry("gh-tollbooth call").split(" -- ")[0])
            self.assertEqual(listed, opts, f"options of `{name}` in SKILL.md's Reference block")
        documented = {k.split()[1] for k in self.entries if k.startswith("gh-tollbooth ")}
        self.assertEqual(documented, set(self.commands), "commands in SKILL.md's Reference block")

    def test_the_global_options_are_listed_once(self):
        self.assertEqual(options(self.entry("Global,")), GLOBAL_OPTIONS)

    def test_call_lists_one_spelling_of_every_option_the_guard_accepts(self):
        # The guard maps every spelling (-f, --raw-field) to one kind; the block names at least one per kind.
        after = options(self.entry("gh-tollbooth call").split(" -- ", 1)[1])
        accepted = {**guard.VALUE_OPTIONS, **guard.FLAG_OPTIONS}
        self.assertLessEqual(after, set(accepted), "call options the guard doesn't accept")
        self.assertEqual({accepted[o] for o in after}, set(accepted.values()), "call options missing from SKILL.md")

    def test_exit_codes_match_the_code(self):
        listed = {int(n) for n in re.findall(r"(?:Exit:|·)\s*(\d+)\b", self.entry("Exit:"))}
        codes = {0} | {v for k, v in vars(core).items() if k.startswith("EX_")}
        self.assertEqual(listed - {130}, codes, "exit codes in SKILL.md's Reference block")

    def test_settings_match_what_the_scripts_read(self):
        source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(SCRIPTS.iterdir()) if p.is_file())
        env = self.entry("Env:")
        # Settings: every TOLLBOOTH_ variable the scripts name as a string. Token: the variables tollbooth_http reads.
        self.assertEqual(set(re.findall(r"\bTOLLBOOTH_[A-Z_]+", env)),
                         set(re.findall(r'"(TOLLBOOTH_[A-Z_]+)"', source)), "TOLLBOOTH_ settings")
        http_source = (SCRIPTS / "tollbooth_http.py").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"\b[A-Z]+_TOKEN\b", env)),
                         set(re.findall(r'"([A-Z]+_TOKEN)"', http_source)), "token variables")


if __name__ == "__main__":
    unittest.main()
