"""Refresh the clock correction sample that clock.corrected_now reads. Stdlib only, no keys.

    python3 -m sniper_office.clock_refresh --out clock_offset.json

offset_s = host_now - real_utc, from the `Date` header of three independent https hosts: 3 rounds, request midpoint,
plus the 0.5 s truncation centre, median written as {"offset_s", "at"}. Fails closed (exit 2, file untouched) on fewer
than 5 good samples or 2 hosts, host medians more than 1.5 s apart, or |offset| > 3600 s. Exit 0 when another
refresher holds the lock. Run it every few minutes: corrected_now treats a sample older than an hour as unknown."""
import argparse
import fcntl
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path


HOSTS = ("https://www.cloudflare.com", "https://www.google.com", "https://example.com")
ROUNDS, ROUND_GAP_S = 3, 0.37
MIN_SAMPLES, MIN_HOSTS, MAX_SPREAD_S, MAX_ABS_OFFSET_S, MAX_RTT_S = 5, 2, 1.5, 3600.0, 3.0


def http_date(host):
    """-> (t0, t1, Date header) around one HEAD request, host clock."""
    req = urllib.request.Request(host, method="HEAD", headers={"User-Agent": "clock-offset-refresh"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=6) as r:
        date = r.headers.get("Date")
    return t0, time.time(), date


def measure(fetch, hosts, sleep):
    per_host = {h: [] for h in hosts}
    for rnd in range(ROUNDS):
        if rnd:
            sleep(ROUND_GAP_S)
        for h in hosts:
            try:
                t0, t1, date = fetch(h)
                real = parsedate_to_datetime(date).timestamp() + 0.5      # Date is truncated: the true instant is in [D, D+1)
                off = (t0 + t1) / 2.0 - real
                if math.isfinite(off) and 0 <= t1 - t0 <= MAX_RTT_S:
                    per_host[h].append(off)
            except Exception:
                continue
    return {h: v for h, v in per_host.items() if v}


def refresh(out, lock_path, *, fetch=http_date, hosts=HOSTS, host_now=time.time, sleep=time.sleep):
    os.makedirs(os.path.dirname(str(lock_path)) or ".", exist_ok=True)
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("clock_refresh status=skipped reason=lock_held")
            return 0
        per_host = measure(fetch, hosts, sleep)
        samples = [x for v in per_host.values() for x in v]
        if len(samples) < MIN_SAMPLES or len(per_host) < MIN_HOSTS:
            print(f"clock_refresh status=refused reason=too_few_samples n={len(samples)} hosts={len(per_host)}", file=sys.stderr)
            return 2
        meds = [statistics.median(v) for v in per_host.values()]
        spread = max(meds) - min(meds)
        offset = statistics.median(samples)
        if spread > MAX_SPREAD_S or not math.isfinite(offset) or abs(offset) > MAX_ABS_OFFSET_S:
            print(f"clock_refresh status=refused reason=bad_measurement offset={offset:.3f} spread={spread:.3f}", file=sys.stderr)
            return 2
        tmp = Path(str(out) + ".tmp")
        tmp.write_text(json.dumps({"offset_s": offset, "at": host_now()}))
        os.replace(tmp, out)   # readers never see a half-written file
        print(f"clock_refresh status=written offset_s={offset:.4f} spread_s={spread:.3f} n={len(samples)} hosts={len(per_host)}")
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--lock", help="default: <out>.lock")
    a = ap.parse_args(argv)
    return refresh(a.out, a.lock or a.out + ".lock")


if __name__ == "__main__":
    sys.exit(main())
