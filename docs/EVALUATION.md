# Evaluation and promotion: design note

Status: design only, nothing built. Stage 4 of `docs/DESIGN.md`. Written 2026-11-25.
Source read for this note: the evaluation modules of the source package at commit `f07f79614` (listed in section 2),
its readiness rules document and sections 4.2 to 4.4 of its design document.

## 1. What this is for

A team of agents makes decisions over a stream of cases. Someone has to answer, honestly, "is this configuration
of the team better than doing the obvious thing, by enough to act on?" The agents, the people who build them and
the person who wants a yes all have ways to bias that answer without meaning to: changing the team mid-run, peeking
at results and stopping when they look good, trying ten configurations and reporting the best, counting only the
cases the team chose to act on, or letting the team learn from the results it is being judged on.

Evaluation in this package is the machinery that closes those doors. Promotion is what a passing result is allowed
to do: file a request to a human. Nothing here ever switches a configuration on by itself.

The package supplies the procedure. The project supplies the meaning: what a case is, what a good outcome is,
what the obvious baseline does.

## 2. What the source does, and which parts are general

| Source module | Lines (approximate for parts of a file) | What it does | General? |
|---|---|---|---|
| `roster.py` (evaluated-epoch functions) | 130 | Freezes a named configuration ("nominee") into an evaluated epoch, numbers the attempt, spends significance budget, appends to the trial registry. Refuses a second open evaluated epoch. | Yes |
| `nominee.py` | 161 | Resolves the nominee once: hashes each named config file, checks the live roster and the memory snapshot against it. | Yes, minus four imports of project rules |
| `launcher_policy.py` | 170 | One-use authority for a freeze or a re-check: proves the config directory is mounted read-only and not writable by the running user; a nonce stops replay. | Yes |
| `memory.py`, `memory_store.py` (already in the package) | 270 | Sealed memory snapshots; quarantined roles read only the snapshot, so lessons learned from evaluation results cannot reach the team being evaluated. | Yes, done |
| `store.py` (already in the package) | | A separate evaluation file for private tables; the supervisor never opens it. | Yes, done |
| `capture.py` (case log part) | 60 | A host process logs every eligible case before any agent sees it, with coverage proofs and recorded gaps. Skipped and never-seen cases stay in the denominator. | Yes |
| `grader.py` (`grade`, `skill`, `grade_epoch`) | 150 | Grades every frozen claim against its outcome under a pinned metric registry; unknown outcomes are censored, not dropped. | Mechanism yes; the outcome resolver is the project's |
| `grader.py` (`export_epoch`, `release_aggregate`) | 200 | Re-verifies the nominee, snapshots, and publishes only approved aggregate fields to the public store. Raw graded rows never leave the evaluation file. | Yes; the field list is the project's |
| `readiness/verdict.py` | 280 | Pure function from an epoch snapshot to `pass`, `fail` or `no_verdict`: clustered one-sided lower bounds on net result and on paired lift over the baseline, both above a minimum effect; replication; positive on enough days; concentration cap; censoring cap; zero-tolerance counters; required proofs. | Shape yes; constants and the cost rule are the project's |
| `readiness/calibration.py` | 160 | Simulates the decision rule on listed data generators to prove the bound keeps its error rate at each attempt number. | Yes, heavy (numpy, scipy) |
| `auditor.py` | 160 | Independent process: verifies hash chains against externally pinned heads, reconciles totals, reads safety counters. | Chain audit and counters yes; reconciliation is the project's |
| `lessons.py`, `training_promote.py` | 100 | Lessons are proposed into memory; promoting shadow lessons into the frozen snapshot is an explicit operator-side act that opens a new epoch. | Yes |
| `null_arm.py`, `null_schedule.py`, `replay_fill.py`, `capture.py` (price path), `outcomes.py`, `eval_orders.py`, `eval_settle.py`, `equity_snapshot.py`, `costs.py`, `eval_report.py`, `call_grader.py` and the two deterministic order-handling modules | about 2,900 | One experiment's rules: its fill model, its books, its baseline's timing, its cost, its report. | No. Stays in the source project as the first plug-in. |

## 3. Vocabulary

