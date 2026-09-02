"""Project-owned parsing for ESPN Fantasy Football free-agent/waiver reads.

The legacy ``espn-api`` free_agents() path performs a filtered player query,
then additional pro-schedule and positional-rating requests to build BoxPlayer
objects. The MCP only exposes player identity, position/team, week points,
opponent/bye state, and injury state, so this module parses exactly those
fields from ESPN's raw player and pro-team-schedule payloads.

ESPN's Fantasy API is undocumented. Treat these request/payload shapes as an
integration contract and keep them fixture-tested.
"""

from __future__ import annotations

from typing import Any

from espn_reference import POSITION_MAP, PRO_TEAM_MAP


FREE_AGENT_CONTEXT_VIEWS = ("mSettings",)
FREE_AGENT_VIEWS = ("kona_player_info",)
PRO_SCHEDULE_VIEWS = ("proTeamSchedules_wl",)


class ESPNFreeAgentPayloadError(ValueError):
    """ESPN returned a free-agent or pro-schedule payload we cannot parse."""


def build_free_agent_filter(size: int, position: str | None = None) -> dict:
    """Reproduce espn-api 0.46.x's free-agent filter contract exactly."""
    slot_filter = []
    if position and position in POSITION_MAP:
        slot_filter = [POSITION_MAP[position]]
    return {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": slot_filter},
            "limit": size,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "sortDraftRanks": {
                "sortPriority": 100,
                "sortAsc": True,
                "value": "STANDARD",
            },
        }
    }


def resolve_free_agent_week(context_payload: Any, requested_week: int | None) -> int:
    """Mirror legacy default-week behavior, including preseason week-1 fallback."""
    if requested_week not in (None, 0, False):
        if isinstance(requested_week, bool) or not isinstance(requested_week, int):
            raise ValueError("week must be an integer")
        return requested_week

    if not isinstance(context_payload, dict):
        raise ESPNFreeAgentPayloadError("ESPN returned an unexpected league context payload")
    scoring_period = context_payload.get("scoringPeriodId", 0) or 0
    status = context_payload.get("status") or {}
    final = status.get("finalScoringPeriod")
    try:
        week = int(scoring_period)
    except (TypeError, ValueError):
        week = 0
    if isinstance(final, int) and week > final:
        week = final
    return week if week >= 1 else 1


def _player_dict(entry: dict) -> dict:
    player = entry.get("player")
    if isinstance(player, dict):
        return player
    pool = entry.get("playerPoolEntry")
    if isinstance(pool, dict) and isinstance(pool.get("player"), dict):
        return pool["player"]
    raise ESPNFreeAgentPayloadError("ESPN free-agent entry is missing player data")


def _position(player: dict) -> str | None:
    name = str(player.get("fullName") or "")
    for slot_id in player.get("eligibleSlots") or []:
        label = POSITION_MAP.get(slot_id)
        if label is None:
            continue
        if (slot_id != 25 and "/" not in label) or "/" in name:
            return label
    return POSITION_MAP.get(player.get("defaultPositionId"))


def _week_points_and_team(player: dict, year: int, week: int) -> tuple[float, float, Any]:
    """Return actual points, projected points, and week-correct pro-team id."""
    points = 0
    projected = 0
    pro_team_id = player.get("proTeamId")
    for stat in player.get("stats") or []:
        if not isinstance(stat, dict):
            continue
        if stat.get("seasonId") != year or stat.get("statSplitTypeId") == 2:
            continue
        if stat.get("scoringPeriodId") != week:
            continue
        source = stat.get("statSourceId")
        value = round(stat.get("appliedTotal", 0) or 0, 2)
        if source == 0:
            points = value
            if stat.get("proTeamId", 0) != 0:
                pro_team_id = stat["proTeamId"]
        else:
            projected = value
    return points, projected, pro_team_id


def build_pro_schedule(schedule_payload: Any, week: int) -> dict[int, int]:
    """Map pro-team id -> opponent id for teams that actually play this week."""
    if not isinstance(schedule_payload, dict):
        raise ESPNFreeAgentPayloadError("ESPN returned an unexpected pro-schedule payload")
    settings = schedule_payload.get("settings") or {}
    pro_teams = settings.get("proTeams")
    if not isinstance(pro_teams, list):
        raise ESPNFreeAgentPayloadError("ESPN pro-schedule payload is missing proTeams")

    result: dict[int, int] = {}
    for team in pro_teams:
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        if not isinstance(team_id, int) or team_id == 0:
            continue
        games_by_period = team.get("proGamesByScoringPeriod") or {}
        games = games_by_period.get(str(week)) if isinstance(games_by_period, dict) else None
        if not isinstance(games, list) or not games:
            continue
        game = games[0]
        if not isinstance(game, dict):
            continue
        away = game.get("awayProTeamId")
        home = game.get("homeProTeamId")
        if team_id == away and isinstance(home, int):
            result[team_id] = home
        elif team_id == home and isinstance(away, int):
            result[team_id] = away
    return result


def build_free_agents(player_payload: Any, schedule_payload: Any, year: int, week: int, *, include_internal: bool = False) -> list[dict]:
    """Build the stable MCP free-agent rows from raw ESPN payloads."""
    if not isinstance(player_payload, dict):
        raise ESPNFreeAgentPayloadError("ESPN returned an unexpected free-agent payload")
    players = player_payload.get("players")
    if not isinstance(players, list):
        raise ESPNFreeAgentPayloadError("ESPN free-agent payload is missing players")
    if not players:
        return []

    pro_schedule = build_pro_schedule(schedule_payload, week)
    result = []
    for entry in players:
        if not isinstance(entry, dict):
            continue
        player = _player_dict(entry)
        points, projected, pro_team_id = _week_points_and_team(player, year, week)
        opponent_id = pro_schedule.get(pro_team_id)
        row = {
            "name": player.get("fullName"),
            "position": _position(player),
            "proTeam": PRO_TEAM_MAP.get(pro_team_id),
            "projected_points": projected,
            "points": points,
            "pro_opponent": PRO_TEAM_MAP.get(opponent_id) if opponent_id is not None else None,
            "on_bye_week": pro_team_id not in pro_schedule,
            "injured": player.get("injured", False),
        }
        if include_internal:
            row["_player_id"] = player.get("id")
            row["_injury_status"] = player.get("injuryStatus")
        result.append(row)
    return result
