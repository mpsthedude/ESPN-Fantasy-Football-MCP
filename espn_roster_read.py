"""Project-owned parsing for ESPN Fantasy Football roster/player reads.

This module converts raw ESPN ``mTeam``/``mRoster`` payloads into the stable
MCP-facing shapes used by roster, player, and lineup-analysis tools. HTTP/session
behavior remains in :mod:`espn_transport`; this module is pure payload translation.

ESPN's Fantasy endpoint is undocumented. Keep these mappings fixture-tested and
change them deliberately if ESPN changes its response shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Compatibility dictionaries only. Request/session/object behavior is owned by
# this project. These tables preserve the existing espn-api-facing labels while
# the final constant-table migration remains a separate concern.
from espn_reference import PLAYER_STATS_MAP, POSITION_MAP, PRO_TEAM_MAP
from espn_league_read import CommissionerSettings, build_commissioner_settings


ROSTER_VIEWS = ("mTeam", "mRoster")


class ESPNRosterPayloadError(ValueError):
    """The ESPN response did not contain the expected roster payload shape."""


def _require_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ESPNRosterPayloadError("ESPN returned an unexpected roster payload")
    return payload


def _teams(payload: dict) -> list[dict]:
    teams = payload.get("teams")
    if not isinstance(teams, list):
        raise ESPNRosterPayloadError("ESPN roster payload is missing teams")
    return [team for team in teams if isinstance(team, dict)]


def _members_by_id(payload: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for member in payload.get("members") or []:
        if isinstance(member, dict) and member.get("id") is not None:
            result[str(member["id"])] = member
    return result


def _team_name(team: dict) -> str:
    name = team.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    location = str(team.get("location") or "Unknown").strip()
    nickname = str(team.get("nickname") or "Unknown").strip()
    return f"{location} {nickname}".strip()


def _owners(team: dict, members_by_id: dict[str, dict]) -> list[dict]:
    owners = []
    for owner_id in team.get("owners") or []:
        member = members_by_id.get(str(owner_id))
        if member is not None:
            owners.append(member)
    return owners


def _overall_record(team: dict) -> dict:
    record = team.get("record") or {}
    overall = record.get("overall") or {}
    return overall if isinstance(overall, dict) else {}


def _roster_entries(team: dict) -> list[dict]:
    roster = team.get("roster") or {}
    entries = roster.get("entries") if isinstance(roster, dict) else None
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ESPNRosterPayloadError("ESPN team roster entries have an unexpected shape")
    return [entry for entry in entries if isinstance(entry, dict)]


def _raw_player(entry: dict) -> dict:
    pool = entry.get("playerPoolEntry") or {}
    player = pool.get("player") if isinstance(pool, dict) else None
    if not isinstance(player, dict):
        player = entry.get("player")
    if not isinstance(player, dict):
        raise ESPNRosterPayloadError("ESPN roster entry is missing player data")
    return player


def _mapped_stat_key(raw_key: Any) -> Any:
    try:
        key = int(raw_key)
    except (TypeError, ValueError):
        return raw_key
    return PLAYER_STATS_MAP.get(key, raw_key)


def _build_stats(player: dict, year: int) -> dict:
    """Mirror espn-api Player.stats construction from raw ESPN stat rows."""
    result: dict = {}
    for stat in player.get("stats") or []:
        if not isinstance(stat, dict):
            continue
        if stat.get("seasonId") != year or stat.get("statSplitTypeId") == 2:
            continue

        breakdown = {
            _mapped_stat_key(key): value
            for key, value in (stat.get("stats") or {}).items()
        }
        points_breakdown = {
            _mapped_stat_key(key): value
            for key, value in (stat.get("appliedStats") or {}).items()
        }
        points = round(stat.get("appliedTotal", 0) or 0, 2)
        avg_points = round(stat.get("appliedAverage", 0) or 0, 2)
        scoring_period = stat.get("scoringPeriodId")
        stat_source = stat.get("statSourceId")

        if stat_source == 0:
            points_key = "points"
            breakdown_key = "breakdown"
            points_breakdown_key = "points_breakdown"
            avg_key = "avg_points"
        else:
            points_key = "projected_points"
            breakdown_key = "projected_breakdown"
            points_breakdown_key = "projected_points_breakdown"
            avg_key = "projected_avg_points"

        row = result.setdefault(scoring_period, {})
        row[points_key] = points
        row[breakdown_key] = breakdown
        row[points_breakdown_key] = points_breakdown
        row[avg_key] = avg_points
    return result


def _position_from_player(player: dict) -> str | None:
    """Preserve espn-api's primary-position selection from eligible slots."""
    name = str(player.get("fullName") or "")
    for slot_id in player.get("eligibleSlots") or []:
        label = POSITION_MAP.get(slot_id)
        if label is None:
            continue
        if (slot_id != 25 and "/" not in label) or "/" in name:
            return label

    # Defensive fallback for unusual payloads that omit eligibleSlots.
    default_position_id = player.get("defaultPositionId")
    return POSITION_MAP.get(default_position_id)


