# Stage 6: moving the source project onto this package

Status: plan only. Nothing in the source project has been changed. Written 2026-11-25 against source commit
`62db7162a` and package commit `138674a`. Like `docs/DESIGN.md`, this file names the private source project on
purpose; the project-word test skips it. Remove it before this repository is made public.

## 1. Facts that shape the plan

| Fact | Where seen | Consequence |
|---|---|---|
| The source project runs from release trees: snapshots of its own git tree at one commit (`.runtime/office/releases/<sha>`, `/srv/sniper-office/release`). | `.runtime/office/current` resolves to a release folder named by a 40-character commit; `seat_publish.publish_release` uses `git archive`. | Code the Office needs must be inside the source project's git tree. A git submodule is not included by `git archive`. |
| "NEVER install into the shared pyenv." | `deploy/office/requirements.txt`, line 1. The same interpreter runs the funded process. | No `pip install` of this package into that interpreter. |
| `sniper_office` is not importable there today. | `python3 -c "import sniper_office"` fails. | Confirms the two rows above. |
| Other sessions are editing Office files in the shared checkout right now. | Three `config/office/seat_*_v2.json` files are modified and uncommitted; the head moved three times during this work. | Stage 6 runs in its own git worktree on its own branch, never in the shared checkout, and is merged by a human. |
| 26 package modules are byte-identical to the source's after renaming the import. 26 differ. 11 exist only in the package. | Comparison script, this session. | Section 3. |
| The Office has been paused since 2026-11-01. | Unit states and cron markers. | No running seat is affected by a merge. A resume after the merge is the real test. |

## 2. How the source project gets the package

Recommended: **a vendored copy at the top of the source tree**, `sniper_office/`, taken from one pinned commit of this
repository.

- It is inside the git tree, so every release snapshot contains it and the sandbox finds it at `/opt/release/sniper_office`,
  which is the path this package's seat host already uses.
- Nothing is installed into the shared interpreter.
- One script copies it from a pinned commit and writes the commit and a tree hash to `sniper_office/PIN`. One test
  recomputes the hash, so a hand edit of the vendored copy fails the source project's test run.
- Cost: the code exists in two repositories. The rule that keeps that honest: the vendored copy is never edited; changes
  go to this repository first and arrive by re-running the script.

Rejected: git submodule (not in `git archive`), `pip install` into the shared interpreter (forbidden there), a path entry
pointing at a sibling folder (release snapshots would not contain the code).

## 3. Modules

Shim technique for every switched module, so that monkeypatching, private names and exception identity keep working:

```python
# sniper/office/clock.py
import sys
import sniper_office.clock as _module
sys.modules[__name__] = _module
```

`sniper.office.clock` and `sniper_office.clock` are then the same module object.

### Slice 1: identical, and every module they import is identical too (18)

`clock`, `netguard`, `search_broker`, `egress_proxy`, `launcher_diagnostics`, `library_audit`, `model_diagnostics`,
`launcher_client`, `claude_seat_auth`, `claude_compat`, `child_launcher`, `launcher_bindings`, `model_auth`,
`supervisor_client`, `shifts`, and the three sandbox scripts `seat_forward`, `seat_browse`, `seat_child`.

The three sandbox scripts are run by file path inside the sandbox. They switch only once the vendored copy is in the
published release, which slice 1 provides; the seat host's path constants stay as they are until slice 4.

Gate: the source project's own tests for these modules pass unchanged, plus its pm_desk tests that import
`egress_proxy`, `netguard` and `clock`.

### Slice 2: identical text, but they import a changed module (8)

`broker`, `work`, `quota_measurement`, `model_proxy`, `scheduler`, `host_auth`, `tools/browse`, `tools/__init__`.

They can only switch together with what they import: `store`, `capabilities`, `runtime_observations`, `models`,
`codex_auth`. So slice 2 is really "the durable core".

### Slice 2 and 3: changed modules, and what the source project must supply

