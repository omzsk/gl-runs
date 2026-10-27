"""Offline integration tests for one-way Git refs and Wiki default aliases."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-repo.sh"


def git(*args, cwd=None, env=None):
    return subprocess.check_output(["git", *map(str, args)], cwd=cwd, env=env, stderr=subprocess.DEVNULL, text=True).strip()


class MirrorLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cfg = self.base / "gitconfig"
        self.cfg.write_text("", encoding="utf-8")
        git("config", "--file", self.cfg, f"url.file://{self.base}/.insteadOf", "https://offline.example/")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.cfg), SOURCE_URL="https://offline.example/source.git",
                        DEST_PATH="myuser/backup", GITLAB_TOKEN="offline-dummy-token",
                        WITH_LFS="false", WITH_WIKI="true", WITH_WIKI_LFS="false", CREATE_PRIVATE="true",
                        TARGET_URL_OVERRIDE=f"file://{self.base}/backup.git",
                        TARGET_WIKI_URL_OVERRIDE=f"file://{self.base}/backup.wiki.git")
        self.source_work = self.new_repo("source", "main")
        self.source_wiki_work = self.new_repo("source.wiki", "master")
        git("init", "--bare", "--initial-branch=main", self.base / "backup.git")
        git("init", "--bare", "--initial-branch=main", self.base / "backup.wiki.git")
        self.dest_wiki_work = self.new_repo("placeholder", "main", bare_path="backup.wiki.git")
        self.commit(self.dest_wiki_work, "home.md", "placeholder", "GitLab Home init")
        git("push", "origin", "main", cwd=self.dest_wiki_work)
        self.commit(self.source_work, "test.txt", "one", "initial source")
        git("tag", "v1", cwd=self.source_work)
        git("push", "origin", "main", "v1", cwd=self.source_work)
        self.commit(self.source_wiki_work, "home.md", "real wiki", "original wiki")
        git("push", "origin", "master", cwd=self.source_wiki_work)

    def new_repo(self, basename, branch, bare_path=None):
        work = self.base / f"{basename}-work"
        bare = self.base / (bare_path or f"{basename}.git")
        if not bare_path:
            git("init", "--bare", f"--initial-branch={branch}", bare)
        git("init", f"--initial-branch={branch}", work)
        git("config", "user.name", "Offline Tester", cwd=work)
        git("config", "user.email", "offline@example.test", cwd=work)
        git("remote", "add", "origin", bare, cwd=work)
        return work

    def commit(self, work, filename, content, msg):
        (work / filename).write_text(content, encoding="utf-8")
        git("add", filename, cwd=work)
        git("commit", "-m", msg, cwd=work)

    def sync(self):
        return subprocess.run(["bash", str(SCRIPT)], env=self.env, cwd=ROOT,
                              capture_output=True, text=True, check=True)

    def ref(self, repo, name):
        return git("--git-dir", self.base / repo, "rev-parse", name)

    def test_mirror_repo_tags_and_wiki_default_alias(self):
        self.sync()
        self.assertEqual(self.ref("source.git", "refs/heads/main"), self.ref("backup.git", "refs/heads/main"))
        self.assertEqual(self.ref("source.git", "refs/tags/v1"), self.ref("backup.git", "refs/tags/v1"))
        self.assertEqual(self.ref("source.wiki.git", "refs/heads/master"),
                         self.ref("backup.wiki.git", "refs/heads/main"))
        self.assertEqual(self.ref("backup.wiki.git", "refs/heads/main"),
                         self.ref("backup.wiki.git", "refs/heads/master"))
        before = git("--git-dir", self.base / "backup.wiki.git", "tag", "-l", "__mirror_backup__/*")
        self.sync()
        after = git("--git-dir", self.base / "backup.wiki.git", "tag", "-l", "__mirror_backup__/*")
        self.assertEqual(before, after, "Unchanged wiki should not create duplicate backup tags")

        self.commit(self.source_work, "test.txt", "two", "commit B")
        git("push", "origin", "main", cwd=self.source_work)
        self.sync()
        head_b = self.ref("backup.git", "refs/heads/main")
        git("reset", "--hard", "HEAD~1", cwd=self.source_work)
        self.commit(self.source_work, "test.txt", "new history", "rewrite history")
        git("push", "--force", "origin", "main", cwd=self.source_work)
        self.sync()
        self.assertEqual(self.ref("source.git", "refs/heads/main"), self.ref("backup.git", "refs/heads/main"))
        archive_oids = git("--git-dir", self.base / "backup.git", "for-each-ref", "--format=%(objectname)",
                           "refs/tags/__mirror_backup__/").splitlines()
        self.assertIn(head_b, archive_oids, "Replaced commit was archived before force push")


if __name__ == "__main__":
    unittest.main()
