# Running seats on a host

A seat is one long-running agent session. Its host process runs as you; every model and tool process runs as a
dedicated system user inside `bwrap`, with no network route except two host proxies. This guide sets that up once.

Requirements: Linux with unprivileged user namespaces, `bwrap`, systemd user managers, `sudo`, Python 3.11 or newer,
the `cryptography` package for the host, and network access to PyPI for the publish step.

## 1. Once, as root

```bash
sudo useradd --system --create-home --home-dir /var/lib/sniper-office --shell /usr/sbin/nologin office
```

```bash
sudo loginctl enable-linger office
```

```bash
sudo install -d -o "$USER" -g office -m 2750 /srv/sniper-office
```

Let your user run commands as `office` without a password. Put this in `/etc/sudoers.d/sniper-office` with `visudo -f`,
replacing `me` with your user name:

```
me ALL=(office) NOPASSWD: ALL
```

Your home directory must not be readable by `office`: `chmod 750 ~`, and never add `office` to your group. That file
permission is the wall that still stands if `bwrap` fails.

Other names or paths: set `"host": {"bridge": ..., "home": ..., "user": ...}` in each seat config.

### Without root for the bridge

Only the system user and the passwordless `sudo -u office` need root. The bridge can be any directory outside `/home`
that you own, with an access list instead of a group. This is how the package's own end-to-end run was done:

```bash
mkdir -m 0750 /var/tmp/office-bridge && setfacl -m u:office:rx,d:u:office:rwX,d:m::rwx /var/tmp/office-bridge
```

```bash
sudo -n -u office mkdir -m 0750 /var/tmp/office-home
```

Then `"host": {"bridge": "/var/tmp/office-bridge", "home": "/var/tmp/office-home", "user": "office"}` and the same
`"bridge"` in the seat config. `/var/tmp` is cleared by some systems on a schedule; use a lasting path for real seats.

## 2. Publish

Publishing copies this package, optionally your project's git-tracked files, and a private Python runtime with the
agent SDK and a headless browser into `/srv/sniper-office`. It refuses while a seat is running, and refuses a tree
that contains a key, a `.env` file, a credential file or a database.

```bash
python3 -m sniper_office.seat_publish
```

With your project's code readable by seats (`/opt/release` inside the sandbox):

```bash
python3 -m sniper_office.seat_publish --repo /path/to/project --sha "$(git -C /path/to/project rev-parse HEAD)"
```

## 3. Configure a seat

Copy `config/seat.example.json`, one file per seat. The keys that matter first:

| Key | Meaning |
|---|---|
| `actor` | `ag_` plus 16 hex characters. The seat's identity in every table. |
| `role` | What other seats call it in `mail_send`. |
| `prompt_file` | The seat's standing instructions. |
| `family`, `model`, `sdk_version` | `claude` or `codex`; `sdk_version` must match the published runtime or usage reads unknown and the seat parks. |
| `claude_credentials` | Your subscription credential file. A read-only snapshot of the access token is given to the seat; nothing is refreshed or written back. |
| `account_poller` | Exactly one seat per account sets this true. The others read its measurements. |
| `wakes_per_hour`, `absolute_ceiling`, `daily_limit`, `uncapped` | Budget. A seat over budget or with unknown usage parks; it does not run unmetered. |
| `denylist` | Hosts no seat may reach (see `config/egress_denylist.example.json`). |
| `data_sources` | Tables `db_read` may show (see `config/data_sources.example.json`). Omit to switch the tool off. |
| `plugins` | Your project's modules (see below). |
| `tool_groups` | Which grouped plug-in tools this seat gets. |
| `heartbeat` | Optional. A JSON file another process touches; if it goes stale or says not ok, seats park so they cannot slow that process down. |

Add the seat to the roster once, so mail can find it:

```bash
python3 -c "import sqlite3,time;from sniper_office import seat_events as E;c=sqlite3.connect('/var/lib/myproject/office/office.db',isolation_level=None);E.ensure_schema(c);c.execute('INSERT OR IGNORE INTO office_v2_roster VALUES(?,?,?,?,?,?,?)',('ag_0123456789abcdef','first-run','reviewer',1,1,0,time.time()))"
```

## 4. Start

Copy `deploy/office-seat@.service` to `~/.config/systemd/user/`, set the Python and config paths in it, then:

```bash
systemctl --user enable --now office-seat@reviewer
```

```bash
journalctl --user -u office-seat@reviewer -f
```

`parked:<reason>` in the log means the seat is refusing to wake and says why.

## 5. Your project's plug-in

A plug-in is a Python module with `register(host)`. It is trusted code and runs in the seat host process, outside
the sandbox. `examples/demo_plugin.py` is a complete one.

| Call | Use |
|---|---|
| `host.add_tool(name, description, properties, required, handler, group=None)` | A tool for the seat. `handler(tools, **arguments)` returns text or a JSON-able value. A name equal to a built-in replaces it. |
| `host.add_wake_source(fn)` | A reason to wake: `fn(seat, urgent)` returns `None` or `dict(type=..., prompt=..., urgent=bool)`. |
| `host.add_busy_check(fn)` | `fn(seat)` true while work is in flight that a session rotation must not interrupt. |
| `host.add_deny_hosts(fn)` | Hosts to refuse at the proxy, re-read every 30 s. If it keeps failing, seats park. |
| `host.add_labels(fn)` | Extra facts saved with each wake. |
| `host.add_retention(fn)` | Your own cleanup rule, run hourly. |
| `host.set_notifier(fn)`, `host.set_pager(fn)` | Where low-priority notices and operator pages go. Without a pager, pages stay in the inbox. |

Built-in tools: `shell`, `browse`, `web_search`, `db_read`, `mail_send`, `mail_read`, `sleep`, `request`,
`notify_operator`.

## 6. If a seat parks at once with `claude_auth_expired`

The seat copies the access token from `claude_credentials` and never refreshes it. An expired file means no wake.
Log in again with the `claude` command line tool, which rewrites that file; the seat resumes by itself on its next
tick.

## 7. Check the walls

```bash
OFFICE_WALLS_REQUIRED=1 OFFICE_WALLS_SECRETS=/path/to/a/secret:/path/to/another python3 -m pytest tests/test_walls.py tests/test_browse_security.py
```

These run the real sandbox as the `office` user and fail if it can read a listed file, reach localhost or the
network directly, or write outside its workspace.
