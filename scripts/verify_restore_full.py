#!/usr/bin/env python3
"""Kiểm tra đối chiếu GitHub ↔ GitLab và restore toàn bộ dữ liệu Git/LFS (chỉ đọc).

Verification scope: branches/tags, reachable Git objects, all LFS OIDs in complete
history including GitLab-only archival refs, and optionally the Git Wiki.
GitLab backup refs are explicitly allowed. API metadata (issues, releases, etc.)
is NOT within the scope of a Git repository backup.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

ARCHIVE_PREFIX = 'refs/tags/__mirror_backup__/'
LFS_OID = re.compile(r'^([0-9a-f]{64})\s+[\*-]\s')
TRANSIENT = ('429', 'rate limit', 'timed out', 'timeout', '503', '502', '504', 'connection reset')

class VerificationError(RuntimeError):
    pass


class RefMismatch(VerificationError):
    """Refs differ; a behind branch is not evidence of corrupt storage."""
    def __init__(self, issues):
        self.issues = issues
        self.status = ('CHƯA ĐỒNG BỘ' if all(x['kind'] == 'BRANCH_CHẬM' for x in issues)
                       else 'LỆCH REFS')
        super().__init__(f'{len(issues)} branch/tag chưa khớp. '
                         'Chi tiết tên ref, SHA và thời gian commit nằm trong bảng dưới.')


class DataCorruption(VerificationError):
    """Integrity check actually found a bad Git object or an invalid LFS hash."""


class MissingLfs(VerificationError):
    """An LFS pointer is not backed by an object at the destination."""
    def __init__(self, message, oids=None):
        self.oids = list(oids or [])
        super().__init__(message)


class SnapshotChanged(VerificationError):
    """One of the remotes changed while the test ran; no fixed comparison point."""
    def __init__(self, label, changes):
        self.changes = changes
        super().__init__(f'{label} thay đổi {len(changes)} ref trong lúc kiểm tra: '
                         + '; '.join(x['ref'] for x in changes[:5]))


def git(*args, cwd=None, check=True, attempts=1, env=None):
    cmd = ['git', *map(str, args)]
    for attempt in range(attempts):
        result = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, check=False)
        if result.returncode == 0 or not check:
            return result
        if attempt < attempts - 1:
            wait = min(60, 15 * (attempt + 1))
            print(f'Cảnh báo: Git trả lỗi, thử lại sau {wait}s ({attempt + 2}/{attempts}).', flush=True)
            time.sleep(wait)
    err = result.stderr.strip()[-700:]
    raise VerificationError(f'Lệnh Git {cmd[1:3]} lỗi (exit={result.returncode}): {err}')


def clone(url, dest):
    for attempt in range(3):
        shutil.rmtree(dest, ignore_errors=True)
        try:
            # --bare fetches heads/tags without GitHub-specific pull request refs.
            git('clone', '--bare', '--', url, dest)
            return
        except VerificationError:
            if attempt == 2:
                raise
            time.sleep(15 * (attempt + 1))


def refmap(repo):
    output = git('for-each-ref', '--format=%(refname) %(objectname)',
                 'refs/heads', 'refs/tags', cwd=repo).stdout
    refs = {}
    for line in output.splitlines():
        ref, oid = line.split(' ', 1)
        refs[ref] = oid.strip()
    return refs


def remote_refs(url):
    result = git('ls-remote', '--refs', '--heads', '--tags', url, attempts=3)
    return {ref: sha for sha, ref in (line.split('\t', 1) for line in result.stdout.splitlines())}


def list_lfs(repo):
    # --all includes deleted/renamed LFS objects across all reachable history.
    out = git('-C', repo, 'lfs', 'ls-files', '--all', '--long').stdout
    oids = set()
    for line in out.splitlines():
        match = LFS_OID.match(line)
        if not match:
            raise VerificationError(f'Không phân tích được LFS manifest: {line[:100]!r}')
        oids.add(match.group(1))
    return oids


def verify_lfs(repo, expected, all_dest_oids, url):
    if not expected.issubset(all_dest_oids):
        missing = sorted(expected - all_dest_oids)
        raise MissingLfs(f'GitLab thiếu {len(missing)} LFS OID so với GitHub; xem danh sách OID trong báo cáo.', oids=missing)
    if not all_dest_oids:
        return {'count': 0, 'bytes': 0, 'archived': 0}
    git('-C', repo, 'config', 'lfs.concurrenttransfers', '2')
    git('-C', repo, 'config', f'lfs.{url}/info/lfs.locksverify', 'false')
    # No prune/clean/push here. Download every OID required to restore all refs.
    git('-C', repo, '-c', f'lfs.url={url}/info/lfs', '-c', 'lfs.fetchinclude=', '-c', 'lfs.fetchexclude=',
        'lfs', 'fetch', '--all', 'origin', attempts=3)
    checked_bytes = 0
    for index, oid in enumerate(sorted(all_dest_oids), 1):
        obj = Path(repo) / 'lfs' / 'objects' / oid[:2] / oid[2:4] / oid
        if not obj.is_file():
            raise MissingLfs(f'LFS object tải từ GitLab không xuất hiện tại runner: {oid}', oids=[oid])
        digest = hashlib.sha256()
        with obj.open('rb') as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != oid:
            raise DataCorruption(f'LFS OBJECT CORRUPT: {oid}')
        checked_bytes += obj.stat().st_size
        if index % 100 == 0:
            print(f'Đã kiểm tra SHA-256: {index}/{len(all_dest_oids)} LFS objects', flush=True)
    return {'count': len(all_dest_oids), 'bytes': checked_bytes,
            'archived': len(all_dest_oids - expected)}


def check_fsck(repo):
    try:
        git('-C', repo, 'fsck', '--full', '--strict', '--no-reflogs', '--no-dangling', attempts=1)
    except VerificationError as exc:
        raise DataCorruption(f'Git object lỗi khi chạy fsck ở {Path(repo).name}: {exc}') from exc


def git_info(repo, oid):
    """Only committer time, not push/backup time. Tags may point at blobs/trees."""
    if not oid:
        return {'commit_time': None, 'subject': None}
    p = git('-C', repo, 'show', '-s', '--format=%cI%x09%s', oid + '^{commit}', check=False)
    if p.returncode != 0:
        return {'commit_time': None, 'subject': None}
    line = p.stdout.splitlines()[0] if p.stdout.splitlines() else ''
    parts = line.split('\t', 1)
    return {'commit_time': parts[0] if parts else None,
            'subject': parts[1][:160] if len(parts) == 2 else None}


def ancestor(repo, old, new):
    """Uses the source's local Git history; returns False if history is unavailable."""
    result = git('-C', repo, 'merge-base', '--is-ancestor', old, new, check=False)
    return result.returncode == 0


