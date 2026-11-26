"""Retention for every Office v2 writer. Bounded deletes, run hourly by the seat host. Spec section 12."""
from pathlib import Path
import re
import sqlite3
import subprocess

from . import quota_measurement as Q,search_broker as S,seat_sandbox as B

DAY=86400
EVENT_DAYS=14
LOG_DAYS=14
SESSION_DAYS=14
BATCH=5000
DEFAULT_CAP=5*1024**3


def _as_office(*argv,timeout=120):
    return subprocess.run(['sudo','-n','-u',B.USER,*argv],capture_output=True,text=True,timeout=timeout,cwd='/')


def prune_events(conn,*,now):
    """The (corrected_at) index bounds this. NULL times stay until the epoch's export: unknown is not old."""
    return conn.execute('DELETE FROM office_v2_events WHERE seq IN (SELECT seq FROM office_v2_events WHERE corrected_at<? ORDER BY corrected_at LIMIT ?)',
                        (now-EVENT_DAYS*DAY,BATCH)).rowcount


def prune_logs(directory,*,now):
    """Whole days, by the date in the name. A name that does not parse is left alone."""
    import calendar,time
    removed=0
    for path in Path(directory).glob('*.jsonl'):
        match=re.search(r'-(\d{8})\.jsonl$',path.name)
        if not match:continue
        try:at=calendar.timegm(time.strptime(match.group(1),'%Y%m%d'))
        except ValueError:continue
        if at<now-LOG_DAYS*DAY:
            path.unlink();removed+=1
    return removed


def prune_sessions(state_dir,*,current=None):
    """Old session files, never the current one. A missing directory is fine before the first session; any other failure is reported."""
    root=str(Path(state_dir)/'.claude/projects')
    keep=['!','-name',current+'.jsonl'] if current else []
    try:
        # sudo itself can exit 1, so absence is a line printed by the command that ran, never a return code.
        present=_as_office('sh','-c','if [ -d "$1" ]; then echo present; else echo absent; fi','sh',root)
        answer=present.stdout.strip() if present.returncode==0 else None
        if answer=='absent':return True
        if answer!='present':return False
        return _as_office('find',root,'-type','f','-mtime','+%d'%SESSION_DAYS,*keep,'-delete').returncode==0
    except (OSError,subprocess.SubprocessError):return False


def session_exists(state_dir,session_id,*,family='claude'):
    """False only on a clean search that found nothing. Anything else raises: a failed read must not throw away a live session."""
    if family=='hermes':   # Hermes keeps sessions in its state database, not one file per session: a present database is a resumable session
        tree,name='.hermes','*.db'
    else:tree,name=('.codex/sessions','*'+session_id+'.jsonl') if family=='codex' else ('.claude/projects',session_id+'.jsonl')
    done=_as_office('find',str(Path(state_dir)/tree),'-name',name)
    if done.returncode:raise OSError('session_search_failed')
    return bool(done.stdout.strip())


def workspace_bytes(path):
    """None when it cannot be measured."""
    try:
        done=_as_office('du','-sb',str(path))
        used=int(done.stdout.split()[0]) if done.returncode==0 else None
        return used if used is not None and used>=0 else None
    except (OSError,subprocess.SubprocessError,IndexError,ValueError):return None


def over_cap(cfg):
    """Admission check between wakes, not a disk quota: a running wake can grow past the cap until it ends. Over cap or unmeasurable means no next wake."""
    cap=cfg.get('workspace_cap_bytes',DEFAULT_CAP)
    for path in (cfg['workspace'],cfg['state_dir']):
        used=workspace_bytes(path)
        if used is None:return 'workspace_size_unknown'
        if used>cap:return 'workspace_over_cap'
    return None


NOTIFICATION_DAYS=90


def prune_notifications(office,*,now):
    """The page log. The inbox text it points at is a mailbox event and goes by the event rule."""
    if not office.execute("SELECT 1 FROM sqlite_master WHERE name='office_v2_notifications'").fetchone():return 0
    return office.execute('DELETE FROM office_v2_notifications WHERE rowid IN (SELECT rowid FROM office_v2_notifications WHERE sent_at<? LIMIT ?)',(now-NOTIFICATION_DAYS*DAY,BATCH)).rowcount


def run(cfg,office,quota,*,now,current=None,extra=()):
    """Every writer the seat runtime has. extra: a plug-in's own rules, callables (cfg, office, now) -> one log line; a failing one is reported and the rest still run."""
    lines=[]
    try:S.from_config(cfg).prune(now);lines.append('search_pruned=True')
    except (OSError,ValueError,S.sqlite3.Error):lines.append('search_pruned=False')
    lines.append('events_deleted=%d'%prune_events(office,now=now))
    lines.append('notifications_deleted=%d'%prune_notifications(office,now=now))
    lines.append('logs_deleted=%d'%prune_logs(cfg['proxy_log_dir'],now=now))
    lines.append('sessions_ok=%s'%prune_sessions(cfg['state_dir'],current=current))
    lines.append('quota_deleted=%s'%Q.prune(quota,now=now))
    for rule in extra:
        try:lines.append(str(rule(cfg,office,now))[:200])
        except Exception as exc:lines.append('plugin_retention_failed=%s'%type(exc).__name__)
    return lines
