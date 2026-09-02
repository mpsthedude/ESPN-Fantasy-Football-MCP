"""
fantasypros_client.py

Standalone FantasyPros API client + local caching layer for the ESPN Fantasy
Football MCP server.

Owns: authentication, HTTP requests, persistent daily quota accounting,
local JSON caching with TTL rules, player identity mapping (FantasyPros <->
ESPN join), dataset normalization, and public-parameter validation helpers.

This module makes NO ESPN API calls and imports nothing from
espn_fantasy_server.py. It is imported BY espn_fantasy_server.py, never the
reverse. No @mcp.tool() decorators live here - MCP tools stay in
espn_fantasy_server.py per the existing single-file-server architecture.

SECURITY: the API key is read once per request via os.environ.get() and is
never logged, cached, or included in any returned payload. Only request
paths, params, and HTTP status codes are logged to stderr. The persisted
quota ledger stores only a date, a count, and a timestamp - never the key,
headers, or auth info.

QUOTA MODEL: `force` means "refresh this dataset even if its cache is
fresh" (cache-staleness override). `allow_soft_limit_override` means
"proceed with a live call even though today's soft limit has been reached"
(quota override). These are intentionally separate knobs.
"""

import os
import sys
import json
import tempfile
import unicodedata
import re
import datetime
import time
import threading
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests

import app_config

# DYNAMIC FANTASYPROS CREDENTIAL RESOLUTION (2026-08-15): winreg is a
# Windows-only stdlib module. Guarded import so this file remains safely
# importable on macOS/Linux/CI - _WINREG_AVAILABLE gates every use below.
try:
    import winreg
    _WINREG_AVAILABLE = True
except ImportError:
    winreg = None
    _WINREG_AVAILABLE = False

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

BASE_URL = "https://api.fantasypros.com/public/v2/json"
# D4B: LEGACY_CACHE_DIR is the pre-D4B module-directory-relative
# location, preserved exactly as before for per-file read-fallback
# purposes only. New writes never target this directory - the new
# authoritative default location is app_config.get_fantasypros_cache_dir().
LEGACY_CACHE_DIR = Path(__file__).resolve().parent / ".fp_cache"
CACHE_DIR = LEGACY_CACHE_DIR  # kept as an alias; no external caller references this name
REQUEST_TIMEOUT = 15  # seconds

DEFAULT_SCORING = "PPR"
CORE_POSITIONS = ["QB", "RB", "WR", "TE"]  # K/DST intentionally excluded until needed

VALID_POSITIONS = {"QB", "RB", "WR", "TE"}
VALID_SCORING = {"PPR", "HALF", "STD"}
DATASET_NAMES = ["players", "rankings", "projections", "injuries", "news"]

# TTLs in seconds (24h / 6-12h / 6-12h / 2-4h / 1-2h per spec)
TTL_SECONDS = {
    "players": 24 * 3600,
    "rankings": 8 * 3600,
    "projections": 8 * 3600,
    "injuries": 3 * 3600,
    "news": 1.5 * 3600,
}

# Daily account quota. Enforced client-side because FantasyPros has not
# returned rate-limit headers on any endpoint observed to date.
DAILY_REQUEST_LIMIT = 50
DAILY_SOFT_LIMIT = 45


def log_error(message: str) -> None:
    """Matches espn_fantasy_server.py's stderr logging convention. Never
    call this with anything containing the API key."""
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class FantasyProsConfigError(Exception):
    """Raised when the API key is missing. Callers catch this and return a
    structured configuration error - never a raw traceback."""
    pass


class FantasyProsQuotaError(Exception):
    """Raised when a single live request would exceed the soft limit
    (without allow_soft_limit_override=True) or the hard limit (always,
    regardless of override)."""
    def __init__(self, details: dict):
        self.details = details
        super().__init__(details.get("message", "FantasyPros daily quota guard triggered."))


class FantasyProsCacheError(Exception):
    """Raised when the FantasyPros cache directory/file cannot be created
    or written (D4B) - e.g. a PermissionError on the app-home cache
    directory. Never includes API-key values, cache contents, or
    authorization headers in its message; only the affected path is
    safe to include. Extends Exception directly (a sibling of
    FantasyProsConfigError/FantasyProsQuotaError, not a subclass of
    either) so it falls through to the existing generic
    `except Exception as e: return _error_response(...)` handling
    already present in every FantasyPros-touching MCP tool, including
    refresh_fantasypros_cache's own trailing catch-all - no
    espn_fantasy_server.py change is required for this to surface as a
    safe, structured MCP error."""
    pass


class FantasyProsRateLimitExhaustedError(Exception):
    """Raised by _request() when a single logical dataset request exhausts
    all permitted HTTP 429 retries (FP_MAX_429_RETRIES). Carries structured,
    secret-free diagnostic detail in .details - status/attempts/retries_used/
    retry_after_used/cache_preserved/message - for refresh_selected's
    failure accounting. Never includes the API key, headers, or raw
    response body."""
    def __init__(self, details: dict):
        self.details = details
        super().__init__(details.get("message", "FantasyPros rate limit retries exhausted."))


# --------------------------------------------------------------------------
# Name / team normalization for fallback matching
# --------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

def normalize_player_name(name: Optional[str]) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[.'\-]", "", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    tokens = [t for t in n.split() if t not in _SUFFIXES]
    return " ".join(tokens).strip()


TEAM_ALIASES = {
    "JAC": "JAX",
    "WSH": "WAS",
}

def normalize_team(team: Optional[str]) -> str:
    if not team:
        return ""
    t = str(team).strip().upper()
    return TEAM_ALIASES.get(t, t)


def normalize_position(position: Optional[str]) -> str:
    return str(position).strip().upper() if position else ""


# --------------------------------------------------------------------------
# Public-parameter validation helpers (return None if valid, else a message)
# --------------------------------------------------------------------------

def validate_position(position: Optional[str]) -> Optional[str]:
    if not position or position.upper() not in VALID_POSITIONS:
        return f"position must be one of {sorted(VALID_POSITIONS)} (got {position!r})."
    return None


def validate_scoring(scoring: Optional[str]) -> Optional[str]:
    if not scoring or scoring.upper() not in VALID_SCORING:
        return f"scoring must be one of {sorted(VALID_SCORING)} (got {scoring!r})."
    return None


def validate_limit(limit, min_val: int = 1, max_val: int = 200) -> Optional[str]:
    if limit is None:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool):
        return f"limit must be an integer between {min_val} and {max_val} (got {limit!r})."
    if limit < min_val or limit > max_val:
        return f"limit must be between {min_val} and {max_val} (got {limit})."
    return None


def validate_datasets(datasets) -> Optional[str]:
    if datasets is None:
        return None
    if not isinstance(datasets, list) or not all(isinstance(d, str) for d in datasets):
        return "datasets must be a list of strings."
    unknown = [d for d in datasets if d not in DATASET_NAMES]
    if unknown:
        return f"Unknown dataset(s) {unknown}. Valid options: {DATASET_NAMES}."
    return None


def validate_compare_players(players) -> Optional[str]:
    if not isinstance(players, list) or not (2 <= len(players) <= 4):
        return "players must be a list of 2-4 player names."
    if any(not isinstance(p, str) or not p.strip() for p in players):
        return "Each player name must be a non-empty string."
    return None


# --------------------------------------------------------------------------
# In-process request tracker (supplementary; the persisted ledger below is
# the authoritative daily counter across process restarts)
# --------------------------------------------------------------------------

class RequestTracker:
    def __init__(self):
        self.requests_made = 0
        self.history: List[Dict[str, Any]] = []

    def record(self, endpoint: str, status_code: int) -> None:
        self.requests_made += 1
        self.history.append({
            "endpoint": endpoint,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "status_code": status_code,
        })


