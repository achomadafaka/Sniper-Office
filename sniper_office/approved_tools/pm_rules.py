"""Page the stored provider's market resolution rules at a fixed snapshot."""

from ._read import db_read

NAME = 'pm_rules'
PAGE_CHARS = 1000
STORAGE_CAPS = (1500, 32768)


@db_read('markets_db')
def read(conn, now, condition_id, offset=0, fetched_at=None):
    if (not isinstance(condition_id, str) or not 1 <= len(condition_id) <= 128
            or type(offset) is not int or offset < 0 or (offset and fetched_at is None)):
        return dict(state='refused', reason='bad_arguments')
    row = conn.execute('SELECT rules, source, fetched_at FROM mk_pm_markets WHERE condition_id=?',
                       (condition_id,)).fetchone()
    if not row or row['fetched_at'] > now or not row['rules']:
        return dict(state='unknown', reason='stored_rules_unavailable')
    if fetched_at is not None and fetched_at != row['fetched_at']:
        return dict(state='unknown', reason='snapshot_changed', fetched_at=row['fetched_at'])
    rules = row['rules']
    if offset >= len(rules):
        return dict(state='refused', reason='offset_out_of_range')
    end = min(offset + PAGE_CHARS, len(rules))
    possibly_truncated = len(rules) in STORAGE_CAPS
    return dict(state='partial' if possibly_truncated else 'known', condition_id=condition_id,
                source=row['source'], fetched_at=row['fetched_at'], stored_chars=len(rules),
                provider_text_possibly_truncated=possibly_truncated, offset=offset,
                next_offset=end if end < len(rules) else None, rules=rules[offset:end])


def register(host):
    host.add_tool(NAME, 'Read one page of stored market resolution rules. For later pages use next_offset and fetched_at from the previous page.',
                  dict(condition_id={'type': 'string'}, offset={'type': 'integer'}, fetched_at={'type': 'number'}),
                  ['condition_id'], read)