def analyze_ref_differences(source_refs, dest_refs, src_repo, dst_repo,
                            wiki=False, source_default=''):
    if not source_refs:
        raise VerificationError('Nguồn không có branch/tag nào; không thể xác minh')
    expected = {ref: oid for ref, oid in source_refs.items()
                if ref.startswith(('refs/heads/', 'refs/tags/'))}
    actual = {ref: oid for ref, oid in dest_refs.items()
              if not ref.startswith(ARCHIVE_PREFIX)}
    extra = set(actual) - set(expected)
    if wiki and source_default and len(extra) == 1:
        alias = next(iter(extra))
        if alias.startswith('refs/heads/') and actual[alias] == source_refs.get('refs/heads/' + source_default):
            extra.clear()
    issues = []
    for ref, source_oid in sorted(expected.items()):
        destination_oid = actual.get(ref)
        if destination_oid == source_oid:
            continue
        entry = {'ref': ref, 'github_sha': source_oid, 'gitlab_sha': destination_oid,
                 'github': git_info(src_repo, source_oid),
                 'gitlab': git_info(dst_repo, destination_oid)}
        if ref.startswith('refs/heads/'):
            if not destination_oid:
                entry.update(kind='BRANCH_THIẾU', reason='GitLab chưa có branch này. Có thể branch mới chưa được backup; chưa thể kết luận nguyên nhân.')
            elif ancestor(src_repo, destination_oid, source_oid):
                behind = git('-C', src_repo, 'rev-list', '--count', f'{destination_oid}..{source_oid}').stdout.strip()
                entry.update(kind='BRANCH_CHẬM', commits_behind=int(behind),
                             reason=f'GitLab chậm {behind} commit; SHA GitLab là tổ tiên SHA GitHub. Chưa phải bằng chứng corrupt.')
            elif ancestor(dst_repo, source_oid, destination_oid):
                entry.update(kind='GITLAB_ĐI_TRƯỚC', reason='GitLab có commit đi trước SHA GitHub; có thể nguồn bị reset hoặc đích bị chỉnh sửa.')
            else:
                entry.update(kind='BRANCH_PHÂN_NHÁNH', reason='Hai đầu branch không nằm trên cùng đường tổ tiên trong lịch sử đã tải; cần xem xét force-push/rebase.')
        else:
            entry.update(kind='TAG_THIẾU' if not destination_oid else 'TAG_KHÁC_SHA',
                         reason='Tag chưa khớp. Có thể backup chậm hoặc tag nguồn đã thay đổi; không kết luận corrupt.')
        issues.append(entry)
    for ref in sorted(extra):
        destination_oid = actual[ref]
        issues.append({'ref': ref, 'kind': 'REF_DƯ', 'github_sha': None,
                       'gitlab_sha': destination_oid, 'github': {'commit_time': None, 'subject': None},
                       'gitlab': git_info(dst_repo, destination_oid),
                       'reason': 'GitLab còn ref không tồn tại trên GitHub; có thể branch/tag bị xóa tại nguồn nhưng chưa đồng bộ.'})
    archive = [ref for ref in dest_refs if ref.startswith(ARCHIVE_PREFIX)]
    info = {'branches': sum(x.startswith('refs/heads/') for x in expected),
            'tags': sum(x.startswith('refs/tags/') for x in expected),
            'backup_tags': len(archive)}
    return info, issues


