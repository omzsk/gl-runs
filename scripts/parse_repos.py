#!/usr/bin/env python3
"""Validate ./repos and emit a GitHub Actions dynamic matrix.

One repo per line:
  https://github.com/R0rt1z2/kaeru.git#wiki=1|lfs=1|wiki_lfs=1|private=1

Target name: kaeru_R0rt1z2 (repository and owner case preserved).
Lines whose first non-space character is #, plus blank lines, are ignored.
"""

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

FLAGS = ("wiki", "lfs", "wiki_lfs", "private")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
HOST_RE = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?\Z")
MAX_REPOS = 256  # GitHub Actions job-matrix limit.


class RepoFileError(ValueError):
    """Invalid line of the repos file."""


def parse_entry(raw: str, line_number: int) -> dict | None:
    """Parse one entry, with errors that point back to the source line."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    def error(reason: str) -> RepoFileError:
        return RepoFileError(f"line {line_number}: {reason}")

    if line.count("#") != 1:
        raise error("expected URL#wiki=0|lfs=0|wiki_lfs=0|private=0 (use 0 or 1)")

    url, options = line.split("#", 1)
    url = url.strip()
    try:
        parsed = urlsplit(url)
        port = parsed.port  # Validate port if present.
    except ValueError as exc:
        raise error(f"invalid URL: {exc}") from exc
    if (
        not url.startswith("https://")
        or parsed.scheme != "https"
        or not parsed.hostname
        or not HOST_RE.fullmatch(parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in url)
    ):
        raise error("source must be a credential-free HTTPS repository URL without a query")
    if port == 0:
        raise error("invalid URL port")

    # A supported clone URL has one namespace/account followed by its repo.
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) != 2:
        raise error("source URL must have the path /USERNAME/REPOSITORY[.git]")
    owner, repo = path_parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if (
        not NAME_RE.fullmatch(owner)
        or not NAME_RE.fullmatch(repo)
        or owner in (".", "..")
        or repo in (".", "..")
    ):
        raise error("repository/username may contain only letters, digits, _, - and .")

    values: dict[str, bool] = {}
    for field in options.split("|"):
        key, sep, value = field.strip().partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in FLAGS:
            raise error(f"unknown or malformed option {field!r}; expected {', '.join(FLAGS)}")
        if key in values:
            raise error(f"duplicate option {key!r}")
        if value not in ("0", "1"):
            raise error(f"{key} must be 0 or 1, got {value!r}")
        values[key] = value == "1"
    if set(values) != set(FLAGS):
        missing = ", ".join(flag for flag in FLAGS if flag not in values)
        raise error(f"missing option(s): {missing}")

    target = f"{repo}_{owner}"
    # These exact keys are consumed by .github/workflows/sync.yml.
    return {"source": url, "target": target, **{flag: values[flag] for flag in FLAGS}}


def parse_file(path: Path) -> list[dict]:
    entries = []
    targets = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        entry = parse_entry(raw, line_number)
        if entry is None:
            continue
        target_key = entry["target"].lower()
        if target_key in targets:
            raise RepoFileError(
                f"line {line_number}: destination {entry['target']!r} already assigned "
                f"on line {targets[target_key]} (case-insensitive)"
            )
        targets[target_key] = line_number
        entries.append(entry)
        if len(entries) > MAX_REPOS:
            raise RepoFileError(f"more than {MAX_REPOS} repositories; split the workflow")
    if not entries:
        raise RepoFileError("repos has no active repositories")
    return entries


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("repos")
    try:
        entries = parse_file(path)
    except (RepoFileError, OSError, UnicodeError) as exc:
        print(f"ERROR: {path}: {exc}", file=sys.stderr)
        return 1
    matrix = json.dumps({"include": entries}, separators=(",", ":"))
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as stream:
            stream.write(f"matrix={matrix}\n")
    else:
        print(matrix)
    print(f"Validated {len(entries)} repositories from {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
