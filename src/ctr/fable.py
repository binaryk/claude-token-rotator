"""Is the Fable model available on an account? (lazy, cached)

The per-model Fable quota is invisible to the cheap rate-limit-header probe: a
raw request with a Fable/Opus model returns 429 on EVERY OAuth token, quota or
not (measured 2026-09-29). Only the real CLI tells, so this runs one tiny
`claude -p` on Fable per account and caches the answer (FABLE_TTL_S).

MEASURED 2026-09-30: `--bare` ignores CLAUDE_CODE_OAUTH_TOKEN ("Not logged
in"), so the probe runs the normal CLI with no setting sources (no user hooks
or plugins fire) and a private CLAUDE_CONFIG_DIR, and hands the token over in
the ENVIRONMENT — never argv, which any user can read with `ps`.

Python 3.8 compatible. stdlib only.
"""

import json
import os
import re
import subprocess
import tempfile
from typing import Dict, Optional, Tuple

from ctr.model import CONFIG_DIR, ENV_VAR

FABLE_MODEL = "claude-fable-5-1"
FABLE_TTL_S = 1800
TIMEOUT_S = 120
PROMPT = "Reply with the single word ok."
STATE_KEY = "fable"

YES, NO, UNKNOWN = "yes", "no", "unknown"
_LIMIT_RE = re.compile(r"limit|quota|usage", re.IGNORECASE)
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def _probe_dir() -> str:
    path = os.path.join(os.path.expanduser(CONFIG_DIR), "fable-probe")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _run(cmd, env: Dict[str, str], cwd: str) -> Tuple[int, str, str]:
    """The single subprocess seam — tests replace this."""
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=TIMEOUT_S, start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", exc.strerror or "cannot run claude"
    return proc.returncode, proc.stdout, proc.stderr


def classify(rc: int, stdout: str, stderr: str) -> Tuple[str, str]:
    """(verdict, short secret-free detail) from one `claude -p --output-format json`."""
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if isinstance(data, dict):
        result = str(data.get("result") or "")
        if not data.get("is_error") and rc == 0:
            return YES, "answered"
        if _LIMIT_RE.search(result):
            return NO, _SECRET_RE.sub("***", result)[:80]
        return UNKNOWN, _SECRET_RE.sub("***", result or "error")[:80]
    text = _SECRET_RE.sub("***", (stderr or stdout or "no output").strip())
    if _LIMIT_RE.search(text):
        return NO, text[:80]
    return UNKNOWN, ("rc=%d %s" % (rc, text))[:80]


def probe(token: str) -> Tuple[str, str]:
    """One live Fable probe for `token`. Costs one tiny Fable request."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", ENV_VAR)}
    env[ENV_VAR] = token
    env["CLAUDE_CONFIG_DIR"] = _probe_dir()
    cmd = ["claude", "-p", PROMPT, "--model", FABLE_MODEL,
           "--output-format", "json", "--setting-sources", ""]
    rc, out, err = _run(cmd, env, tempfile.gettempdir())
    return classify(rc, out.replace(token, "***"), err.replace(token, "***"))


def cached(state: Dict, label: str, now: int, ttl_s: int = FABLE_TTL_S) -> Optional[Dict]:
    """The cached {"verdict","detail","checked_at"} for label, when fresh."""
    entry = (state.get(STATE_KEY) or {}).get(label)
    if not isinstance(entry, dict):
        return None
    try:
        age = now - int(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        return None
    return entry if 0 <= age <= ttl_s else None


def remember(store, label: str, verdict: str, detail: str, now: int) -> Dict:
    """Persist one verdict in state.json and return the entry."""
    entry = {"verdict": verdict, "detail": detail, "checked_at": int(now)}
    state = store.state()
    table = dict(state.get(STATE_KEY) or {})
    table[label] = entry
    state[STATE_KEY] = table
    store.save_state(state)
    return entry


__all__ = ["FABLE_MODEL", "FABLE_TTL_S", "YES", "NO", "UNKNOWN",
           "cached", "classify", "probe", "remember"]
