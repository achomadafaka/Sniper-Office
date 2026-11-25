# Office crew

Interactive Claude Code / Codex sessions and the operator can talk to seats by role, through [crewlink](https://example.invalid/local) (a local mailbox and wake bridge, unchanged).
Seats stay sandboxed and keep using `mail_send` / `mail_read`; `sniper_office.crew_bridge` runs on the host and moves messages both ways.

| Direction | What happens |
|---|---|
| crew -> seat | `office-crew send --from operator --to reviewer --message-file note.txt` becomes Office mail to every unsealed seat with that role and wakes it (pending mail is a wake reason). The seat reads it as `crew:operator_cli`, text opening `CREW operator_cli (crew, not the operator)`. |
| seat -> crew | Every seat mail (to a seat, or `to=operator`) is copied into the crew mailbox from the sender's rostered role. `office-crew read --to operator` (or `--to head`) shows it. Text is marked as seat-written data. |

Rules the bridge enforces:

- One message, one delivery. Crew to seat: the Office message id derives from the crewlink id and the seat, and the crewlink row's `delivery_status` becomes `office_delivered` (or `office_refused` with `error`). Seat to crew: the crew row id is `office:<message id>`, and a durable cursor (`crew_bridge_cursor` in the Office db) records progress. History from before the bridge first started is not copied.
- The sender is asserted by the host. Seat mail takes its role from the Office roster (the socket-bound identity). A crew message that names a seat role as its sender is refused. crewlink does not authenticate `--from`, so crew mail is peer data: `operator` is mapped to `operator_cli`, never to the operator. Only the Office page's authenticated route is the operator.
- Add `operator_cli` (and any other non-seat crew role you use) to `crew_roles` in the Office config, so the page labels that mail as crew.
- `office-crew send` to a seat role prints `delivery_failed` (crewlink finds no session behind the role). The message is stored; the bridge flips the row to `office_delivered` within `poll_s` seconds.
- The bridge opens crewlink's `mailbox.sqlite3` (table `messages`) directly. If crewlink changes that schema, `crew_bridge.MAILBOX_DDL` and the two queries change with it.

## Install

1. Crew config: copy `run/config/crewlink/sniper-office.json` to `~/.config/crewlink/sniper-office.json` (crewlink keys, plus an `office` block: `db`, `seat_roles`, `poll_s`).
2. Command: `python3 ~/Desktop/crewlink/crewlink/install.py --config ~/.config/crewlink/sniper-office.json` (writes `~/.local/bin/office-crew`; needs a Python with `websockets`).
3. crewlink's own bridge (for interactive session delivery): copy `~/Desktop/crewlink/examples/bridge.service` to `~/.config/systemd/user/office-crew-bridge.service`, replace `<command>` with `office-crew`, then `systemctl --user daemon-reload && systemctl --user enable --now office-crew-bridge.service`.
4. Hooks, one per client, as in the crewlink README: `office-crew session-start --client claude` in `~/.claude/settings.json`; `office-crew session-start --client codex` in `~/.codex/hooks.json`.
5. Publish this release (the units run `/srv/sniper-office/release`), then copy `deploy/office-crew-host.service` to `~/.config/systemd/user/` and `systemctl --user enable --now office-crew-host`.
6. Check: `python3 -m sniper_office.crew_bridge --config ~/.config/crewlink/sniper-office.json --once`, then `office-crew send --from operator --to reviewer --message-file note.txt` and `office-crew read --to reviewer`.
