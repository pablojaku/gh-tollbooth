"""Sweep tests: the query file, and many repository searches sent in batches.
Run: uv run pytest
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tollbooth_core as core
import tollbooth_sweep as sweeping

SECRET = "aws_secret_access_key = FAKEsecretFAKEsecret"


class QueryFile(unittest.TestCase):
    """A narrow .tsv format; refusals name the path and the line, never the file's text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text, name="q.tsv"):
        f = self.dir / name
        f.write_bytes(text.encode() if isinstance(text, str) else text)
        return f

    def refused(self, path):
        with self.assertRaises(core.TollboothError) as cm:
            sweeping.read_queries(str(path))
        self.assertEqual(cm.exception.code, core.EX_USAGE)
        self.assertNotIn("FAKE", str(cm.exception))
        return str(cm.exception)

    def test_slices_and_queries(self):
        f = self.write("# comment\n\nA\tcode wiki\r\nB\tx sort:updated-desc\n  \n")
        self.assertEqual(sweeping.read_queries(str(f)), [sweeping.Query("A", "code wiki sort:stars-desc"),
                                                         sweeping.Query("B", "x sort:updated-desc")])

    def test_only_a_small_regular_tsv_file(self):
        self.refused(self.write(SECRET + "\n", "creds"))
        self.refused(self.dir / "missing.tsv")
        real = self.write("A\tx\n", "real.tsv")
        os.symlink(real, self.dir / "link.tsv")
        self.refused(self.dir / "link.tsv")
        (self.dir / "d.tsv").mkdir()
        self.refused(self.dir / "d.tsv")
        os.mkfifo(self.dir / "f.tsv")
        self.assertIn("not a regular file", self.refused(self.dir / "f.tsv"))  # and the open didn't hang
        self.assertIn("64 KB", self.refused(self.write("A\tx\n#" + "y" * 65536 + "\n")))

    def test_the_size_is_checked_before_reading(self):
        f = self.write("A\tx\n")
        regular = mock.Mock(st_mode=0o100644, st_size=70_000)
        with mock.patch.object(core.os, "fstat", return_value=regular):
            self.assertIn("64 KB", self.refused(f))

    def test_a_bad_line_names_its_number_not_its_text(self):
        for bad in ("no tab " + SECRET, "bad slice\t" + SECRET, "\t" + SECRET, "A\t   ", "L" * 41 + "\t" + SECRET,
                    "A\t" + SECRET + "\tmore", "A\t" + "x" * 300):
            message = self.refused(self.write("# c\n" + bad + "\n"))
            self.assertIn("q.tsv:2", message, bad[:20])
            self.assertNotIn("xxxx", message)

    def test_hidden_characters(self):
        for bad in ("\x00", "\x1b", "\x7f", "​", "﻿", "\x0b", "\x85", "؜", "\U000e0041", "️"):
            message = self.refused(self.write("A\tfine\nB\tbad%sbad\n" % (bad + SECRET)))
            self.assertIn("q.tsv:2", message, repr(bad))
            self.assertNotIn("bad", message.split("q.tsv:2")[1])

    def test_text_that_is_not_utf8(self):
        for raw in (b"A\t\xff\xfe\n", b"A\tx\xed\xa0\x80y\n"):  # the second is a lone surrogate, CESU-style
            self.assertNotIn("codec", self.refused(self.write(raw)))

    def test_an_empty_file(self):
        self.refused(self.write("# only a comment\n"))

    def test_githubs_256_character_limit_counts_the_added_sort(self):
        fits = "x" * (256 - len(" sort:stars-desc"))
        self.assertEqual(len(sweeping.read_queries(str(self.write(f"A\t{fits}\n")))[0].text), 256)
        self.assertIn("256", self.refused(self.write(f"A\t{fits}y\n")))


if __name__ == "__main__":
    unittest.main()
