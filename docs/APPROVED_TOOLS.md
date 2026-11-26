# Reviewed seat tools

A seat can submit a proposal in `/opt/workspace/tools/<name>/` and file `request(kind="tool")` with its path, signature, purpose and sample output. Read the submission as the Office service user, read-only. Treat its source, claims and fixtures as untrusted text. The host registry imports reviewed repository modules only.

## Review checklist

- Confirm a recorded request or repeated work. Check the current catalog for overlap and record the request event ID.
- Prefer existing host evidence and reuse existing calculations. Make unknown, stale, truncated and empty explicit. Describe source and observation cutoffs.
- Port only the reviewed implementation. Reject secrets, credentials, `.env` access, signing, live order paths, arbitrary filesystem access, dynamic imports, shell execution and unapproved network calls.
- Use fixed host-configured data sources, parameterized SQL and read-only connections for evidence tools. Bound inputs, time range, row and output budgets. Test malformed inputs, stale and missing evidence, and unauthorized roles.
- Register one reviewed module per tool and add a least-privilege role grant. Verify the intended role sees the tool and a role without a grant cannot call it. Run the relevant tests and the full gate.
- Record the disposition in `config/tool_request_dispositions.json` by exact request event ID. Use `building`, `shipped` or `refused`, with a reason and the tool name when known. No entry means new. Shipped means reviewed source landed. Verify loaded grants separately after release.

## Granting tools

Merge `config/approved_tools.example.json` into the trusted seat config. Append `sniper_office.approved_tools` to existing `plugins` and merge the desired role entry into `approved_tools`. Only the configured role's entry is used. Existing `denied_tools` still applies. No grant is inferred from a request or workspace file.

- `debate_index(since_s=86400, limit=20)` uses `markets_ledger_db`. It returns current-ledger story IDs, turn counts and latest turn times.
- `pm_rules(condition_id, offset=0, fetched_at=null)` uses `markets_db`. Repeat with the returned offset and snapshot time. A refresh invalidates the cursor. Historical retention boundaries remain possible truncation.
- `narrative_guess_coverage(guess_id=null, theme_id=null, slot_ts=null)` uses `narrative_db`. Specify an ID or a theme and slot. Counts describe the observed pool sample under the current name/symbol matcher. Theme-only matches cannot be estimated from that feed.