| Term | Meaning |
|---|---|
| Case | One thing the team could act on. Logged by the host before any agent sees it. |
| Decision | What the team committed for a case, including "skip". Frozen with the evidence it saw. |
| Outcome | What happened, read by the project's resolver at a fixed horizon. `observed`, `censored` (unknowable) or `pending`. |
| Score | A number per case and arm, in one unit, from the project's scorer. |
| Baseline arm | A fixed rule with no model in it, run on the same cases. Frozen before the first epoch. |
| Epoch | A stretch of time under one unchanged configuration. `discovery` (free to change, never evidence) or `evaluated`. |
| Nominee | The exact configuration under evaluation: file hashes, roster, model ids, memory snapshot id. |
| Trial | One evaluated epoch's entry in the append-only registry: attempt number `k`, hypothesis id, significance spent, status. |
| Look | A pre-registered moment when results may be read. Any other read of evaluated results is refused. |
| Verdict | `pass`, `fail` or `no_verdict`. Underpowered, incomplete or tampered is `no_verdict`, never `fail` and never `pass`. |
| Promotion request | A ticket to a human carrying the sealed verdict. The only effect of a pass. |

## 4. The procedure the package owns

1. **Design is frozen first.** A design file, pinned by hash, states the estimand, minimum effect `delta0`, planning
   effect `delta1`, cluster keys, horizon, minimum clusters, censoring cap, concentration cap, look schedule, stop
   rule, cost rule id and the names of the zero-tolerance counters. Code does not carry defaults for any of them.
2. **Opening an evaluated epoch** takes a hypothesis id and a nominee. The package derives `k` as one more than the
   highest attempt in the registry and the significance for that attempt (`family_alpha / (k * (k + 1))`, so the sum
   over all attempts stays under the family level however many are made). A caller-supplied `k` or alpha that
   disagrees is refused. One evaluated epoch may be open at a time.
3. **The nominee is resolved once**, under the one-use authority, and its hash is written into the epoch row and
   the trial row. The memory snapshot is sealed at the same instant.
4. **During the epoch** the supervisor refuses any roster, prompt, model or tool change (each would open a new
   epoch and end this one as `BROKEN`). Quarantined roles read the sealed snapshot only. Evaluated results are not
   readable by any role before a look.
5. **Cases are logged by the host**, not by agents, with coverage proofs. A gap in the log is recorded as a gap.
6. **Grading runs in a separate process** that owns the evaluation file. For every logged case it asks the
   project for the team's score, the baseline's score and the outcome state, and appends a grade row.
7. **At a look**, the grader re-verifies the nominee against the live files and roster. A mismatch spends the trial
   as `BROKEN`. Otherwise it builds the epoch snapshot and computes the verdict.
8. **The verdict** is a pure function of the snapshot and the design:
   - any zero-tolerance counter above zero, a missing proof, censored share above the cap, fewer clusters than
     planned, or a look outside the schedule gives `no_verdict`;
   - `pass` needs the clustered one-sided lower bound, at this attempt's significance, above `delta0` for both the
     net result and the paired difference to the baseline, plus the design's side conditions (positive on at least
     N cluster periods, top-3 share under the cap);
   - a `pass` in a first epoch is `pass_pending_replication`; promotion needs a second, non-overlapping epoch with
     the identical nominee hash.
9. **Release** writes approved aggregate fields, the verdict and the chain heads it was computed from into the
   public store. Raw grades stay private.
10. **Promotion** files a ticket (the existing `tickets` module) with the release's hash. A human approves or does
    not. The package never applies it.

## 5. What a project implements

One object, registered through the plug-in host (`host.set_evaluation(...)`), with five methods. All are called by
the grader process, never by an agent.

| Method | Returns | Notes |
|---|---|---|
| `cases(epoch_id, after)` | Logged cases in arrival order, each with an id, a first-eligible time and its cluster keys. | The project's host process is what logs them; this only reads. |
| `team_score(case, decision, horizon_at)` | `(state, value)` | `state` is `observed`, `censored` or `pending`. A skipped case scores what skipping scores, usually zero: it is not dropped. |
| `baseline_score(case, horizon_at)` | `(state, value)` | The frozen rule on the same case, under the same outcome model. |
| `fixed_cost(epoch_id, look_at)` | A number in the score's unit | Charged against the team's total, never against the baseline's. |
| `counters(epoch_id)` | `{name: count or None}` | `None` is unknown and blocks a verdict, like a positive count. |

And two files: the design file (step 1) and the list of aggregate field names it wants released.

Everything in the last row of the section 2 table becomes the source project's implementation of these five
methods. Nothing in it moves into the package.

## 6. Data

