# Stand-alone UI: design note

Status: 5a to 5c are built (`views`, `ui`, `ui_static/`): Overview, Seat and Mail, and the two write actions. The read models were written
fresh against the package's tables, not moved from the source. Mail is a list, not a table, after a phone-width check. 5d (requests, budget, evaluation, health) and 5e (project panels) are built too. 5f, the illustrated floor, is not.
Stage 5 of `docs/DESIGN.md`. Written 2026-11-25.
Source read for this note, at source commit `73f57c051`: the web routes file for the existing page (223 lines), its
template, the feed and floor read models, and three of its UI design documents (live floor, virtual office,
readability).

## 1. What it is for

One person runs a handful of seats. They need to see, without opening a database: who is awake, who is parked and
why, what each seat did in its last wake, what the seats are saying to each other and to them, what the seats are
asking for, and what it is costing. And they need to do three things: write to a seat, clear a park, answer a
request.

Today that page exists only inside the source project's dashboard. It cannot be used by another project, and half
of it shows that project's own data.

## 2. What the existing page is, and what to keep

| Fact about the existing page | Consequence here |
|---|---|
| A blueprint inside the project's web server, sharing its login and its process. | The new UI is its own small server in this package. No shared process with any project. |
| 17 script and style files, about 5,600 lines, no build step, polling every five seconds while visible. | Keep: plain scripts, no build step, same polling rule. |
| Rooms and seat positions are fixed by seat name in the scripts. A seat with an unknown name falls back to a list. | Layout is driven by the roster. Nothing is keyed on a role name. |
| About half the views are the project's own (its books, charts, research feed, a network graph). | Those stay in the project. This UI offers panels a project can fill (section 6). |
| A written truth contract: every read is a bounded tail with a truncation flag; unknown is never shown as zero; every value carries its time; seat text is rendered as text, never as markup; no execution controls. | Keep all of it. It is the best part of the existing page. |
| The project's own readability review found the illustrated floor took the whole first screen, text was 10 to 13 px, one table was 1,675 px wide in a 1,092 px column, and every panel competed on one long page. It recommended a plain workspace with sections, the floor kept as one section. | Start from the workspace. The illustrated floor is optional and later (section 8, 5f). |
| Operator actions already exist on the server side: `seat_events.operator_dm`, `seat_host.clear_park`. | The UI calls these. It adds no new authority. |

## 3. What the package already records

Everything below is in tables this package owns, so every view in section 5 works for any project with no plug-in.

| Data | Where |
|---|---|
| Seats: role, epoch, generation, incarnation, sealed or not | `office_v2_roster` |
| Per-seat timeline: wake start and end, thoughts, tool calls and results, page visits, requests, parks, session swaps, workspace file list, notebook tail | `office_v2_events` |
| Mail between seats and with the operator, read state, web-taint flag on the sender | `office_dm_events`, `office_dm_pending` |
| Tasks a seat set for itself or another | `office_task_heads` |
| Tickets to humans and their history | `tickets` |
| Wakes, tokens per wake, account usage windows, park clears | quota database |
| One-shot turns and model calls (stage 3 path) | `turn_runs`, `model_calls`, `handoff_events` |
| Epochs, trial registry, released verdicts | `epochs`, `trial_registry`, `sealed_views` |
| Supervisor lease and heartbeat | `supervisor_lease`, `heartbeats` |

