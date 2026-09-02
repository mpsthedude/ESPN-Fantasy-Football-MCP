"""
league_registry.py

Multi-League Foundation - PHASE 1 (local, non-secret league registry).

Pure configuration module: registry file path, JSON loading, schema
validation, alias normalization/lookup, league_id lookup, default-league
lookup, and a defensive secret-scan. Contains NO ESPN network logic and NO
FantasyPros logic - those remain in espn_fantasy_server.py / fantasypros_client.py.

This module intentionally contains ZERO commissioner concepts, ZERO role
concepts, and ZERO write-capability concepts. Out of scope for Phase 1.
"""

import os
import json
import re

import app_config

REGISTRY_FILENAME = "league_registry.json"
# D3D-B: LEGACY_REGISTRY_PATH is the pre-D3D-B source-relative location,
# preserved exactly as before for read-fallback purposes only. New writes
# never target this path (this module has no write capability at all).
# The new authoritative default location is app_config.get_league_registry_path().
LEGACY_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), REGISTRY_FILENAME)
REGISTRY_PATH = LEGACY_REGISTRY_PATH  # kept as an alias; no external caller references this name

SCHEMA_VERSION = 1

# Case-insensitive; matched against every dict key anywhere in the parsed
# JSON structure (not just top-level or per-league). Any match is a hard
# configuration-level rejection - the registry must NEVER contain secrets.
_SECRET_LIKE_KEYS = {
    "espn_s2", "swid", "cookie", "password", "token", "secret",
    "api_key", "apikey", "authorization",
}

_ALIAS_PATTERN = re.compile(r"^[a-z0-9_-]+$")

_ACCESS_STATUSES = {
    "accessible", "inaccessible", "authentication_required",
    "season_unavailable", "team_not_resolved", "ambiguous_team_ownership",
    "disabled",
}


class RegistryError(Exception):
    """Configuration-level failure: invalid JSON, unsupported version,
    missing/invalid schema fields, or a secret-like key found anywhere in
    the file. Distinct from an individual league's ESPN access failure,
    which is a per-entry runtime status, not a RegistryError."""
    pass


