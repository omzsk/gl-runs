"""Offline integration tests for refs, backup archive, and real git fsck.

Git LFS executable is absent from this environment: tests emulate only
`git lfs ls-files --all --long` returning an empty inventory. Therefore LFS
network download and hash verification need a real GitHub Actions run.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_restore_full.py'
REAL_GIT = '/usr/bin/git'


def shell(*args, cwd=None):
    return subprocess.check_output([REAL_GIT, *args], cwd=cwd, text=True).strip()


def test_all():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        fakegit = bin_dir / 'git'
        fakegit.write_text('''#!/bin/bash
for arg in "$@"; do
  if [[ "$arg" == 'lfs' ]]; then
    # Only mock empty LFS listing; verify all other ops with real Git.
    [[ " $* " == *' ls-files '* ]] && exit 0
    echo 'Unexpected LFS use' >&2
    exit 1
  fi
done
exec /usr/bin/git "$@"
''')
        fakegit.chmod(0o755)
        work = root / 'work'
        shell('init', '-b', 'main', str(work))
        shell('config', 'user.email', 'test@example.com', cwd=work)
        shell('config', 'user.name', 'Tester', cwd=work)
        (work / 'readme.txt').write_text('test data')
        shell('add', '.', cwd=work)
        shell('commit', '-m', 'init', cwd=work)
        shell('tag', '-a', 'annotated', '-m', 'annotated', cwd=work)
        origin, dest = root / 'origin.git', root / 'dest.git'
        shell('clone', '--bare', str(work), str(origin))
        shell('clone', '--bare', str(work), str(dest))
        env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', VERIFY_ALLOW_LOCAL='1')
        def check(expect, wiki='false'):
            args = ['python3', str(SCRIPT), '--source', str(origin), '--destination', str(dest),
                    '--target', 'testrepo', '--wiki', wiki, '--lfs', 'true', '--wiki-lfs', 'true',
                    '--report', str(root / 'report.json')]
            p = subprocess.run(args, env=env, text=True, capture_output=True)
            rep = json.loads((root / 'report.json').read_text())
            print(rep['status'], rep['repository'].get('status'), (p.stderr or '')[-100:])
            assert (p.returncode == 0) == expect, (p.stdout, p.stderr)
            return rep
        assert check(True)['repository']['tags'] == 1
        shell('--git-dir=' + str(dest), 'tag', '__mirror_backup__/2026/branches/main', 'main')
        assert check(True)['repository']['backup_tags'] == 1
        # intentionally mismatch commit with source, should fail.
        (work / 'readme.txt').write_text('updated data')
        shell('add', '.', cwd=work)
        shell('commit', '-m', 'new', cwd=work)
        shell('push', str(dest), 'main', cwd=work)
        report = check(False)
        assert report['repository']['status'] == 'LỆCH REFS'
        assert report['repository']['issues'][0]['kind'] == 'GITLAB_ĐI_TRƯỚC'
        assert report['repository']['issues'][0]['ref'] == 'refs/heads/main'
        assert report['repository']['issues'][0]['github_sha']
        assert report['repository']['issues'][0]['gitlab_sha']
        shell('--git-dir=' + str(dest), 'update-ref', 'refs/heads/main', shell('rev-parse', 'main', cwd=origin))
        assert check(True)['status'] == 'ĐẠT'
        # A new GitHub commit after backup: GitLab is a reachable ancestor, not corrupt.
        (work / 'readme.txt').write_text('new github commit after last backup')
        shell('add', '.', cwd=work)
        shell('commit', '-m', 'github new commit', cwd=work)
        shell('push', str(origin), 'main', cwd=work)
        report = check(False)
        assert report['status'] == 'CHƯA ĐỒNG BỘ', report
        issue = report['repository']['issues'][0]
        assert issue['kind'] == 'BRANCH_CHẬM' and issue['commits_behind'] == 2, issue
        assert issue['github']['commit_time'] and issue['gitlab']['commit_time']
        # Bring destination back in sync and create an extra GitLab branch.
        shell('--git-dir=' + str(origin), 'push', str(dest), 'main:main')
        assert check(True)['status'] == 'ĐẠT'
        shell('--git-dir=' + str(dest), 'branch', 'unmatched-branch', 'main')
        report = check(False)
        assert report['status'] == 'LỆCH REFS'
        assert report['repository']['issues'][0]['kind'] == 'REF_DƯ'
        shell('--git-dir=' + str(dest), 'branch', '-D', 'unmatched-branch')
        assert check(True)['status'] == 'ĐẠT'
        print('Local non-LFS integration passed')


if __name__ == '__main__':
    test_all()


def test_lfs_sha():
    import importlib.util
    import hashlib
    spec = importlib.util.spec_from_file_location('verify', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        data = b'full restore check content\n'
        oid = hashlib.sha256(data).hexdigest()
        path = repo / 'lfs' / 'objects' / oid[:2] / oid[2:4] / oid
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        original = mod.git
        try:
            mod.git = lambda *args, **kwargs: None  # Mock Git LFS network only.
            out = mod.verify_lfs(str(repo), {oid}, {oid}, 'https://gitlab.test/o/repo.git')
            assert out['count'] == 1 and out['bytes'] == len(data)
            path.write_bytes(b'CORRUPT\n')
            try:
                mod.verify_lfs(str(repo), {oid}, {oid}, 'https://gitlab.test/o/repo.git')
                assert False, 'corrupt LFS object should not pass'
            except mod.VerificationError as e:
                assert 'CORRUPT' in str(e)
            try:
                mod.verify_lfs(str(repo), {oid}, set(), 'https://gitlab.test/o/repo.git')
                assert False, 'missing LFS OID should not pass'
            except mod.MissingLfs as e:
                assert 'thiếu' in str(e)
        finally:
            mod.git = original
    print('Local LFS SHA-256 checks passed (network mocked)')


if __name__ == '__main__':
    test_lfs_sha()


def test_snapshot_changed():
    import importlib.util
    spec = importlib.util.spec_from_file_location('restore_mod', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    original = mod.remote_refs
    try:
        mod.remote_refs = lambda _: {'refs/heads/main': 'b' * 40}
        try:
            mod.check_unchanged('https://github.com/x/y.git', {'refs/heads/main': 'a' * 40}, 'GitHub')
            assert False, 'Should fail if the upstream moves mid-run'
        except mod.SnapshotChanged as exc:
            assert exc.changes[0]['old'] == 'a' * 40
            assert exc.changes[0]['new'] == 'b' * 40
    finally:
        mod.remote_refs = original
    print('GitHub ref changes mid-run are reported as uncertain')


test_snapshot_changed()
