#!/usr/bin/env python3
"""Check availability of all source-history LFS objects on GitLab.

Uses the Git LFS *download* Batch API in small batches.  No actual LFS files
are downloaded again.  Local objects were already fetched and validated by the
Git LFS client before this post-sync check.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib import error, parse, request

OID_LINE = re.compile(r"^([0-9a-f]{64})\s+[\*-]\s")
BATCH_SIZE = 100
MAX_ATTEMPTS = 5


class ValidationError(Exception):
    pass


def source_objects(repo: Path, manifest: Path) -> list[dict]:
    # The manifest was collected before fetching destination refs, because
    # archive-only destination LFS OIDs may not be present in the source cache.
    oids = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = OID_LINE.match(line)
        if not match:
            raise ValidationError(f"Unrecognized git lfs ls-files output: {line[:120]!r}")
        oids.add(match.group(1))

    objects = []
    for oid in sorted(oids):
        local_file = repo / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        if not local_file.is_file():
            raise ValidationError(f"Source LFS object missing locally: {oid}")
        objects.append({"oid": oid, "size": local_file.stat().st_size})
    return objects


def endpoint(destination: str) -> str:
    parsed = parse.urlsplit(destination)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError("Destination LFS API must use HTTPS without embedded credentials")
    if not parsed.path.endswith(".git") or parsed.query or parsed.fragment:
        raise ValidationError("Destination must be the HTTPS .git repository URL")
    return destination + "/info/lfs/objects/batch"


class NoRedirect(request.HTTPRedirectHandler):
    # Never forward a GitLab token to a different host via HTTP redirects.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_batch(opener, url: str, token: str, batch: list[dict], sleep=time.sleep) -> dict:
    payload = json.dumps({"operation": "download", "transfers": ["basic"], "objects": batch}).encode()
    basic = base64.b64encode(("oauth2:" + token).encode()).decode()
    for attempt in range(MAX_ATTEMPTS):
        req = request.Request(
            url, data=payload, method="POST", headers={
                "Accept": "application/vnd.git-lfs+json",
                "Content-Type": "application/vnd.git-lfs+json",
                "Authorization": "Basic " + basic,
            },
        )
        try:
            with opener.open(req, timeout=90) as response:
                return json.loads(response.read())
        except error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < MAX_ATTEMPTS:
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(1, min(90, float(retry_after)))
                except ValueError:
                    delay = min(90, 2 ** (attempt + 1))
                print(f"GitLab LFS validation HTTP {exc.code}; retrying in {delay:g}s...", file=sys.stderr)
                sleep(delay)
                continue
            raise ValidationError(f"GitLab LFS batch API returned HTTP {exc.code}") from exc
        except error.URLError as exc:
            raise ValidationError(f"GitLab LFS batch API connection failed: {exc.reason}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("GitLab LFS batch API returned invalid JSON") from exc
    raise ValidationError("LFS verification exhausted retries")


def assert_batch_available(batch: list[dict], response: dict) -> None:
    if not isinstance(response, dict) or not isinstance(response.get("objects"), list):
        raise ValidationError("GitLab LFS batch response has no objects list")
    expected = {obj["oid"]: obj["size"] for obj in batch}
    found = set()
    for obj in response["objects"]:
        if not isinstance(obj, dict) or not isinstance(obj.get("oid"), str):
            raise ValidationError("Invalid GitLab LFS batch object")
        oid = obj["oid"]
        if oid not in expected or oid in found:
            raise ValidationError(f"Unexpected/duplicate LFS OID in response: {oid}")
        found.add(oid)
        if obj.get("error"):
            raise ValidationError(f"GitLab lacks LFS object {oid}: {obj['error'].get('code', 'unknown')}")
        if obj.get("size") != expected[oid]:
            raise ValidationError(f"GitLab LFS object size mismatch: {oid}")
        if not isinstance(obj.get("actions"), dict) or not isinstance(obj["actions"].get("download"), dict):
            raise ValidationError(f"GitLab does not offer LFS download action for {oid}")
        if not obj["actions"]["download"].get("href"):
            raise ValidationError(f"GitLab LFS download action has no URL for {oid}")
    if found != expected.keys():
        raise ValidationError(f"GitLab did not return {len(expected.keys() - found)} LFS object(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Bare source clone (already fetched)")
    parser.add_argument("--destination", required=True, help="HTTPS GitLab .git URL")
    parser.add_argument("--manifest", type=Path, required=True, help="Source LFS OIDs captured before destination fetch")
    args = parser.parse_args()
    try:
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            raise ValidationError("GITLAB_TOKEN is missing")
        url = endpoint(args.destination)
        objects = source_objects(args.repo, args.manifest)
        opener = request.build_opener(NoRedirect())
        for offset in range(0, len(objects), BATCH_SIZE):
            batch = objects[offset:offset + BATCH_SIZE]
            response = fetch_batch(opener, url, token, batch)
            assert_batch_available(batch, response)
        print(f"VERIFY OK (LFS): {len(objects)} source-history objects are downloadable on GitLab (batch metadata check).")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(f"- **Verify LFS: PASS** ({len(objects)} objects available via GitLab LFS API)\n")
        return 0
    except (ValidationError, subprocess.CalledProcessError, OSError) as exc:
        print(f"VERIFY FAILED (LFS): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