_tracker = RequestTracker()


# --------------------------------------------------------------------------
# Persistent daily quota ledger (.fp_cache/request_usage.json)
# --------------------------------------------------------------------------

def _resolve_cache_read_path(filename: str) -> Path:
    """Per-file new-then-legacy read resolution (D4B). Never creates
    directories. If the exact file exists at the new app-home cache
    location, that wins outright - even if malformed, since presence of
    the new file makes it authoritative and this function is never
    consulted a second time for the same call. Otherwise falls back to
    the legacy per-file path only if THAT exact file exists. If neither
    exists, returns the new path (so missing-file callers report/behave
    against the current/expected location)."""
    new_dir = app_config.get_fantasypros_cache_dir()
    new_path = new_dir / filename
    if new_path.exists():
        return new_path
    legacy_path = LEGACY_CACHE_DIR / filename
    if legacy_path.exists():
        return legacy_path
    return new_path


def _usage_path() -> Path:
    """Read resolution ONLY (D4B) - see _resolve_cache_read_path. Never
    creates a directory. Writes must use _usage_write_path() instead,
    never this function, so that an update is never accidentally written
    back into a legacy file that this function may have resolved to for
    reading."""
    return _resolve_cache_read_path("request_usage.json")


def _usage_write_path() -> Path:
    """Write destination for the quota/usage ledger - ALWAYS the new
    app-home location, regardless of where the most recent read came
    from (D4B). Never creates the directory itself; _atomic_write_json
    creates it at actual write time."""
    return app_config.get_fantasypros_cache_dir() / "request_usage.json"


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _load_usage() -> dict:
    """Reads the persisted ledger (new-then-legacy per-file fallback via
    _usage_path()). Resets automatically (and rewrites to the NEW
    location only - never mutating a legacy source file) if the file is
    missing, corrupted, or the local calendar date has changed since it
    was last written. D4B release-critical: when the read resolves to a
    same-day-valid LEGACY ledger, that state is returned as-is (the
    starting count is preserved) and nothing is written here - migration
    only happens when a caller subsequently calls _increment_usage(),
    which writes the updated result to the new location."""
    path = _usage_path()
    today = _today_str()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today and "requests_made_today" in data:
                return data
        except (json.JSONDecodeError, OSError) as e:
            log_error(f"Quota ledger read failed, resetting: {e}")
    fresh = {"date": today, "requests_made_today": 0, "last_request_at": None}
    _atomic_write_json(_usage_write_path(), fresh)
    return fresh


