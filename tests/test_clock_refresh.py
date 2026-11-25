import json

from sniper_office import clock, clock_refresh as R

DATE = 'Wed, 25 Nov 2026 14:00:00 GMT'
REAL = 1795615200.0   # DATE as a timestamp


def fetch_at(host_t):
    return lambda host: (host_t, host_t, DATE)


def test_writes_a_sample_corrected_now_accepts(tmp_path):
    out = tmp_path / 'clock.json'
    assert R.refresh(str(out), str(tmp_path / 'lock'), fetch=fetch_at(REAL + 10.5), sleep=lambda s: None, host_now=lambda: REAL + 10.5) == 0
    assert abs(json.loads(out.read_text())['offset_s'] - 10.0) < 1e-6
    assert abs(clock.corrected_now(out, host_now=REAL + 20.5) - (REAL + 10.5)) < 1e-6


def test_too_few_hosts_leaves_the_file_untouched(tmp_path):
    out = tmp_path / 'clock.json'
    assert R.refresh(str(out), str(tmp_path / 'lock'), fetch=fetch_at(REAL), hosts=('https://a.example',), sleep=lambda s: None) == 2
    assert not out.exists()
