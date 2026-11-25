"""Fake local children only: no systemd/model/auth calls."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sniper_office.supervisor import run_child


def run(tmp_path, code, timeout=1, limit=1024):
    return run_child([sys.executable,'-I','-B','-c',code],timeout_s=timeout,cwd=tmp_path,env={'PATH':os.defpath},max_output_bytes=limit)


def test_capture_stderr_without_corrupting_stdout_or_exit(tmp_path):
    result=run(tmp_path,"import os;os.write(1,b'proof');os.write(2,b'PermissionError: config denied\\n');raise SystemExit(7)")
    assert result['outcome']=='child_error' and result['returncode']==7
    assert result['stdout']==b'proof'
    assert result['stderr']==b'PermissionError: config denied\n'


def test_stderr_flood_is_bounded_not_deadlocked(tmp_path):
    result=run(tmp_path,"import os;os.write(2,b'x'*1000000)",limit=4096)
    assert result['outcome']=='output_limit'
    assert len(result['stderr'])<=16384


def test_grandchild_holding_only_stderr_is_reaped_at_deadline(tmp_path):
    code="import os,time;pid=os.fork();os.write(2,b'child started\\n');os.close(1);time.sleep(30) if pid==0 else None"
    result=run(tmp_path,code,timeout=.25)
    assert result['outcome']=='timeout' and b'child started' in result['stderr']


def test_tail_redacts_credentials_before_utf8_byte_cap():
    from sniper_office.launcher_diagnostics import stderr_tail
    raw=('Error: TLS failed\n'+json.dumps({'accessToken':'secret-access','refresh_token':'secret-refresh','password':'secret-password'})+'\nAuthorization: Bearer secret-bearer\napi_key=secret-key\nhttps://user:secret-url@example.invalid/\nsk-ant-'+('A'*60)+'\neyJ'+('B'*60)+'\n').encode()
    clean=stderr_tail(raw)
    for secret in ('secret-access','secret-refresh','secret-password','secret-bearer','secret-key','secret-url','A'*60,'B'*60):assert secret not in clean
    assert 'TLS failed' in clean
    assert len(stderr_tail(('😀'*5000).encode()).encode())<=4096
    assert 'secret-partial' not in stderr_tail(b'secret-partial\nError: safe\n',truncated=True)


def test_persist_private_exclusive_file_and_refuse_symlink(tmp_path):
    from sniper_office.launcher_diagnostics import persist_stderr
    persist_stderr(tmp_path,'request',b'token=secret-value\nError: denied\n')
    path=tmp_path/'request.stderr.txt'
    assert path.stat().st_mode & 0o777==0o600 and 'secret-value' not in path.read_text()
    outside=tmp_path/'outside';outside.write_text('unchanged')
    (tmp_path/'other.stderr.txt').symlink_to(outside)
    with pytest.raises(FileExistsError):persist_stderr(tmp_path,'other',b'new')
    assert outside.read_text()=='unchanged'


@pytest.mark.parametrize('raw,secrets',[
    (b'Authorization: Bearer short-secret',('short-secret',)),
    (b'Authorization: Basic dXNlcjpwYXNz',('dXNlcjpwYXNz',)),
    (b'Cookie: a=short-secret; b=another-secret',('short-secret','another-secret')),
    (b'[sdk] Authorization: Bearer short-secret',('short-secret',)),
])
def test_standalone_secret_headers_are_redacted_in_full(raw,secrets):
    from sniper_office.launcher_diagnostics import stderr_tail
    clean=stderr_tail(raw)
    assert all(s not in clean for s in secrets)