def parse_roster_entry(entry: dict, year: int) -> dict:
    player = _raw_player(entry)
    stats = _build_stats(player, year)
    pro_team_id = player.get("proTeamId")
    return {
        "player_id": player.get("id"),
        "name": player.get("fullName"),
        "position": _position_from_player(player),
        "proTeam": PRO_TEAM_MAP.get(pro_team_id),
        "points": stats.get(0, {}).get("points", 0),
        "projected_points": stats.get(0, {}).get("projected_points", 0),
        "stats": stats,
        "injured": player.get("injured", False),
        "injury_status": player.get("injuryStatus"),
        "lineup_slot": POSITION_MAP.get(entry.get("lineupSlotId"), ""),
    }


def _all_pro_team_schedules(schedule_payload: Any) -> dict[int, dict]:
    """Mirror espn-api BaseLeague._get_all_pro_schedule from raw season data."""
    payload = _require_dict(schedule_payload)
    settings = payload.get("settings") or {}
    pro_teams = settings.get("proTeams")
    if not isinstance(pro_teams, list):
        raise ESPNRosterPayloadError("ESPN pro-schedule payload is missing proTeams")

    schedules: dict[int, dict] = {}
    for team in pro_teams:
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        if not isinstance(team_id, int):
            continue
        pro_games = team.get("proGamesByScoringPeriod", {})
        schedules[team_id] = pro_games if isinstance(pro_games, dict) else {}
    return schedules


def _player_schedule(pro_team_schedule: dict[int, dict], pro_team_id: Any) -> dict:
    """Mirror espn-api Player.schedule construction exactly enough for lineup use."""
    result = {}
    schedule = pro_team_schedule.get(pro_team_id, {})
    if not isinstance(schedule, dict):
        return result

    for key, games in schedule.items():
        if not isinstance(games, list) or not games:
            continue
        game = games[0]
        if not isinstance(game, dict):
            continue
        away = game.get("awayProTeamId")
        home = game.get("homeProTeamId")
        opponent_id = away if away != pro_team_id else home
        result[key] = {
            "team": PRO_TEAM_MAP.get(opponent_id),
            "date": datetime.fromtimestamp(game["date"] / 1000.0),
        }
    return result


def build_lineup_team(payload: Any, schedule_payload: Any, team_id: int, year: int) -> tuple[dict | None, list[int]]:
    """Build the exact factual roster shape consumed by ``optimize_lineup``.

    The legacy optimizer consumed ``espn-api`` Player objects. This direct parser
    reproduces only the fields that optimizer reads, including the full 17-game
    NFL schedule-key map used to verify bye weeks. It intentionally leaves the
    wrapper-only top-level ``projected_points`` field as ``None``; weekly
    recommendations continue to come exclusively from ``stats[week]``.
    """
    payload = _require_dict(payload)
    teams = _teams(payload)
    valid_ids = sorted(int(team["id"]) for team in teams if isinstance(team.get("id"), int))
    team = next((item for item in teams if item.get("id") == team_id), None)
    if team is None:
        return None, valid_ids

    pro_schedules = _all_pro_team_schedules(schedule_payload)
    roster = []
    for entry in _roster_entries(team):
        raw_player = _raw_player(entry)
        parsed = parse_roster_entry(entry, year)
        eligible_slots = [
            POSITION_MAP[slot_id]
            for slot_id in raw_player.get("eligibleSlots") or []
            if slot_id in POSITION_MAP
        ]
        roster.append({
            "name": parsed["name"],
            "position": parsed["position"],
            "proTeam": parsed["proTeam"],
            "projected_points": None,
            "points": parsed["points"],
            "lineup_slot": parsed["lineup_slot"],
            "injury_status": raw_player.get("injuryStatus"),
            "eligible_slots": eligible_slots,
            "projected_total_points": parsed["projected_points"],
            "_ol_raw_stats": parsed["stats"],
            "_ol_raw_schedule": _player_schedule(pro_schedules, raw_player.get("proTeamId")),
        })

    return {
        "team_id": team.get("id"),
        "team_name": _team_name(team),
        "roster": roster,
    }, valid_ids


def build_team_roster(payload: Any, team_id: int, year: int) -> tuple[dict | None, list[int]]:
    payload = _require_dict(payload)
    teams = _teams(payload)
    members = _members_by_id(payload)
    valid_ids = sorted(int(team["id"]) for team in teams if isinstance(team.get("id"), int))
    team = next((item for item in teams if item.get("id") == team_id), None)
    if team is None:
        return None, valid_ids

    overall = _overall_record(team)
    roster = []
    for entry in _roster_entries(team):
        player = parse_roster_entry(entry, year)
        roster.append({
            "name": player["name"],
            "position": player["position"],
            "proTeam": player["proTeam"],
            "points": player["points"],
            "projected_points": player["projected_points"],
            "stats": player["stats"],
        })

    return {
        "team_name": _team_name(team),
        "owner": _owners(team, members),
        "wins": overall.get("wins", 0),
        "losses": overall.get("losses", 0),
        "roster": roster,
    }, valid_ids