| Table | File | State |
|---|---|---|
| `epochs`, `org_changes`, `sealed_views`, `memory_snapshots`, `claims`, `decisions`, `decision_bundles` | public store | Exist. |
| `grades` | evaluation file | Exists. |
| `trial_registry` | public store | Was removed from the default schema in stage 1 as a project table. It is general and comes back as a built-in, same name and columns, so the source project's existing file opens unchanged. |
| `evaluation_cases`, `evaluation_gaps`, `evaluation_coverage` | public store | New built-ins. The source project keeps its own three tables under their existing names through `tables` in its config and its `cases()` reads them; no data moves. |

## 7. Statistics

The source computes two bounds per quantity: a circular block bootstrap over days and a cluster-robust t bound over
non-overlapping day blocks, and requires a simulation proof that the chosen method holds its error rate at each
attempt number before it will return `pass`.

Proposal: ship the cluster-robust t bound and the alpha-spending rule as the built-in method, in pure Python (a t
quantile needs about 30 lines; no numpy or scipy in the core). Ship the bootstrap and the calibration harness as an
optional extra that depends on numpy and scipy. The design file names the method; a design that names the
bootstrap without the extra installed is refused.

Limits to state in the README, as the source states its own:
- A t bound over few clusters is conservative and has low power. With under about 10 cluster periods the realistic
  result is `no_verdict`.
- Alpha spending makes each later attempt harder. That is the intent: ten tries at a 5% family level leaves the
  tenth attempt about 0.05%.
- A `pass` says the lower bound cleared `delta0` on these cases under this outcome model. Whether the outcome
  model matches reality is the project's claim, carried in as a named proof, not something the package checks.

## 8. Custody

| Actor | Public store | Evaluation file | Config directory |
|---|---|---|---|
| Supervisor and seats | read, write | never opened | read |
| Grader process | read; write only at release | read, write | read, verified read-only |
| Auditor process | read | read | read |
| Operator side | everything | everything | write, only between evaluated epochs |

The one-use authority (`launcher_policy.py`) is what proves the last column: the freeze and every re-check must
see the config directory through a read-only mount that the running user cannot remount. That needs a root-owned
mount set up once. It is a real host requirement, so it is switchable in the design file
(`require_readonly_config`); with it off, a verdict carries `config_custody: unproven` and cannot be `pass`, only
`pass_unproven_custody`.

## 9. Staged plan

| Stage | Content | Rough size |
|---|---|---|
| 4a | Registry and freeze: `trial_registry` back as a built-in; the evaluated-epoch functions back in `roster`; `nominee` with project rules injected; `launcher_policy`; their tests, taken from the source. | 500 lines copied, 150 changed |
| 4b | Case log and grader loop: three tables, `evaluation.py` with the five-method interface, grade rows, quarantine wiring, a grader entry point. | 400 new lines, 300 lines of tests |
| 4c | Verdict and release: design file loader, pure-Python clustered bound, alpha spending, `no_verdict` rules, replication, release of aggregates, promotion ticket. | 450 new lines, 400 lines of tests |
| 4d | Optional extra: bootstrap bound and calibration harness from the source. | 450 lines copied |
| 4e | A complete toy project in `examples/`: a made-up team answering questions with a known answer key, a fixed-rule baseline, one discovery and two evaluated epochs, ending in a promotion ticket. This is the README's proof that it runs. | 200 lines |

Smallest useful version: 4a to 4c plus 4e. The source project's own plug-in (its five methods over its existing
books) is part of stage 6, not of this stage.

## 10. Decisions for the user

| # | Decision | Recommendation |
|---|---|---|
| 1 | How much statistics in the core | Pure-Python clustered t bound and alpha spending built in; bootstrap and calibration as an optional extra (section 7). |
| 2 | What a pass does | Files a ticket for a human. No automatic switch, ever. |
| 3 | Replication required before a promotion request | Yes, as in the source: two non-overlapping epochs, same nominee hash. |
| 4 | Read-only config proof | Keep, switchable, and a verdict without it is labelled and cannot be a plain `pass` (section 8). |
| 5 | Grader as a separate process with its own file | Yes. Same OS user at first; a separate user is a later hardening. |
| 6 | Auditor in this stage | Chain audit against pinned heads and the counters read: yes, it is small and the verdict depends on it. Reconciliation of project totals stays in the project. |
| 7 | First real consumer | Needs your answer. The paused source experiment is the obvious one, but its plug-in is stage 6. A second, different consumer would test whether this interface is general or just the first experiment's shape. |
| 8 | Order of work | 4a, 4b, 4c, 4e, then stop and review before 4d. |
