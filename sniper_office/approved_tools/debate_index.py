"""Index recent debate IDs in the current markets ledger."""

from ._read import db_read

NAME = 'debate_index'


@db_read('markets_ledger_db')
def read(conn, now, since_s=86400, limit=20):
    if type(since_s) is not int or not 1 <= since_s <= 604800 or type(limit) is not int or not 1 <= limit <= 40:
        return dict(state='refused', reason='bad_arguments')
    since = now - since_s
    rows = conn.execute("""
        SELECT news_id, COUNT(*) AS turns, MAX(at) AS last_at
        FROM bk_debates WHERE at >= ? AND at <= ?
        GROUP BY news_id ORDER BY last_at DESC, news_id LIMIT ?
    """, (since, now, limit + 1)).fetchall()
    return dict(state='known', scope='current_markets_ledger', as_of=now, since=since,
                limited=len(rows) > limit, items=[dict(row) for row in rows[:limit]])


def register(host):
    host.add_tool(NAME, 'Find recent debated news IDs in the current ledger, then read turns with story_debates.',
                  dict(since_s={'type': 'integer'}, limit={'type': 'integer'}), [], read)