def compare_refs(source_refs, dest_refs, wiki=False, source_default='',
                 source_repo=None, dest_repo=None):
    """For source/destination bare clones, return an inventory or structured mismatch."""
    info, issues = analyze_ref_differences(source_refs, dest_refs, source_repo, dest_repo,
                                           wiki=wiki, source_default=source_default)
    if issues:
        raise RefMismatch(issues)
    return info


def current_default(repo):
    result = git('-C', repo, 'symbolic-ref', '-q', '--short', 'HEAD', check=False)
    return result.stdout.strip() if result.returncode == 0 else ''


def check_unchanged(url, original, label):
    now = remote_refs(url)
    if now != original:
        changed = []
        for ref in sorted(set(original) | set(now)):
            if original.get(ref) != now.get(ref):
                changed.append({'ref': ref, 'old': original.get(ref), 'new': now.get(ref)})
        raise SnapshotChanged(label, changed)


def wiki_url(repo_url):
    if repo_url.endswith('.git'):
        return repo_url[:-4] + '.wiki.git'
    return repo_url + '.wiki.git'


def wiki_exists(url, is_source):
    """Return False only for an unambiguous Git 'repo not found'; else fail closed."""
    p = git('ls-remote', '--heads', url, check=False)
    if p.returncode == 0:
        return bool(p.stdout.strip())
    stderr = p.stderr.lower()
    if 'repository not found' in stderr or 'not found' in stderr or '404' in stderr:
        # GitHub can report hidden private wikis as 404. Treat as unknown
        # rather than claim absence when source repo itself may be private.
        if is_source:
            return False
        return False
    raise VerificationError(f'Không truy cập được Wiki: {p.stderr.strip()[-300:]}')


