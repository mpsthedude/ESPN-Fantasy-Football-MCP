"""Project-owned parsing for ESPN historical fantasy lineup reads.

Commissioner audit paths need only a narrow subset of espn-api BoxScore and
BoxPlayer behavior: team IDs, lineup slots, eligible slots, injury status, and
a reliable historical bye-week signal. This module parses those fields directly
from ESPN's filtered scoreboard payload plus the season pro-team schedule.

The returned compatibility objects deliberately expose only the attributes the
existing commissioner code consumes. They are not general BoxScore replacements.
ESPN's Fantasy endpoint is undocumented, so these mappings are covered by
deterministic fixture-style tests and should change deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from espn_reference import POSITION_MAP


HISTORICAL_LINEUP_VIEWS = ("mMatchupScore", "mScoreboard")
HISTORICAL_PRO_SCHEDULE_VIEWS = ("proTeamSchedules_wl",)


class ESPNHistoricalLineupPayloadError(ValueError):
    """The ESPN response did not contain the expected historical lineup shape."""


@dataclass(frozen=True)
class HistoricalTeamRef:
    team_id: int


@dataclass
class HistoricalLineupPlayer:
    playerId: int | None
    name: str | None
    slot_position: str
    eligibleSlots: list[str]
    injuryStatus: str | None
    on_bye_week: bool


@dataclass
class HistoricalLineupBox:
    home_team: HistoricalTeamRef | None
    home_lineup: list[HistoricalLineupPlayer]
    away_team: HistoricalTeamRef | None
    away_lineup: list[HistoricalLineupPlayer]


def _require_dict(payload: Any, label: str) -> dict:
    if not isinstance(payload, dict):
        raise ESPNHistoricalLineupPayloadError(f"ESPN returned an unexpected {label} payload")
    return payload


def _position_label(slot_id: Any) -> str:
    try:
        return POSITION_MAP.get(int(slot_id), str(slot_id) if slot_id is not None else "")
    except (TypeError, ValueError):
        return str(slot_id or "")


def _eligible_slot_labels(player: dict) -> list[str]:
    raw = player.get("eligibleSlots") or []
    if not isinstance(raw, list):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup player has invalid eligibleSlots")
    return [_position_label(slot_id) for slot_id in raw]


def _scheduled_pro_team_ids(pro_schedule_payload: Any, week: int) -> set[int]:
    payload = _require_dict(pro_schedule_payload, "pro schedule")
    settings = payload.get("settings") or {}
    pro_teams = settings.get("proTeams") or []
    if not isinstance(pro_teams, list):
        raise ESPNHistoricalLineupPayloadError("ESPN pro schedule payload has invalid proTeams")

    scheduled: set[int] = set()
    week_key = str(week)
    for team in pro_teams:
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        games = team.get("proGamesByScoringPeriod") or {}
        if (
            isinstance(team_id, int)
            and not isinstance(team_id, bool)
            and team_id != 0
            and isinstance(games, dict)
            and bool(games.get(week_key))
        ):
            scheduled.add(team_id)
    return scheduled


def _player_dict(entry: dict) -> dict:
    pool = entry.get("playerPoolEntry") or {}
    player = pool.get("player") if isinstance(pool, dict) else None
    if not isinstance(player, dict):
        player = entry.get("player")
    if not isinstance(player, dict):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup entry is missing player data")
    return player


def _historical_pro_team_id(player: dict, week: int) -> Any:
    """Match espn-api BoxPlayer's week-specific pro-team preference."""
    pro_team_id = player.get("proTeamId")
    stats = player.get("stats") or []
    if not isinstance(stats, list):
        return pro_team_id
    for stat in stats:
        if not isinstance(stat, dict):
            continue
        if (
            stat.get("scoringPeriodId") == week
            and stat.get("statSourceId") == 0
            and stat.get("proTeamId", 0) != 0
        ):
            return stat.get("proTeamId")
    return pro_team_id


def _parse_lineup(entries: Any, scheduled_pro_teams: set[int], week: int) -> list[HistoricalLineupPlayer]:
    if not isinstance(entries, list):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup roster has invalid entries")

    result: list[HistoricalLineupPlayer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ESPNHistoricalLineupPayloadError("ESPN historical lineup contains an invalid entry")
        player = _player_dict(entry)
        pro_team_id = _historical_pro_team_id(player, week)
        result.append(
            HistoricalLineupPlayer(
                playerId=player.get("id"),
                name=player.get("fullName"),
                slot_position=_position_label(entry.get("lineupSlotId")),
                eligibleSlots=_eligible_slot_labels(player),
                injuryStatus=player.get("injuryStatus"),
                on_bye_week=pro_team_id not in scheduled_pro_teams,
            )
        )
    return result


def _parse_side(side: Any, scheduled_pro_teams: set[int], week: int) -> tuple[HistoricalTeamRef | None, list[HistoricalLineupPlayer]]:
    if side is None:
        return None, []
    if not isinstance(side, dict):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup matchup has an invalid team side")

    team_id = side.get("teamId")
    if team_id is None:
        return None, []
    if isinstance(team_id, bool) or not isinstance(team_id, int):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup matchup has an invalid teamId")

    roster = side.get("rosterForCurrentScoringPeriod")
    if not isinstance(roster, dict):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup matchup is missing rosterForCurrentScoringPeriod")
    lineup = _parse_lineup(roster.get("entries"), scheduled_pro_teams, week)
    return HistoricalTeamRef(team_id), lineup


def build_historical_lineup_boxes(
    scoreboard_payload: Any,
    pro_schedule_payload: Any,
    week: int,
) -> list[HistoricalLineupBox]:
    """Build the narrow BoxScore-compatible objects used by commissioner reads."""
    if isinstance(week, bool) or not isinstance(week, int) or week <= 0:
        raise ValueError("week must be a positive integer")

    payload = _require_dict(scoreboard_payload, "historical lineup")
    schedule = payload.get("schedule")
    if not isinstance(schedule, list):
        raise ESPNHistoricalLineupPayloadError("ESPN historical lineup payload is missing schedule")

    scheduled_pro_teams = _scheduled_pro_team_ids(pro_schedule_payload, week)
    boxes: list[HistoricalLineupBox] = []
    for matchup in schedule:
        if not isinstance(matchup, dict):
            raise ESPNHistoricalLineupPayloadError("ESPN historical lineup schedule contains an invalid matchup")
        home_team, home_lineup = _parse_side(matchup.get("home"), scheduled_pro_teams, week)
        away_team, away_lineup = _parse_side(matchup.get("away"), scheduled_pro_teams, week)
        boxes.append(
            HistoricalLineupBox(
                home_team=home_team,
                home_lineup=home_lineup,
                away_team=away_team,
                away_lineup=away_lineup,
            )
        )
    return boxes
