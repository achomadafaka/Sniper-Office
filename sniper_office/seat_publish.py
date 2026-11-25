"""Publishes what the sandbox user may read: this package, optionally a project's git-tracked tree, and a Python runtime. Nothing under /home is reachable from that user."""
import hashlib
import os
from pathlib import Path

from . import seat_sandbox as B
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile

FORBIDDEN=re.compile(r'(^|/)(\.env(\..*)?|.*keypair.*|.*\.(key|pem|p12|pfx)|\.credentials\.json|credentials\.json|runtime\.json|.*\.db(-wal|-shm)?)$',re.I)
# certifi's public CA bundle is the only .pem allowed: it holds no private key.
ALLOWED=re.compile(r'(^|/)(\.env\.example|certifi/cacert\.pem|.*/test[^/]*\.pem)$')
# Header plus key body: source that only names the marker (log redaction, this file) is not a key.
PRIVATE=re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/]{32}')
# Latest at 2026-10-30. A new SDK resets usage scope, so a bump restarts calibration: move these with the seat configs' sdk_version.
SEAT_PINS={'claude-agent-sdk':'0.2.175','playwright':'1.64.1'}
SEAT_CLI_SHA256='4487042b4c4f5d88d4568f68ae98aae0b1ffae58864cf57fb868641bc5f84594'   # the CLI 2.1.314 in that wheel


def _has_key(path):
    """Streams every file, however large. The overlap keeps a key split across two chunks visible."""
    tail=b''
    with open(path,'rb') as f:
        for part in iter(lambda:f.read(1024**2),b''):
            # Keep a header across arbitrarily long whitespace without retaining that padding.
            data=re.sub(rb'\s+',b' ',tail+part)
            if PRIVATE.search(data):return True
            tail=data[-256:]
    return False


SEAT_UNITS=['office-seat@*']   # host units that import the release; a project appends its own before publishing


def seats_running():
    """An idle seat host runs no sandbox under the sandbox user, so its unit counts too. Every seat model process lives under a --die-with-parent bwrap; the uid's idle desktop services (pipewire, dbus) do not. A failed read counts as running."""
    try:
        out=subprocess.run(['ps','-u',B.USER,'-o','comm='],capture_output=True,text=True,timeout=10,check=True).stdout
        units=subprocess.run(['systemctl','--user','list-units','--no-legend','--plain','--state=active,activating,reloading,deactivating',*SEAT_UNITS],capture_output=True,text=True,timeout=10,check=True).stdout
    except (OSError,subprocess.SubprocessError):return True
    return 'bwrap' in out.split() or bool(units.strip())


def refused(root):
    """Paths that must never be published: names on the list, private key text, or anything but plain files and dirs."""
    bad=[]
    root=Path(root)
    for path in root.rglob('*'):
        rel=path.relative_to(root).as_posix();mode=path.lstat().st_mode
        if stat.S_ISLNK(mode):
            target=path.resolve()
            if not target.is_relative_to(root.resolve()):bad.append(rel+':symlink_out')
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):bad.append(rel+':type');continue
        if stat.S_ISREG(mode):
            if FORBIDDEN.search(rel) and not ALLOWED.search(rel):bad.append(rel+':name')
            elif _has_key(path):bad.append(rel+':private_key')
    return bad


def _swap(new,dest):
    """A bind mount pins the old directory, not its files, so deleting the old tree would break a running seat. Callers refuse while one runs."""
    bad=refused(new)
    if bad:
        shutil.rmtree(new);raise ValueError('publish_refused:'+','.join(sorted(bad)[:8]))
    # copytree's copystat drops setgid, so groups vary and the host user cannot chgrp to the sandbox user. The 2750 bridge dir is the gate; nothing inside is secret.
    subprocess.run(['chmod','-R','go+rX,go-w',str(new)],check=True)
    old=Path(str(dest)+'.old')
    if dest.exists():dest.rename(old)
    new.rename(dest)
    if old.exists():shutil.rmtree(old)