def one_kind(source_url, dest_url, work, kind, with_lfs, is_wiki=False):
    src = str(work / (kind + '-github.git'))
    dst = str(work / (kind + '-gitlab.git'))
    clone(source_url, src)
    clone(dest_url, dst)
    src_refs, dst_refs = refmap(src), refmap(dst)
    source_default = current_default(src)
    try:
        info = compare_refs(src_refs, dst_refs, wiki=is_wiki,
                            source_default=source_default,
                            source_repo=src, dest_repo=dst)
    except RefMismatch as e:
        # Only classify as "behind" after confirming snapshots stayed stable.
        # A new GitHub commit during this test is an indeterminate snapshot.
        check_unchanged(source_url, src_refs, 'GitHub')
        check_unchanged(dest_url, dst_refs, 'GitLab')
        # Even with an out-of-date backup, test its Git object database.
        check_fsck(src)
        check_fsck(dst)
        raise
    # Default branch names should also match for the main repository.
    if not is_wiki and current_default(src) != current_default(dst):
        check_unchanged(source_url, src_refs, 'GitHub')
        check_unchanged(dest_url, dst_refs, 'GitLab')
        dest_default = current_default(dst)
        raise RefMismatch([{'kind': 'NHÁNH_MẶC_ĐỊNH', 'ref': 'HEAD',
                            'github_sha': src_refs.get('refs/heads/' + source_default),
                            'gitlab_sha': dst_refs.get('refs/heads/' + dest_default),
                            'github': git_info(src, src_refs.get('refs/heads/' + source_default)),
                            'gitlab': git_info(dst, dst_refs.get('refs/heads/' + dest_default)),
                            'reason': f'Nhánh mặc định GitHub={source_default}, GitLab={dest_default}. '
                                      'Tên HEAD không giống nhau; chưa có bằng chứng corrupt.'}])
    print(f'PASS: {kind}: {info["branches"]} branch, {info["tags"]} tag; '
          f'{info["backup_tags"]} tag lưu lịch sử', flush=True)
    check_fsck(src)
    check_fsck(dst)
    print(f'PASS: {kind}: git fsck --full --strict ở cả GitHub và GitLab', flush=True)
    src_lfs = list_lfs(src)
    dst_lfs = list_lfs(dst)
    if src_lfs and not with_lfs:
        raise VerificationError('Nguồn có LFS nhưng cờ lfs=0; không được đánh dấu PASS.')
    # Even if flag lfs=0, validate any archived LFS objects at destination.
    lfs = verify_lfs(dst, src_lfs, dst_lfs, dest_url)
    print(f'PASS: {kind}: {lfs["count"]} LFS object, SHA-256 hợp lệ, '
          f'{lfs["bytes"]:,} bytes', flush=True)
    check_unchanged(source_url, src_refs, 'GitHub')
    check_unchanged(dest_url, dst_refs, 'GitLab')
    return {**info, 'lfs': lfs, 'status': 'ĐẠT'}


def safe_md(s):
    """Escape untrusted remote text in GitHub-flavored Markdown and HTML summary."""
    value = html.escape(str(s), quote=True)
    return value.replace('|', '\\|').replace('`', '\\`').replace('\n', ' ').replace('\r', ' ')[:600]


def short_oid(oid):
    return oid[:12] if oid else '—'


def glyph(status):
    return {'ĐẠT': '✅', 'KHÔNG CÓ': '⏭️', 'BỎ QUA': '⏭️',
            'CHƯA ĐỒNG BỘ': '⚠️', 'LỆCH REFS': '❌',
            'THIẾU LFS': '❌', 'LỖI DỮ LIỆU': '🚨',
            'CHƯA XÁC MINH': '❔'}.get(status, '❌')


def guidance(status):
    return {
        'CHƯA ĐỒNG BỘ': 'GitLab đang ở commit cũ hơn. Kiểm tra lần backup gần nhất, chạy sync rồi kiểm tra lại; đây không phải bằng chứng corrupt.',
        'LỆCH REFS': 'Kiểm tra branch/tag được chỉ rõ phía dưới. Xem workflow sync để xác định force-push, tag đổi hoặc branch bị xoá.',
        'THIẾU LFS': 'Kiểm tra OID LFS bị thiếu, log bước upload LFS trên workflow backup và quyền truy cập GitLab.',
        'LỖI DỮ LIỆU': 'Ưu tiên điều tra Git object/LFS nêu trong lỗi. Chạy xác minh lại để loại trừ lỗi runner/mạng; chưa nên dùng bản này để restore.',
        'CHƯA XÁC MINH': 'Dữ liệu chưa được xác minh đầy đủ. Kiểm tra rate limit, token, nguồn thay đổi khi test và chạy lại.',
    }.get(status, 'Xem log của repository và tải JSON artifact để điều tra.')


