"""Trusted launcher-only composition. No credentials or completion callbacks enter the supervisor.

The launcher config is checked against three facts about this host that the config itself must not be able to choose. Set them once, in code, before build_auth:

    launcher_runtime.configure(control_root='/var/lib/myproject/office-control',
                               supervisor_unit='/etc/systemd/user/office-supervisor.service',
                               site_packages='/home/me/.local/lib/python3.13/site-packages')"""
import hashlib
import json
import os
from pathlib import Path
import shlex

from sniper_office.child_launcher import read_regular
from sniper_office.host_auth import Auth,approved_path
from sniper_office.launcher_bindings import Completion
from sniper_office import library_audit


CONTROL_ROOT=None      # private directory holding auth-private, children, launcher-state, guard and launcher-sock
SUPERVISOR_UNIT=None   # the supervisor's unit file: its bwrap mounts are read to prove the supervisor cannot reach the private parts
SITE_PACKAGES=None     # the one library directory model children may mount, audited before use


def configure(*,control_root,supervisor_unit,site_packages):
    global CONTROL_ROOT,SUPERVISOR_UNIT,SITE_PACKAGES
    paths=[Path(control_root),Path(supervisor_unit),Path(site_packages)]
    if not all(p.is_absolute() for p in paths):raise ValueError('launcher_layout')
    CONTROL_ROOT,SUPERVISOR_UNIT,SITE_PACKAGES=paths


def build_auth(config_path,*,expected_sha256,clock,alert):
    """One externally pinned config owns auth, completion and actual unit/child mount identities."""
    if None in (CONTROL_ROOT,SUPERVISOR_UNIT,SITE_PACKAGES):raise ValueError('launcher_layout_unconfigured')
    raw=read_regular(approved_path(str(config_path)))
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('launcher_config_unapproved')
    config=json.loads(raw)
    if type(config)is not dict:raise ValueError('launcher_config_shape')
    try:
        if approved_path(config['claude_credentials'])!=Path.home()/'.claude/.credentials.json':
            raise ValueError('launcher_claude_source')
        if config['state_root']!=config['launcher_state']:raise ValueError('launcher_state_conflict')
        control=CONTROL_ROOT
        for key,name in [('private_state','auth-private'),('child_root','children'),('launcher_state','launcher-state')]:
            if approved_path(config[key])!=control/name:raise ValueError('launcher_private_location')
        if approved_path(config['guard_file'])!=control/'guard/guard.json':raise ValueError('launcher_guard_location')
        if set(config['adapters'])!={'claude_sdk','codex_exec'}:raise ValueError('launcher_adapters')
        model_roots={str(control/'guard')}|{str(approved_path(src))for adapter in config['adapters'].values()for src,_ in adapter['ro_binds']}
        if type(config['model_roots'])is not list or set(config['model_roots'])!=model_roots:
            raise ValueError('launcher_model_roots')
        unit=SUPERVISOR_UNIT
        text=read_regular(unit).decode().replace('%h',str(Path.home())).replace('%U',str(os.getuid()))
        commands=[line.split('=',1)[1]for line in text.splitlines()if line.startswith('ExecStart=')]
        if len(commands)!=1:raise ValueError('launcher_supervisor_unit')
        args=shlex.split(commands[0])
        roots={args[i+1]for i,arg in enumerate(args)if arg=='--bind'}
        if type(config['supervisor_roots'])is not list or set(config['supervisor_roots'])!=roots:
            raise ValueError('launcher_supervisor_roots')
        if not any(args[i:i+2]==['--tmpfs',str(control)]for i in range(len(args))):
            raise ValueError('launcher_supervisor_private_mount')
        if approved_path(config['socket_path'])!=control/'launcher-sock/launcher.sock':
            raise ValueError('launcher_socket_location')
        for path in (control/'guard',control/'launcher-sock'):
            if not any(args[i:i+3]==['--ro-bind',str(path),str(path)]for i in range(len(args))):
                raise ValueError('launcher_supervisor_control_mount')
        for at,arg in enumerate(args):
            if arg in ('--bind','--ro-bind') and any(p==str(control) or p.startswith(str(control)+'/') for p in args[at+1:at+3]):
                if args[at:at+3]not in (['--ro-bind',str(control/'guard'),str(control/'guard')],
                                           ['--ro-bind',str(control/'launcher-sock'),str(control/'launcher-sock')],
                                           ['--ro-bind',str(control/'transport.json'),str(control/'transport.json')]):
                    raise ValueError('launcher_supervisor_private_mount')
    except (KeyError,TypeError,IndexError) as exc:raise ValueError('launcher_config_shape') from exc
    site=SITE_PACKAGES
    for adapter in config['adapters'].values():
        sources=[src for src,dst in adapter['ro_binds']if dst=='/opt/site']
        if sources!=[str(site)]:raise ValueError('launcher_library_mount')
    report=library_audit.audit(site)
    if type(report)is not dict or report.get('state')!='passed' or report.get('root')!=str(site):
        raise ValueError('launcher_library_audit_unknown')
    # Auth rechecks the same raw-byte pin before touching private state; Completion reads only this protected root.
    return Auth(config_path,expected_sha256=expected_sha256,clock=clock,alert=alert,
                completed=Completion(config['launcher_state']))
