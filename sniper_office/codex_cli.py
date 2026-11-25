"""One isolated Codex CLI call on a subscription login, and the handling of its auth file. Stdlib only.

The CLI may rotate the refresh token while it runs. Each call therefore works on a private copy of auth.json, and the copy is written
back to the real file only when it is the same account with only the rotating fields changed. `W` in codex_json_locked is the object
that supplies isolation(tmp, codex, home), prepare_codex_home(tmp), sync_auth_back(copy) and minimal_env(home): this module itself
works for the last three; isolation is the caller's, because what must be hidden depends on the host."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

DEADLINE_S = 60.0
ALLOWED_ITEMS = ("agent_message", "reasoning")          # any other JSON item type is a tool event
_ROTATING_TOKENS = ("id_token", "access_token", "refresh_token")
_INITIAL_SHA = {}          # copy path -> sha256 of the real auth.json when the copy was made


def _page(text, page=None):
    """A page failure never breaks the call. With no pager the text goes to stderr."""
    try:
        if page is not None:
            page(text)
            return
    except Exception:
        pass
    print("[codex-cli] " + text, file=sys.stderr)


def minimal_env(codex_home=None):
    """The whole environment the CLI gets. The caller's environment may hold every secret the host has, and a sandbox passes it through;
    inside, `env` would put them all in the conversation. Only PATH, HOME, the locale and the per-call CODEX_HOME pass."""
    # exact names, never a prefix: another secret under a CODEX_ name would pass
    keep = {} if codex_home is None else {"CODEX_HOME": str(codex_home)}   # never inherited from the caller
    keep.update({"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path.home()),
                 "LANG": os.environ.get("LANG", "C.UTF-8")})
    return keep


class CodexBusy(Exception):
    pass


@contextmanager
def codex_slot(timeout_s=120.0, path=None, poll_s=0.5):
    """ONE CLI call at a time across every caller that shares the login (one account, one auth file): an exclusive flock held for the duration
    of a single call. A second caller WAITS (polling) up to `timeout_s`, then raises CodexBusy. `path` is required: the lock file every caller agrees on."""
    if path is None:
        raise ValueError("codex_lock_path_required")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    waited = 0.0
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if waited >= timeout_s:
                    raise CodexBusy()
                time.sleep(poll_s)
                waited += poll_s
        yield
    finally:
        fh.close()                                             # closing the descriptor releases the flock


def prepare_codex_home(tmp, real_home=None):
    """tmp/codex-home (0700) holding only a copy of auth.json (0600). config.toml, sessions, plugins and
    memories of the real ~/.codex are NOT copied. Returns (codex_home, auth_copy) or (None, None) when
    the real login is missing."""
    real = Path(real_home or Path.home() / ".codex") / "auth.json"
    if not real.is_file():
        return None, None
    home = Path(tmp) / "codex-home"
    home.mkdir(mode=0o700)
    copy = home / "auth.json"
    shutil.copyfile(real, copy)
    copy.chmod(0o600)
    _INITIAL_SHA[str(copy)] = hashlib.sha256(copy.read_bytes()).hexdigest()   # in memory: the jail cannot edit it
    while len(_INITIAL_SHA) > 64:                              # a scratch path is used once; keep the table bounded
        _INITIAL_SHA.pop(next(iter(_INITIAL_SHA)))
    return home, copy


def _jwt_sub(token):
    import base64
    try:
        body = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("sub")
    except (AttributeError, IndexError, ValueError):
        return None


def auth_writeback_refusal(old, new):
    """None when `new` is the SAME login as `old` with only the rotating fields changed; else a slug.
    The copy sits in the jail's writable scratch dir, so whatever ran inside could have rewritten it: it
    must never be able to swap the account or strip the login."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return "not_an_object"
    if set(old) != set(new):
        return "keys_changed"
    for k in old:
        if k not in ("tokens", "last_refresh") and old[k] != new[k]:
            return "field_changed:" + k
    ot, nt = old.get("tokens"), new.get("tokens")
    if not isinstance(ot, dict) or not isinstance(nt, dict) or set(ot) != set(nt):
        return "tokens_shape"
    if ot.get("account_id") != nt.get("account_id") or not ot.get("account_id"):
        return "account_id_changed"
    for k in ot:
        if k not in _ROTATING_TOKENS and ot[k] != nt[k]:
            return "token_field_changed:" + k
    for k in _ROTATING_TOKENS:
        if k in nt and not (isinstance(nt[k], str) and nt[k]):
            return "token_empty:" + k
    if _jwt_sub(ot.get("id_token")) != _jwt_sub(nt.get("id_token")):
        return "subject_changed"
    if not isinstance(new.get("last_refresh"), str):
        return "last_refresh_shape"
    return None


