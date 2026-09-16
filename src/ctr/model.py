"""Frozen data contracts shared by every ctr module.

Python 3.8 compatible (the Mac's python3 is 3.8.0). No third-party imports.
Nothing in this module performs I/O.
"""

import time
from typing import Dict, List, NamedTuple, Optional

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

CONFIG_DIR = "~/.config/ctr"
TOKENS_FILE = "~/.config/ctr/tokens.json"
STATE_FILE = "~/.config/ctr/state.json"
ACTIVE_FILE = "~/.config/ctr/active.sh"
CONFIG_FILE = "~/.config/ctr/config.json"
LOG_FILE = "~/Library/Logs/ctr.log"
LAUNCHD_LABEL = "com.binarcode.ctr-monitor"
LAUNCHD_PLIST = "~/Library/LaunchAgents/com.binarcode.ctr-monitor.plist"

#: Keychain service name for a ctr-managed token is KEYCHAIN_PREFIX + label.
KEYCHAIN_PREFIX = "ctr:"
#: The keychain item Claude Code itself writes for an interactive login.
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"

#: Environment variable the Claude CLI honours for a long-lived token.
#: MEASURED 2026-09-15: setting it flips `claude auth status` authMethod from
#: "claude.ai" (keychain) to "oauth_token" -> it takes precedence over the
#: keychain login for NEW processes. Already-running processes are unaffected.
ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

#: Marker lines that guard ctr's block in ~/.zshrc (idempotent install).
ZSHRC_BEGIN = "# >>> ctr (claude-token-rotator) >>>"
ZSHRC_END = "# <<< ctr (claude-token-rotator) <<<"

#: Only ever show this many leading characters of a token. Never more.
TOKEN_SHOW_CHARS = 6

# ---------------------------------------------------------------------------
# Defaults (overridable via ~/.config/ctr/config.json)
# ---------------------------------------------------------------------------

DEFAULTS = {
    # Switch away from the active token when its 5h utilisation crosses this.
    "switch_at_5h": 85.0,
    # ...or when its 7d utilisation crosses this.
    "switch_at_7d": 95.0,
    # Hysteresis: a token that triggered a switch is not eligible to become
    # active again until its 5h utilisation drops back below this.
    "recover_below_5h": 60.0,
    "recover_below_7d": 80.0,
    # A candidate must be at least this many percentage points better on 5h
    # than the current active token, otherwise we stay put (anti-flap).
    "min_improvement": 10.0,
    # Do not switch more often than this many seconds (anti-flap).
    "min_switch_interval_s": 600,
    # Seconds a usage reading stays fresh in the cache.
    "cache_ttl_s": 60,
    # HTTP timeout for a single probe, seconds.
    "http_timeout_s": 20,
    # Monitor: run rollover automatically after a switch.
    "auto_rollover": False,
}


def merged_config(user_config: Optional[Dict] = None) -> Dict:
    """DEFAULTS overlaid with user_config. Unknown keys are ignored."""
    out = dict(DEFAULTS)
    for key, value in (user_config or {}).items():
        if key in DEFAULTS:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Token registry entry (mirrors one element of tokens.json["tokens"])
# ---------------------------------------------------------------------------


class TokenRecord(NamedTuple):
    """A registered token. NEVER carries the secret itself."""

    label: str  # unique, [a-z0-9][a-z0-9_-]*
    account: str  # email, or "" if unknown
    subscription: str  # "max" | "pro" | "" if unknown
    added_at: str  # ISO 8601, local time
    kind: str  # "oat" (long-lived setup-token) | "oauth" (interactive)
    note: str = ""

    def to_json(self) -> Dict:
        return dict(self._asdict())

    @staticmethod
    def from_json(data: Dict) -> "TokenRecord":
        return TokenRecord(
            label=data["label"],
            account=data.get("account", ""),
            subscription=data.get("subscription", ""),
            added_at=data.get("added_at", ""),
            kind=data.get("kind", "oat"),
            note=data.get("note", ""),
        )