def build_player_stats(payload: Any, player_name: str, year: int) -> dict | None:
    """Search roster order exactly as the legacy tool did and return first match."""
    payload = _require_dict(payload)
    needle = str(player_name).lower()
    for team in _teams(payload):
        for entry in _roster_entries(team):
            player = parse_roster_entry(entry, year)
            name = player.get("name")
            if isinstance(name, str) and needle in name.lower():
                return {
                    "name": name,
                    "position": player["position"],
                    "team": player["proTeam"],
                    "points": player["points"],
                    "projected_points": player["projected_points"],
                    "stats": player["stats"],
                    "injured": player["injured"],
                }
    return None


def build_all_rosters(payload: Any, league_id: int, year: int, *, detailed: bool = False) -> dict:
    payload = _require_dict(payload)
    teams_out = []
    for team in _teams(payload):
        overall = _overall_record(team)
        roster_out = []
        for entry in _roster_entries(team):
            player = parse_roster_entry(entry, year)
            row = {
                "name": player["name"],
                "position": player["position"],
                "proTeam": player["proTeam"],
                "projected_points": player["projected_points"],
                "points": player["points"],
                "lineup_slot": player["lineup_slot"],
            }
            if detailed:
                row["stats"] = player["stats"]
            roster_out.append(row)

        teams_out.append({
            "team_id": team.get("id"),
            "team_name": _team_name(team),
            "wins": overall.get("wins", 0),
            "losses": overall.get("losses", 0),
            "roster": roster_out,
        })

    return {
        "league_id": league_id,
        "year": year,
        "team_count": len(teams_out),
        "teams": teams_out,
    }


COMMISSIONER_CURRENT_VIEWS = ("mSettings", "mTeam", "mRoster")


@dataclass
class CommissionerRosterPlayer:
    playerId: int | None
    name: str | None
    lineupSlot: str
    eligibleSlots: list[str]
    injuryStatus: str | None


@dataclass
class CommissionerTeam:
    team_id: int
    team_name: str | None
    roster: list[CommissionerRosterPlayer]


@dataclass
class CommissionerLeagueSnapshot:
    league_id: int
    year: int
    current_week: int
    settings: CommissionerSettings
    teams: list[CommissionerTeam]


def _commissioner_team_name(team: dict):
    """Mirror espn-api Team.team_name semantics, including the literal Unknown fallback."""
    name = team.get("name", "Unknown")
    if name == "Unknown":
        return "%s %s" % (team.get("location", "Unknown"), team.get("nickname", "Unknown"))
    return name


def _commissioner_current_week(payload: dict, year: int) -> int:
    """Mirror BaseLeague.current_week, including pre-2018 uncapped behavior."""
    raw = payload.get("scoringPeriodId", 0)
    try:
        scoring_period = int(raw)
    except (TypeError, ValueError):
        scoring_period = 0
    if year < 2018:
        return scoring_period
    status = payload.get("status") or {}
    final = status.get("finalScoringPeriod")
    if isinstance(final, int) and scoring_period > final:
        return final
    return scoring_period


def build_commissioner_snapshot(payload: Any, league_id: int, year: int) -> CommissionerLeagueSnapshot:
    """Build the narrow League/Team/Player compatibility surface used by basic commissioner reads."""
    payload = _require_dict(payload)
    raw_teams = _teams(payload)
    teams: list[CommissionerTeam] = []
    for raw_team in raw_teams:
        team_id = raw_team.get("id")
        if isinstance(team_id, bool) or not isinstance(team_id, int):
            continue
        roster: list[CommissionerRosterPlayer] = []
        for entry in _roster_entries(raw_team):
            player = _raw_player(entry)
            eligible = [POSITION_MAP[slot_id] for slot_id in (player.get("eligibleSlots") or [])]
            roster.append(CommissionerRosterPlayer(
                playerId=player.get("id"),
                name=player.get("fullName"),
                lineupSlot=POSITION_MAP.get(entry.get("lineupSlotId"), ""),
                eligibleSlots=eligible,
                injuryStatus=player.get("injuryStatus", entry.get("injuryStatus")),
            ))
        teams.append(CommissionerTeam(
            team_id=team_id,
            team_name=_commissioner_team_name(raw_team),
            roster=roster,
        ))
    teams.sort(key=lambda team: team.team_id)
    return CommissionerLeagueSnapshot(
        league_id=league_id,
        year=year,
        current_week=_commissioner_current_week(payload, year),
        settings=build_commissioner_settings(payload, year),
        teams=teams,
    )
