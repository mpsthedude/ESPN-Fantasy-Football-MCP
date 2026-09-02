"""
app_config.py

Project-owned configuration foundation for the Fantasy Football MCP server.

Owns application-home resolution, the read-only credentials.json loader,
ESPN credential extraction/resolution, SportsGameOdds API-key resolution,
and project-owned paths for registry, commissioner, draft-strategy, and
FantasyPros cache state. It never persists secrets. Any remaining legacy
compatibility fallback is integration-specific and must not be added here.

SECURITY: this module never logs, prints, or includes credential VALUES in
any exception message or return value. Only the credentials file PATH
(never its contents) may appear in error text. This module never defines a
repr-bearing credentials class - callers receive plain dicts, and no
diagnostic here ever stringifies their contents.

CONFIG SCOPE: read-only. This module may resolve and return provider
credentials to trusted callers, but it never saves credentials or writes
credentials.json. Secret persistence remains deliberately out of scope.
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any


class ConfigError(Exception):
    """Raised for malformed/invalid project configuration. Messages must
    never include credential values - only paths/categories are safe to
    include."""
    pass


_APP_HOME_ENV_VAR = "FANTASY_FOOTBALL_MCP_HOME"
_APP_HOME_DIRNAME = ".fantasy-football-mcp"
_CREDENTIALS_FILENAME = "credentials.json"


def get_app_home() -> Path:
    """Resolve the project-owned application home directory.

    Precedence:
        1. FANTASY_FOOTBALL_MCP_HOME environment variable, if defined and
           non-blank after stripping surrounding whitespace. Interior
           whitespace within the path itself is preserved untouched.
        2. Path.home() / ".fantasy-football-mcp"

    Never creates the directory. Never inspects cwd. Never falls back to
    ~/.orcha - that legacy convention remains owned by individual existing
    integrations, not this new abstraction.
    """
    override = os.environ.get(_APP_HOME_ENV_VAR)
    if override is not None and override.strip():
        return Path(os.path.expanduser(override.strip()))
    return Path.home() / _APP_HOME_DIRNAME


def get_credentials_path() -> Path:
    """Return the path to the generic credentials file. Never creates it."""
    return get_app_home() / _CREDENTIALS_FILENAME


def load_credentials(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the generic credentials JSON object.

    - Missing file -> {} (this is normal/expected, not an error).
    - Empty or whitespace-only file -> ConfigError (malformed, not treated
      as a valid empty configuration).
    - Malformed JSON -> ConfigError with a safe, path-only message.
    - Non-object JSON root (list/string/number/etc.) -> ConfigError.
    - Valid JSON object -> returned as-is. Unknown/partial provider keys
      are allowed; no closed schema is enforced in D3B.
    """
    target = path if path is not None else get_credentials_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError(f"Invalid credentials configuration JSON at {target}.")

    if not isinstance(data, dict):
        raise ConfigError(f"Invalid credentials configuration JSON at {target}.")

    return data


def get_fantasypros_api_key_from_credentials(path: Optional[Path] = None) -> Optional[str]:
    """Extract credentials["fantasypros"]["api_key"] from the generic
    credentials file.

    Returns a stripped, non-empty string if present and usable.
    Returns None if the fantasypros section or api_key field is absent,
    null, blank, or not a string.
    Raises ConfigError if the fantasypros section is present but is not a
    JSON object (e.g. a string or list) - that is malformed configuration,
    not simply "not configured".

    Never logs, prints, or includes the api_key value in any exception or
    return value beyond the key itself.
    """
    data = load_credentials(path)
    fp_section = data.get("fantasypros")
    if fp_section is None:
        return None
    if not isinstance(fp_section, dict):
        raise ConfigError(
            "Invalid credentials configuration: the 'fantasypros' section "
            "must be a JSON object."
        )
    api_key = fp_section.get("api_key")
    if not isinstance(api_key, str):
        return None
    stripped = api_key.strip()
    return stripped or None


def get_sportsgameodds_api_key_from_credentials(path: Optional[Path] = None) -> Optional[str]:
    """Extract credentials["sportsgameodds"]["api_key"] from credentials.json.

    Returns a stripped non-empty key when configured, otherwise None. A
    present sportsgameodds section must be a JSON object. Credential values
    are never logged or included in error text.
    """
    data = load_credentials(path)
    section = data.get("sportsgameodds")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(
            "Invalid credentials configuration: the 'sportsgameodds' section "
            "must be a JSON object."
        )
    api_key = section.get("api_key")
    if api_key is None:
        return None
    if not isinstance(api_key, str):
        raise ConfigError(
            "Invalid credentials configuration: 'sportsgameodds.api_key' must be a string."
        )
    stripped = api_key.strip()
    return stripped or None


def resolve_sportsgameodds_api_key(path: Optional[Path] = None):
    """Resolve a SportsGameOdds API key from environment or credentials.json.

    Precedence:
        1. SPORTSGAMEODDS_API_KEY environment variable
        2. credentials["sportsgameodds"]["api_key"]
        3. None

    Returns (api_key, source_label), where source_label is "environment" or
    "project_credentials_file". The key is never logged here.
    """
    raw = os.environ.get("SPORTSGAMEODDS_API_KEY")
    if raw is not None and raw.strip():
        return (raw.strip(), "environment")

    file_key = get_sportsgameodds_api_key_from_credentials(path)
    if file_key is not None:
        return (file_key, "project_credentials_file")
    return None