def sync_auth_back(copy, real_home=None, page=None):
    """codex may rotate the refresh token inside the copy; a rotated token that is lost would log the real
    ~/.codex out. Returns 'unchanged', 'written', 'skipped:real_rotated_meanwhile' (the real file changed while the jail ran: keep it) or 'rejected:<why>'. A write needs the SAME account and
    only rotating fields changed (auth_writeback_refusal); the previous file is kept as auth.json.pre-checker
    (0600) before an atomic rename. A mismatch leaves the real login alone and pages a defect."""
    real = Path(real_home or Path.home() / ".codex") / "auth.json"
    initial = _INITIAL_SHA.get(str(copy))
    try:
        new_b, old_b = copy.read_bytes(), real.read_bytes()
    except OSError:
        return "rejected:unreadable"
    if initial is None:
        return "rejected:no_initial_hash"                  # a copy this process did not make
    if hashlib.sha256(new_b).hexdigest() == initial:
        return "unchanged"                                 # codex did not rotate inside the jail
    if hashlib.sha256(old_b).hexdigest() != initial:
        # another process sharing this login rotated the REAL file while the jail ran:
        # the jail's copy is STALE, not newer. Keep the real file, drop the copy.
        return "skipped:real_rotated_meanwhile"
    try:
        why = auth_writeback_refusal(json.loads(old_b), json.loads(new_b))
    except ValueError:
        why = "not_json"
    if why is not None:
        _page("codex auth write-back REFUSED (%s); real login untouched" % why, page)
        return "rejected:" + why
    try:
        backup = real.with_name("auth.json.pre-checker")
        backup.write_bytes(old_b)
        backup.chmod(0o600)
        stage = real.with_name(".auth.json.checker-%d" % os.getpid())
        with stage.open("wb") as staged:
            os.fchmod(staged.fileno(), 0o600)
            staged.write(new_b)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(stage, real)
        directory = os.open(real.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _INITIAL_SHA[str(copy)] = hashlib.sha256(new_b).hexdigest()   # the real file is now the copy's content
        return "written"
    except OSError:
        return "rejected:write_failed"


def codex_json_locked(W, prompt, *, timeout_s=DEADLINE_S, page=None, schema_obj=None,
                       model=None, effort=None, started=None, codex_path=None):
    """One isolated, bounded CLI process group. No tool event can become an accepted answer.

    Stream IDs and any model echo are observed evidence; a requested model is never an echo.
    """
    import os
    import selectors
    import signal
    started = time.monotonic() if started is None else started
    meta = {"tool_events": 0, "usage": None, "auth": None,
            "session_id": None, "result_id": None, "model_echo": None, "warnings": []}
    wall = lambda: (time.monotonic() - started) * 1000
    if not isinstance(model, str) or not model:
        raise ValueError("model_required")
    codex = codex_path or shutil.which("codex")
    meta["cli_path"] = codex
    if not codex:
        return None, "cli_missing", wall(), meta
    if not shutil.which("bwrap"):
        return None, "no_isolation", wall(), meta
    with tempfile.TemporaryDirectory(prefix="office-codex-") as tmp:
        schema, out = Path(tmp) / "schema.json", Path(tmp) / "answer.json"
        schema.write_text(json.dumps({"type": "object"} if schema_obj is None else schema_obj))
        home, auth = W.prepare_codex_home(tmp)
        if home is None:
            return None, "no_codex_login", wall(), meta
        cmd = W.isolation(tmp, codex, home) + [codex, "exec", "--json", "--sandbox", "read-only", "--ephemeral",
               "--ignore-rules", "--skip-git-repo-check", "-C", tmp, "-m", model,
               "--output-schema", str(schema), "-o", str(out)]
        if effort is not None:
            cmd += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        cmd += [prompt]
        if time.monotonic() - started >= timeout_s:
            return None, "timeout", wall(), meta
        error, tool, proc = None, None, None
        with open(Path(tmp) / "stderr.txt", "w") as err_file, selectors.DefaultSelector() as sel:
            try:
                proc = subprocess.Popen(cmd, cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=err_file, env=W.minimal_env(home), start_new_session=True)
                os.set_blocking(proc.stdout.fileno(), False)
                sel.register(proc.stdout, selectors.EVENT_READ)
                pending = b""
                while proc.poll() is None or sel.get_map():
                    left = timeout_s - (time.monotonic() - started)
                    if left <= 0:
                        error = "timeout"
                        break
                    if not sel.select(timeout=min(left, 0.1)):
                        continue
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        sel.unregister(proc.stdout)
                        if not pending:
                            continue
                        chunk = b"\n"             # accept the final JSON event without a trailing newline
                    pending += chunk
                    if len(pending) > 1048576:
                        error = "stream_too_large"
                        break
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            ev = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(ev, dict):
                            continue
                        item = ev.get("item") or {}
                        if not isinstance(item, dict):
                            item = {}
                        if str(ev.get("type", "")).startswith("item.") and item.get("type") == "error":
                            # Exec error-items are nonterminal warnings; only the final turn/exit decides failure.
                            message = str(item.get("message") or "")
                            warning = ("model_metadata_fallback" if "Model metadata for" in message and
                                       "Defaulting to fallback metadata" in message else "cli_warning")
                            if len(meta["warnings"]) < 64:
                                meta["warnings"].append(warning)
                            continue
                        if ev.get("type") in ("error", "turn.failed"):
                            message = str(item.get("message") or ev.get("error") or ev.get("message") or "").lower()
                            error = ("rate_limit" if "rate limit" in message or "rate_limit" in message else
                                     "auth_expired" if any(x in message for x in ("unauthorized", "authentication", "expired token"))
                                     else "provider_error")
                            meta["provider_error_kind"] = (
                                "model_unavailable" if "model" in message and any(x in message for x in ("not supported", "does not exist", "not found", "not available")) else
                                "schema" if "schema" in message else
                                "local_setup" if any(x in message for x in ("sandbox", "permission", "initialize", "configuration")) else
                                "other")
                            break
                        if str(ev.get("type", "")).startswith("item.") and item.get("type") not in ALLOWED_ITEMS:
                            tool = item.get("type") or "unknown"
                            meta["tool_events"] += 1
                            meta["tool_event_type"] = tool
                            error = "tool_event"
                            break
                        if ev.get("type") == "thread.started":
                            meta["session_id"] = ev.get("thread_id")
                        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                            meta["result_id"] = item.get("id")
                        echo = ev.get("model") or item.get("model")
                        if echo is not None:
                            meta["model_echo"] = echo
                        if ev.get("type") == "turn.completed":
                            meta["usage"] = ev.get("usage")
                    if error:
                        break
            finally:
                if proc is not None:
                    # Kill the owned group even if its leader exited while descendants still hold stdout.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    proc.stdout.close()
                if tool is None:
                    meta["auth"] = W.sync_auth_back(auth)
        if tool is not None:
            _page("codex call: tool event %r; isolated call aborted" % tool, page)
        if error is None and proc.returncode != 0:
            error = "exit_%d" % proc.returncode
        if error is None and time.monotonic() - started >= timeout_s:
            error = "timeout"
        if error:
            return None, error, wall(), meta
        return (out.read_text() if out.exists() else None), None, wall(), meta


_codex_json_locked = codex_json_locked   # older name
