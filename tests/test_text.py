"""Tests for tollbooth_text: which characters are hidden, refused coming in and marked going out.
Built from code points, so this file holds no hidden characters itself.
Run: uv run pytest
"""
import unittest

import tollbooth_text as text

# What the guard refused before categories: controls (but tab and newline), NEL, zero-width and bidi
# controls, line and paragraph separators, the BOM.
OLD_LIST = [*range(0x00, 0x09), *range(0x0B, 0x20), 0x7F, 0x85, *range(0x200B, 0x2010), 0x2028, 0x2029,
            *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF]
# Format and control characters the old list let through: C1 controls, the soft hyphen, the Arabic letter
# mark (a bidi control), the Mongolian vowel separator, word joiners, tag characters ("ASCII smuggling").
MORE = [*range(0x80, 0xA0), 0xAD, 0x061C, 0x180E, *range(0x2060, 0x2065), 0xE0001, *range(0xE0020, 0xE0080)]
# Variation selectors are combining marks, not format characters, so the categories alone miss them;
# "emoji smuggling" hides one byte in each after a visible character.
SELECTORS = [*range(0x180B, 0x180E), 0x180F, *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)]
VISIBLE = "plain ASCII ~!@#$%^&*() 0-9, accents éßΩ, CJK 日本, a heart ❤, a face \U0001F600"


class Hidden(unittest.TestCase):
    def test_everything_the_old_list_refused(self):
        for cp in OLD_LIST:
            self.assertTrue(text.has_hidden(f"a{chr(cp)}b"), hex(cp))

    def test_the_format_and_control_characters_it_missed(self):
        for cp in MORE:
            self.assertTrue(text.has_hidden(f"a{chr(cp)}b"), hex(cp))

    def test_variation_selectors(self):
        for cp in SELECTORS:
            self.assertTrue(text.has_hidden(f"❤{chr(cp)}"), hex(cp))

    def test_lone_surrogates(self):
        self.assertTrue(text.has_hidden("a\udc80b"))

    def test_visible_text_is_not_hidden(self):
        self.assertFalse(text.has_hidden(VISIBLE))
        self.assertEqual(text.printable(VISIBLE), VISIBLE)


class Directions(unittest.TestCase):
    def test_tab_and_newline_may_come_in(self):
        self.assertFalse(text.has_hidden("query {\n\tviewer { login }\n}"))

    def test_but_never_go_out(self):
        self.assertEqual(text.printable("a\tb\nc\rd"), "a?b?c?d")

    def test_each_hidden_character_prints_as_a_question_mark(self):
        self.assertEqual(text.printable("a\x1b]0;x\x07b"), "a?]0;x?b")    # a terminal title sequence
        self.assertEqual(text.printable("x‮evil‬"), "x?evil?")   # bidi override
        self.assertEqual(text.printable("❤️"), "❤?")       # a selector after a heart
        self.assertEqual(text.printable("a\U000E0041\U000E0042b"), "a??b")  # tag characters


class ResourceName(unittest.TestCase):
    def test_only_lower_case_names_are_shown(self):
        for name in ("core", "graphql", "dependency_sbom", "a" * 40):
            self.assertTrue(text.RESOURCE_RE.fullmatch(name), name)
        for name in ("", "evil\x1b]0;x\x07", "Core", "core\n", "a" * 41, "core search"):
            self.assertFalse(text.RESOURCE_RE.fullmatch(name), name)


if __name__ == "__main__":
    unittest.main()
