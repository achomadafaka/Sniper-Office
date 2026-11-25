from pathlib import Path

from sniper_office import config

# Module-level tables are process-wide, so the example config is applied once for the whole run.
config.load(Path(__file__).resolve().parents[1] / 'config' / 'office.example.json')
