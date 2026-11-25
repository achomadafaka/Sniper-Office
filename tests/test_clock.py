import json
from sniper_office.clock import corrected_now


def test_clock_requires_fresh_numeric_single_sample(tmp_path):
    path = tmp_path / 'clock.json'
    assert corrected_now(path, host_now=120.) is None
    for doc in ({'offset_s': 17., 'at': 100.}, {'offset_s': 0., 'at': 100.}):
        path.write_text(json.dumps(doc))
        assert corrected_now(path, host_now=120.) == 120. - doc['offset_s']
    for doc in ({'offset_s': True, 'at': 100.}, {'offset_s': 0.}, {'offset_s': 0., 'at': -4000.}, {'offset_s': 0., 'at': 121.}):
        path.write_text(json.dumps(doc))
        assert corrected_now(path, host_now=120.) is None


def test_clock_huge_or_insane_offset_fails_closed_without_raising(tmp_path):
    path = tmp_path / 'clock.json'
    for raw in ('{"offset_s": 1' + '0' * 400 + ', "at": 100.0}', '{"offset_s": -1' + '0' * 400 + ', "at": 100.0}',
                '{"offset_s": 1e300, "at": 100.0}', '{"offset_s": 7200.0, "at": 100.0}', '{"offset_s": -7200.0, "at": 100.0}',
                '{"offset_s": 0.0, "at": 1' + '0' * 400 + '}'):
        path.write_text(raw)
        assert corrected_now(path, host_now=120.) is None, raw
    assert corrected_now(path, host_now=10 ** 400) is None
    path.write_text('{"offset_s": 3600.0, "at": 100.0}')
    assert corrected_now(path, host_now=120.) == 120. - 3600.      # the bound is inclusive and a legitimate offset still works
