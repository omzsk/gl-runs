"""Bảng tổng hợp không artifact, thử với dữ liệu API mô phỏng."""
import importlib.util
from pathlib import Path
from unittest.mock import patch
from io import BytesIO
import json

script = Path(__file__).resolve().parents[1] / 'scripts/restore_summary_jobs.py'
spec = importlib.util.spec_from_file_location('no_artifact_summary', script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

repos = [{'target':'KernelSU_tiann'}, {'target':'KernelSU_5ec1cff'}, {'target':'Repo_ABC'}]
jobs = [
    {'name':'Restore KernelSU_tiann','status':'completed','conclusion':'success',
     'html_url':'https://github.com/test/repo/actions/runs/123/job/11'},
    {'name':'Restore KernelSU_5ec1cff','status':'completed','conclusion':'failure',
     'html_url':'https://github.com/test/repo/actions/runs/123/job/12'},
    {'name':'Restore Repo_ABC','status':'completed','conclusion':'success',
     'html_url':'https://github.com/test/repo/actions/runs/123/job/13'},
]
md, ok = mod.make_summary(repos,jobs,'success','failure')
assert not ok and '2/3' in md and '1 repository' in md and 'Repo_ABC' in md
assert 'https://github.com/test/repo/actions/runs/123/job/12' in md
assert 'không thể phân biệt' in md
assert 'JSON artifact' not in md
md, ok = mod.make_summary(repos,[{**x, 'conclusion':'success'} for x in jobs], 'success','success')
assert ok and '3/3' in md
md, ok = mod.make_summary(repos, jobs[:2], 'success','failure')
assert not ok and 'CHƯA XÁC MINH' in md
md, ok = mod.make_summary(repos, [], 'failure', 'skipped', 'GitHub API lỗi')
assert not ok and 'GitHub API lỗi' in md

class FakeResponse:
    def __init__(self, content): self.file=BytesIO(json.dumps(content).encode('utf-8'))
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, *a): return self.file.read(*a)

fake = FakeResponse({'total_count':3, 'jobs':jobs})
with patch.object(mod, 'urlopen',return_value=fake) as mocked:
    loaded = mod.get_jobs('https://api.github.com','test/repo',123,'FAKE_TOKEN')
    assert len(loaded)==3
    url = mocked.call_args.args[0]
    assert '/actions/runs/123/jobs?' in url.full_url
    assert url.get_header('Authorization') == 'Bearer FAKE_TOKEN'
print('No-artifact aggregate summary tests PASSED')