# ---------------------------------------------------------------------------
# Usage reading
# ---------------------------------------------------------------------------

#: Probe strategies. See usage.py.
PROBE_OAUTH_USAGE = "oauth_usage"  # GET /api/oauth/usage  (needs user:profile)
PROBE_RATELIMIT_HEADERS = "ratelimit_headers"  # POST /v1/messages max_tokens=0

#: `Usage.error` for the one "failure" that is not a probe failure at all: the
#: label has no keychain item, so nothing was sent and nothing can be learnt by
#: trying again. `usage.probe_all` still returns one Usage per record, so this
#: arrives at the selector looking exactly like a transient 429 — and the
#: two-consecutive-failures de-bounce (RULING 8.1) would make the monitor sit
#: on a token it can never read for a whole extra interval. The selector keys
#: on this exact string to trigger immediately instead.
NO_TOKEN_ERROR = "no token stored for this label"

#: Why a probe failed. The distinction is load-bearing (v1.1, 2026-09-16):
#:
#: FAILURE_TRANSPORT — curl never got an HTTP status back: DNS did not resolve,
#:   the connection failed or timed out, TLS broke, or we killed it ourselves.
#:   The API said NOTHING about this token, so it is not evidence about the
#:   token and must never count toward a switch. Measured live: two ticks 7.5
#:   hours apart, both with the Mac asleep or freshly woken and no DNS, drove
#:   `probe_failures` to 2 and produced `no_candidate`. With a second token
#:   registered that sequence switches the fleet off a healthy token and parks
#:   it for a full cooldown, during a blip the next token would hit identically.
#:
#: FAILURE_HTTP — the API answered ABOUT this token (401, 403, 429, 5xx). That
#:   is evidence, and it counts toward MIN_CONSECUTIVE_FAILURES.
#:
#: FAILURE_LOCAL — we never tried: no keychain item for the label. A second
#:   tick cannot learn more, so it triggers immediately (see _evaluate_trigger).
#:
#: "" means unclassified, and is treated as FAILURE_HTTP so anything not
#: explicitly marked keeps v1 behaviour.
FAILURE_TRANSPORT = "transport"
FAILURE_HTTP = "http"
FAILURE_LOCAL = "local"


class Usage(NamedTuple):
    """One token's measured utilisation.

    `five_h` / `seven_d` are PERCENTAGES 0..100 (not fractions), normalised
    from whichever probe produced them. `ok` is False when the probe failed;
    in that case `error` explains why and the percentages are None.
    """

    label: str
    five_h: Optional[float]
    seven_d: Optional[float]
    five_h_reset: Optional[int]  # unix seconds
    seven_d_reset: Optional[int]  # unix seconds
    status: str  # "allowed" | "rejected" | "unknown"
    probe: str  # PROBE_* constant, or "" when failed
    ok: bool
    error: str = ""
    checked_at: Optional[int] = None  # unix seconds
    failure_kind: str = ""  # FAILURE_* when ok is False; "" when ok

    @property
    def usable(self) -> bool:
        """True when this token may be selected as active."""
        return self.ok and self.five_h is not None and self.status != "rejected"

    def to_json(self) -> Dict:
        return dict(self._asdict())

    @staticmethod
    def from_json(data: Dict) -> "Usage":
        """Tolerant of a cache written by an older ctr: unknown keys are
        dropped and missing ones fall back to the field default."""
        fields = Usage._fields
        return Usage(**{k: v for k, v in (data or {}).items() if k in fields})

    @staticmethod
    def failed(
        label: str,
        error: str,
        checked_at: Optional[int] = None,
        kind: str = FAILURE_HTTP,
    ) -> "Usage":
        """A failed reading. `kind` defaults to FAILURE_HTTP so an unclassified
        failure keeps v1's counting behaviour rather than silently going quiet."""
        return Usage(
            label=label,
            five_h=None,
            seven_d=None,
            five_h_reset=None,
            seven_d_reset=None,
            status="unknown",
            probe="",
            ok=False,
            error=error,
            checked_at=checked_at if checked_at is not None else int(time.time()),
            failure_kind=kind,
        )


