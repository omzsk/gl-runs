"""Wiki absence vs authentication errors: don't claim corrupt or Wiki PASS."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

script = Path(__file__).resolve().parents[1] / 'scripts/verify_restore_full.py'
spec = importlib.util.spec_from_file_location('verify_wiki_under_test', script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def probe(host, returncode, stderr='', stdout=''):
    with patch.object(mod, 'git', return_value=SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)) as mocked:
        result = mod.wiki_exists('https://' + host + '/a/b.wiki.git', host == 'github.com')
        env = mocked.call_args.kwargs['env']
        if host == 'github.com':
            assert env['GIT_TERMINAL_PROMPT'] == '0'
            assert not env.get('GIT_ASKPASS'), 'GitLab-only helper must not be used for GitHub Wiki'
        return result


assert probe('github.com', 0, stdout='1a2b3c\trefs/heads/master\n') is True
assert probe('github.com', 0, stdout='') is False
assert probe('github.com', 128, stderr='fatal: repository not found') is False
assert probe('gitlab.com', 128, stderr='fatal: repository not found') is False
assert probe('github.com', 128, stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled") is None
assert probe('gitlab.com', 128, stderr="fatal: could not read Username for 'https://gitlab.com': terminal prompts disabled") is None
try:
    probe('github.com', 128, stderr='fatal: HTTP 429 rate limited')
    raise AssertionError('429 must not be treated as nonexistent Wiki')
except mod.VerificationError:
    pass

result = {
    'target': 'Test', 'time_utc': '2026-10-28T12:00:00Z',
    'status': 'ĐẠT SOURCE CODE',
    'repository': {'status':'ĐẠT','branches':1,'tags':2,'backup_tags':0,
                   'lfs':{'count':4,'bytes':1024,'archived':1}},
    'wiki': {'status': 'CHƯA XÁC MINH', 'stage': 'Phát hiện Wiki',
             'error': 'GitHub yêu cầu đăng nhập khi đọc Wiki'},
}
summary = mod.render_summary(result)
assert 'ĐẠT SOURCE CODE' in summary
assert 'Wiki chưa thể xác minh' in summary
assert 'Gi' in summary
print('Wiki auth vs absence tests PASSED')