def _increment_usage() -> dict:
    """Loads the current usage (preserving whatever starting count
    _load_usage() resolved - new, legacy, or fresh), increments it, and
    writes the UPDATED result to the NEW app-home location only (D4B) -
    never back to a legacy source file, even if that is where the
    starting count was read from."""
    usage = _load_usage()
    usage["requests_made_today"] += 1
    usage["last_request_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    _atomic_write_json(_usage_write_path(), usage)
    return usage


def get_usage_summary() -> dict:
    usage = _load_usage()
    used = usage["requests_made_today"]
    return {
        "date": usage["date"],
        "requests_used_today": used,
        "estimated_requests_remaining_today": max(0, DAILY_REQUEST_LIMIT - used),
        "daily_soft_limit": DAILY_SOFT_LIMIT,
        "daily_hard_limit": DAILY_REQUEST_LIMIT,
        "last_request_at": usage.get("last_request_at"),
    }


def _check_quota_guard(allow_soft_limit_override: bool) -> Optional[dict]:
    """Returns None if a single live call may proceed, else a structured
    error dict. The hard limit is never bypassable, even with override."""
    used = _load_usage()["requests_made_today"]
    if used >= DAILY_REQUEST_LIMIT:
        return {
            "error": "quota_exceeded",
            "message": f"Daily hard limit of {DAILY_REQUEST_LIMIT} FantasyPros requests reached "
                       f"today ({used} used). Refusing further live calls regardless of override.",
            "requests_used_today": used,
            "daily_soft_limit": DAILY_SOFT_LIMIT, "daily_hard_limit": DAILY_REQUEST_LIMIT,
        }
    if used >= DAILY_SOFT_LIMIT and not allow_soft_limit_override:
        return {
            "error": "quota_soft_limit",
            "message": f"Daily soft limit of {DAILY_SOFT_LIMIT} reached ({used} used). "
                       f"Pass allow_soft_limit_override=True to proceed up to the hard limit of {DAILY_REQUEST_LIMIT}.",
            "requests_used_today": used,
            "daily_soft_limit": DAILY_SOFT_LIMIT, "daily_hard_limit": DAILY_REQUEST_LIMIT,
        }
    return None


# --------------------------------------------------------------------------
# Atomic cache I/O
# --------------------------------------------------------------------------

def _cache_path(dataset_key: str) -> Path:
    """Read resolution ONLY (D4B) - see _resolve_cache_read_path. Never
    creates a directory. Writes must use _cache_write_path() instead."""
    return _resolve_cache_read_path(f"{dataset_key}.json")


def _cache_write_path(dataset_key: str) -> Path:
    """Write destination for a cache dataset - ALWAYS the new app-home
    location (D4B), regardless of where the most recent read came from.
    Never creates the directory itself."""
    return app_config.get_fantasypros_cache_dir() / f"{dataset_key}.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write via temp file + os.replace so an interrupted write can never
    corrupt the existing cache file. os.replace is atomic on both Windows
    (same volume) and POSIX.

    D4B: callers always pass a NEW-location path (via _usage_write_path()
    or _cache_write_path()), so the mkdir below only ever creates the new
    app-home cache directory - never the installed module/source
    directory. A PermissionError/OSError creating that directory or the
    temp file is converted to a safe FantasyProsCacheError naming only
    the affected path (never the payload or any credential) - this falls
    through cleanly to every FantasyPros MCP tool's existing generic
    `except Exception` error handling with zero espn_fantasy_server.py
    changes required."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FantasyProsCacheError(f"FantasyPros cache directory is not writable: {path.parent}") from e
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    except OSError as e:
        raise FantasyProsCacheError(f"FantasyPros cache directory is not writable: {path.parent}") from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _read_cache(dataset_key: str) -> Optional[dict]:
    path = _cache_path(dataset_key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_error(f"Cache read failed for {dataset_key}, treating as missing/stale: {e}")
        return None


def _is_stale(cache_obj: Optional[dict], ttl_seconds: float) -> bool:
    if not cache_obj or "fetched_at" not in cache_obj:
        return True
    try:
        fetched_at = datetime.datetime.fromisoformat(cache_obj["fetched_at"].replace("Z", "+00:00"))
    except ValueError:
        return True
    age = datetime.datetime.now(datetime.timezone.utc) - fetched_at
    return age.total_seconds() > ttl_seconds


# --------------------------------------------------------------------------
# Core HTTP layer
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# DYNAMIC FANTASYPROS CREDENTIAL RESOLUTION (2026-08-15)
#
# PROBLEM SOLVED: FANTASYPROS_API_KEY set/rotated at Windows User scope
# previously required restarting the MCP host process (and sometimes
# Explorer/sign-out) before a running process could see the new value,
# because os.environ is populated once from the OS-inherited process
# environment block at process start and is never live-updated by Windows
# when the registry changes. This block adds a live HKCU\Environment
# registry read that is performed FRESH on every single credential
# resolution (never cached), so a value set or rotated at the Windows User
# level while this process is already running is observable on the very
# next FantasyPros operation - no restart of Orcha, Explorer, sign-out, or
# reboot required. Non-Windows / process-env deployments are fully
# preserved as a portable fallback. No pre-existing FantasyPros
# secret-file convention was found anywhere in this codebase (grep-
# confirmed against espn_fantasy_server.py, fantasypros_client.py,
# league_registry.py, performance_regression.py) - the local secret-file
# tier below is a new, minimal, single-purpose addition, not a migration
# of an existing format. NEVER logs, caches, or exposes the resolved key
# value anywhere - only safe source metadata (a fixed enum string) may be
# used internally, and this resolver is never wired to any @mcp.tool().
# --------------------------------------------------------------------------

_FANTASYPROS_SECRET_FILE = os.path.join(os.path.expanduser("~"), ".orcha", "secrets", "fantasypros_api_key.txt")


def _read_windows_user_env_registry(name: str) -> Optional[str]:
    """Directly reads a User-scope environment variable from
    HKCU Environment (registry) via the stdlib winreg module, bypassing this
    process's own (possibly stale) inherited environment block entirely.
    Called fresh on every resolution - never cached - so a later Windows
    User-scope change is always observable on the next call. Returns None
    (NEVER raises) if winreg is unavailable (non-Windows), the value does
    not exist, or any registry/permission error occurs - callers fall
    through to the next credential source in that case."""
    if not _WINREG_AVAILABLE:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except Exception:
        return None


def _read_secret_file_api_key() -> Optional[str]:
    """New, minimal, single-purpose local secret-file fallback (see module
    note above - no prior FantasyPros secret-file convention existed to
    preserve). Expects a single line containing only the key at
    ~/.orcha/secrets/fantasypros_api_key.txt. Never raises; returns None on
    any missing file, I/O error, or empty/whitespace-only content."""
    try:
        if not os.path.exists(_FANTASYPROS_SECRET_FILE):
            return None
        with open(_FANTASYPROS_SECRET_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return content or None
    except Exception:
        return None


def _resolve_fantasypros_api_key_with_source():
    """Returns (key_or_None, source_or_None) where source is one of
    "windows_user_environment" / "process_environment" /
    "project_credentials_file" / "local_secret_file".
    Precedence: (1) live Windows User-scope registry - Windows only,
    intentionally checked FIRST and re-read fresh every call so it always
    wins over a possibly-stale inherited process environment value, which
    is exactly what allows a Windows User-level rotation to take effect
    without any restart; (2) process environment - the portable path for
    non-Windows/deployment environments, and the effective Windows path too
    whenever the registry lookup itself yields nothing; (3) the new
    project-owned generic credentials file (~/.fantasy-football-mcp/
    credentials.json by default, see app_config.py) - checked before the
    legacy Orcha-specific file so new public users are not silently routed
    around a file they intentionally created; (4) the legacy local secret
    file. Empty/whitespace-only values at ANY source are treated as
    unavailable and fall through to the next source normally. A malformed
    project credentials file (present but invalid JSON/schema) is treated
    as a genuine configuration error and is NOT silently skipped in favor
    of the legacy file - app_config.ConfigError propagates to the caller
    unchanged, by design. NEVER logs or exposes the key value - only the
    safe source label is returned alongside it, and even that must never
    appear in any MCP tool output."""
    windows_val = _read_windows_user_env_registry("FANTASYPROS_API_KEY")
    if windows_val and windows_val.strip():
        return windows_val.strip(), "windows_user_environment"
    process_val = os.environ.get("FANTASYPROS_API_KEY")
    if process_val and process_val.strip():
        return process_val.strip(), "process_environment"
    project_val = app_config.get_fantasypros_api_key_from_credentials()
    if project_val and project_val.strip():
        return project_val.strip(), "project_credentials_file"
    file_val = _read_secret_file_api_key()
    if file_val and file_val.strip():
        return file_val.strip(), "local_secret_file"
    return None, None


def _resolve_fantasypros_api_key() -> Optional[str]:
    """Centralized credential resolver - the ONLY function anywhere in this
    codebase that acquires FANTASYPROS_API_KEY. Called fresh (never cached)
    from _get_api_key() on every _request() call, so credential
    availability/rotation is always observable on the NEXT FantasyPros
    operation. Returns None if no source yields a usable value; raising a
    structured error on total absence is _get_api_key()'s responsibility,
    not this function's."""
    key, _source = _resolve_fantasypros_api_key_with_source()
    return key


def _get_api_key() -> str:
    key = _resolve_fantasypros_api_key()
    if not key:
        raise FantasyProsConfigError(
            "FANTASYPROS_API_KEY is not available. Supported sources (checked in order, "
            "no MCP host restart required for the first): a Windows User-scope environment "
            "variable (read live from the registry every time), a process environment "
            "variable, or a local secret file at ~/.orcha/secrets/fantasypros_api_key.txt."
        )
    return key


# --------------------------------------------------------------------------
# FANTASYPROS RATE-LIMIT RESILIENCE (2026-08-15)
#
# PROBLEM SOLVED: a registry-aware refresh can legitimately require many
# live FantasyPros requests in one batch. Firing them as fast as Python can
# issue them (the prior behavior) triggered upstream HTTP 429 responses
# after ~7 rapid requests. This block adds (a) a small minimum interval
# between ANY two live HTTP attempts (prevention), and (b) bounded 429
# retry with Retry-After support / exponential-backoff fallback (reaction),
# entirely inside _request() - the single centralized transport boundary
# every refresh_* function already funnels through. No caller (refresh_*,
# refresh_selected, or the server) needed to change.
#
# QUOTA SAFETY: the persisted daily ledger counts HTTP ATTEMPTS (confirmed
# by inspection - _increment_usage() fires for every response received,
# success or error, BEFORE raise_for_status()), NOT logical dataset
# successes. Every retry is therefore a real attempt against the same
# ledger. _check_quota_guard() is re-evaluated fresh before EACH attempt
# (initial and every retry) via the loop below, so the existing hard/soft
# limit enforcement automatically blocks a retry the instant it would
# cross a limit - no separate bookkeeping needed, and no new path can
# silently burn unlimited requests.
# --------------------------------------------------------------------------

FP_MIN_REQUEST_INTERVAL_SECONDS = 1.0   # base pacing between any two live HTTP attempts
FP_MAX_429_RETRIES = 3                  # retries AFTER the initial attempt (max 4 attempts/dataset)
FP_429_BACKOFF_BASE_SECONDS = 2.0       # exponential backoff base when Retry-After is absent/unusable
FP_MAX_RETRY_AFTER_SECONDS = 30.0       # ceiling on any single wait, whether from Retry-After or backoff

_pacing_lock = threading.Lock()
_last_request_monotonic = None


def _pace_before_request() -> None:
    """Enforces FP_MIN_REQUEST_INTERVAL_SECONDS between any two live HTTP
    attempts (initial or retry) using time.monotonic() - never wall-clock -
    so pacing is immune to system clock changes. Lock-protected so two
    threads cannot race past the interval. Only ever called immediately
    before an actual requests.get() call - never on cache hits, dry runs,
    decision-tool calls, or league switching."""
    global _last_request_monotonic
    with _pacing_lock:
        now = time.monotonic()
        if _last_request_monotonic is not None:
            wait = FP_MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_monotonic)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _last_request_monotonic = now


def _parse_retry_after(resp) -> Optional[float]:
    """Parses a 429 response's Retry-After header. Supports both legal
    forms - integer/float seconds, and an HTTP-date (RFC 7231) via
    email.utils.parsedate_to_datetime, stdlib, no new dependency. Returns
    None (never raises) for a missing, empty, or unparseable header, or a
    date already in the past resolves to 0.0. Ceiling is NOT applied here -
    callers apply FP_MAX_RETRY_AFTER_SECONDS."""
    val = resp.headers.get("Retry-After")
    if not val or not val.strip():
        return None
    val = val.strip()
    try:
        seconds = float(val)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt is None:
            return None
        now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo is not None else datetime.datetime.now()
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


def _request(path: str, params: Optional[dict] = None, allow_soft_limit_override: bool = False) -> dict:
    """Single low-level GET, gated by the persisted daily quota guard and
    paced/retried against HTTP 429. Increments the persisted counter for
    any ATTEMPT that reaches FantasyPros and gets a response - success or
    HTTP error alike, including every 429 retry - but NOT for calls blocked
    by the guard or lost to a network-level failure before a response was
    received (both behaviors identical to the pre-existing implementation).

    Note: this uses allow_soft_limit_override, NOT force. `force` (used by
    the refresh_* functions below) only controls whether a fresh cache is
    bypassed; it never affects the quota guard."""
    attempts_made = 0
    retries_used = 0
    last_retry_after_used = None

    while True:
        guard = _check_quota_guard(allow_soft_limit_override)
        if guard is not None:
            guard = dict(guard)
            guard["attempts_made_this_call"] = attempts_made
            guard["retries_used_this_call"] = retries_used
            raise FantasyProsQuotaError(guard)

        _pace_before_request()

        key = _get_api_key()
        url = f"{BASE_URL}{path}"
        headers = {"x-api-key": key}
        log_error(f"FantasyPros GET {path} params={params} "
                  f"allow_soft_limit_override={allow_soft_limit_override} attempt={attempts_made + 1}")
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

        attempts_made += 1
        _increment_usage()
        _tracker.record(path, resp.status_code)

        if resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()

        if retries_used >= FP_MAX_429_RETRIES:
            raise FantasyProsRateLimitExhaustedError({
                "status": "rate_limited",
                "path": path,
                "attempts": attempts_made,
                "retries_used": retries_used,
                "retry_after_used": last_retry_after_used,
                "cache_preserved": True,
                "message": f"FantasyPros returned HTTP 429 after {attempts_made} attempt(s) "
                           f"({retries_used} retries) for {path}; giving up for this dataset. "
                           f"Prior successful cache data was not modified.",
            })

        retry_after = _parse_retry_after(resp)
        if retry_after is not None:
            wait = min(retry_after, FP_MAX_RETRY_AFTER_SECONDS)
        else:
            wait = min(FP_429_BACKOFF_BASE_SECONDS * (2 ** retries_used), FP_MAX_RETRY_AFTER_SECONDS)
        last_retry_after_used = wait
        retries_used += 1
        log_error(f"FantasyPros 429 for {path}; retry {retries_used}/{FP_MAX_429_RETRIES} after {wait:.1f}s "
                  f"(retry_after_header={'yes' if retry_after is not None else 'no'})")
        time.sleep(wait)


# --------------------------------------------------------------------------
# Dataset: PLAYERS  (/nfl/players) - the only dataset with true ADP fields
# --------------------------------------------------------------------------

def _normalize_players(raw: dict) -> dict:
    normalized = []
    for p in raw.get("players", []) or []:
        normalized.append({
            "fp_player_id": p.get("player_id"),
            "name": p.get("player_name"),
            "position": normalize_position(p.get("position_id")),
            "team": normalize_team(p.get("team_id")),
            "sportsdata_id": p.get("sportsdata_player_id"),
            "rank_ecr": p.get("rank_ecr"),
            "rank_ecr_ppr": p.get("rank_ecr_ppr"),
            "rank_ecr_half": p.get("rank_ecr_half"),
            "rank_adp": p.get("rank_adp"),
            "rank_adp_ppr": p.get("rank_adp_ppr"),
            "_norm_name": normalize_player_name(p.get("player_name")),
        })
    return {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "endpoint": "/nfl/players",
        "season": raw.get("season"),
        "scoring": None,
        "record_count": len(normalized),
        "fp_tier": raw.get("tier"),
        "public_api_limited": raw.get("public_api_limited"),
        "players": normalized,
    }


def refresh_players(force: bool = False, allow_soft_limit_override: bool = False) -> dict:
    cached = _read_cache("players")
    if not force and not _is_stale(cached, TTL_SECONDS["players"]):
        return {"dataset": "players", "source": "cache", "record_count": cached.get("record_count")}
    raw = _request("/nfl/players", allow_soft_limit_override=allow_soft_limit_override)
    normalized = _normalize_players(raw)
    _atomic_write_json(_cache_write_path("players"), normalized)
    return {"dataset": "players", "source": "live", "record_count": normalized["record_count"]}


def get_players_cache() -> Optional[dict]:
    return _read_cache("players")


def _dataset_needs_live_call(dataset_key: str, ttl_seconds: float, force: bool) -> bool:
    """Pure local check (zero API cost) used by the pre-flight batch
    estimator: would this specific dataset actually require a live call
    right now, given current cache state and the force flag?"""
    if force:
        return True
    return _is_stale(_read_cache(dataset_key), ttl_seconds)


# --------------------------------------------------------------------------
# Dataset: RANKINGS (per position; /nfl/{season}/consensus-rankings)
# NOTE: contains ownership %, NOT true ADP. True ADP comes from players.
# --------------------------------------------------------------------------

def _rankings_cache_key(position: str, scoring: str) -> str:
    return f"rankings_{position.upper()}_{scoring.upper()}"


def _normalize_rankings(raw: dict, position: str, scoring: str) -> dict:
    normalized = []
    for p in raw.get("players", []) or []:
        normalized.append({
            "fp_player_id": p.get("player_id"),
            "name": p.get("player_name"),
            "team": normalize_team(p.get("player_team_id")),
            "position": normalize_position(p.get("player_position_id")),
            "bye_week": p.get("player_bye_week"),
            "rank_ecr": p.get("rank_ecr"),
            "pos_rank": p.get("pos_rank"),
            "tier": p.get("tier"),
            "rank_min": p.get("rank_min"),
            "rank_max": p.get("rank_max"),
            "rank_ave": p.get("rank_ave"),
            "rank_std": p.get("rank_std"),
            "avg_ownership_pct": p.get("player_owned_avg"),
            "espn_ownership_pct": p.get("player_owned_espn"),
            "yahoo_ownership_pct": p.get("player_owned_yahoo"),
            "yahoo_id": p.get("player_yahoo_id"),
            "cbs_id": p.get("cbs_player_id"),
            "sportsdata_id": p.get("sportsdata_id"),
            "_norm_name": normalize_player_name(p.get("player_name")),
        })
    return {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "endpoint": "/nfl/{season}/consensus-rankings",
        "season": raw.get("year"),
        "scoring": scoring.upper(),
        "position": position.upper(),
        "record_count": len(normalized),
        "fp_tier": raw.get("tier"),
        "public_api_limited": raw.get("public_api_limited"),
        "total_experts": raw.get("total_experts"),
        "last_updated": raw.get("last_updated"),
        "players": normalized,
    }


def refresh_rankings(position: str, scoring: str = DEFAULT_SCORING, force: bool = False,
                      allow_soft_limit_override: bool = False, season: Optional[int] = None) -> dict:
    key = _rankings_cache_key(position, scoring)
    cached = _read_cache(key)
    if not force and not _is_stale(cached, TTL_SECONDS["rankings"]):
        return {"dataset": key, "source": "cache", "record_count": cached.get("record_count")}
    season = season or datetime.datetime.now().year
    raw = _request(f"/nfl/{season}/consensus-rankings",
                    params={"position": position.upper(), "scoring": scoring.upper()},
                    allow_soft_limit_override=allow_soft_limit_override)
    normalized = _normalize_rankings(raw, position, scoring)
    _atomic_write_json(_cache_write_path(key), normalized)
    return {"dataset": key, "source": "live", "record_count": normalized["record_count"]}


def get_rankings_cache(position: str, scoring: str = DEFAULT_SCORING) -> Optional[dict]:
    return _read_cache(_rankings_cache_key(position, scoring))


def get_rankings_list(position: str, scoring: str = DEFAULT_SCORING, limit: Optional[int] = None) -> dict:
    cache = get_rankings_cache(position, scoring)
    if not cache:
        return {"error": "cache_missing", "message": f"No cached rankings for {position}/{scoring}. "
                "Call refresh_fantasypros_cache first.", "position": position, "scoring": scoring}
    players = cache.get("players", [])
    if limit:
        players = players[:limit]
    return {
        "position": position.upper(), "scoring": scoring.upper(),
        "fetched_at": cache.get("fetched_at"), "record_count": len(players),
        "total_available": cache.get("record_count"), "fp_tier": cache.get("fp_tier"),
        "players": players,
    }


# --------------------------------------------------------------------------
# Dataset: PROJECTIONS (/nfl/{season}/projections)
# NOTE: omitting `week` returns season-long/draft projections (confirmed
# working with real data). Explicit in-season week numbers return empty
# until FantasyPros publishes that week's projections. The `scoring` param
# appears cosmetic on this endpoint - both points and points_ppr are always
# present, so the correct field is selected client-side.
# --------------------------------------------------------------------------

def _projections_cache_key(position: str, scoring: str, week: int) -> str:
    return f"projections_{position.upper()}_{scoring.upper()}_wk{week}"


def _normalize_projections(raw: dict, position: str, scoring: str) -> dict:
    scoring_field = {"PPR": "points_ppr", "HALF": "points_half", "STD": "points"}.get(scoring.upper(), "points_ppr")
    normalized = []
    for p in raw.get("players", []) or []:
        stats = p.get("stats", {}) or {}
        normalized.append({
            "fp_player_id": p.get("fpid"),
            "name": p.get("name"),
            "team": normalize_team(p.get("team_id")),
            "position": normalize_position(p.get("position_id")),
            "projected_points": stats.get(scoring_field),
            "projected_points_std": stats.get("points"),
            "projected_points_ppr": stats.get("points_ppr"),
            "projected_points_half": stats.get("points_half"),
            "_norm_name": normalize_player_name(p.get("name")),
        })
    return {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "endpoint": "/nfl/{season}/projections",
        "season": raw.get("season"),
        "week": raw.get("week"),
        "scoring": scoring.upper(),
        "position": position.upper(),
        "record_count": len(normalized),
        "fp_tier": raw.get("tier"),
        "public_api_limited": raw.get("public_api_limited"),
        "players": normalized,
    }


def refresh_projections(position: str, scoring: str = DEFAULT_SCORING, week: int = 0,
                         force: bool = False, allow_soft_limit_override: bool = False,
                         season: Optional[int] = None) -> dict:
    key = _projections_cache_key(position, scoring, week)
    cached = _read_cache(key)
    if not force and not _is_stale(cached, TTL_SECONDS["projections"]):
        return {"dataset": key, "source": "cache", "record_count": cached.get("record_count")}
    season = season or datetime.datetime.now().year
    params = {"position": position.upper(), "scoring": scoring.upper()}
    if week and week > 0:
        params["week"] = week
    raw = _request(f"/nfl/{season}/projections", params=params,
                    allow_soft_limit_override=allow_soft_limit_override)
    normalized = _normalize_projections(raw, position, scoring)
    if normalized["record_count"] == 0:
        normalized["empty_reason"] = ("No projections published yet for this week/position "
                                       "(common during preseason). Cached as empty to avoid "
                                       "re-querying every call; TTL still applies.")
    _atomic_write_json(_cache_write_path(key), normalized)
    return {"dataset": key, "source": "live", "record_count": normalized["record_count"]}


def get_projections_cache(position: str, scoring: str = DEFAULT_SCORING, week: int = 0) -> Optional[dict]:
    return _read_cache(_projections_cache_key(position, scoring, week))


# --------------------------------------------------------------------------
# Dataset: INJURIES (/nfl/injuries) - league-wide, no position/season params
# --------------------------------------------------------------------------

def _normalize_injuries(raw: dict) -> dict:
    normalized = []
    for p in raw.get("injuries", []) or []:
        normalized.append({
            "fp_player_id": p.get("player_id"),
            "yahoo_id": p.get("yahoo_id"),
            "name": p.get("name"),
            "team": normalize_team(p.get("team_id")),
            "position": normalize_position(p.get("position_id")),
            "status": p.get("status"),
            "status_short": p.get("status_short"),
            "injury_type": p.get("injury_type"),
            "comment": p.get("comment"),
            "updated": p.get("injury_update_date"),
            "probability_of_playing": p.get("probability_of_playing"),
            "_norm_name": normalize_player_name(p.get("name")),
        })
    return {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "endpoint": "/nfl/injuries",
        "season": None, "scoring": None,
        "record_count": len(normalized),
        "fp_tier": None,
        "injuries": normalized,
    }


def refresh_injuries(force: bool = False, allow_soft_limit_override: bool = False) -> dict:
    cached = _read_cache("injuries")
    if not force and not _is_stale(cached, TTL_SECONDS["injuries"]):
        return {"dataset": "injuries", "source": "cache", "record_count": cached.get("record_count")}
    raw = _request("/nfl/injuries", allow_soft_limit_override=allow_soft_limit_override)
    normalized = _normalize_injuries(raw)
    _atomic_write_json(_cache_write_path("injuries"), normalized)
    return {"dataset": "injuries", "source": "live", "record_count": normalized["record_count"]}


def get_injuries_cache() -> Optional[dict]:
    return _read_cache("injuries")


# --------------------------------------------------------------------------
# Dataset: NEWS (/nfl/news) - league-wide feed
# --------------------------------------------------------------------------

def _normalize_news(raw: dict) -> dict:
    normalized = []
    for item in raw.get("items", []) or []:
        normalized.append({
            "news_id": item.get("id"),
            "fp_player_id": item.get("player_id"),
            "team": normalize_team(item.get("team_id")),
            "created": item.get("created"),
            "title": item.get("title"),
            "desc": item.get("desc"),
            "impact": item.get("impact"),
            "link": item.get("link"),
        })
    return {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "endpoint": "/nfl/news",
        "season": None, "scoring": None,
        "record_count": len(normalized),
        "fp_tier": None,
        "news": normalized,
    }


def refresh_news(force: bool = False, allow_soft_limit_override: bool = False) -> dict:
    cached = _read_cache("news")
    if not force and not _is_stale(cached, TTL_SECONDS["news"]):
        return {"dataset": "news", "source": "cache", "record_count": cached.get("record_count")}
    raw = _request("/nfl/news", allow_soft_limit_override=allow_soft_limit_override)
    normalized = _normalize_news(raw)
    _atomic_write_json(_cache_write_path("news"), normalized)
    return {"dataset": "news", "source": "live", "record_count": normalized["record_count"]}


def get_news_cache() -> Optional[dict]:
    return _read_cache("news")


def get_news_for_player(fp_player_id, limit: int = 3) -> List[dict]:
    cache = get_news_cache() or {}
    items = [n for n in cache.get("news", []) if n.get("fp_player_id") == fp_player_id]
    return items[:limit]


# --------------------------------------------------------------------------
# Pre-flight batch quota estimation + bulk refresh dispatcher
# --------------------------------------------------------------------------

def _estimate_batch_requests(wanted: List[str], positions: List[str], scoring: str, force: bool) -> int:
    """Pure local estimate (zero API cost) of how many live calls this
    exact batch would need right now, given current cache staleness and
    the force flag. Used to refuse an under-quota batch BEFORE starting,
    rather than discovering the shortfall mid-refresh."""
    estimate = 0
    if "players" in wanted and _dataset_needs_live_call("players", TTL_SECONDS["players"], force):
        estimate += 1
    if "rankings" in wanted:
        for pos in positions:
            if _dataset_needs_live_call(_rankings_cache_key(pos, scoring), TTL_SECONDS["rankings"], force):
                estimate += 1
    if "projections" in wanted:
        for pos in positions:
            if _dataset_needs_live_call(_projections_cache_key(pos, scoring, 0), TTL_SECONDS["projections"], force):
                estimate += 1
    if "injuries" in wanted and _dataset_needs_live_call("injuries", TTL_SECONDS["injuries"], force):
        estimate += 1
    if "news" in wanted and _dataset_needs_live_call("news", TTL_SECONDS["news"], force):
        estimate += 1
    return estimate


def refresh_selected(datasets: Optional[List[str]] = None, force: bool = False,
                      allow_soft_limit_override: bool = False, scoring: str = DEFAULT_SCORING,
                      positions: Optional[List[str]] = None) -> dict:
    """Refresh some or all datasets. Performs a pre-flight estimate of the
    live requests this batch could require; refuses to start at all if that
    estimate exceeds today's remaining quota, and requires
    allow_soft_limit_override=True if it would cross the soft limit while
    staying under the hard limit."""
    wanted = datasets or DATASET_NAMES
    positions = positions or CORE_POSITIONS

    usage_before = get_usage_summary()
    used = usage_before["requests_used_today"]
    remaining = usage_before["estimated_requests_remaining_today"]
    estimated = _estimate_batch_requests(wanted, positions, scoring, force)

    if estimated > remaining:
        return {
            "error": "quota_insufficient",
            "message": f"This refresh could require up to {estimated} live requests, but only "
                       f"{remaining} remain today (used {used}/{DAILY_REQUEST_LIMIT}). "
                       f"Refusing to begin a partial refresh.",
            "requests_used_today": used,
            "requests_remaining_today": remaining,
            "estimated_requests_required": estimated,
            "requested_datasets": wanted,
            "daily_soft_limit": DAILY_SOFT_LIMIT, "daily_hard_limit": DAILY_REQUEST_LIMIT,
        }

    if used + estimated > DAILY_SOFT_LIMIT and not allow_soft_limit_override:
        return {
            "error": "quota_soft_limit_batch",
            "message": f"This refresh could require up to {estimated} live requests, which would push "
                       f"today's usage from {used} to {used + estimated}, crossing the soft limit of "
                       f"{DAILY_SOFT_LIMIT} (hard limit {DAILY_REQUEST_LIMIT}). "
                       f"Pass allow_soft_limit_override=True to proceed.",
            "requests_used_today": used,
            "requests_remaining_today": remaining,
            "estimated_requests_required": estimated,
            "requested_datasets": wanted,
            "daily_soft_limit": DAILY_SOFT_LIMIT, "daily_hard_limit": DAILY_REQUEST_LIMIT,
        }

    refreshed, served_from_cache, failures = [], [], []

    def _run(dataset_label, fn, *args, **kwargs):
        try:
            r = fn(*args, **kwargs)
            (refreshed if r["source"] == "live" else served_from_cache).append(r["dataset"])
        except FantasyProsQuotaError as e:
            failures.append({"dataset": dataset_label, "error": "quota_guard", **e.details})
        except FantasyProsRateLimitExhaustedError as e:
            failures.append({"dataset": dataset_label, "error": "rate_limited", **e.details})
        except FantasyProsConfigError as e:
            failures.append({"dataset": dataset_label, "error": "configuration_error", "message": str(e)})
        except Exception as e:
            failures.append({"dataset": dataset_label, "error": "request_failed", "message": str(e)})

    if "players" in wanted:
        _run("players", refresh_players, force=force, allow_soft_limit_override=allow_soft_limit_override)

    if "rankings" in wanted:
        for pos in positions:
            _run(f"rankings_{pos}", refresh_rankings, pos, scoring, force=force,
                 allow_soft_limit_override=allow_soft_limit_override)

    if "projections" in wanted:
        for pos in positions:
            _run(f"projections_{pos}", refresh_projections, pos, scoring, week=0, force=force,
                 allow_soft_limit_override=allow_soft_limit_override)

    if "injuries" in wanted:
        _run("injuries", refresh_injuries, force=force, allow_soft_limit_override=allow_soft_limit_override)

    if "news" in wanted:
        _run("news", refresh_news, force=force, allow_soft_limit_override=allow_soft_limit_override)

    usage_after = get_usage_summary()
    delta = usage_after["requests_used_today"] - used

    return {
        "datasets_refreshed_live": refreshed,
        "datasets_served_from_cache": served_from_cache,
        "failures": failures,
        "request_count_this_call": delta,
        "requests_used_today": usage_after["requests_used_today"],
        "estimated_requests_remaining_today": usage_after["estimated_requests_remaining_today"],
        "daily_soft_limit": DAILY_SOFT_LIMIT,
        "daily_hard_limit": DAILY_REQUEST_LIMIT,
    }


# --------------------------------------------------------------------------
# ADP (derived from the PLAYERS cache - rankings do NOT contain true ADP)
# --------------------------------------------------------------------------

def get_adp_list(position: str, scoring: str = DEFAULT_SCORING, limit: Optional[int] = None) -> dict:
    cache = get_players_cache()
    if not cache:
        return {"error": "cache_missing", "message": "No cached players data. Call refresh_fantasypros_cache first."}
    adp_field = "rank_adp_ppr" if scoring.upper() == "PPR" else "rank_adp"
    pos = position.upper()
    rows = [p for p in cache.get("players", []) if p.get("position") == pos and p.get(adp_field)]
    rows.sort(key=lambda p: p[adp_field])
    if limit:
        rows = rows[:limit]
    return {
        "position": pos, "scoring": scoring.upper(), "adp_field_used": adp_field,
        "fetched_at": cache.get("fetched_at"), "record_count": len(rows),
        "players": [{"name": r["name"], "team": r["team"], "adp": r[adp_field],
                     "fp_player_id": r["fp_player_id"]} for r in rows],
        "note": "ADP sourced from /nfl/players (rank_adp/rank_adp_ppr) - "
                "the consensus-rankings endpoint only contains ownership %, not true ADP.",
    }


# --------------------------------------------------------------------------
# Player identity matching (fallback: normalized name + team + position)
# --------------------------------------------------------------------------

def match_player(name: str, team: Optional[str] = None, position: Optional[str] = None) -> dict:
    """Match an ESPN player against the FantasyPros players cache.

    match_method: exact_name_team_position | name_position_only |
      name_only_single_candidate | ambiguous_multiple_candidates |
      no_match | cache_missing

    match_confidence: high | medium | low | ambiguous | none

    NEVER silently resolves an ambiguous match - callers must inspect
    `candidates` when match_method == "ambiguous_multiple_candidates"."""
    cache = get_players_cache()
    if not cache:
        return {"match_method": "cache_missing", "match_confidence": "none", "candidates": []}

    norm_name = normalize_player_name(name)
    norm_team = normalize_team(team) if team else None
    norm_pos = normalize_position(position) if position else None

    candidates = [p for p in cache.get("players", []) if p.get("_norm_name") == norm_name]

    if not candidates:
        return {"match_method": "no_match", "match_confidence": "none", "candidates": []}

    if norm_team and norm_pos:
        exact = [c for c in candidates if c.get("team") == norm_team and c.get("position") == norm_pos]
        if len(exact) == 1:
            return {"match_method": "exact_name_team_position", "match_confidence": "high",
                     "candidates": [exact[0]]}

    if norm_pos:
        pos_matches = [c for c in candidates if c.get("position") == norm_pos]
        if len(pos_matches) == 1:
            return {"match_method": "name_position_only", "match_confidence": "medium",
                     "candidates": [pos_matches[0]]}

    if len(candidates) == 1:
        return {"match_method": "name_only_single_candidate", "match_confidence": "medium",
                 "candidates": candidates}

    return {"match_method": "ambiguous_multiple_candidates", "match_confidence": "ambiguous",
             "candidates": candidates}


# --------------------------------------------------------------------------
# Compact combined player intelligence (cache-only; zero live calls)
# --------------------------------------------------------------------------

def build_player_intelligence(name: str, team: Optional[str] = None, position: Optional[str] = None,
                               scoring: str = DEFAULT_SCORING) -> dict:
    match = match_player(name, team, position)
    if match["match_confidence"] in ("none", "ambiguous") or not match["candidates"]:
        return {
            "query": {"name": name, "team": team, "position": position},
            "match_method": match["match_method"], "match_confidence": match["match_confidence"],
            "candidates": match["candidates"],
            "message": "No confident single match - resolve ambiguity before using this result."
                        if match["match_confidence"] == "ambiguous" else "No match found in cached players data.",
        }

    base = match["candidates"][0]
    fp_id = base.get("fp_player_id")
    pos = base.get("position")

    rankings_cache = get_rankings_cache(pos, scoring) if pos else None
    ranking_row = None
    if rankings_cache:
        ranking_row = next((r for r in rankings_cache.get("players", []) if r.get("fp_player_id") == fp_id), None)

    projections_cache = get_projections_cache(pos, scoring, week=0) if pos else None
    projection_row = None
    if projections_cache:
        projection_row = next((r for r in projections_cache.get("players", []) if r.get("fp_player_id") == fp_id), None)

    injuries_cache = get_injuries_cache()
    injury_row = None
    if injuries_cache:
        injury_row = next((r for r in injuries_cache.get("injuries", []) if r.get("fp_player_id") == fp_id), None)

    news_items = get_news_for_player(fp_id, limit=3) if fp_id else []

    return {
        "query": {"name": name, "team": team, "position": position},
        "match_method": match["match_method"], "match_confidence": match["match_confidence"],
        "fp_player_id": fp_id,
        "name": base.get("name"), "team": base.get("team"), "position": pos,
        "ecr": (ranking_row or {}).get("rank_ecr"),
        "pos_rank": (ranking_row or {}).get("pos_rank"),
        "tier": (ranking_row or {}).get("tier"),
        "rank_min": (ranking_row or {}).get("rank_min"),
        "rank_max": (ranking_row or {}).get("rank_max"),
        "rank_std": (ranking_row or {}).get("rank_std"),
        "bye_week": (ranking_row or {}).get("bye_week"),
        "espn_ownership_pct": (ranking_row or {}).get("espn_ownership_pct"),
        "adp": base.get("rank_adp_ppr") if scoring.upper() == "PPR" else base.get("rank_adp"),
        "projected_points": (projection_row or {}).get("projected_points"),
        "injury_status": (injury_row or {}).get("status"),
        "injury_comment": (injury_row or {}).get("comment"),
        "recent_news": news_items,
        "cache_timestamps": {
            "players": (get_players_cache() or {}).get("fetched_at"),
            "rankings": (rankings_cache or {}).get("fetched_at"),
            "projections": (projections_cache or {}).get("fetched_at"),
            "injuries": (injuries_cache or {}).get("fetched_at"),
            "news": (get_news_cache() or {}).get("fetched_at"),
        },
    }


def compare_players_from_cache(names: List[str], teams: Optional[List[Optional[str]]] = None,
                                positions: Optional[List[Optional[str]]] = None,
                                scoring: str = DEFAULT_SCORING) -> dict:
    """Compares 2-4 players entirely from cache. No live FantasyPros
    'compare' endpoint was found in the public v2 API surface validated
    this session (only /players, /consensus-rankings, /projections,
    /injuries, /news were confirmed) - so this always runs from cache."""
    teams = teams or [None] * len(names)
    positions = positions or [None] * len(names)
    results = [build_player_intelligence(n, t, p, scoring) for n, t, p in zip(names, teams, positions)]
    return {
        "source": "cache_only",
        "live_api_calls_consumed": 0,
        "note": "No confirmed FantasyPros 'compare' endpoint exists on the validated public v2 API "
                "surface; comparison is built from cached players/rankings/projections/injuries/news.",
        "players": results,
    }


# --------------------------------------------------------------------------
# Waiver-ranking support: FantasyPros-only signals (no ESPN objects here)
# --------------------------------------------------------------------------

OWNERSHIP_BANDS = [
    (90.0, "extreme"),
    (75.0, "strong"),
    (50.0, "moderate"),
]

def ownership_anomaly_band(espn_ownership_pct: Optional[float]) -> str:
    """Descriptive band for how anomalous it is that a player is a free
    agent here despite global ESPN ownership. 'none' means not anomalous."""
    if espn_ownership_pct is None:
        return "unknown"
    for threshold, band in OWNERSHIP_BANDS:
        if espn_ownership_pct >= threshold:
            return band
    return "none"


TIER_QUALITY_LABELS = {
    1: "elite", 2: "strong", 3: "solid_starter", 4: "flex_caliber",
    5: "depth", 6: "speculative", 7: "deep_speculative",
}

def describe_player_quality(tier: Optional[int]) -> str:
    """Short external-quality label from FantasyPros tier alone. Independent
    of any specific roster - this is about the player, not the fit."""
    if tier is None:
        return "unknown"
    return TIER_QUALITY_LABELS.get(tier, "unranked_deep_bench")


# --------------------------------------------------------------------------
# Cache freshness reporting (reuses existing _is_stale / TTL_SECONDS)
# --------------------------------------------------------------------------

def _dataset_freshness(cache_obj: Optional[dict], ttl_seconds: float) -> dict:
    if not cache_obj or "fetched_at" not in cache_obj:
        return {"fetched_at": None, "age_seconds": None, "is_stale": True,
                 "ttl_seconds": ttl_seconds, "status": "missing"}
    try:
        fetched_at = datetime.datetime.fromisoformat(cache_obj["fetched_at"].replace("Z", "+00:00"))
    except ValueError:
        return {"fetched_at": cache_obj.get("fetched_at"), "age_seconds": None, "is_stale": True,
                 "ttl_seconds": ttl_seconds, "status": "unreadable_timestamp"}
    age = (datetime.datetime.now(datetime.timezone.utc) - fetched_at).total_seconds()
    stale = age > ttl_seconds
    return {"fetched_at": cache_obj.get("fetched_at"), "age_seconds": round(age),
             "is_stale": stale, "ttl_seconds": ttl_seconds, "status": "stale" if stale else "fresh"}


def get_cache_freshness_report(positions: List[str], scoring_bucket: str = DEFAULT_SCORING) -> dict:
    """Per-dataset freshness for players/rankings/projections/injuries,
    scoring-bucket-aware. Read-only, zero API cost. Stale is reported, not
    enforced - callers decide whether to proceed or refresh first."""
    report = {"players": _dataset_freshness(get_players_cache(), TTL_SECONDS["players"])}
    for pos in positions:
        report[f"rankings_{pos}"] = _dataset_freshness(
            get_rankings_cache(pos, scoring_bucket), TTL_SECONDS["rankings"])
        report[f"projections_{pos}"] = _dataset_freshness(
            get_projections_cache(pos, scoring_bucket, week=0), TTL_SECONDS["projections"])
    report["injuries"] = _dataset_freshness(get_injuries_cache(), TTL_SECONDS["injuries"])
    return report


# --------------------------------------------------------------------------
# Injury signal classification (materially affects confidence, never
# invents medical probabilities - maps observed status strings only)
# --------------------------------------------------------------------------

INJURY_MATERIAL_STATUSES = {"IR", "OUT", "PUP", "SUSPENSION", "NFI"}
INJURY_CAUTION_STATUSES = {"DOUBTFUL", "QUESTIONABLE", "DNP"}

def classify_injury_signal(injury_status: Optional[str]) -> dict:
    """Returns a label + neutral factual note. Never estimates a
    probability of playing - only reflects the status string FantasyPros
    itself reports."""
    if not injury_status:
        return {"label": "healthy_or_no_flag", "note": "No injury designation reported."}
    status_upper = str(injury_status).strip().upper()
    if status_upper in INJURY_MATERIAL_STATUSES:
        return {"label": "materially_reduced",
                 "note": f"Reported status '{injury_status}' materially reduces confidence "
                         f"unless explicitly being added as a stash."}
    if status_upper in INJURY_CAUTION_STATUSES:
        return {"label": "caution",
                 "note": f"Reported status '{injury_status}' warrants caution; availability is uncertain."}
    return {"label": "caution", "note": f"Unrecognized status '{injury_status}' reported; treat as a caution."}


# --------------------------------------------------------------------------
# Multi-signal upgrade assessment (rules-based, no weighted numeric score)
# --------------------------------------------------------------------------

MEANINGFUL_PROJECTION_DELTA = 10.0
STRONG_PROJECTION_DELTA = 40.0

def assess_upgrade_signals(add_intel: dict, drop_intel: dict, roster_utility: str,
                            can_enter_rotation: bool, drop_is_expendable: bool,
                            drop_zero_espn_projection: bool, drop_feasible: bool,
                            scarcity_note: Optional[str] = None) -> dict:
    """Rules-based, no weighted numeric score. A drop with no usable
    FantasyPros numeric evaluation (resolved match, but ecr/projection both
    None - e.g. K positions) does not force insufficient_data; it's flagged
    via drop_external_value_missing and classified on qualitative ESPN-side
    + feasibility evidence alone, with no fabricated delta. Injury signal
    on the ADD can materially cap the direction. Ownership/scarcity are
    corroborating-only - they can upgrade a verdict one notch but never
    rescue a roster-construction-negative verdict."""
    add_conf, drop_conf = add_intel.get("match_confidence"), drop_intel.get("match_confidence")

    if add_conf in ("ambiguous", "none"):
        return {
            "fp_projection_delta": None, "fp_ecr_delta": None, "fp_adp_delta": None, "fp_tier_delta": None,
            "roster_utility": roster_utility, "can_enter_rotation": can_enter_rotation,
            "drop_is_expendable": drop_is_expendable, "drop_external_value_missing": False,
            "positional_scarcity": scarcity_note, "injury_signal": None,
            "direction": "insufficient_data",
            "signal_summary": "Add candidate has an unresolved FantasyPros identity match; "
                               "no numeric or qualitative signals can be trusted.",
        }

    injury = classify_injury_signal(add_intel.get("injury_status"))
    add_proj, drop_proj = add_intel.get("projected_points"), drop_intel.get("projected_points")
    add_ecr, drop_ecr = add_intel.get("ecr"), drop_intel.get("ecr")

    drop_external_value_missing = (drop_conf not in ("ambiguous", "none")
                                     and drop_proj is None and drop_ecr is None)

    if drop_conf in ("ambiguous", "none") or (add_proj is None) or (drop_proj is None and not drop_external_value_missing):
        return {
            "fp_projection_delta": None, "fp_ecr_delta": None, "fp_adp_delta": None, "fp_tier_delta": None,
            "roster_utility": roster_utility, "can_enter_rotation": can_enter_rotation,
            "drop_is_expendable": drop_is_expendable, "drop_external_value_missing": drop_external_value_missing,
            "positional_scarcity": scarcity_note, "injury_signal": injury,
            "direction": "insufficient_data",
            "signal_summary": "Drop candidate has an unresolved FantasyPros identity match or "
                               "the add candidate itself lacks a usable projection.",
        }

    if drop_external_value_missing:
        proj_delta = ecr_delta = adp_delta = tier_delta = None
        if drop_zero_espn_projection and not drop_is_expendable:
            direction = "marginal"
        elif drop_zero_espn_projection and drop_feasible and add_proj and add_proj > 0:
            direction = "strong_upgrade" if (add_intel.get("tier") or 99) <= 4 else "upgrade"
        else:
            direction = "marginal"
        summary = ("Drop candidate has a resolved FantasyPros identity but no usable ECR/projection "
                   "for this position (e.g. K/D-ST are not FantasyPros-enriched here). Classification "
                   "relies on ESPN's zero-projection/non-starter signal and lineup feasibility only - "
                   "no FantasyPros numeric delta is claimed for the drop side.")
    else:
        adp_a, adp_d = add_intel.get("adp"), drop_intel.get("adp")
        tier_a, tier_d = add_intel.get("tier"), drop_intel.get("tier")
        proj_delta = round(add_proj - drop_proj, 2)
        ecr_delta = (drop_ecr - add_ecr) if (add_ecr is not None and drop_ecr is not None) else None
        adp_delta = (adp_d - adp_a) if (adp_a is not None and adp_d is not None) else None
        tier_delta = (tier_d - tier_a) if (tier_a is not None and tier_d is not None) else None

        favorable = sum(1 for d in (proj_delta, ecr_delta, adp_delta, tier_delta) if d is not None and d > 0)
        unfavorable = sum(1 for d in (proj_delta, ecr_delta, adp_delta, tier_delta) if d is not None and d < 0)

        if not drop_is_expendable or not drop_feasible:
            direction = "marginal"
        elif proj_delta >= STRONG_PROJECTION_DELTA and favorable >= 3:
            direction = "strong_upgrade"
        elif proj_delta >= MEANINGFUL_PROJECTION_DELTA and favorable > unfavorable:
            direction = "upgrade"
        elif proj_delta <= -MEANINGFUL_PROJECTION_DELTA and unfavorable > favorable:
            direction = "downgrade"
        else:
            direction = "marginal"
        summary = f"{favorable} favorable signal(s), {unfavorable} unfavorable signal(s) of 4 comparable metrics."

    if injury["label"] == "materially_reduced" and direction in ("strong_upgrade", "upgrade"):
        direction = "marginal"
        summary += " Downgraded from a positive signal set due to a material injury designation on the add."
    elif injury["label"] == "caution" and direction == "strong_upgrade":
        direction = "upgrade"
        summary += " Moderated from strong_upgrade due to an injury caution on the add."

    ownership_band = add_intel.get("_ownership_band")
    if direction == "upgrade" and ownership_band in ("extreme", "strong") and (scarcity_note or "").startswith("Ranked #1"):
        direction = "strong_upgrade"
        summary += f" Corroborated by a {ownership_band} ownership anomaly and #1 positional scarcity."

    return {
        "fp_projection_delta": proj_delta, "fp_ecr_delta": ecr_delta,
        "fp_adp_delta": adp_delta, "fp_tier_delta": tier_delta,
        "roster_utility": roster_utility, "can_enter_rotation": can_enter_rotation,
        "drop_is_expendable": drop_is_expendable, "drop_external_value_missing": drop_external_value_missing,
        "positional_scarcity": scarcity_note, "injury_signal": injury,
        "direction": direction, "signal_summary": summary,
    }