def issue_line(problem):
    kind = safe_md(problem.get('kind', 'KHÁC'))
    name = safe_md(problem.get('ref', 'không rõ ref'))
    behind = problem.get('commits_behind')
    intro = f' — chậm **{behind} commit**' if behind is not None else ''
    return f'**`{name}`** (`{kind}`){intro}: GitHub `{short_oid(problem.get("github_sha"))}` → GitLab `{short_oid(problem.get("gitlab_sha"))}`.'


def section_brief(section, label):
    status = section.get('status', 'CHƯA XÁC MINH')
    if status == 'ĐẠT':
        return (f'- **{label}:** ✅ **PASS** — Git fsck PASS; '
                f'{section["branches"]} branch, {section["tags"]} tag khớp SHA; '
                f'{section["backup_tags"]} tag lưu lịch sử được giữ.')
    if status in ('KHÔNG CÓ', 'BỎ QUA'):
        return f'- **{label}:** ⏭️ **{status}** — {safe_md(section.get("note", "Không có dữ liệu để kiểm tra."))}'
    count = len(section.get('issues') or [])
    more = f' — {count} ref khác nhau' if count else ''
    where = f' (bước: {safe_md(section["stage"])})' if section.get('stage') else ''
    return f'- **{label}:** {glyph(status)} **{status}**{more}{where}.'


def lfs_brief(section, label='LFS'):
    if section.get('status') == 'ĐẠT':
        lfs = section['lfs']
        return (f'- **{label}:** ✅ **PASS** — tải và xác minh SHA-256 của '
                f'{lfs["count"]} object ({lfs["bytes"]:,} byte); '
                f'{lfs["archived"]} object chỉ thuộc lịch sử backup cũ.')
    return f'- **{label}:** ⏸️ **CHƯA XÁC NHẬN** — không có kết quả kiểm tra SHA-256 đầy đủ cho hạng mục này.'


def render_diagnostics(section, title, limit=20):
    if section.get('status') in ('ĐẠT', 'KHÔNG CÓ', 'BỎ QUA'):
        return []
    lines = [f'### Chi tiết lỗi — {title}', '']
    if section.get('stage'):
        lines.append(f'- **Bước gặp vấn đề:** {safe_md(section["stage"])}.')
    if section.get('error'):
        lines.append(f'- **Thông báo:** {safe_md(section["error"])}')
    for problem in (section.get('issues') or [])[:3]:
        lines.append('- **Ref lệch:** ' + issue_line(problem))
        if problem.get('reason'):
            lines.append('  - **Nguyên nhân:** ' + safe_md(problem['reason']))
    oids = section.get('missing_oids') or []
    if oids:
        lines.append(f'- **LFS thiếu:** {len(oids)} object, ví dụ: ' + ', '.join(f'`{safe_md(o)}`' for o in oids[:3]))
    for item in (section.get('snapshot_changes') or [])[:3]:
        lines.append(f'- **Ref đổi trong khi test:** `{safe_md(item.get("ref", "?"))}`: '
                     f'`{short_oid(item.get("old"))}` → `{short_oid(item.get("new"))}`.')
    lines.append('- **Khuyến nghị:** ' + guidance(section.get('status')))
    issues = section.get('issues') or []
    changes = section.get('snapshot_changes') or []
    if issues or changes or len(oids) > 3:
        lines += ['', f'<details><summary>Xem bảng chi tiết {title} ({len(issues)} ref, {len(changes)} thay đổi lúc test, {len(oids)} LFS thiếu)</summary>', '']
        if issues:
            lines += ['| Ref | Loại và giải thích | GitHub SHA / commit UTC | GitLab SHA / commit UTC |',
                      '|---|---|---|---|']
            for item in issues[:limit]:
                gh = item.get('github') or {}
                gl = item.get('gitlab') or {}
                reason = item.get('reason', '')
                if item.get('commits_behind') is not None:
                    reason += f' (chậm {item["commits_behind"]} commit)'
                lines.append(f'| `{safe_md(item.get("ref", "?"))}` | `{safe_md(item.get("kind", ""))}` — {safe_md(reason)} '
                             f'| `{short_oid(item.get("github_sha"))}` / `{safe_md(gh.get("commit_time") or "—")}` '
                             f'| `{short_oid(item.get("gitlab_sha"))}` / `{safe_md(gl.get("commit_time") or "—")}` |')
            if len(issues) > limit:
                lines.append(f'\nCòn {len(issues)-limit} ref khác trong JSON artifact.')
        if changes:
            lines += ['', '| Ref thay đổi trong khi test | SHA ban đầu | SHA sau |', '|---|---|---|']
            for item in changes[:limit]:
                lines.append(f'| `{safe_md(item.get("ref", "?"))}` | `{short_oid(item.get("old"))}` | `{short_oid(item.get("new"))}` |')
        if len(oids) > 3:
            lines += ['', '**Danh sách LFS OID thiếu (tối đa 20):**', '']
            lines.extend(f'- `{safe_md(oid)}`' for oid in oids[:limit])
        lines += ['', '</details>']
    return lines + ['']


