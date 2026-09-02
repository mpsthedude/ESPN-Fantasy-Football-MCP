"""ESPN account league discovery and explicit local-registry synchronization.

Discovery is read-only against ESPN. Registry synchronization is an explicit,
local-only write to the project application home and never persists ESPN
credentials. The ESPN fan-profile endpoint is undocumented, so parsing is
intentionally defensive and every candidate league is verified against the
normal ESPN fantasy league read endpoint before it is returned or saved.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import app_config
import league_registry
from espn_transport import ESPNAccessError, ESPNTransport, ESPNTransportError


_MAX_CANDIDATES = 50


def _current_football_year() -> int:
    now = datetime.datetime.now()
    return now.year if now.month >= 7 else now.year - 1


def _normalized_member_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().strip("{}").lower()


def _resolve_credentials() -> tuple[str, str, str]:
    """Resolve credentials without ever exposing their values.

    The production app_config contract uses ESPN_S2 + ESPN_SWID. Quick Desktop
    installations have also historically used SWID, so discovery accepts SWID
    as a compatibility alias when ESPN_SWID is absent. The alias is never
    persisted and never logged.
    """
    raw_s2 = os.environ.get("ESPN_S2")
    raw_primary_swid = os.environ.get("ESPN_SWID")
    raw_legacy_swid = os.environ.get("SWID")

    s2 = raw_s2.strip() if isinstance(raw_s2, str) else ""
    primary_swid = raw_primary_swid.strip() if isinstance(raw_primary_swid, str) else ""
    legacy_swid = raw_legacy_swid.strip() if isinstance(raw_legacy_swid, str) else ""

    if primary_swid and legacy_swid and primary_swid != legacy_swid:
        raise app_config.ConfigError(
            "ESPN_SWID and SWID are both configured but do not match. Keep only one SWID value."
        )
    swid = primary_swid or legacy_swid

    if s2 and swid:
        return s2, swid, "environment"
    if bool(s2) != bool(swid):
        raise app_config.ConfigError(
            "ESPN authentication requires ESPN_S2 plus ESPN_SWID (or the SWID compatibility alias)."
        )

    resolved = app_config.resolve_espn_credentials()
    if resolved is None:
        raise app_config.ConfigError(
            "ESPN credentials are not configured. Set ESPN_S2 and ESPN_SWID (or SWID in Quick Desktop)."
        )
    return resolved


def _candidate_ids_from_payload(payload: Any) -> set[int]:
    """Extract plausible fantasy-football league IDs from shape-drifting fan data."""
    found: set[int] = set()

    def add(value: Any) -> None:
        if isinstance(value, bool):
            return
        try:
            league_id = int(str(value).strip())
        except (TypeError, ValueError):
            return
        if league_id > 0:
            found.add(league_id)

    def walk(node: Any) -> None:
        if len(found) >= _MAX_CANDIDATES:
            return
        if isinstance(node, dict):
            lower = {str(k).lower(): v for k, v in node.items()}
            for key in ("leagueid", "league_id"):
                if key in lower:
                    add(lower[key])

            # Some ESPN preference payloads encode game + league in an ID-like string.
            for key, value in node.items():
                if isinstance(value, str):
                    text = value.lower()
                    if "ffl" in text or "football" in text:
                        for match in re.finditer(r"(?:ffl|football)[^0-9]{0,12}(\d{3,12})", text):
                            add(match.group(1))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            text = node.lower()
            if "ffl" in text or "football" in text:
                for match in re.finditer(r"(?:ffl|football)[^0-9]{0,12}(\d{3,12})", text):
                    add(match.group(1))

    walk(payload)
    return found


def _extract_league_name(data: dict, league_id: int) -> str:
    settings = data.get("settings") or {}
    name = settings.get("name") or data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"ESPN League {league_id}"


def _extract_my_team_name(data: dict, swid: str) -> Optional[str]:
    member_id = _normalized_member_id(swid)
    for team in data.get("teams") or []:
        owners = team.get("owners") or []
        if any(_normalized_member_id(owner) == member_id for owner in owners):
            for key in ("name", "teamName", "abbrev"):
                value = team.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            location = team.get("location")
            nickname = team.get("nickname")
            joined = " ".join(str(v).strip() for v in (location, nickname) if v)
            return joined or None
    return None


def _fetch_fan_profile(transport: ESPNTransport, swid: str) -> Any:
    return transport.fetch_fan_profile(swid)

def _verify_league(
    transport: ESPNTransport,
    league_id: int,
    year: int,
    swid: str,
) -> Optional[dict]:
    try:
        data = transport.fetch_league(
            league_id,
            year,
            views=("mSettings", "mTeam"),
        )
    except ESPNAccessError as exc:
        if exc.status_code in (401, 403, 404):
            return None
        raise

    returned_id = data.get("id") or data.get("leagueId") or league_id
    try:
        returned_id = int(returned_id)
    except (TypeError, ValueError):
        return None
    if returned_id != league_id:
        return None
    return {
        "league_id": league_id,
        "display_name": _extract_league_name(data, league_id),
        "year": year,
        "my_team_name": _extract_my_team_name(data, swid),
    }

def discover_espn_leagues(year: Optional[int] = None) -> dict:
    resolved_year = _current_football_year() if year is None else year
    if isinstance(resolved_year, bool) or not isinstance(resolved_year, int) or not (2000 <= resolved_year <= 2100):
        raise ValueError("year must be an integer between 2000 and 2100")

    espn_s2, swid, source = _resolve_credentials()
    transport = ESPNTransport(espn_s2, swid)

    payload = _fetch_fan_profile(transport, swid)
    candidate_ids = sorted(_candidate_ids_from_payload(payload))[:_MAX_CANDIDATES]

    discovered = []
    for league_id in candidate_ids:
        verified = _verify_league(transport, league_id, resolved_year, swid)
        if verified is not None:
            discovered.append(verified)

    discovered.sort(key=lambda row: (row["display_name"].casefold(), row["league_id"]))
    return {
        "status": "ok",
        "year": resolved_year,
        "credential_source": source,
        "candidate_count": len(candidate_ids),
        "league_count": len(discovered),
        "leagues": discovered,
        "note": (
            "ESPN league discovery uses an undocumented ESPN fan-profile endpoint. "
            "Every returned league was re-verified through the standard fantasy league read endpoint."
        ),
    }


def _slugify(name: str, league_id: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if not slug:
        slug = f"league_{league_id}"
    if not re.match(r"^[a-z0-9_-]+$", slug):
        slug = f"league_{league_id}"
    return slug


def _load_registry_for_sync() -> Optional[dict]:
    try:
        return league_registry.load_registry()
    except league_registry.RegistryError as exc:
        # A truly missing registry is a valid first-run state. Any malformed
        # existing registry must fail closed rather than be overwritten.
        if "registry file not found" in str(exc):
            return None
        raise


def _merge_registry(existing: Optional[dict], discovered: list[dict]) -> tuple[dict, list[dict]]:
    if existing is None:
        merged = {"version": league_registry.SCHEMA_VERSION, "default_league": "", "leagues": {}}
    else:
        merged = json.loads(json.dumps(existing))

    leagues = merged.setdefault("leagues", {})
    by_id = {entry.get("league_id"): alias for alias, entry in leagues.items() if isinstance(entry, dict)}
    changes = []

    for row in discovered:
        league_id = row["league_id"]
        display_name = row["display_name"]
        if league_id in by_id:
            alias = by_id[league_id]
            entry = leagues[alias]
            if not entry.get("display_name") and display_name:
                entry["display_name"] = display_name
                changes.append({"action": "updated_display_name", "alias": alias, "league_id": league_id})
            continue

        base = _slugify(display_name, league_id)
        alias = base
        if alias in leagues:
            alias = f"{base}_{league_id}"
        suffix = 2
        while alias in leagues:
            alias = f"{base}_{league_id}_{suffix}"
            suffix += 1
        leagues[alias] = {"league_id": league_id, "display_name": display_name, "enabled": True}
        by_id[league_id] = alias
        changes.append({"action": "added", "alias": alias, "league_id": league_id, "display_name": display_name})

    if not merged.get("default_league") and leagues:
        merged["default_league"] = sorted(leagues.keys())[0]

    errors = league_registry.validate_registry(merged)
    if errors:
        raise league_registry.RegistryError("merged registry validation failed: " + "; ".join(errors))
    return merged, changes


def _atomic_write_registry(data: dict) -> Path:
    target = app_config.get_league_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix="league_registry.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def sync_espn_leagues(year: Optional[int] = None, confirm: bool = False) -> dict:
    discovery = discover_espn_leagues(year)
    existing = _load_registry_for_sync()
    merged, changes = _merge_registry(existing, discovery["leagues"])
    target = app_config.get_league_registry_path()

    result = {
        "status": "ok",
        "year": discovery["year"],
        "discovered_league_count": discovery["league_count"],
        "changes": changes,
        "change_count": len(changes),
        "registry_path": str(target),
        "write_performed": False,
        "confirmation_required": not confirm,
    }
    if not confirm:
        result["message"] = "Preview only. Call again with confirm=true to write the merged registry."
        return result

    _atomic_write_registry(merged)
    result["write_performed"] = True
    result["confirmation_required"] = False
    result["message"] = "League registry synchronized successfully. Existing aliases and entries were preserved."
    return result


def _safe_error(action: str, exc: Exception) -> dict:
    if isinstance(exc, (ValueError, app_config.ConfigError, league_registry.RegistryError)):
        return {"error": "configuration_error", "message": str(exc)}
    if isinstance(exc, ESPNTransportError):
        return {
            "error": "espn_discovery_request_failed",
            "message": f"Unable to {action} because ESPN did not return a usable response. Credentials may be expired or the undocumented discovery endpoint may have changed.",
        }
    return {"error": "request_failed", "message": f"Unable to {action}."}


def register_espn_league_discovery_tools(mcp) -> None:
    @mcp.tool()
    async def discover_my_espn_leagues(year: Optional[int] = None) -> dict:
        """Discover this authenticated ESPN account's fantasy-football leagues.

        Read-only. Uses configured ESPN cookies, discovers candidate leagues from
        ESPN's fan profile, then verifies every result against the normal ESPN
        fantasy league endpoint. No registry file is required and nothing is
        written. Use this for first-run setup or to find newly joined leagues.
        """
        try:
            return discover_espn_leagues(year)
        except Exception as exc:
            return _safe_error("discover ESPN fantasy leagues", exc)

    @mcp.tool()
    async def sync_my_espn_leagues(year: Optional[int] = None, confirm: bool = False) -> dict:
        """Preview or explicitly sync discovered ESPN leagues into league_registry.json.

        Existing aliases/configuration are preserved. New leagues receive stable
        aliases derived from their ESPN names. Default behavior is preview-only;
        set confirm=true only after the user explicitly asks to update the local
        registry. ESPN credentials are never written to the registry.
        """
        try:
            return sync_espn_leagues(year, confirm=confirm)
        except Exception as exc:
            return _safe_error("synchronize ESPN fantasy leagues", exc)
