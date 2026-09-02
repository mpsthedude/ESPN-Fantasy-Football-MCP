"""Project-owned composition for compact ESPN league snapshots.

This module builds the factual, AI-oriented ``get_league_snapshot`` surface
from raw ESPN league payloads that already contain settings, teams, rosters,
standings, and draft detail. HTTP/session behavior remains in
:mod:`espn_transport`; free-agent enrichment remains a separate filtered read.

ESPN's Fantasy endpoints are undocumented. Keep these mappings fixture-tested
and change them deliberately when ESPN changes its payload.
"""

from __future__ import annotations

from typing import Any

from espn_league_read import CORE_LEAGUE_VIEWS, build_league_info, build_league_settings
from espn_roster_read import ROSTER_VIEWS, build_all_rosters


SNAPSHOT_VIEWS = tuple(dict.fromkeys((*CORE_LEAGUE_VIEWS, *ROSTER_VIEWS, "mDraftDetail")))


class ESPNSnapshotPayloadError(ValueError):
    """The ESPN response did not contain the expected snapshot shape."""


def _require_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ESPNSnapshotPayloadError("ESPN returned an unexpected snapshot payload")
    return payload


def _raw_teams(payload: dict) -> list[dict]:
    teams = payload.get("teams")
    if not isinstance(teams, list):
        raise ESPNSnapshotPayloadError("ESPN snapshot payload is missing teams")
    return [team for team in teams if isinstance(team, dict)]


def _team_name(team: dict) -> str:
    name = team.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    location = str(team.get("location") or "Unknown").strip()
    nickname = str(team.get("nickname") or "Unknown").strip()
    return f"{location} {nickname}".strip()


def _overall_record(team: dict) -> dict:
    record = team.get("record") or {}
    overall = record.get("overall") or {}
    return overall if isinstance(overall, dict) else {}


def _legacy_rank(team: dict) -> int | None:
    """Mirror Team.final_standing-or-standing used by the legacy snapshot."""
    final = team.get("rankCalculatedFinal")
    standing = team.get("playoffSeed")
    value = final or standing
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _build_snapshot_standings(payload: dict) -> list[dict]:
    teams = _raw_teams(payload)

    # espn-api's League.standings() sorts by rankCalculatedFinal when non-zero,
    # otherwise playoffSeed. Keep payload order as the final deterministic tie
    # breaker, matching Python's stable sort used by the wrapper.
    ordered = sorted(
        enumerate(teams),
        key=lambda pair: (
            _legacy_rank(pair[1]) is None,
            _legacy_rank(pair[1]) if _legacy_rank(pair[1]) is not None else 10**9,
            pair[0],
        ),
    )

    rows = []
    for _, team in ordered:
        overall = _overall_record(team)
        rows.append(
            {
                "team_id": team.get("id"),
                "team_name": _team_name(team),
                "rank": _legacy_rank(team),
                "wins": overall.get("wins", 0),
                "losses": overall.get("losses", 0),
                "points_for": overall.get("pointsFor", 0),
                "points_against": round(overall.get("pointsAgainst", 0) or 0, 2),
            }
        )
    return rows


def _draft_completed(payload: dict) -> bool:
    """Mirror ``bool(league.draft)``, not merely ESPN's drafted flag."""
    detail = payload.get("draftDetail")
    if detail is None:
        return False
    if not isinstance(detail, dict):
        raise ESPNSnapshotPayloadError("ESPN snapshot payload has invalid draftDetail")
    picks = detail.get("picks")
    if picks is None:
        return False
    if not isinstance(picks, list):
        raise ESPNSnapshotPayloadError("ESPN snapshot payload has invalid draft picks")
    return bool(picks)


def build_league_snapshot_base(payload: Any, league_id: int, year: int) -> dict:
    """Build the non-free-agent portion of the legacy league snapshot."""
    payload = _require_dict(payload)

    info = build_league_info(payload, year)
    settings = build_league_settings(payload, league_id, year)
    roster_bundle = build_all_rosters(payload, league_id, year, detailed=False)

    # espn-api sorts League.teams by team_id before the snapshot walks it.
    roster_teams = sorted(
        roster_bundle.get("teams") or [],
        key=lambda team: (
            team.get("team_id") is None,
            team.get("team_id") if isinstance(team.get("team_id"), int) else 10**9,
        ),
    )
    rosters = []
    for team in roster_teams:
        rosters.append(
            {
                "team_id": team.get("team_id"),
                "team_name": team.get("team_name"),
                "wins": team.get("wins", 0),
                "losses": team.get("losses", 0),
                "roster": [
                    {
                        "name": player.get("name"),
                        "position": player.get("position"),
                        "proTeam": player.get("proTeam"),
                        "projected_points": player.get("projected_points"),
                    }
                    for player in team.get("roster") or []
                    if isinstance(player, dict)
                ],
            }
        )

    return {
        "league_id": league_id,
        "year": year,
        "league_name": settings.get("league_name"),
        "current_week": info.get("current_week"),
        "scoring_type": settings.get("scoring_type"),
        "roster_slot_counts": settings.get("roster_slot_counts") or {},
        "scoring_rules": settings.get("scoring_rules") or [],
        "standings": _build_snapshot_standings(payload),
        "rosters": rosters,
        "draft_completed": _draft_completed(payload),
    }