def render_summary(report):
    repo = report.get('repository') or {}
    wiki = report.get('wiki') or {}
    status = report.get('status', 'CHƯA XÁC MINH')
    lines = [f'## Restore {safe_md(report["target"])} — {glyph(status)} {status}', '',
             section_brief(repo, 'Repository'),
             lfs_brief(repo),
             section_brief(wiki, 'Wiki'),
             *([lfs_brief(wiki, 'Wiki LFS')] if wiki.get('status') == 'ĐẠT' else []),
             f'- **Kết luận:** {glyph(status)} **{status}** — '
             + ('Các hạng mục trong phạm vi đã kiểm tra đều đạt.' if status == 'ĐẠT' else 'Chưa thể kết luận backup đã khớp đầy đủ.'), '',
             f'**Bắt đầu kiểm tra (UTC):** `{safe_md(report.get("time_utc", "—"))}`', '']
    if status != 'ĐẠT':
        for name, sect in [('Repository', repo), ('Wiki', wiki)]:
            lines.extend(render_diagnostics(sect, name))
    lines += ['> **Lưu ý:** `CHƯA ĐỒNG BỘ` không đồng nghĩa corrupt. '
              'Wiki `BỎ QUA`/`KHÔNG CÓ` không phải bằng chứng đã khôi phục nội dung Wiki. '
              'Thời gian commit trong bảng là committer date, không phải thời gian backup. '
              'JSON artifact chứa SHA đầy đủ và mọi ref lệch.']
    return '\n'.join(lines) + '\n'


