#!/usr/bin/env python3
"""Tổng hợp trạng thái Restore Test từ GitHub Actions Jobs API, không dùng artifacts.

GitHub không tự truyền Step Summary giữa các job matrix; trạng thái chi tiết
Git/LFS/Wiki vẫn nằm trong Step Summary của từng job Restore <target>.
"""
import html
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def md(value):
    text = html.escape(str(value), quote=True)
    return text.replace('|', '\\|').replace('`', '\\`').replace('\n', ' ').replace('\r', ' ')


def get_jobs(api_url, repository, run_id, token):
    if not token:
        raise RuntimeError('Không có quyền đọc job GitHub Actions (thiếu GITHUB_TOKEN).')
    all_jobs = []
    for page in range(1, 21):
        path = f'/repos/{quote(repository, safe="/")}/actions/runs/{int(run_id)}/jobs?per_page=100&page={page}'
        url = api_url.rstrip('/') + path
        req = Request(url, headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'restore-test-no-artifact',
        })
        for attempt in range(3):
            try:
                with urlopen(req, timeout=20) as response:
                    data = json.load(response)
                break
            except HTTPError as exc:
                if attempt < 2 and exc.code in (429, 500, 502, 503, 504):
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(f'GitHub Actions API trả HTTP {exc.code}; chưa thể tổng hợp job.') from None
            except (URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError('Không kết nối được GitHub Actions API; chưa thể tổng hợp job.') from None
        jobs = data.get('jobs', [])
        if not isinstance(jobs, list):
            raise RuntimeError('GitHub Actions API trả về dữ liệu job không hợp lệ.')
        all_jobs.extend(jobs)
        total_count = data.get('total_count')
        if len(jobs) < 100 or (isinstance(total_count, int) and len(all_jobs) >= total_count):
            return all_jobs
    raise RuntimeError('GitHub Actions API có quá nhiều trang job; chưa tổng hợp đủ.')


def make_summary(selected, jobs, prepare_status, verify_status, api_error=None):
    lookup = {str(j.get('name')): j for j in jobs if isinstance(j, dict)}
    n_pass = n_fail = n_unknown = 0
    lines = ['## Tổng hợp Restore Test — không lưu Artifact', '']
    table = ['| Repository | Trạng thái kiểm tra | Mở job |', '|---|---|---|']
    for entry in selected:
        name = str(entry['target'])
        job = lookup.get('Restore ' + name)
        if not job or job.get('status') != 'completed':
            state = '❔ CHƯA XÁC MINH'
            n_unknown += 1
        elif job.get('conclusion') == 'success':
            state = '✅ JOB PASS (xem Summary Wiki)'
            n_pass += 1
        else:
            state = '❌ CHƯA ĐẠT (xem Summary của job)'
            n_fail += 1
        job_url = job.get('html_url') if job else None
        # URL is supplied by GitHub Jobs API; only link to GitHub job pages.
        if job_url and job_url.startswith('https://github.com/'):
            link = f'[Xem chi tiết]({job_url})'
        else:
            link = 'Xem danh sách jobs bên trên'
        table.append(f'| `{md(name)}` | {state} | {link} |')
    lines += [f'- **Job PASS:** {n_pass}/{len(selected)} repository (có thể chỉ ĐẠT SOURCE CODE nếu Wiki chưa xác minh)',
              f'- **Chưa đạt:** {n_fail} repository',
              f'- **Chưa xác minh được trạng thái job:** {n_unknown}',
              f'- **Chuẩn bị:** `{md(prepare_status)}`; **Kiểm tra:** `{md(verify_status)}`.', '']
    if api_error:
        lines += [f'**Cảnh báo:** {md(api_error)}', '']
    lines += table + ['',
              '> **Xem lỗi chi tiết:** mở Summary của job `Restore <tên repo>` để xem trạng thái '
              'Repository, LFS, Wiki, SHA và OID. Job PASS không nhất thiết là Wiki PASS. Bảng trên chỉ đọc trạng thái job, '
              '**không thể phân biệt** CHƯA ĐỒNG BỘ / LỆCH REFS / THIẾU LFS / CORRUPT.',
              '> Không upload/download GitHub Actions Artifact. Kết quả JSON tạm bị xóa cùng runner.', '']
    complete = (bool(selected) and n_pass == len(selected) and not api_error
                and prepare_status == 'success' and verify_status == 'success')
    return '\n'.join(lines), complete


def main():
    try:
        selected = json.loads(os.environ.get('MATRIX_JSON') or '{}').get('include', [])
    except (TypeError, ValueError):
        selected = []
    prepared = os.environ.get('PREPARE_STATUS', 'unknown')
    verified = os.environ.get('VERIFY_STATUS', 'unknown')
    jobs, error = [], None
    if selected and prepared == 'success':
        try:
            jobs = get_jobs(os.environ.get('GH_API_URL', 'https://api.github.com'),
                            os.environ['GH_REPOSITORY'], os.environ['GH_RUN_ID'],
                            os.environ.get('GH_TOKEN', ''))
        except (RuntimeError, KeyError, ValueError) as exc:
            error = str(exc)
    elif not selected:
        error = 'Không có danh sách repository; bước chuẩn bị có thể đã thất bại.'
    rendered, ok = make_summary(selected, jobs, prepared, verified, error)
    print(rendered)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write(rendered + '\n')
    if not ok:
        print('Restore Test: chưa phải mọi repository đều ĐẠT; xem Summary từng job.', file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
