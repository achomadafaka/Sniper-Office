#!/usr/bin/env python3
"""One seat's host process: proxies, account reading, retention and the wake loop.

    python3 -m sniper_office.seat_main --config /etc/myproject/seat_reviewer.json

Runs as the host user, never as the sandbox user. config/seat.example.json lists every key."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid

from . import claude_seat_auth as A,egress_proxy as P,model_proxy as MP,plugins,quota_measurement as Q,quota_read,seat_events as E,seat_host as H,seat_retention as R,seat_sandbox as B,seat_tools as T,work as W

LOG=logging.getLogger('sniper_office.seat_main')
MODEL_HOSTS=frozenset({'anthropic.com','claude.ai','claude.com'})   # what a Claude harness sandbox may reach; cfg['model_hosts'] replaces it
POLL_S=5
RETENTION_EVERY_S=3600
WINDOW_RESET_GRACE_S=5


def stop_seat_units(cfg):
    """Every wake and tool unit of this seat. They live under the sandbox user's manager, so stopping the host does not reach them."""
    stopped=subprocess.run(['sudo','-n','-u',B.USER,'env','XDG_RUNTIME_DIR=/run/user/%d'%B.office_uid(),'systemctl','--user','stop',T.UNIT_PREFIX+'%s-*'%cfg['actor']],capture_output=True,cwd='/',timeout=60)
    if stopped.returncode:raise SystemExit('seat_units_not_stopped: '+stopped.stderr.decode()[:200])


def prepare(cfg):
    """Everything a bind needs must exist before bwrap runs. Paths the sandbox user owns are made as that user."""
    seat=Path(cfg.get('bridge',B.BRIDGE))/'seats'/cfg['actor'];seat.mkdir(parents=True,exist_ok=True)
    made=subprocess.run(['sudo','-n','-u',B.USER,'mkdir','-p',cfg['workspace']+'/tmp',cfg['state_dir']+'/.claude',cfg['state_dir']+'/.codex/sessions',cfg['state_dir']+'/tmp'],capture_output=True,cwd='/')
    if made.returncode:raise SystemExit('seat_dirs_failed: '+made.stderr.decode()[:200])
    # A host crash frees the lifecycle lock while its wake unit can still run: end any leftover before this host can start another.
    stop_seat_units(cfg)
    if cfg.get('family')=='codex':
        import shutil
        for source,name,pin in ((cfg['codex_binary'],'codex',cfg['codex_sha256']),(cfg['codex_host_binary'],'codex-code-mode-host',cfg['codex_host_sha256'])):
            shutil.copyfile(source,seat/(name+'.tmp'))
            if hashlib.sha256((seat/(name+'.tmp')).read_bytes()).hexdigest()!=pin:raise SystemExit('codex_binary_not_pinned')
            os.chmod(seat/(name+'.tmp'),0o755);os.replace(seat/(name+'.tmp'),seat/name)
    return seat


def live_hosts(host):
    """Hosts the plug-ins say the seats must never reach, read now. Any source failing fails the whole read: a partial set must not pass for the full one."""
    found=set()
    for source in host.deny_host_sources:
        found|={h for h in source() if isinstance(h,str) and h}
    return frozenset(found)


class DenyRefresher:
    """Keeps the deny set current during wakes too: the proxies read it per connection. The protected side can change hosts or be briefly unreadable,
    so failure is tolerated for GRACE_S, then parks wakes and stops a running one. The set only grows."""
    INTERVAL_S=30
    GRACE_S=75   # with 30 s refresh and 10 s watcher sampling, a running wake stops within 120 s, plus stop latency

    def __init__(self,host,proxies,clock=time.time):
        self.host,self.proxies,self.clock=host,proxies,clock
        self.failing_since=None

    def refresh(self):
        try:added=live_hosts(self.host)
        except Exception as exc:
            LOG.warning('deny_hosts_refresh_failed %s',type(exc).__name__)
            if self.failing_since is None:self.failing_since=self.clock()
            return
        for proxy in self.proxies:proxy.deny=frozenset(proxy.deny|added)
        self.failing_since=None

    def park_reason(self):
        return 'deny_hosts_unknown' if self.failing_since is not None and self.clock()-self.failing_since>=self.GRACE_S else None

    def run(self):
        while True:
            self.refresh()
            time.sleep(self.INTERVAL_S)