def render_aggregate_summary(selected, reports, prepare_status, verify_status):
    counts = {}
    ok = 0
    for entry in selected:
        status = reports.get(entry['target'], {}).get('status', 'CHƯA XÁC MINH')
        counts[status] = counts.get(status, 0) + 1
        if status == 'ĐẠT':
            ok += 1
    rows = [f'## Tổng hợp Restore Test — {ok}/{len(selected)} repository ĐẠT', '',
            f'- **✅ ĐẠT:** {ok}',
            f'- **⚠️ CHƯA ĐỒNG BỘ:** {counts.get("CHƯA ĐỒNG BỘ", 0)}',
            f'- **❌ LỆCH REFS:** {counts.get("LỆCH REFS", 0)}',
            f'- **❌ THIẾU LFS:** {counts.get("THIẾU LFS", 0)}',
            f'- **🚨 LỖI DỮ LIỆU:** {counts.get("LỖI DỮ LIỆU", 0)}',
            f'- **❔ CHƯA XÁC MINH/khác:** {len(selected) - sum(counts.get(s, 0) for s in ("ĐẠT", "CHƯA ĐỒNG BỘ", "LỆCH REFS", "THIẾU LFS", "LỖI DỮ LIỆU"))}', '',
            f'**Trạng thái chạy:** chuẩn bị `{safe_md(prepare_status)}`, kiểm tra `{safe_md(verify_status)}`.', '']
    for entry in selected:
        name = entry['target']
        rep = reports.get(name)
        status = rep.get('status', 'CHƯA XÁC MINH') if rep else 'CHƯA XÁC MINH'
        if status == 'ĐẠT':
            continue
        rows.append(f'### {glyph(status)} {safe_md(name)} — {status}')
        if not rep:
            rows += ['- **Không tìm thấy JSON report:** kiểm tra log job, lỗi runner hoặc bước upload artifact.', '']
            continue
        for key, label in (('repository', 'Repository'), ('wiki', 'Wiki')):
            section = rep.get(key) or {}
            if section.get('status') in ('ĐẠT', 'KHÔNG CÓ', 'BỎ QUA'):
                continue
            rows.append(section_brief(section, label))
            issues = section.get('issues') or []
            for issue in issues[:3]:
                rows.append('  - **Sai khác:** ' + issue_line(issue))
            if not issues and section.get('error'):
                rows.append('  - **Lỗi:** ' + safe_md(section['error']))
            missing = section.get('missing_oids') or []
            if missing:
                rows.append('  - **LFS OID thiếu:** ' + ', '.join(f'`{safe_md(oid)}`' for oid in missing[:3]))
            for change in (section.get('snapshot_changes') or [])[:3]:
                rows.append(f'  - **Ref đang thay đổi:** `{safe_md(change.get("ref", "?"))}` '
                            f'`{short_oid(change.get("old"))}` → `{short_oid(change.get("new"))}`')
        rows.append(f'- **Khuyến nghị:** {guidance(status)}')
        rows.append('')
    rows += ['<details><summary>Xem danh sách tất cả repository được kiểm tra</summary>', '',
             '| Repository | Repository & LFS | Wiki | Kết quả |', '|---|---|---|---|']
    for entry in selected:
        name = entry['target']
        rep = reports.get(name, {})
        status = rep.get('status', 'CHƯA XÁC MINH')
        rows.append(f'| `{safe_md(name)}` | {safe_md((rep.get("repository") or {}).get("status", "Không có report"))} '
                    f'| {safe_md((rep.get("wiki") or {}).get("status", "Không có report"))} | {glyph(status)} **{status}** |')
    rows += ['', '</details>', '',
             '> Khi ref lệch, LFS có thể chưa được kiểm tra. Đọc Summary của từng job `Restore ...` '
             'để xem SHA, thời gian commit, và bảng chi tiết; JSON artifact lưu toàn bộ kết quả.']
    return '\n'.join(rows) + '\n', ok

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--destination', required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--wiki', choices=['true', 'false'], required=True)
    p.add_argument('--lfs', choices=['true', 'false'], required=True)
    p.add_argument('--wiki-lfs', choices=['true', 'false'], required=True)
    p.add_argument('--report', required=True)
    args = p.parse_args()
    report = {'target': args.target, 'source': args.source,
              'destination': args.destination, 'time_utc': datetime.now(timezone.utc).isoformat(),
              'repository': {'status': 'CHƯA XÁC MINH'},
              'wiki': {'status': 'CHƯA XÁC MINH'}, 'status': 'CHƯA XÁC MINH'}
    ok = True
    try:
        # Validate URLs. Local paths are allowed only for offline self-tests.
        for url in (args.source, args.destination):
            parts = urlsplit(url)
            if not ((parts.scheme == 'https' and parts.netloc and not parts.username and not parts.password)
                    or (os.environ.get('VERIFY_ALLOW_LOCAL') == '1' and not parts.scheme)):
                raise VerificationError('URL không hợp lệ hoặc chứa thông tin đăng nhập.')
        with tempfile.TemporaryDirectory(prefix='restore-check-', dir=os.environ.get('RUNNER_TEMP')) as temp:
            root = Path(temp)
            for kind, source, dest, lfs in (
                ('repository', args.source, args.destination, args.lfs == 'true'),
                ('wiki', wiki_url(args.source), wiki_url(args.destination), args.wiki_lfs == 'true'),
            ):
                if kind == 'wiki':
                    if args.wiki == 'false':
                        report['wiki'] = {'status': 'BỎ QUA', 'note': 'Cấu hình wiki=0 (không nằm trong phạm vi backup).'}
                        continue
                    try:
                        found_source = wiki_exists(source, True)
                        found_dest = wiki_exists(dest, False)
                        if not found_source and not found_dest:
                            report['wiki'] = {'status': 'KHÔNG CÓ', 'note': 'Không phát hiện nhánh Wiki ở hai bên.'}
                            continue
                        if not found_source or not found_dest:
                            raise VerificationError('Wiki chỉ có ở một phía hoặc không thể đọc; không giống nhau.')
                    except Exception as e:
                        report['wiki'] = {'status': 'CHƯA XÁC MINH', 'error': str(e)}
                        ok = False
                        continue
                try:
                    report[kind] = one_kind(source, dest, root, kind, lfs, is_wiki=(kind == 'wiki'))
                except RefMismatch as e:
                    report[kind] = {'status': e.status, 'stage': 'Branches / Tags', 'error': str(e), 'issues': e.issues,
                                    'note': 'Refs chưa khớp. Git fsck đã chạy; LFS chưa kiểm tra trong lượt này.'}
                    ok = False
                    print(f'::warning title={args.target} {kind}: {e.status}::{len(e.issues)} ref khác nhau; xem Summary/JSON.', flush=True)
                    for item in e.issues[:10]:
                        print(f'  {item["kind"]}: {item["ref"]} GitHub={short_oid(item.get("github_sha"))} '
                              f'GitLab={short_oid(item.get("gitlab_sha"))} — {item["reason"]}', flush=True)
                except MissingLfs as e:
                    report[kind] = {'status': 'THIẾU LFS', 'error': str(e), 'stage': 'Git LFS', 'missing_oids': e.oids}
                    ok = False
                    print(f'::error title={args.target} {kind}: thiếu LFS::{str(e)}', flush=True)
                except SnapshotChanged as e:
                    report[kind] = {'status': 'CHƯA XÁC MINH', 'stage': 'Kiểm tra snapshot', 'error': str(e), 'snapshot_changes': e.changes}
                    ok = False
                    print(f'::warning title={args.target} {kind}: nguồn thay đổi::{str(e)}', flush=True)
                except DataCorruption as e:
                    report[kind] = {'status': 'LỖI DỮ LIỆU', 'stage': 'Git fsck' if str(e).startswith('Git object') else 'Git LFS SHA-256', 'error': str(e)}
                    ok = False
                    print(f'::error title={args.target} {kind}: lỗi dữ liệu::{str(e)}', flush=True)
                except VerificationError as e:
                    report[kind] = {'status': 'CHƯA XÁC MINH', 'error': str(e)}
                    ok = False
                    print(f'FAIL {kind}: {e}', file=sys.stderr, flush=True)
                except Exception as e:
                    report[kind] = {'status': 'CHƯA XÁC MINH', 'error': str(e)}
                    ok = False
                    print(f'FAIL {kind}: {e}', file=sys.stderr, flush=True)
    except Exception as e:
        ok = False
        report['repository'] = {'status': 'CHƯA XÁC MINH', 'error': str(e)}
        report['wiki'] = {'status': 'CHƯA XÁC MINH', 'error': 'Không thể chạy đầy đủ do lỗi thiết lập.'}
    statuses = {report['repository'].get('status'), report['wiki'].get('status')}
    if ok:
        report['status'] = 'ĐẠT'
    elif 'LỖI DỮ LIỆU' in statuses:
        report['status'] = 'LỖI DỮ LIỆU'
    elif 'THIẾU LFS' in statuses:
        report['status'] = 'THIẾU LFS'
    elif 'LỆCH REFS' in statuses:
        report['status'] = 'LỆCH REFS'
    elif 'CHƯA XÁC MINH' in statuses:
        report['status'] = 'CHƯA XÁC MINH'
    elif 'CHƯA ĐỒNG BỘ' in statuses:
        report['status'] = 'CHƯA ĐỒNG BỘ'
    else:
        report['status'] = 'KHÔNG ĐẠT'
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    rendered = render_summary(report)
    print(rendered, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as output:
            output.write(rendered + '\n')
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main())
