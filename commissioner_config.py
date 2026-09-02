"""
commissioner_config.py

Commissioner READ/AUDIT eligibility configuration - PHASE C1.

Pure configuration module, deliberately mirroring league_registry.py's
style: file path, JSON loading, schema validation, and a resolver. Contains
NO ESPN network logic, NO FantasyPros logic, and NO write-capability
concepts of any kind.

CRITICAL SECURITY/SCOPE BOUNDARY: an entry existing here means ONLY "this
league is eligible for commissioner READ/AUDIT tools." It NEVER means ESPN
write permission has been granted, verified, or is even relevant here.
Future write capability (C10+) requires an entirely separate authorization
system - this module must never grow write-related fields.

Deliberately SEPARATE from league_registry.json (per explicit design
decision): the navigation registry is about "which leagues can I browse
one-at-a-time," a completely different concern from "which league(s) is
the user authorized to run commissioner audits against." Conflating the
two would make a future navigation-registry change accidentally grant or
revoke commissioner eligibility, which must never happen implicitly.
"""

import os
import json

import app_config

CONFIG_FILENAME = "commissioner_config.json"
# D3D-B: LEGACY_CONFIG_PATH is the pre-D3D-B source-relative location,
# preserved exactly as before for read-fallback purposes only. New writes
# never target this path (this module has no write capability at all).
# The new authoritative default location is app_config.get_commissioner_config_path().
LEGACY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)
CONFIG_PATH = LEGACY_CONFIG_PATH  # kept as an alias; no external caller references this name

SCHEMA_VERSION = 1

# Same defensive secret scan convention as league_registry.py - this file
# must never contain credentials, regardless of what a future edit adds.
_SECRET_LIKE_KEYS = {
    "espn_s2", "swid", "cookie", "password", "token", "secret",
    "api_key", "apikey", "authorization",
}

# Defense in depth: reject any key that looks like it is trying to encode a
# write-permission concept, a verified ESPN role, or duplicated
# navigation-registry state. This config grants READ eligibility ONLY.
_OUT_OF_SCOPE_KEYS = {
    "write_authorized", "write_permission", "write_enabled", "can_write",
    "commissioner_role", "is_commissioner", "verified_commissioner",
    "role", "permissions", "capabilities", "my_team_id", "scoring",
    "team_count", "default_league",
}


class CommissionerConfigError(Exception):
    """Configuration-level failure: invalid JSON, unsupported version,
    missing/invalid schema fields, a secret-like key, or an out-of-scope
    (write/role) key found anywhere in the file. Distinct from a
    per-request resolution outcome (unknown alias, disabled league, etc.),
    which callers surface as a structured tool error, not this exception."""
    pass


