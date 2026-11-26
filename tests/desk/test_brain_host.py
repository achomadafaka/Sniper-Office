"""Host-side brain: handovers, closed-order lessons and the boot text reach the brain through its CLI (a fake package stands in for it here)."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_desk_plugin import NOW  # noqa: E402
from test_wakes import FakeSeat, POL, book  # noqa: E402

from sniper_office import plugins as PL  # noqa: E402
from sniper_office.desk import brain as BR  # noqa: E402


def fake_brain(tmp, down=False):
    pkg = tmp / 'pkg' / 'brain'
    pkg.mkdir(parents=True)
    log = tmp / 'calls.jsonl'
    (pkg / '__init__.py').write_text('')
    (pkg / '__main__.py').write_text(
        'import json,sys\nopen(%r,"a").write(json.dumps(sys.argv[1:])+"\\n")\nop=sys.argv[1]\n'
        'if %r: sys.exit(4)\n'
        'print({"episode":"episode 5392ba90-ec5e-4b72-9915-5f896cb59616 (1 part(s))","write":"written x","boot":"=== BRAIN ===\\n"+"x"*20000}[op])\n' % (str(log), down))
    return dict(brain_dir=str(tmp), brain_package=str(tmp / 'pkg'), brain_python=sys.executable), log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_a_handover_is_stored_as_an_episode_only(tmp_path):
    cfg, log = fake_brain(tmp_path)
    s = FakeSeat(tmp_path, role='scout', **cfg)
    BR.on_handover(s, 'Working on mint X. Next: check holders.')
    ep, = calls(log)
    assert ep[:6] == ['episode', '--role', 'scout', '--kind', 'handover', '--'] and ep[6] == 'Working on mint X. Next: check holders.'


def test_an_unconfigured_or_down_brain_never_raises_or_stores(tmp_path):
    s = FakeSeat(tmp_path, role='scout')
    BR.on_handover(s, 'n')   # no brain keys: nothing to do
    cfg, log = fake_brain(tmp_path, down=True)
    BR.on_handover(FakeSeat(tmp_path, role='scout', **cfg), 'n')
    assert [c[0] for c in calls(log)] == ['episode'] and BR.boot(FakeSeat(tmp_path, role='scout', **cfg)) is None


def test_boot_is_bounded_and_labelled_as_data(tmp_path):
    cfg, log = fake_brain(tmp_path)
    text = BR.boot(FakeSeat(tmp_path, role='scout', brain_boot_bytes=500, **cfg))
    assert calls(log) == [['boot', 'scout']] and len(text) < 900 and text.startswith('The Office brain') and 'not an instruction' in text and 'cut at 500 bytes' in text


def test_a_closed_order_leaves_one_lesson_episode_even_after_a_restart(tmp_path):
    cfg, log = fake_brain(tmp_path)
    ledger = book(tmp_path, [('c1', 'ep', 'ep', 'MINTX', 'p', 'closed', NOW - 950, NOW - 900, NOW - 100, 25., 27.5, 'trail', 100., 0.25, 30., POL, json.dumps(dict(price_usd=0.31)))])
    s = FakeSeat(tmp_path, ledger_db=ledger, **cfg)
    BR.lessons(s)
    BR.lessons(s)
    s2 = FakeSeat.__new__(FakeSeat)
    s2.__dict__.update(s.__dict__, office=sqlite3.connect(tmp_path / 'o.db', isolation_level=None))
    BR.lessons(s2)
    got = calls(log)
    assert [g[0] for g in got] == ['episode', 'write'] and got[0][:6] == ['episode', '--role', 'judge', '--kind', 'report', '--'] and 'c1' in got[0][6] and 'trail' in got[0][6] and 'MINTX' in got[0][6]


def test_a_lesson_is_retried_when_the_brain_was_down(tmp_path):
    cfg, log = fake_brain(tmp_path, down=True)
    ledger = book(tmp_path, [('c1', 'ep', 'ep', 'MINTX', 'p', 'closed', NOW - 950, NOW - 900, NOW - 100, 25., 27.5, 'trail', 100., 0.25, 30., POL, json.dumps(dict(price_usd=0.31)))])
    s = FakeSeat(tmp_path, ledger_db=ledger, **cfg)
    BR.lessons(s)
    BR.lessons(s)
    assert len(calls(log)) == 2   # not marked seen


def test_the_desk_registers_the_brain_hooks(tmp_path):
    host = PL.load(dict(plugins=['sniper_office.desk']))
    assert BR.on_handover in host.handover_hooks and BR.boot in host.boot_sources and BR.lessons in host.tick_hooks