def _normalize_alias(raw) -> str:
    """Lowercase + trim only. No fuzzy matching by design (spec: 'do not
    over-engineer fuzzy matching')."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _scan_for_secret_keys(obj, path="root") -> list:
    """Recursively walks the ENTIRE parsed JSON structure (any depth,
    dicts and lists) and returns a list of human-readable violation
    strings for every dict key that case-insensitively matches a
    secret-like name. Never echoes the associated value."""
    violations = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.strip().lower() in _SECRET_LIKE_KEYS:
                violations.append(f"secret-like key '{k}' found at {path}.{k} - "
                                    f"credentials must never be stored in the registry")
            violations.extend(_scan_for_secret_keys(v, path=f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            violations.extend(_scan_for_secret_keys(item, path=f"{path}[{i}]"))
    return violations


def validate_registry(data) -> list:
    """Pure validation function. Returns a list of error strings; an
    empty list means the registry is valid. Never raises - callers
    (load_registry) decide whether to raise RegistryError."""
    errors = []

    # Secret scan always runs first, regardless of what else is wrong.
    errors.extend(_scan_for_secret_keys(data))

    if not isinstance(data, dict):
        errors.append("registry root must be a JSON object")
        return errors  # nothing else is checkable

    version = data.get("version")
    if version != SCHEMA_VERSION:
        errors.append(f"unsupported or missing 'version' (expected {SCHEMA_VERSION}, got {version!r})")

    leagues = data.get("leagues")
    if not isinstance(leagues, dict):
        errors.append("'leagues' must be a JSON object mapping alias -> league entry")
        leagues = {}

    seen_league_ids = {}
    for alias_key, entry in leagues.items():
        if not isinstance(alias_key, str) or not alias_key.strip():
            errors.append(f"alias key {alias_key!r} must be a non-empty string")
            continue

        normalized = _normalize_alias(alias_key)
        if alias_key != normalized:
            errors.append(f"alias key '{alias_key}' must already be stored in canonical "
                            f"lowercase/trimmed form (expected '{normalized}')")
        if not _ALIAS_PATTERN.match(normalized):
            errors.append(f"alias '{alias_key}' has invalid format - only lowercase "
                            f"letters, digits, '_' and '-' are allowed")

        if not isinstance(entry, dict):
            errors.append(f"league entry for alias '{alias_key}' must be a JSON object")
            continue

        league_id = entry.get("league_id")
        if isinstance(league_id, bool) or not isinstance(league_id, int) or league_id <= 0:
            errors.append(f"league entry '{alias_key}': 'league_id' must be a positive integer "
                            f"(got {league_id!r})")
        else:
            if league_id in seen_league_ids:
                errors.append(f"duplicate league_id {league_id} used by both "
                                f"'{seen_league_ids[league_id]}' and '{alias_key}'")
            else:
                seen_league_ids[league_id] = alias_key

        display_name = entry.get("display_name")
        if display_name is not None and not isinstance(display_name, str):
            errors.append(f"league entry '{alias_key}': 'display_name' must be a string if present")

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"league entry '{alias_key}': 'enabled' must be a boolean if present "
                            f"(got {enabled!r})")

        # Reject any key outside the intentionally minimal v1 schema surface
        # that looks like duplicated ESPN state or a permission/role concept.
        # (Defense in depth - the secret scan above already catches credential
        # keys anywhere; this additionally flags accidental scope creep.)
        _OUT_OF_SCOPE_KEYS = {
            "role", "commissioner", "my_team_id", "scoring_rules", "roster_slots",
            "team_count", "standings", "current_year", "permissions", "capabilities",
        }
        present_out_of_scope = _OUT_OF_SCOPE_KEYS & set(entry.keys())
        if present_out_of_scope:
            errors.append(f"league entry '{alias_key}': out-of-scope key(s) "
                            f"{sorted(present_out_of_scope)} not permitted in Phase 1 schema")

    default_league = data.get("default_league")
    if not isinstance(default_league, str) or not default_league.strip():
        errors.append("'default_league' must be a non-empty string naming a configured alias")
    else:
        normalized_default = _normalize_alias(default_league)
        if normalized_default not in leagues:
            errors.append(f"'default_league' ('{default_league}') does not match any configured "
                            f"alias in 'leagues'")

    return errors


def load_registry(path: str = None) -> dict:
    """Loads and validates the registry file. Raises RegistryError on any
    malformed-registry condition (invalid JSON, unsupported version,
    missing/invalid fields, or a secret-like key anywhere). Returns the
    parsed dict unchanged on success. Callers that need a registry error
    to NOT break unrelated tools should catch RegistryError explicitly.

    D3D-B path resolution (only when path is NOT supplied - an explicit
    path argument is always used exactly as given, with no fallback of
    any kind, and reports missing-at-that-exact-path if absent):
        1. app_config.get_league_registry_path() (new, authoritative) -
           if this exact file exists, it is used, period. A malformed
           file at this location raises RegistryError below and NEVER
           falls through to the legacy file - presence of the new file
           makes it authoritative.
        2. LEGACY_REGISTRY_PATH - used only when the new file does not
           exist at all.
        3. Neither exists - RegistryError naming the new (expected)
           location, with the legacy location also mentioned for context.
    """
    if path is not None:
        target = path
        if not os.path.exists(target):
            raise RegistryError(f"registry file not found at {target}")
    else:
        new_path = app_config.get_league_registry_path()
        if os.path.exists(new_path):
            target = new_path
        elif os.path.exists(LEGACY_REGISTRY_PATH):
            target = LEGACY_REGISTRY_PATH
        else:
            raise RegistryError(
                f"registry file not found at {new_path} "
                f"(legacy path {LEGACY_REGISTRY_PATH} also absent)"
            )

    try:
        with open(target, "r", encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as e:
        raise RegistryError(f"could not read registry file: {e}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RegistryError(f"registry file is not valid JSON: {e}")

    errors = validate_registry(data)
    if errors:
        raise RegistryError("registry validation failed:\n  - " + "\n  - ".join(errors))

    return data


def list_enabled_leagues(registry: dict) -> list:
    """Returns [(alias, entry), ...] for every entry with enabled != False,
    in stable alias-sorted order."""
    leagues = registry.get("leagues", {})
    return [(alias, entry) for alias, entry in sorted(leagues.items())
            if entry.get("enabled", True) is not False]


def resolve_alias(registry: dict, alias_input: str):
    """Resolves a user-supplied alias (any case/whitespace) against the
    registry. Returns (normalized_alias, entry) on success, or raises
    RegistryError with the list of valid aliases on failure. Does NOT
    fuzzy-match."""
    normalized = _normalize_alias(alias_input)
    leagues = registry.get("leagues", {})
    if not normalized:
        raise RegistryError("alias must be a non-empty string")
    if normalized not in leagues:
        valid = sorted(leagues.keys())
        raise RegistryError(f"unknown alias '{alias_input}' - valid aliases: {valid}")
    return normalized, leagues[normalized]


def resolve_league_id(registry: dict, league_id: int):
    """Resolves a numeric league_id against the registry. Returns
    (alias, entry) on success, or raises RegistryError if no configured
    entry matches (this league_id is simply not registered - the ad-hoc
    explicit-ID path via Tools #1-25 remains fully available regardless)."""
    leagues = registry.get("leagues", {})
    for alias, entry in leagues.items():
        if entry.get("league_id") == league_id:
            return alias, entry
    raise RegistryError(f"league_id {league_id} is not registered")


def get_default_league(registry: dict):
    """Returns (alias, entry) for the registry's default_league. Raises
    RegistryError if the registry itself is malformed enough that no
    default can be resolved (should not happen post-validation, but kept
    defensive for direct callers that skip load_registry)."""
    default_alias = _normalize_alias(registry.get("default_league", ""))
    leagues = registry.get("leagues", {})
    if default_alias not in leagues:
        raise RegistryError(f"default_league '{registry.get('default_league')}' is not a configured alias")
    return default_alias, leagues[default_alias]
