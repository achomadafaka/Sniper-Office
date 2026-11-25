"""How a project adds its own tools, wake reasons and notifications to a seat. Nothing here is discovered: the seat config names the modules.

    "plugins": ["myproject.office_plugin"]

Each named module has `register(host)`. It runs once, in the seat host process, with full host authority: it is trusted code, like the config."""
from dataclasses import dataclass
import importlib
import re

_NAME=re.compile(r'[a-z][a-z0-9_]{0,47}\Z')


@dataclass(frozen=True)
class Tool:
    description: str
    properties: dict
    required: list
    handler: object            # handler(tools, **arguments) -> text, a JSON-able value, or seat_tools.result(text, error)
    group: str | None = None   # a grouped tool exists only for seats whose config lists the group in "tool_groups"
    call_refs: object = None   # optional (arguments) -> dict stored on the tool_call event
    result_refs: object = None # optional (arguments, text) -> dict stored on the tool_result event


class Host:
    def __init__(self,cfg):
        self.cfg=cfg;self.tools={};self.wake_sources=[];self.deny_host_sources=[];self.label_sources=[];self.busy_checks=[];self.retention_rules=[];self.panels={}
        self.notifier=None;self.pager=None

    def add_tool(self,name,description,properties,required,handler,*,group=None,call_refs=None,result_refs=None):
        """A name that matches a built-in tool replaces it for this seat."""
        if (not isinstance(name,str) or not _NAME.match(name) or not isinstance(description,str) or not isinstance(properties,dict)
                or not isinstance(required,list) or not callable(handler) or (group is not None and not (isinstance(group,str) and _NAME.match(group)))):
            raise ValueError('plugin_tool')
        self.tools[name]=Tool(description,properties,list(required),handler,group,call_refs,result_refs)

    def add_wake_source(self,fn):
        """fn(seat, urgent) -> None or dict(type='new_item', prompt='host data shown to the seat', urgent=bool). Called on every host tick, once with urgent True (ahead of the
        hourly wake reserve) and once with False: answer only the call that matches. Keep it cheap and bounded; keep your own "seen" marks in seat.state."""
        if not callable(fn):raise ValueError('plugin_wake_source')
        self.wake_sources.append(fn)

    def add_busy_check(self,fn):
        """fn(seat) -> True while work is in flight that a session rotation must not interrupt. Raising reads as busy."""
        if not callable(fn):raise ValueError('plugin_busy_check')
        self.busy_checks.append(fn)

    def add_panel(self,name,title,read):
        """A view of the project's own data on the operator page. read(now) returns one of three shapes, which the page renders itself:
          {'shape':'table','columns':[...],'rows':[[...],...]}            up to 16 columns, 200 rows
          {'shape':'facts','items':[{'label','value','unit','as_of'},...]}
          {'shape':'series','points':[[unix_time,value],...],'unit':...}  up to 2,000 points
        Optional in each: 'as_of', 'limited', 'note'. Runs in the UI server process on every poll: open your data read-only and keep it bounded."""
        if not isinstance(name,str) or not _NAME.match(name) or not isinstance(title,str) or not 1<=len(title)<=60 or not callable(read):raise ValueError('plugin_panel')
        self.panels[name]=(title,read)

    def add_retention(self,fn):
        """fn(cfg, office_conn, now) -> one log line. Runs hourly with the built-in rules; delete in bounded batches."""
        if not callable(fn):raise ValueError('plugin_retention')
        self.retention_rules.append(fn)

    def add_deny_hosts(self,fn):
        """fn() -> iterable of host names the egress proxy must refuse, re-read on the host's refresh timer. Raising means unknown: the host parks wakes after its grace period."""
        if not callable(fn):raise ValueError('plugin_deny_hosts')
        self.deny_host_sources.append(fn)

    def add_labels(self,fn):
        """fn(tools) -> dict merged into the labels saved with each wake."""
        if not callable(fn):raise ValueError('plugin_labels')
        self.label_sources.append(fn)

    def set_notifier(self,fn):
        """fn(event: dict) for low-priority host notices, such as a seat filing a request. Rate-limited by the caller; runs off the tool path."""
        if not callable(fn):raise ValueError('plugin_notifier')
        self.notifier=fn

    def set_pager(self,fn):
        """fn(text) pages the operator. Raise ValueError when it is known not sent; any other exception means it may have gone out."""
        if not callable(fn):raise ValueError('plugin_pager')
        self.pager=fn


def load(cfg):
    host=Host(cfg)
    names=cfg.get('plugins',[])
    if not isinstance(names,list) or not all(isinstance(n,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*',n) for n in names):raise ValueError('plugins_config')
    for name in names:
        importlib.import_module(name).register(host)
    return host