Not recorded anywhere, so the UI cannot show it: live progress inside a running wake (events are written as they
happen, but a model's partial output is not), and cost in money (subscriptions are flat; only tokens are known).

## 4. Shape

```
browser  --HTTP, localhost-->  ui server (this package, one process, standard library)
                                  |  read-only SQLite connections, one bounded read per request
                                  |  two write actions through existing functions
                                  v
                          store database, quota database
```

- **Server:** `python3 -m sniper_office.ui --config ui.json`. Standard library `http.server`; no web framework,
  so the package keeps its no-dependency core. One JSON endpoint per view, plus the static files.
- **Reads:** each request opens the databases with `mode=ro`, runs under a one-second deadline, reads a capped tail
  and says whether it was cut. The read models (`read_feed`, the seat summary, the request list) come from the
  source, about 450 lines, moved into a `views` module with their tests.
- **Client:** one page, plain scripts and one stylesheet, served from the package. Polls every five seconds while
  the tab is visible, one request in flight, manual refresh always available.
- **No outside requests:** no fonts, scripts or images from other hosts. A content security policy of
  `default-src 'self'` enforces it.

## 5. Views

| View | Shows | Source |
|---|---|---|
| Overview | One row per seat: state (awake, asleep until, parked and the reason), last wake's end and outcome, wakes and tokens today, unread mail, open requests. A banner for anything that needs the operator: a park, a page that did not send, an auth failure, a stale supervisor heartbeat. | roster, events, quota, mail |
| Seat | The seat's timeline, newest first, filterable by kind. Each tool call paired with its result. Workspace file list and the end of the notebook. Its mail. Its budget numbers. | events, mail, quota |
| Mail | Threads between seats and with the operator. A compose box for operator mail to one seat. | mail |
| Requests | What seats asked for (`request` events) and tickets with their state and history. | events, tickets |
| Budget | Wakes per hour and day per seat, tokens per wake, the account's usage windows and when each resets, which seat polls the account. | quota |
| Evaluation | Open epoch and its kind, the trial registry in order, each released verdict with its numbers and reasons. | epochs, trial registry, sealed views |
| Health | Lease holder and age, supervisor heartbeat, turn states, hash-chain check of the append-only tables on demand. | lease, heartbeats, turns, store |
| Project panels | Whatever the project registers (section 6). | plug-in |

Every list states its coverage ("last 64 events for this seat") and every number its time. A value the server
could not read is shown as unknown with the reason, never as blank or zero.

## 6. Project panels

A project adds its own data without shipping any script: `host.add_panel(name, title, read)`. `read(now)` returns
one of three declared shapes, which the UI renders itself.

| Shape | Fields | Rendered as |
|---|---|---|
| `table` | `columns`, `rows` (capped at 200), `limited`, `as_of` | Sortable table |
| `facts` | `items`: label, value, unit, `as_of`, state | Key and value list |
| `series` | `points`: time and value (capped at 2,000), `unit` | Line chart drawn on a canvas, no chart library |

All panel text is rendered as text. A panel that raises or returns another shape shows as unavailable; it cannot
break the page. A project that needs more than these three shapes keeps that view in its own application, as the
source project will for its books and charts.

## 7. Safety

| Risk | Rule |
|---|---|
| Seat-written text is untrusted, and some of it came from web pages. | Rendered with `textContent` only. No markup, no link made from seat text, no markdown. Tested with script-bearing fixtures. |
| The page can act (write mail, clear a park). | Server binds to `127.0.0.1` by default. Every write needs a token from the config file sent in a header, a JSON content type and a same-origin check. Size caps as in the existing route. |
| Someone wants it from another machine. | Their own tunnel or reverse proxy in front. The server gets no login system of its own. |
| A slow or huge read stalls the store for the seats. | Read-only connections, a deadline, capped tails, no table scans; the rules of the existing truth contract. |
| Private evaluation rows. | The server never opens the evaluation file. It shows released aggregates only. |
| The UI as a way to grant authority. | It calls `operator_dm` and `clear_park` and nothing else. It cannot change a role, a tool, a prompt or a budget. |

## 8. Staged plan

| Stage | Content | Rough size |
|---|---|---|
| 5a | `views` module: feed, seat summary, mail, requests, budget, evaluation and health read models, with the source's tests where they exist. | 450 lines moved, 250 new, tests |
| 5b | Server and the Overview, Seat and Mail views. | 350 lines of Python, 900 of script and style |
| 5c | Write actions: operator mail, clear park. Token, origin and size checks. | 150 lines, tests |
| 5d | Requests, Budget, Evaluation and Health views. | 500 lines of script |
| 5e | Project panels. | 150 lines of Python, 250 of script |
| 5f | Optional illustrated floor: rooms from a config file that maps roles to rooms, seats placed from the roster. | Design first |

Smallest useful version: 5a to 5c. It answers "what are my seats doing, and let me write to one".

Verification per stage: read-model tests on fixture databases; a browser run at 1440 px and 390 px with real fixture
data, including a seat with a role the UI has never heard of, an empty store, a store it cannot open, and
script-bearing seat text.

## 9. Decisions for the user

| # | Decision | Recommendation |
|---|---|---|
| 1 | Server | Standard library `http.server` inside this package. No web framework dependency. |
| 2 | Client | Plain scripts, no build step, as the existing page. |
| 3 | First layout | A readable workspace with the views of section 5. The illustrated floor is 5f, optional, and its rooms come from config. |
| 4 | Write actions in the first version | Operator mail and clear park. Ticket transitions and anything else wait. |
| 5 | Access | Localhost only, a token for writes. Remote access is your own tunnel in front. |
| 6 | Project data | Declarative panels in three shapes. No project scripts in this UI. |
| 7 | The existing page in the source project | Leave it until stage 6. Then its seat views are replaced by this UI and its own-data views stay in that project. |
| 8 | Look and feel | Needs your steer before 5b: plain and dense like a terminal dashboard, or closer to the existing page's styled rooms. The recommendation is plain and dense, dark and light, 14 px minimum text. |
