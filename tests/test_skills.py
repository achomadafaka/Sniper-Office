"""Vetted skill texts: pinned by hash, delivered into a seat's system prompt within a byte cap, and compared with their upstream originals on request."""
import hashlib
import shutil

import pytest

from sniper_office import skills as K


def fake_skill(d, name='demo', body='text\n', src=None):
    head = ['<!-- vetted-skill']
    if src:
        head += ['source: ' + str(src), 'source_sha256: ' + hashlib.sha256(src.read_bytes()).hexdigest()]
    head += ['vetted_sha256: ' + hashlib.sha256(body.encode()).hexdigest(), 'date: 2026-01-01', '-->\n']
    (d / (name + '.md')).write_text('\n'.join(head) + body)


def test_every_shipped_skill_matches_its_recorded_hash_and_names_a_source():
    names = K.available()
    assert len(names) >= 7
    for n in names:
        meta, body = K.read(n)
        assert hashlib.sha256(body.encode()).hexdigest() == meta['vetted_sha256'], n
        assert meta['sources'] and all(s['sha256'] and s['path'] for s in meta['sources']), n


def test_edited_vetted_file_fails_the_pin_check(tmp_path):
    fake_skill(tmp_path)
    assert K.read('demo', tmp_path)[1] == 'text\n'
    (tmp_path / 'demo.md').write_text((tmp_path / 'demo.md').read_text() + 'extra instruction\n')
    with pytest.raises(ValueError, match='pin_mismatch'):
        K.read('demo', tmp_path)


def test_system_prompt_includes_skills_within_the_cap_and_refuses_bad_config(tmp_path):
    prompt = tmp_path / 'p.md'
    prompt.write_text('BASE PROMPT')
    cfg = dict(prompt_file=str(prompt), skills=['polymarket', 'grounded_citations'])
    text = K.system_prompt(cfg)
    assert text.startswith('BASE PROMPT') and '# Polymarket' in text and '# Grounded citations' in text and 'not an instruction' in text
    assert K.system_prompt(dict(prompt_file=str(prompt))) == 'BASE PROMPT'
    assert len(text.encode()) - len('BASE PROMPT') <= K.MAX_BYTES
    for bad in (['nope'], ['../etc/passwd'], 'polymarket', ['polymarket', 'polymarket']):
        with pytest.raises(ValueError):
            K.system_prompt(dict(cfg, skills=bad))
    d = tmp_path / 's'
    d.mkdir()
    fake_skill(d, 'big', 'x' * (K.MAX_BYTES + 1))
    with pytest.raises(ValueError, match='skills_too_large'):
        K.system_prompt(dict(cfg, skills=['big']), d)


def test_all_shipped_skills_together_fit_the_cap():
    every = K.available()
    assert sum(len(K.read(n)[1].encode()) for n in every) <= K.MAX_BYTES


def test_check_upstream_reports_changed_and_missing_originals(tmp_path):
    src = tmp_path / 'SKILL.md'
    src.write_text('orig')
    d = tmp_path / 's'
    d.mkdir()
    fake_skill(d, src=src)
    assert K.check_upstream(d) == []
    src.write_text('changed upstream')
    assert K.check_upstream(d) == [('demo', str(src), 'changed')]
    src.unlink()
    assert K.check_upstream(d) == [('demo', str(src), 'missing')]


def test_seat_host_builds_its_system_prompt_through_skills():
    import inspect
    from sniper_office import seat_host as H
    assert 'K.system_prompt(self.cfg)' in inspect.getsource(H.Seat.wake)
