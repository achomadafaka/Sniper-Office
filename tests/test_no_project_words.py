"""This repository holds the generic runtime only. Domain words belong in the project that plugs in."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
# This file is skipped below: it has to spell the words. The project's own name (sniper-office, sniper_office) passes.
WORDS = ['meme' 'coin', 'wal' 'let', 'sol' 'ana', 'poly' 'market', 'pump' 'swap', 'pump' r'\.fun', r'\bmi' r'nts?\b', r'\bco' r'ins?\b',
         r'\btra' r'ders?\b', r'\btra' r'ding\b', r'\bsco' r'uts?\b', r'\bjud' r'ges?\b', r'\bde' r'sk\b', 'near_' 'pass',
         'sni' r'per(?![-_ ]office)']
CASED = [r'\b(?:Al' 'pha|Be' 'ta|Gam' 'ma|Del' 'ta|Eps' 'ilon|Ze' 'ta|Lam' 'bda)\b']    # the source project's crew names
# The desk plug-in is the project that plugs in, shipped beside the runtime: its tool names and data are the domain. Only these paths are exempt; the runtime proper stays guarded.
PLUGIN_PATHS = ('sniper_office/desk/', 'tests/desk/', 'docs/DESK.md', 'deploy/office-paper-filler.service', 'config/paper_filler.example.json', 'deploy/office-desk-feed.service', 'config/desk_feed.example.json', 'config/office_data_sources.example.json', 'deploy/office-j7-listener.service', 'deploy/office-j7-ticks.service', 'config/j7_listener.example.json', 'config/j7_ticks.example.json',
                'config/j7_traders.example.json', 'config/book_policy_copy.example.json', 'config/paper_filler_copy.example.json', 'config/seat_copy_desk.example.json', '.brain/')
PLUGIN_PATHS += ('sniper_office/markets/', 'tests/markets/', 'docs/MARKETS.md', 'deploy/office-markets-', 'config/markets_')    # the markets plug-in, as above
PATTERN = re.compile('(?i:' + '|'.join(WORDS) + ')|' + '|'.join(CASED))


def test_no_domain_or_project_words_in_tracked_or_new_files():
    listed = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard'],
                            capture_output=True, text=True, check=True).stdout.split('\n')
    hits = []
    for name in filter(None, listed):
        path = ROOT / name
        # Skipped: this file, which has to spell the words, and the two extraction records, which have to name the source.
        if not path.is_file() or path == Path(__file__).resolve() or name in ('docs/DESIGN.md', 'docs/STAGE6.md') or name.startswith(PLUGIN_PATHS) or name.startswith('floor/'):
            continue
        if PATTERN.search(name):
            hits.append(name)
        for number, line in enumerate(path.read_text(errors='replace').split('\n'), 1):
            if 'provenance:' in line.lower():      # the one README line that names where the code came from
                continue
            if PATTERN.search(line):
                hits.append('%s:%d: %s' % (name, number, line.strip()[:120]))
    assert not hits, '\n'.join(hits)


def test_the_plugin_exemption_covers_only_the_plugin_paths():
    assert PATTERN.search('sniper_office/desk/paper.py') and not any(n.startswith(PLUGIN_PATHS) for n in ('sniper_office/seat_tools.py', 'tests/test_plugins.py', 'docs/SETUP.md', 'sniper_office/deskx.py'))