def publish_release(repo,sha,bridge,*,running=seats_running):
    """This package, plus a project's git-tracked files at a full SHA when repo is given (so seats can read the code they work on). The `.env.example` is left out rather than allowed through."""
    if repo is not None and not re.fullmatch(r'[0-9a-f]{40}',str(sha)):raise ValueError('publish_sha')
    if running():raise ValueError('publish_seats_running')
    bridge=Path(bridge);new=bridge/('release.new')
    if new.exists():shutil.rmtree(new)
    new.mkdir()
    if repo is not None:
        with tempfile.TemporaryFile() as raw:
            subprocess.run(['git','-C',str(repo),'archive','--format=tar',sha],stdout=raw,check=True)
            raw.seek(0)
            with tarfile.open(fileobj=raw) as tar:tar.extractall(new,filter='data')
        (new/'.env.example').unlink(missing_ok=True)
        (new/'PUBLISHED_SHA').write_text(sha+'\n')
    # The sandbox children run from /opt/release/sniper_office. The running package wins over a copy in the project tree, so host and sandbox are one version.
    package=new/'sniper_office'
    if package.exists():shutil.rmtree(package)
    shutil.copytree(Path(__file__).resolve().parent,package,ignore=shutil.ignore_patterns('__pycache__'))
    _swap(new,bridge/'release')


def stale_pins():
    """Operator order 2026-10-30: seats stay on the latest tooling. Returns what is behind; a failed lookup is reported, not read as current."""
    behind={}
    for name,pinned in SEAT_PINS.items():
        try:
            out=subprocess.run([sys.executable,'-m','pip','index','versions',name],capture_output=True,text=True,timeout=60).stdout
            latest=re.match(r'\S+ \(([^)]+)\)',out).group(1)
        except (OSError,subprocess.SubprocessError,AttributeError):latest='unknown'
        if latest!=pinned:behind[name]=dict(pinned=pinned,latest=latest)
    return behind


def publish_runtime(bridge,*,running=seats_running):
    """Interpreter, stdlib, the pinned seat packages and their browser. Nothing else comes along. Not v1's vendored SDK: v1 keeps its own pin for rollback."""
    if running():raise ValueError('publish_seats_running')
    bridge=Path(bridge);new=bridge/'runtime.new';prefix=Path(sys.base_prefix)
    if new.exists():shutil.rmtree(new)
    version='python%d.%d'%sys.version_info[:2]
    (new/'bin').mkdir(parents=True);(new/'lib').mkdir()
    shutil.copy2(prefix/'bin'/version,new/'bin'/version);(new/'bin/python3').symlink_to(version)
    soname=sysconfig.get_config_var('INSTSONAME')
    shutil.copy2(prefix/'lib'/soname,new/'lib'/soname)
    site=new/'lib'/version/'site-packages'
    shutil.copytree(prefix/'lib'/version,new/'lib'/version,ignore=shutil.ignore_patterns('site-packages','__pycache__','test'))
    site.mkdir()
    try:
        subprocess.run([sys.executable,'-m','pip','install','--quiet','--no-input','--target',str(site),*['%s==%s'%kv for kv in SEAT_PINS.items()]],check=True,capture_output=True,timeout=600)
        cli=site/'claude_agent_sdk/_bundled/claude'
        if hashlib.sha256(cli.read_bytes()).hexdigest()!=SEAT_CLI_SHA256:raise ValueError('publish_cli_hash')
        env=dict(os.environ,PYTHONPATH=str(site),PLAYWRIGHT_BROWSERS_PATH=str(new/'browsers'))
        subprocess.run([sys.executable,'-m','playwright','install','chromium-headless-shell'],check=True,capture_output=True,timeout=600,env=env)
    except (OSError,subprocess.SubprocessError,ValueError):
        shutil.rmtree(new);raise
    _swap(new,bridge/'runtime')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo');parser.add_argument('--sha');parser.add_argument('--bridge',default=str(B.BRIDGE))
    args=parser.parse_args()
    behind=stale_pins()
    if behind:print('publish_pins_not_latest',behind)
    from sniper_office.seat_host import lifecycle
    with lifecycle(args.bridge):   # the lock a wake holds for its whole run
        publish_runtime(args.bridge);publish_release(args.repo,args.sha,args.bridge)
