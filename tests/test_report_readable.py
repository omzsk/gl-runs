"""Tests for concise GitHub Actions job summary and detailed diagnostics."""
import importlib.util
from pathlib import Path

script = Path(__file__).resolve().parents[1] / 'scripts/verify_restore_full.py'
spec = importlib.util.spec_from_file_location('report_module', script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ok = {
    'target':'KernelSU_tiann', 'source':'https://github.com/example/repo.git',
    'destination':'https://gitlab.com/example/repo.git', 'time_utc':'2026-10-28T10:00:00+00:00',
    'status':'ĐẠT',
    'repository': {'status':'ĐẠT','branches':3,'tags':2,'backup_tags':7,
                   'lfs':{'count':20,'bytes':10240,'archived':3}},
    'wiki':{'status':'KHÔNG CÓ','note':'Không phát hiện nhánh Wiki ở hai bên.'},
}
text=mod.render_summary(ok)
assert 'Restore KernelSU_tiann' in text
assert '**Repository:** ✅ **PASS**' in text
assert '**LFS:** ✅ **PASS**' in text
assert '20 object' in text
assert '**Wiki:** ⏭️ **KHÔNG CÓ**' in text
assert '### Chi tiết lỗi' not in text

behind={**ok,'status':'CHƯA ĐỒNG BỘ','repository': {
    'status':'CHƯA ĐỒNG BỘ', 'stage':'Branches / Tags', 'error':'1 branch chậm',
    'issues':[{'kind':'BRANCH_CHẬM','ref':'refs/heads/main','github_sha':'a'*40,
               'gitlab_sha':'b'*40,'commits_behind':2, 'reason':'Lệch <script> & theo thời gian',
               'github':{'commit_time':'2026-10-28T08:00:00+00:00'},
               'gitlab':{'commit_time':'2026-10-27T08:00:00+00:00'}}],
}}
text=mod.render_summary(behind)
assert '**LFS:** ⏸️ **CHƯA XÁC NHẬN**' in text
assert '`refs/heads/main`' in text
assert 'chậm **2 commit**' in text
assert 'aaaaaaaaaaaa' in text and 'bbbbbbbbbbbb' in text
assert '<script>' not in text and '&lt;script&gt;' in text
assert '<details>' in text and 'committer' in text

missing={**ok,'status':'THIẾU LFS','repository': {
    'status':'THIẾU LFS','stage':'Git LFS', 'error':'Thiếu object',
    'missing_oids':['f'*64,'e'*64],
}}
text=mod.render_summary(missing)
assert 'LFS thiếu' in text
assert 'ffffffff' in text
assert 'eeeeeeeeeeee' in text

new_source={**ok,'status':'CHƯA XÁC MINH','repository': {
    'status':'CHƯA XÁC MINH','stage':'Kiểm tra snapshot',
    'error':'Nguồn GitHub đã thay đổi',
    'snapshot_changes':[{'ref':'refs/heads/main','old':'a'*40,'new':'b'*40}],
}}
assert 'Ref đổi trong khi test' in mod.render_summary(new_source)

assert "JSON artifact" not in mod.render_summary(behind)
print('Vietnamese concise summary tests PASSED')