| Module | Change in the package | What the source project adds |
|---|---|---|
| `store` | Seven project tables and six project indexes left the default schema. `trial_registry` is built in again. | `tables` in its config: `opportunity_log`, `opportunity_gaps`, `opportunity_coverage`, `outcome_captures`, `fee_attestations`, `cost_allocations`, and their columns and indexes. Existing database files open unchanged. |
| `capabilities` | Role table is empty until configured. | `roles` and `cut_free_tools` in its config, equal to today's literals. |
| `memory` | Roles, quarantined roles and source keys are config. | `memory` in its config; `source_keys` must be `coin`, `creator`, `minute` to match stored entries. |
| `tickets` | Crew ids and approver role are config; status `codex_queued` is `crew_queued`. | `tickets` in its config. One reader of the old status string to update or map. |
| `supervisor` | Role alias and the deferral reason are config. | `role_aliases: {"Desk ops": "ops"}`, `deferrable_reasons: ["desk_judge_work_unknown"]`. |
| `runtime_observations` | The order hand-off left the buffer; `hooks` instead. `memo_to_judge` is a config mapping. | One hook function holding the removed lines; `handoff_kinds`. |
| `loops`, `memory_store` | Docstrings only. | Nothing. |
| `models` | No built-in transports. | Two lines at start-up that set `models.TRANSPORTS` to its existing adapters. |
| `codex_auth`, `model_child` | Import `codex_cli` instead of two of the project's scripts. | Nothing; behaviour is the same code. Its other callers of those scripts are untouched. |
| `org` | Pod template and limits are generic. | Its `default_org.json` and `limits.json` rewritten in the new shape, with new pins. |
| `roster` | Pilot seed removed; evaluated-epoch gate is now live and takes the launcher's approvers from code. | Keeps `seed_pilot_org` as its own function. Sets `launcher_policy.configure(...)`. |
| `nominee`, `launcher_policy` | Generic; project rules through `check`. | Its present rules (cost rule, pods, book policy, execution bound, calibration) as one `check` function. |
| `launcher_runtime` | Host layout set in code. | One `configure(...)` call with today's three paths. |
| `seat_sandbox`, `seat_events`, `seat_publish` | Paths, user, crew roles configurable; publish always adds the package. | `host` and `crew_roles` in its config, equal to today's values. Its paper-watcher unit added to `SEAT_UNITS`. |
| `seat_retention` | Project cleanup rules removed. | Its market, chain, watch, book-snapshot and label rules as `add_retention` functions. |
| `seat_db` | Rewritten around a spec file; lost notes, day files and text markers. | Keeps its own `seat_db` and registers its `db_read` as a plug-in tool, replacing the built-in. |
| `seat_tools` | About 30 project tools removed. | A plug-in module registering them: the market, chain, network, watch, feed, sheet, book and paper tools, and the two brain tools. |
| `seat_host` | Project wake reasons, the ledger check before rotation and paging are hooks; the guard reasons are renamed. | `add_wake_source` for ticket, position and post-mortem wakes; `add_busy_check`; a pager; `heartbeat` and `heartbeat_ok_key: "auth_ok"` in config; a reader that accepts both the old `runner_*` and new `guard_*` park reasons. |

### Stay as they are (about 120 modules)

Everything listed in `docs/DESIGN.md` section 1d: the paper book, the experiment, the research tools, the views, the
one-off transitions. They keep importing `sniper.office.<name>` and get the package's objects through the shims.

## 4. Order and gates

| Slice | Content | Gate before the next slice |
|---|---|---|
| 0 | Worktree and branch outside the shared checkout. Vendoring script, `PIN`, the hash test. | Source project's full test run is unchanged. |
| 1 | The 18 shims of section 3. | Its Office and pm_desk tests pass. |
| 2 | Its config file for the package, loaded once at import of `sniper.office`. Durable-core shims. | Its Office tests pass. Its existing `office.db` opens and `verify_chain` passes on every append-only table. |
| 3 | Launcher, auth, org, roster shims; org files rewritten. | Its launcher and roster tests pass. |
| 4 | The plug-in module; seat host, tool server, publish, retention shims; entry script reduced to a config and plug-in. | Its seat tests pass. One real wake from a published release on this host with the plug-in loaded, on a test bridge. |
| 5 | Delete the shimmed module bodies' tests that now live in this repository; keep its own. Operator page replaces its seat views. | Its full test run passes. |

Each slice is one commit on the branch. A slice whose gate fails is fixed or reverted before the next starts.
Rollback of the whole stage is not merging the branch.

## 5. What is not known yet

- Whether the source project's roughly 286 Office test files monkeypatch things the shims cannot carry. Slice 1 will show.
- Whether anything reads the old park-reason strings or the old ticket status besides the places found by search.
- How long its full test run takes in a worktree and whether it needs data files that are not in git.
