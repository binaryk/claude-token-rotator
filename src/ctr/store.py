"""On-disk state for ctr: the token registry, monitor state and user config.

Layout under `~/.config/ctr` (directory 0700, files 0600):

    tokens.json   {"version":1,"active":"<label>|null","tokens":[TokenRecord,...]}
    state.json    {"last_switch_at":int,"parked":{...},"cache":{label:Usage},
                   "strategies":{label:PROBE_*}}
    config.json   user overrides for model.DEFAULTS (optional, hand-written)

**No file written here ever contains a secret.** Tokens live only in the macOS
keychain; `add()` actively refuses a record that looks like it carries one.

Writes are atomic: a 0600 temp file in the same directory, fsync, then
`os.replace`, so a crash can never leave a half-written registry.

Python 3.8 compatible. stdlib only.
"""

import json
import os
import re
import tempfile
from typing import Dict, List, Optional

from ctr.model import (
    CONFIG_DIR,
    PROBE_OAUTH_USAGE,
    PROBE_RATELIMIT_HEADERS,
    TokenRecord,
    Usage,
    merged_config,
    valid_label,
)

TOKENS_VERSION = 1
DIR_MODE = 0o700
FILE_MODE = 0o600

#: Anything matching these in a registry record is treated as a leaked secret.
_SECRET_PREFIXES = ("sk-ant-", "sk-ant-oat", "sk-")
_SECRET_RUN = re.compile(r"[A-Za-z0-9_\-]{40,}")
_KNOWN_PROBES = (PROBE_OAUTH_USAGE, PROBE_RATELIMIT_HEADERS)


class StoreError(RuntimeError):
    """The on-disk state is unusable (corrupt JSON, unwritable directory)."""


def _empty_state() -> Dict:
    return {"last_switch_at": 0, "parked": {}, "cache": {}, "strategies": {}}


def looks_like_secret(text: str) -> bool:
    """True when a registry value looks like a token rather than metadata."""
    if not isinstance(text, str):
        return False
    lowered = text.strip().lower()
    for prefix in _SECRET_PREFIXES:
        if lowered.startswith(prefix):
            return True
    return bool(_SECRET_RUN.search(text))


