"""Pure-local checks for the repos configuration parser."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PARSER = ROOT / "scripts" / "parse_repos.py"
spec = importlib.util.spec_from_file_location("parse_repos", PARSER)
mod = importlib.util.module_from_spec(spec)
sys.modules["parse_repos"] = mod
spec.loader.exec_module(mod)


class ParseRepoTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repos"
            path.write_text(text, encoding="utf-8")
            return mod.parse_file(path)

    def test_bundled_48_repos(self):
        entries = mod.parse_file(ROOT / "repos")
        self.assertEqual(len(entries), 48)
        self.assertEqual(entries[0]["source"], "https://github.com/tiann/KernelSU.git")
        self.assertEqual(entries[0]["target"], "KernelSU_tiann")
        self.assertEqual(entries[4]["target"], "KernelSU_JingMatrix")
        self.assertEqual(entries[-1]["target"], "LSPromise_LSPosed")
        self.assertTrue(all(all(flag in e for flag in mod.FLAGS) for e in entries))

    def test_comments_blanks_crlf_and_flags(self):
        text = " # comment\r\n\r\nhttps://github.com/R0rt1z2/kaeru.git#wiki=0|lfs=1|wiki_lfs=0|private=0\r\n"
        entries = self.parse(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["target"], "kaeru_R0rt1z2")
        self.assertEqual({k: entries[0][k] for k in mod.FLAGS},
                         {"wiki": False, "lfs": True, "wiki_lfs": False, "private": False})

    def test_options_any_order(self):
        entry = self.parse("https://git.example.com/OWNER/CamelCase#private=1|wiki_lfs=1|wiki=1|lfs=0\n")[0]
        self.assertEqual(entry["target"], "CamelCase_OWNER")
        self.assertFalse(entry["lfs"])
        self.assertTrue(entry["private"])

    def test_source_without_dot_git_and_all_false(self):
        entry = self.parse("https://git.example.com/AcCoUnT/repo#wiki=0|lfs=0|wiki_lfs=0|private=0\n")[0]
        self.assertEqual(entry["source"], "https://git.example.com/AcCoUnT/repo")
        self.assertEqual(entry["target"], "repo_AcCoUnT")
        self.assertFalse(any(entry[flag] for flag in mod.FLAGS))

    def test_bad_inputs(self):
        invalid = [
            "", "# only comments\n", "https://github.com/u/r.git",
            "http://github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|private=1",
            "HTTPS://github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|private=1",
            "https://user:password@github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|private=1",
            "https://github.com/u/sub/r#wiki=1|lfs=1|wiki_lfs=1|private=1",
            "https://github.com/u/r?x=y#wiki=1|lfs=1|wiki_lfs=1|private=1",
            "https://github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|private=2",
            "https://github.com/u/r#wiki=1|lfs=1|private=1",
            "https://github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|private=1|wiki=0",
            "https://github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|other=0|private=1",
            "https://github.com/u/r#wiki=1|lfs=1|wiki_lfs=1|private=1#bad",
            "https://github.com/u/r name#wiki=1|lfs=1|wiki_lfs=1|private=1",
            "https://github.com/u/..#wiki=1|lfs=1|wiki_lfs=1|private=1",
        ]
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(mod.RepoFileError):
                self.parse(case)

    def test_duplicate_target_even_if_case_differs(self):
        self.assertRaises(mod.RepoFileError, self.parse,
            "https://github.com/X/Repo#wiki=0|lfs=0|wiki_lfs=0|private=0\n"
            "https://github.com/x/repo#wiki=1|lfs=1|wiki_lfs=1|private=1\n")

    def test_output_job_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = dict(os.environ, GITHUB_OUTPUT=str(output))
            result = subprocess.run([sys.executable, str(PARSER), str(ROOT / "repos")],
                                    capture_output=True, text=True, env=env, check=True)
            self.assertEqual(result.stdout, "")
            self.assertIn("Validated 48", result.stderr)
            matrix = json.loads(output.read_text().strip().removeprefix("matrix="))
            self.assertEqual(len(matrix["include"]), 48)


if __name__ == "__main__":
    unittest.main()