# ---------------------------------------------------------------------------
# Monitor decision (pure output of selector.decide)
# ---------------------------------------------------------------------------


class Decision(NamedTuple):
    """What the monitor should do this tick. Produced by selector.decide()."""

    action: str  # "switch" | "hold" | "no_candidate" | "no_active"
    target: Optional[str]  # label to switch to, when action == "switch"
    reason: str  # one line, safe to log and to show in a notification
    triggered: bool  # True when the active token crossed a threshold


# ---------------------------------------------------------------------------
# herdr pane (rollover)
# ---------------------------------------------------------------------------


class Pane(NamedTuple):
    """One herdr pane hosting a claude agent."""

    pane_id: str  # e.g. "w6:pC"
    tab_id: str
    name: str  # herdr agent name, "" when unnamed
    status: str  # herdr agent_status: idle|working|done|blocked|unknown
    session_id: str  # claude session id, "" when unknown
    cwd: str
    tab_label: str = ""

    def to_json(self) -> Dict:
        return dict(self._asdict())


class RolloverPlan(NamedTuple):
    """What ctr rollover intends to do to one pane."""

    pane: Pane
    act: bool  # True -> exit-and-resume this pane
    reason: str  # why we act, or why we skip


# ---------------------------------------------------------------------------
# Redaction helper — the single place that decides how a token is displayed.
# ---------------------------------------------------------------------------


def redact(secret: Optional[str]) -> str:
    """Return a safe display form of a token. Never returns the full secret."""
    if not secret:
        return "(none)"
    head = secret[:TOKEN_SHOW_CHARS]
    return "%s… (len %d)" % (head, len(secret))


def pct(value: Optional[float]) -> str:
    """Format a 0..100 percentage for display."""
    if value is None:
        return "  ?"
    return "%3.0f%%" % value


def fmt_reset(epoch: Optional[int], now: Optional[int] = None) -> str:
    """Human 'in 2h14m' style for a reset timestamp."""
    if not epoch:
        return "-"
    now = int(time.time()) if now is None else now
    delta = epoch - now
    if delta <= 0:
        return "now"
    hours, rem = divmod(delta, 3600)
    minutes = rem // 60
    if hours:
        return "in %dh%02dm" % (hours, minutes)
    return "in %dm" % minutes


VALID_LABEL_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_-"


def valid_label(label: str) -> bool:
    """Labels are lowercase, start alphanumeric, and shell/keychain safe."""
    if not label or len(label) > 40:
        return False
    if label[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        return False
    return all(char in VALID_LABEL_CHARS for char in label)


__all__ = [
    "CONFIG_DIR",
    "TOKENS_FILE",
    "STATE_FILE",
    "ACTIVE_FILE",
    "CONFIG_FILE",
    "LOG_FILE",
    "LAUNCHD_LABEL",
    "LAUNCHD_PLIST",
    "KEYCHAIN_PREFIX",
    "CLAUDE_KEYCHAIN_SERVICE",
    "ENV_VAR",
    "ZSHRC_BEGIN",
    "ZSHRC_END",
    "TOKEN_SHOW_CHARS",
    "DEFAULTS",
    "merged_config",
    "TokenRecord",
    "PROBE_OAUTH_USAGE",
    "PROBE_RATELIMIT_HEADERS",
    "NO_TOKEN_ERROR",
    "FAILURE_TRANSPORT",
    "FAILURE_HTTP",
    "FAILURE_LOCAL",
    "Usage",
    "Decision",
    "Pane",
    "RolloverPlan",
    "redact",
    "pct",
    "fmt_reset",
    "valid_label",
]
