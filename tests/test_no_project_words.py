"""This repository holds the generic runtime only. Domain words belong in the project that plugs in."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# This file is skipped below: it has to spell the words. The project's own name (sniper-office, sniper_office) passes.
WORDS = ['meme' 'coin', 'wal' 'let', 'sol' 'ana', 'poly' 'market', 'pump' 'swap', 'pump' r'\.fun', r'\bmi' r'nts?\b', r'\bco' r'ins?\b',
         r'\btra' r'ders?\b', r'\btra' r'ding\b', r'\bsco' r'uts?\b', r'\bjud' r'ges?\b', r'\bde' r'sk\b', 'near_' 'pass',
         'sni' r'per(?![-_ ]office)']
CASED = [r'\b(?:Al' 'pha|Be' 'ta|Gam' 'ma|Del' 'ta|Eps' 'ilon|Ze' 'ta|Lam' 'bda|Co' r'dex)\b']    # the source project's crew names
PATTERN = re.compile('(?i:' + '|'.join(WORDS) + ')|' + '|'.join(CASED))


def test_no_domain_or_project_words_in_tracked_or_new_files():
    listed = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard'],
                            capture_output=True, text=True, check=True).stdout.split('\n')
    hits = []
    for name in filter(None, listed):
        path = ROOT / name
        if not path.is_file() or path == Path(__file__).resolve():
            continue
        if PATTERN.search(name):
            hits.append(name)
        for number, line in enumerate(path.read_text(errors='replace').split('\n'), 1):
            if 'provenance:' in line.lower():      # the one README line that names where the code came from
                continue
            if PATTERN.search(line):
                hits.append('%s:%d: %s' % (name, number, line.strip()[:120]))
    assert not hits, '\n'.join(hits)