class Store:
    """Reads and writes ctr's configuration directory."""

    def __init__(self, config_dir: Optional[str] = None):
        self.config_dir = os.path.expanduser(config_dir or CONFIG_DIR)
        self.tokens_path = os.path.join(self.config_dir, "tokens.json")
        self.state_path = os.path.join(self.config_dir, "state.json")
        self.config_path = os.path.join(self.config_dir, "config.json")
        self.active_path = os.path.join(self.config_dir, "active.sh")

    # -- token registry ---------------------------------------------------

    def tokens(self) -> List[TokenRecord]:
        data = self._read_tokens_file()
        raw = data.get("tokens")
        if not isinstance(raw, list):
            return []
        records = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise StoreError("tokens.json entry %d is not an object" % index)
            try:
                records.append(TokenRecord.from_json(item))
            except KeyError as exc:
                raise StoreError("tokens.json entry %d is missing %s" % (index, exc))
        return records

    def add(self, record: TokenRecord) -> None:
        """Append a record. Raises ValueError on a bad or duplicate label."""
        if not valid_label(record.label):
            raise ValueError(
                "invalid label %r — use lowercase letters, digits, '-' and '_'"
                % record.label
            )
        self._assert_no_secret(record)
        data = self._read_tokens_file()
        existing = data.get("tokens") if isinstance(data.get("tokens"), list) else []
        for item in existing:
            if isinstance(item, dict) and item.get("label") == record.label:
                raise ValueError("a token labelled %r is already registered" % record.label)
        data["tokens"] = list(existing) + [record.to_json()]
        self._write_tokens_file(data)

    def remove(self, label: str) -> bool:
        data = self._read_tokens_file()
        existing = data.get("tokens") if isinstance(data.get("tokens"), list) else []
        kept = [
            item
            for item in existing
            if not (isinstance(item, dict) and item.get("label") == label)
        ]
        if len(kept) == len(existing):
            return False
        data["tokens"] = kept
        if data.get("active") == label:
            data["active"] = None
        self._write_tokens_file(data)
        return True

    def get(self, label: str) -> Optional[TokenRecord]:
        for record in self.tokens():
            if record.label == label:
                return record
        return None

    def active(self) -> Optional[str]:
        value = self._read_tokens_file().get("active")
        return value if isinstance(value, str) and value else None

    def set_active(self, label: Optional[str]) -> None:
        """Point the registry at `label` (None clears it)."""
        data = self._read_tokens_file()
        if label is not None:
            known = [
                item.get("label")
                for item in (data.get("tokens") or [])
                if isinstance(item, dict)
            ]
            if label not in known:
                raise ValueError("no token labelled %r is registered" % label)
        data["active"] = label
        self._write_tokens_file(data)

    # -- monitor state ----------------------------------------------------

    def state(self) -> Dict:
        raw = self._read_json(self.state_path)
        if not isinstance(raw, dict):
            return _empty_state()
        state = _empty_state()
        for key, value in raw.items():
            state[key] = value
        for key in ("parked", "cache", "strategies"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        try:
            state["last_switch_at"] = int(state.get("last_switch_at") or 0)
        except (TypeError, ValueError):
            state["last_switch_at"] = 0
        return state

    def save_state(self, state: Dict) -> None:
        if not isinstance(state, dict):
            raise ValueError("state must be a dict")
        self._write_atomic(self.state_path, state)

    # -- config -----------------------------------------------------------

    def config(self) -> Dict:
        raw = self._read_json(self.config_path)
        return merged_config(raw if isinstance(raw, dict) else None)

    # -- usage cache ------------------------------------------------------

    def cache_get(self, label: str, ttl_s: int, now: int) -> Optional[Usage]:
        """The cached reading for `label` when it is still fresh, else None."""
        entry = self.state().get("cache", {}).get(label)
        if not isinstance(entry, dict):
            return None
        try:
            usage = Usage.from_json(entry)
        except TypeError:
            return None  # written by an older/newer ctr — treat as a miss
        checked_at = usage.checked_at
        if not checked_at:
            return None
        age = now - int(checked_at)
        if age < 0 or age > max(0, ttl_s):
            return None
        return usage

    def cache_put(self, usage: Usage) -> None:
        """Cache a reading and remember which probe strategy produced it."""
        state = self.state()
        cache = dict(state.get("cache", {}))
        cache[usage.label] = usage.to_json()
        state["cache"] = cache
        if usage.ok and usage.probe in _KNOWN_PROBES:
            strategies = dict(state.get("strategies", {}))
            strategies[usage.label] = usage.probe
            state["strategies"] = strategies
        self.save_state(state)

    def probe_strategy(self, label: str) -> Optional[str]:
        """The probe strategy that last worked for `label` (usage.probe `prefer`)."""
        value = self.state().get("strategies", {}).get(label)
        return value if value in _KNOWN_PROBES else None

    # -- internals --------------------------------------------------------

    def _assert_no_secret(self, record: TokenRecord) -> None:
        for field, value in record.to_json().items():
            if field == "label":
                continue
            if isinstance(value, str) and looks_like_secret(value):
                raise ValueError(
                    "refusing to write field %r to tokens.json: it looks like a "
                    "token. Secrets belong in the keychain only." % field
                )

    def _read_tokens_file(self) -> Dict:
        raw = self._read_json(self.tokens_path)
        if raw is None:
            return {"version": TOKENS_VERSION, "active": None, "tokens": []}
        if not isinstance(raw, dict):
            raise StoreError("%s is not a JSON object" % self.tokens_path)
        raw.setdefault("version", TOKENS_VERSION)
        raw.setdefault("active", None)
        if not isinstance(raw.get("tokens"), list):
            raw["tokens"] = []
        return raw

    def _write_tokens_file(self, data: Dict) -> None:
        data["version"] = TOKENS_VERSION
        self._write_atomic(self.tokens_path, data)

    def _read_json(self, path: str):
        """Parsed JSON, or None when the file is absent.

        A corrupt tokens.json is an explicit error (it is the user's registry);
        a corrupt state.json is discarded, because it is a rebuildable cache.
        """
        try:
            with open(path, "r") as handle:
                text = handle.read()
        except (IOError, OSError):
            return None
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            if path == self.state_path:
                return None
            raise StoreError("%s is not valid JSON (%s)" % (path, exc))

    def ensure_dir(self) -> str:
        """Create the config dir 0700 and return it."""
        try:
            if not os.path.isdir(self.config_dir):
                os.makedirs(self.config_dir, DIR_MODE)
            elif (os.stat(self.config_dir).st_mode & 0o777) != DIR_MODE:
                os.chmod(self.config_dir, DIR_MODE)
        except OSError as exc:
            raise StoreError("cannot prepare %s: %s" % (self.config_dir, exc.strerror))
        return self.config_dir

    def _write_atomic(self, path: str, data: Dict) -> None:
        self.ensure_dir()
        handle = None
        fd, tmp_path = tempfile.mkstemp(prefix=".ctr-", suffix=".tmp", dir=self.config_dir)
        try:
            os.fchmod(fd, FILE_MODE)
            handle = os.fdopen(fd, "w")
            fd = -1
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(tmp_path, path)
        except Exception as exc:
            if handle is not None:
                handle.close()
            elif fd >= 0:
                os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if isinstance(exc, OSError):
                raise StoreError("cannot write %s: %s" % (path, exc.strerror))
            raise


__all__ = ["Store", "StoreError", "looks_like_secret", "TOKENS_VERSION"]