def start_proxies(cfg,seat,host):
    deny,version=P.load_denylist(cfg['denylist'])
    if host.deny_host_sources:
        # Fails closed if a plug-in's hosts cannot be read. Only the count is logged: hostnames can carry keys.
        live=live_hosts(host)
        if not live:raise SystemExit('deny_hosts_empty')
        deny=deny|live
        LOG.warning('deny_hosts_loaded count=%d',len(live))
    ca=P.load_or_create_ca(cfg['ca_dir'],cfg['actor'])
    (seat/'ca.pem').write_bytes(Path(cfg['ca_dir'],'ca.pem').read_bytes())
    uid=B.office_uid();log=Path(cfg['proxy_log_dir']);log.mkdir(parents=True,exist_ok=True)
    tool=P.Proxy(str(seat/'tool-proxy.sock'),peer_uid=uid,deny=deny,ca=ca,log=str(log/'tool-%Y%m%d.jsonl'),search_broker=P.S.from_config(cfg),read_only_hosts=P.load_read_only_hosts(cfg['denylist']));tool.start()
    if cfg.get('family')=='codex':model=MP.ModelProxy(str(seat/'model-proxy.sock'),peer_uid=uid,deny=deny,ca=ca,actor=cfg['actor'],account_alias=cfg['account_alias'],quota_db=cfg['quota_db'],auth_file=cfg['codex_auth'],log=str(log/'model-%Y%m%d.jsonl'))
    else:model=P.Proxy(str(seat/'model-proxy.sock'),peer_uid=uid,deny=deny,allow=frozenset(cfg.get('model_hosts',MODEL_HOSTS)),log=str(log/'model-%Y%m%d.jsonl'))
    model.start()
    LOG.warning('proxies_up denylist_sha=%s',version)
    return tool,model


def _window_reset_since(quota,alias,latest,now):
    """A window the newest reading names has reset since that reading, so seats would park on an expired window until the next poll. A failed poll after the reset is itself the latest, so this can't hammer a rate limit."""
    row=quota.execute("SELECT payload_json FROM quota_account_snapshots WHERE account_alias=? AND observed_at=? LIMIT 1",(alias,latest)).fetchone()
    try:windows=json.loads(row[0])['windows'] if row else {}
    except (ValueError,KeyError,TypeError):return False
    return any(isinstance(w,dict) and isinstance(w.get('resets_at'),(int,float)) and latest<w['resets_at']<=now-WINDOW_RESET_GRACE_S for w in windows.values())


def poll_codex_sandboxed(cfg):
    """Never the CLI on the real home: its rate-limit read can refresh and rotate the real credential. Here it holds the placeholder token only."""
    try:
        done=subprocess.run(['sudo','-n','-u',B.USER]+B.codex_args(cfg['actor'],cfg['state_dir'],bridge=cfg.get('bridge'),host_sock=False)+['/opt/runtime/bin/python3',T.RELEASE+'codex_child.py','--poll'],
                            input=json.dumps(dict(account_id=MP.read_token(cfg['codex_auth'])[1]))+'\n',capture_output=True,text=True,timeout=60,cwd='/')
        doc=json.loads(done.stdout.splitlines()[-1])
        return doc['raw'],doc['error']
    except (OSError,ValueError,KeyError,TypeError,IndexError,subprocess.SubprocessError):
        LOG.warning('account_poll_failed codex');return None,'transport'