def get_espn_credentials_from_credentials(path: Optional[Path] = None):
    """Extract credentials["espn"]["espn_s2"]/["swid"] from the generic
    credentials file (D3C).

    Returns (espn_s2, swid) as a stripped, non-empty string pair if both
    are present and usable.
    Returns None if the espn section is absent or empty, or if BOTH
    espn_s2 and swid are absent/null/blank (fully unconfigured is normal).
    Raises ConfigError if:
        - the espn section is present but is not a JSON object, or
        - espn_s2 or swid is present but not a string, or
        - only one of espn_s2/swid resolves to a non-blank string (a
          partial pair is a misconfiguration, never silently accepted).

    Never logs, prints, or includes either credential value in any
    exception message.
    """
    data = load_credentials(path)
    espn_section = data.get("espn")
    if espn_section is None:
        return None
    if not isinstance(espn_section, dict):
        raise ConfigError(
            "Invalid credentials configuration: the 'espn' section must be "
            "a JSON object."
        )
    if not espn_section:
        return None

    raw_s2 = espn_section.get("espn_s2")
    raw_swid = espn_section.get("swid")

    if raw_s2 is not None and not isinstance(raw_s2, str):
        raise ConfigError(
            "Invalid credentials configuration: 'espn_s2' must be a string."
        )
    if raw_swid is not None and not isinstance(raw_swid, str):
        raise ConfigError(
            "Invalid credentials configuration: 'swid' must be a string."
        )

    s2 = raw_s2.strip() if isinstance(raw_s2, str) else ""
    swid = raw_swid.strip() if isinstance(raw_swid, str) else ""
    s2_present = bool(s2)
    swid_present = bool(swid)

    if s2_present and swid_present:
        return (s2, swid)
    if not s2_present and not swid_present:
        return None
    raise ConfigError(
        "ESPN credentials configuration must provide both espn_s2 and swid."
    )


def resolve_espn_credentials(path: Optional[Path] = None):
    """Resolve ESPN credentials for public/local-first use (D3C).

    Returns (espn_s2, swid, source_label) where source_label is one of
    "environment" / "project_credentials_file", or None if nothing is
    configured anywhere.

    Precedence:
        1. ESPN_S2 + ESPN_SWID environment variables, as a PAIR. If both
           are present and non-blank, they win outright and the
           credentials file is never even consulted.
        2. The generic project credentials file (see
           get_espn_credentials_from_credentials).
        3. None (nothing configured - this is the normal, expected case
           for most users and callers must treat it exactly like "no
           configuration" today).

    A partial environment pair (only ESPN_S2 or only ESPN_SWID set) is a
    misconfiguration and raises ConfigError immediately - it is NEVER
    combined with a value from the credentials file. Credential pairs
    always come from exactly one source.

    Never logs, prints, or includes either credential value in any
    exception message. No Windows Registry behavior. No ~/.orcha fallback
    - that legacy convention is intentionally not part of this resolver.
    """
    raw_s2 = os.environ.get("ESPN_S2")
    raw_swid = os.environ.get("ESPN_SWID")
    s2 = raw_s2.strip() if raw_s2 is not None else ""
    swid = raw_swid.strip() if raw_swid is not None else ""
    s2_present = bool(s2)
    swid_present = bool(swid)

    if s2_present and swid_present:
        return (s2, swid, "environment")
    if s2_present != swid_present:
        raise ConfigError(
            "ESPN_S2 and ESPN_SWID environment variables must both be set "
            "together, or both left unset."
        )

    file_pair = get_espn_credentials_from_credentials(path)
    if file_pair is not None:
        return (file_pair[0], file_pair[1], "project_credentials_file")
    return None


def get_league_registry_path() -> Path:
    """New default location for the non-secret league navigation registry
    (D3D-B). Derived exclusively from get_app_home(), so
    FANTASY_FOOTBALL_MCP_HOME automatically redirects it. Never creates
    the directory or file. Legacy source-relative fallback handling
    remains entirely inside league_registry.py - this function knows
    nothing about legacy paths."""
    return get_app_home() / "league_registry.json"


def get_commissioner_config_path() -> Path:
    """New default location for the non-secret commissioner READ/AUDIT
    eligibility config (D3D-B). Derived exclusively from get_app_home().
    Never creates the directory or file. Legacy fallback handling remains
    entirely inside commissioner_config.py."""
    return get_app_home() / "commissioner_config.json"


def get_draft_strategy_dir() -> Path:
    """New default directory for persisted draft-strategy artifacts
    (D3D-B). Derived exclusively from get_app_home(). Intentionally uses
    a plain (non-dot-prefixed) directory name, unlike the legacy
    .draft_strategy/ convention - the leading dot existed only to avoid
    cluttering a source checkout, which no longer applies once this state
    lives in its own dedicated application-home directory. Never creates
    the directory. Legacy fallback handling remains entirely inside
    draft_strategy_store.py."""
    return get_app_home() / "draft_strategy"


def get_sportsgameodds_cache_dir() -> Path:
    """Return the project-owned SportsGameOdds non-secret metadata cache directory.

    Derived exclusively from get_app_home(). The client owns all cache schema,
    filenames, TTLs, and writes; app_config only owns path resolution.
    """
    return get_app_home() / "sgo_cache"


def get_fantasypros_cache_dir() -> Path:
    """New default directory for FantasyPros cache/quota-ledger artifacts
    (D4B). Derived exclusively from get_app_home(), so
    FANTASY_FOOTBALL_MCP_HOME automatically redirects it. Never creates
    the directory, never inspects legacy cache state, and contains no
    FantasyPros-specific schema/filename logic - all per-file naming and
    legacy-fallback handling remains entirely inside fantasypros_client.py."""
    return get_app_home() / "fp_cache"