def _normalize_alias(raw) -> str:
    """Lowercase + trim only - matches league_registry.py's convention."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _scan_for_secret_keys(obj, path="root") -> list:
    """Recursively walks the ENTIRE parsed JSON structure and returns
    human-readable violation strings for any dict key that
    case-insensitively matches a secret-like name. Never echoes values."""
    violations = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.strip().lower() in _SECRET_LIKE_KEYS:
                violations.append(f"secret-like key '{k}' found at {path}.{k} - "
                                    f"credentials must never be stored in commissioner_config.json")
            violations.extend(_scan_for_secret_keys(v, path=f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            violations.extend(_scan_for_secret_keys(item, path=f"{path}[{i}]"))
    return violations


def validate_config(data) -> list:
    """Pure validation function. Returns a list of error strings; an empty
    list means the config is valid. Never raises - load_config() decides
    whether to raise CommissionerConfigError."""
    errors = []

    errors.extend(_scan_for_secret_keys(data))

    if not isinstance(data, dict):
        errors.append("commissioner_config root must be a JSON object")
        return errors

    version = data.get("version")
    if version != SCHEMA_VERSION:
        errors.append(f"unsupported or missing 'version' (expected {SCHEMA_VERSION}, got {version!r})")

    leagues = data.get("leagues")
    if not isinstance(leagues, dict):
        errors.append("'leagues' must be a JSON object mapping alias -> league entry")
        leagues = {}

    seen_enabled_league_ids = {}
    for alias_key, entry in leagues.items():
        if not isinstance(alias_key, str) or not alias_key.strip():
            errors.append(f"alias key {alias_key!r} must be a non-empty string")
            continue

        if not isinstance(entry, dict):
            errors.append(f"commissioner league entry for alias '{alias_key}' must be a JSON object")
            continue

        present_out_of_scope = _OUT_OF_SCOPE_KEYS & set(entry.keys())
        if present_out_of_scope:
            errors.append(f"commissioner league entry '{alias_key}': out-of-scope key(s) "
                            f"{sorted(present_out_of_scope)} not permitted - this config grants "
                            f"READ eligibility only, never write/role concepts")

        league_id = entry.get("league_id")
        if isinstance(league_id, bool) or not isinstance(league_id, int) or league_id <= 0:
            errors.append(f"commissioner league entry '{alias_key}': 'league_id' must be a positive "
                            f"integer (got {league_id!r})")
            league_id = None

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"commissioner league entry '{alias_key}': 'enabled' must be a boolean "
                            f"if present (got {enabled!r})")
            enabled = False

        if league_id is not None and enabled:
            if league_id in seen_enabled_league_ids:
                errors.append(f"duplicate enabled league_id {league_id} used by both "
                                f"'{seen_enabled_league_ids[league_id]}' and '{alias_key}'")
            else:
                seen_enabled_league_ids[league_id] = alias_key

    return errors


def load_config(path: str = None) -> dict:
    """Loads and validates commissioner_config.json. Raises
    CommissionerConfigError on any malformed-config condition. Returns the
    parsed dict unchanged on success.

    D3D-B path resolution (only when path is NOT supplied - an explicit
    path argument is always used exactly as given, with no fallback of
    any kind, and reports missing-at-that-exact-path if absent):
        1. app_config.get_commissioner_config_path() (new, authoritative) -
           if this exact file exists, it is used, period. A malformed
           file at this location raises CommissionerConfigError below and
           NEVER falls through to the legacy file - presence of the new
           file makes it authoritative.
        2. LEGACY_CONFIG_PATH - used only when the new file does not
           exist at all.
        3. Neither exists - CommissionerConfigError naming the new
           (expected) location, with the legacy location also mentioned
           for context.
    """
    if path is not None:
        target = path
        if not os.path.exists(target):
            raise CommissionerConfigError(f"commissioner config file not found at {target}")
    else:
        new_path = app_config.get_commissioner_config_path()
        if os.path.exists(new_path):
            target = new_path
        elif os.path.exists(LEGACY_CONFIG_PATH):
            target = LEGACY_CONFIG_PATH
        else:
            raise CommissionerConfigError(
                f"commissioner config file not found at {new_path} "
                f"(legacy path {LEGACY_CONFIG_PATH} also absent)"
            )

    try:
        with open(target, "r", encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as e:
        raise CommissionerConfigError(f"could not read commissioner config file: {e}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise CommissionerConfigError(f"commissioner config file is not valid JSON: {e}")

    errors = validate_config(data)
    if errors:
        raise CommissionerConfigError("commissioner config validation failed:\n  - " + "\n  - ".join(errors))

    return data


def list_enabled_commissioner_leagues(config: dict) -> list:
    """Returns [(alias, entry), ...] for every entry with enabled != False,
    in stable alias-sorted order."""
    leagues = config.get("leagues", {})
    return [(alias, entry) for alias, entry in sorted(leagues.items())
            if entry.get("enabled", True) is not False]


def resolve_commissioner_league(config: dict, alias: str = None, league_id: int = None):
    """Reusable commissioner-league resolver/guard - the SAME guard every
    C1-C9 commissioner tool must call before any ESPN fetch.

    Returns (resolved_alias, entry) on success.

    Raises CommissionerConfigError with one of these exact message
    prefixes on failure (callers map these to structured tool error codes):
        "not_configured"      - alias/league_id unknown OR configured but disabled
        "mismatch"             - alias and league_id both supplied but resolve
                                  to two different configured leagues
        "target_required"      - neither supplied AND more than one enabled
                                  commissioner league exists
    (exactly zero enabled commissioner leagues also raises "not_configured",
    since there is nothing eligible to select by default)

    Deliberately does NOT fall back to any navigation-registry default -
    commissioner selection is its own namespace/security boundary, never
    inherited from league_registry.json's default_league.
    """
    leagues = config.get("leagues", {})
    enabled = dict(list_enabled_commissioner_leagues(config))

    def _by_alias(a):
        normalized = _normalize_alias(a)
        entry = enabled.get(normalized)
        return (normalized, entry) if entry is not None else (None, None)

    def _by_league_id(lid):
        for a, entry in enabled.items():
            if entry.get("league_id") == lid:
                return a, entry
        return None, None

    if alias is not None and league_id is not None:
        alias_norm, alias_entry = _by_alias(alias)
        if alias_entry is None:
            raise CommissionerConfigError(f"not_configured: alias '{alias}' is not an enabled "
                                            f"commissioner league")
        id_alias_norm, id_entry = _by_league_id(league_id)
        if id_entry is None:
            raise CommissionerConfigError(f"not_configured: league_id {league_id} is not an "
                                            f"enabled commissioner league")
        if alias_norm != id_alias_norm:
            raise CommissionerConfigError(f"mismatch: alias '{alias}' resolves to '{alias_norm}' but "
                                            f"league_id {league_id} resolves to '{id_alias_norm}' - "
                                            f"these must identify the same commissioner league")
        return alias_norm, alias_entry

    if alias is not None:
        alias_norm, alias_entry = _by_alias(alias)
        if alias_entry is None:
            raise CommissionerConfigError(f"not_configured: alias '{alias}' is not an enabled "
                                            f"commissioner league")
        return alias_norm, alias_entry

    if league_id is not None:
        id_alias_norm, id_entry = _by_league_id(league_id)
        if id_entry is None:
            raise CommissionerConfigError(f"not_configured: league_id {league_id} is not an "
                                            f"enabled commissioner league")
        return id_alias_norm, id_entry

    # Neither alias nor league_id supplied.
    if len(enabled) == 0:
        raise CommissionerConfigError("not_configured: no enabled commissioner league is configured")
    if len(enabled) == 1:
        only_alias, only_entry = next(iter(enabled.items()))
        return only_alias, only_entry
    raise CommissionerConfigError(f"target_required: {len(enabled)} enabled commissioner leagues "
                                    f"are configured ({sorted(enabled.keys())}) - alias or league_id "
                                    f"must be specified")
