"""Bounded reads from a fixed database key in trusted seat configuration."""

from contextlib import closing
from functools import wraps
import sqlite3

from ..markets.common import ro


def db_read(config_key):
    def decorate(handler):
        @wraps(handler)
        def call(tools, **args):
            path = tools.cfg.get(config_key)
            now = tools.clock()
            if not path or now is None:
                return dict(state='unknown', reason='database_unconfigured' if not path else 'clock_unknown')
            try:
                with closing(ro(path)) as conn:
                    conn.execute('PRAGMA query_only=ON')
                    remaining = 2000

                    def budget():
                        nonlocal remaining
                        remaining -= 1
                        return remaining <= 0

                    conn.set_progress_handler(budget, 1000)
                    return handler(conn, now, **args)
            except sqlite3.Error:
                return dict(state='unknown', reason='database_read_failed')
        return call
    return decorate
