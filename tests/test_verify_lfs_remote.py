"""Unit tests for metadata-only GitLab LFS verification."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from email.message import Message

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_lfs_remote", ROOT / "scripts" / "verify_lfs_remote.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeResponse:
    def __init__(self, result):
        import json
        self.payload = json.dumps(result).encode()
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        pass
    def read(self):
        return self.payload


class FakeOpener:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
    def open(self, req, timeout):
        import base64
        import json
        self.calls += 1
        self.request = req
        self.body = json.loads(req.data)
        self.auth = base64.b64decode(req.get_header("Authorization").split(" ", 1)[1]).decode()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class VerifyLFSRemoteTests(unittest.TestCase):
    def test_manifest_is_source_only_and_sizes_read_from_local_cache(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            data = b"example LFS content"
            oid = hashlib.sha256(data).hexdigest()
            object_path = repo / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(data)
            manifest = repo / "manifest.txt"
            manifest.write_text(f"{oid} * test-file.bin\n{oid} - renamed-file.bin\n")
            self.assertEqual(mod.source_objects(repo, manifest), [{"oid": oid, "size": len(data)}])
            object_path.unlink()
            with self.assertRaisesRegex(mod.ValidationError, "missing locally"):
                mod.source_objects(repo, manifest)

    def test_batch_api_checks_download_and_size(self):
        oid = "a" * 64
        batch = [{"oid": oid, "size": 123}]
        good = {"objects": [{"oid": oid, "size": 123, "actions": {"download": {"href": "https://example.test/download"}}}]}
        fake = FakeOpener([good])
        result = mod.fetch_batch(fake, "https://gitlab.com/repo.git/info/lfs/objects/batch", "dummy", batch)
        mod.assert_batch_available(batch, result)
        self.assertEqual(fake.auth, "oauth2:dummy")
        self.assertEqual(fake.body["operation"], "download")
        with self.assertRaisesRegex(mod.ValidationError, "lacks LFS object"):
            mod.assert_batch_available(batch, {"objects": [{"oid": oid, "size": 123, "error": {"code": 404}}]})
        with self.assertRaisesRegex(mod.ValidationError, "size mismatch"):
            mod.assert_batch_available(batch, {"objects": [{"oid": oid, "size": 122, "actions": {"download": {"href": "x"}}}]})
        with self.assertRaisesRegex(mod.ValidationError, "did not return"):
            mod.assert_batch_available(batch, {"objects": []})

    def test_rate_limit_retries_then_validates(self):
        oid = "b" * 64
        batch = [{"oid": oid, "size": 10}]
        headers = Message()
        headers["Retry-After"] = "1"
        err = HTTPError("https://gitlab.com/test", 429, "Too many requests", headers, None)
        good = {"objects": [{"oid": oid, "size": 10, "actions": {"download": {"href": "https://example.test/file"}}}]}
        fake = FakeOpener([err, good])
        sleeps = []
        mod.assert_batch_available(batch, mod.fetch_batch(fake, "https://example.test", "token", batch, sleep=sleeps.append))
        self.assertEqual(fake.calls, 2)
        self.assertEqual(sleeps, [1])

    def test_destination_requires_gitlab_https_url(self):
        self.assertEqual(mod.endpoint("https://gitlab.com/owner/repo.git"), "https://gitlab.com/owner/repo.git/info/lfs/objects/batch")
        for url in ["http://gitlab.com/o/r.git", "https://user:password@gitlab.com/o/r.git", "https://gitlab.com/o/r", "https://gitlab.com/o/r.git?key=foo"]:
            with self.subTest(url=url), self.assertRaises(mod.ValidationError):
                mod.endpoint(url)


if __name__ == "__main__":
    unittest.main()