def poll_account(cfg,quota,host,*,now=time.time):
    """Seats share one account and one rate-limited usage endpoint, so exactly one seat polls (config account_poller). The others only read; if the poller is down their reading ages out and they park.
    A failed read is recorded as unknown, never skipped."""
    if not cfg.get('account_poller'):return None
    latest=quota.execute('SELECT max(observed_at) FROM quota_account_snapshots WHERE account_alias=?',(cfg['account_alias'],)).fetchone()[0]
    at=now()
    if latest is not None and 0<=at-latest<cfg['account_poll_s'] and not _window_reset_since(quota,cfg['account_alias'],latest,at):return None
    codex=cfg.get('family')=='codex'
    raw,error=poll_codex_sandboxed(cfg) if codex else quota_read.poll_claude(cfg['claude_credentials'])
    row=Q.record_account_snapshot(quota,snapshot_id=uuid.uuid4().hex,provider='codex' if codex else 'claude',account_alias=cfg['account_alias'],observed_at=now(),raw=raw,error=error)
    Q.prune(quota,now=now())
    if not codex and host.pager is not None:
        try:A.quota_page(cfg,row,page=host.pager)
        except Exception:LOG.warning('quota_auth_page_failed')
    return row


def poll_loop(cfg,host,*,wait=time.sleep):
    """Its own thread and connection: a wake can run an hour, and the other seats' reading must not age out meanwhile."""
    while True:
        try:
            quota=sqlite3.connect(cfg['quota_db'],timeout=5,isolation_level=None)
            try:poll_account(cfg,quota,host)
            finally:quota.close()
        except (OSError,ValueError,sqlite3.Error,subprocess.SubprocessError):LOG.exception('account_poll_failed')
        wait(5)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--config',required=True);parser.add_argument('--force-swap',action='store_true',help='hand this seat over to a fresh session now')
    parser.add_argument('--stop-children',action='store_true',help='end this seat\'s wake and tool units, then exit; the service runs this on every stop')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,format='%(asctime)s %(name)s %(message)s')
    cfg=json.loads(Path(args.config).read_text())
    if cfg.get('host'):B.configure(**cfg['host'])
    if args.stop_children:return stop_seat_units(cfg)
    host=plugins.load(cfg)   # trusted project code: tools, wake reasons, deny hosts, pager
    seat=prepare(cfg);proxies=start_proxies(cfg,seat,host)
    office=sqlite3.connect(cfg['office_db'],timeout=5,isolation_level=None);W.ensure_schema(office);E.ensure_schema(office)
    quota=sqlite3.connect(cfg['quota_db'],timeout=5,isolation_level=None);Q.ensure_schema(quota);H.ensure_schema(quota)
    state=Path(cfg['host_state']);state.parent.mkdir(parents=True,exist_ok=True)
    refresher=DenyRefresher(host,proxies)
    if host.deny_host_sources:threading.Thread(target=refresher.run,daemon=True).start()
    seat_host=H.Seat(cfg=cfg,office_conn=office,quota_conn=quota,clock=time.time,extra_park=refresher.park_reason,plugins=host)
    if args.force_swap:
        with H.lifecycle(cfg.get('bridge')):seat_host.force_swap()
        return
    threading.Thread(target=poll_loop,args=(cfg,host),daemon=True).start()
    last=None;pruned=0
    while True:
        if time.time()-pruned>=RETENTION_EVERY_S:
            for line in R.run(cfg,office,quota,now=time.time(),current=seat_host.state.get('session_id'),extra=host.retention_rules):LOG.warning('retention %s',line)
            pruned=time.time()
        try:outcome=seat_host.tick()
        except ValueError as exc:outcome='refused:'+str(exc)   # lifecycle_busy: a publish holds the lock
        if outcome!=last and outcome!='idle':LOG.warning('seat actor=%s outcome=%s',cfg['actor'],outcome)
        last=outcome
        time.sleep(POLL_S)


if __name__=='__main__':main()
