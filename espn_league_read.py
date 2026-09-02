"""Project-owned parsing for ESPN Fantasy Football core league reads.

This module converts raw ESPN mSettings/mTeam/mMatchup/mStandings payloads into
stable MCP-facing dictionaries. It deliberately does not construct espn-api
League/Team objects, so metadata, settings, team summaries, and standings can
be read without the wrapper's additional player/schedule/draft requests.

ESPN's Fantasy endpoint is undocumented. Keep these field mappings covered by
fixture-style tests and change them deliberately when ESPN changes its payload.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

# Compatibility dictionaries only. HTTP/session/object behavior is owned by
# this project. These constants preserve the existing public tool output while
# the project gradually internalizes ESPN's stat/slot label tables as well.
from espn_reference import POSITION_MAP, SETTINGS_SCORING_FORMAT_MAP


CORE_LEAGUE_VIEWS = ("mTeam", "mMatchup", "mSettings", "mStandings")


class ESPNLeaguePayloadError(ValueError):
    """The ESPN response did not contain the expected core league shape."""


def _require_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ESPNLeaguePayloadError("ESPN returned an unexpected league payload")
    return payload


def _settings(payload: dict) -> dict:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ESPNLeaguePayloadError("ESPN league payload is missing settings")
    return settings


def _teams(payload: dict) -> list[dict]:
    teams = payload.get("teams")
    if not isinstance(teams, list):
        raise ESPNLeaguePayloadError("ESPN league payload is missing teams")
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


def _normalize_owner_id(raw: Any) -> str | None:
    """Normalize an ESPN member/SWID value for equality testing only."""
    if not raw or not isinstance(raw, str):
        return None
    return raw.strip().strip("{}").upper()


def resolve_my_team_from_payload(payload: Any, authenticated_swid: str | None) -> dict:
    """Resolve the authenticated owner to one raw ESPN team without exposing IDs.

    Mirrors the legacy server-side ``_resolve_my_team`` contract exactly:
    braces/case are ignored for comparison, one match resolves, zero matches
    remain unresolved, and multiple matches are reported explicitly rather
    than guessed. Returned data never includes the authenticated SWID/member ID.
    """
    payload = _require_dict(payload)
    teams = _teams(payload)
    members = _members_by_id(payload)
    target = _normalize_owner_id(authenticated_swid)
    if not target:
        return {
            "status": "team_not_resolved",
            "team_id": None,
            "team_name": None,
            "resolution_method": "no_credential_available",
            "candidates": [],
        }

    matches: list[dict] = []
    for team in teams:
        owner_ids = [_normalize_owner_id(owner.get("id")) for owner in _owners(team, members)]
        if target in owner_ids:
            matches.append(team)

    if len(matches) == 1:
        team = matches[0]
        return {
            "status": "resolved",
            "team_id": team.get("id"),
            "team_name": _team_name(team),
            "resolution_method": "owner_swid_match",
            "candidates": [],
        }
    if not matches:
        return {
            "status": "team_not_resolved",
            "team_id": None,
            "team_name": None,
            "resolution_method": "owner_swid_match",
            "candidates": [],
        }
    return {
        "status": "ambiguous_team_ownership",
        "team_id": None,
        "team_name": None,
        "resolution_method": "owner_swid_match",
        "candidates": [
            {"team_id": team.get("id"), "team_name": _team_name(team)}
            for team in matches
        ],
    }


def _overall_record(team: dict) -> dict:
    record = team.get("record") or {}
    overall = record.get("overall") or {}
    return overall if isinstance(overall, dict) else {}


def _rank(team: dict, fallback: int) -> int:
    value = team.get("rankFinal") or team.get("rankCalculatedFinal") or team.get("playoffSeed") or fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def build_league_info(payload: Any, year: int) -> dict:
    payload = _require_dict(payload)
    settings = _settings(payload)
    teams = _teams(payload)
    status = payload.get("status") or {}
    scoring_period = payload.get("scoringPeriodId") or 0
    final_period = status.get("finalScoringPeriod")
    try:
        current_week = int(scoring_period)
    except (TypeError, ValueError):
        current_week = 0
    if isinstance(final_period, int) and current_week > final_period:
        current_week = final_period
    return {
        "name": settings.get("name"),
        "year": year,
        "current_week": current_week,
        "nfl_week": status.get("latestScoringPeriod"),
        "team_count": len(teams),
        "teams": [_team_name(team) for team in teams],
        "scoring_type": (settings.get("scoringSettings") or {}).get("scoringType"),
    }


def build_league_settings(payload: Any, league_id: int, year: int) -> dict:
    payload = _require_dict(payload)
    settings = _settings(payload)
    schedule = settings.get("scheduleSettings") or {}
    scoring = settings.get("scoringSettings") or {}
    roster = settings.get("rosterSettings") or {}

    lineup_counts = roster.get("lineupSlotCounts") or {}
    position_labels = list(POSITION_MAP.values())[: len(lineup_counts)]
    position_slot_counts = dict(zip(position_labels, list(lineup_counts.values())))

    scoring_rules = []
    for item in scoring.get("scoringItems") or []:
        if not isinstance(item, dict) or item.get("statId") is None:
            continue
        stat_id = item["statId"]
        base = SETTINGS_SCORING_FORMAT_MAP.get(stat_id, {"abbr": "Unknown", "label": "Unknown"})
        rule = copy.deepcopy(base)
        rule["id"] = stat_id
        override = (item.get("pointsOverrides") or {}).get("16")
        rule["points"] = override or item.get("points", 0)
        scoring_rules.append(rule)

    return {
        "league_id": league_id,
        "year": year,
        "league_name": settings.get("name"),
        "scoring_type": scoring.get("scoringType"),
        "team_count": settings.get("size"),
        "playoff_team_count": schedule.get("playoffTeamCount"),
        "roster_slot_counts": position_slot_counts,
        "scoring_rules": scoring_rules,
    }


def build_standings(payload: Any) -> list[dict]:
    payload = _require_dict(payload)
    teams = _teams(payload)
    members = _members_by_id(payload)
    ordered = sorted(enumerate(teams, start=1), key=lambda pair: _rank(pair[1], pair[0]))
    standings = []
    for fallback, team in ordered:
        overall = _overall_record(team)
        standings.append({
            "rank": _rank(team, fallback),
            "team_name": _team_name(team),
            "owner": _owners(team, members),
            "wins": overall.get("wins", 0),
            "losses": overall.get("losses", 0),
            "points_for": overall.get("pointsFor", 0),
            "points_against": round(overall.get("pointsAgainst", 0) or 0, 2),
        })
    return standings


def _team_outcomes(payload: dict, team_id: int) -> list[str]:
    outcomes = []
    schedule = payload.get("schedule") or []
    for matchup in schedule:
        if not isinstance(matchup, dict):
            continue
        home = matchup.get("home") or {}
        away = matchup.get("away") or {}
        home_id = home.get("teamId", -1)
        away_id = away.get("teamId", -1)
        if team_id not in (home_id, away_id):
            continue
        winner = matchup.get("winner")
        if winner == "UNDECIDED":
            outcomes.append("U")
        elif winner == "TIE":
            outcomes.append("T")
        elif (team_id == home_id and winner == "HOME") or (team_id == away_id and winner == "AWAY"):
            outcomes.append("W")
        else:
            outcomes.append("L")
    return outcomes


def build_team_info(payload: Any, team_id: int) -> tuple[dict | None, list[int]]:
    payload = _require_dict(payload)
    teams = _teams(payload)
    members = _members_by_id(payload)
    valid_ids = sorted(int(t["id"]) for t in teams if isinstance(t.get("id"), int))
    team = next((t for t in teams if t.get("id") == team_id), None)
    if team is None:
        return None, valid_ids

    overall = _overall_record(team)
    counters = team.get("transactionCounter") or {}
    simulation = team.get("currentSimulationResults") or {}
    final_standing = team.get("rankFinal") or team.get("rankCalculatedFinal")
    return {
        "team_name": _team_name(team),
        "owner": _owners(team, members),
        "wins": overall.get("wins", 0),
        "losses": overall.get("losses", 0),
        "ties": overall.get("ties", 0),
        "points_for": overall.get("pointsFor", 0),
        "points_against": round(overall.get("pointsAgainst", 0) or 0, 2),
        "acquisitions": counters.get("acquisitions", 0),
        "drops": counters.get("drops", 0),
        "trades": counters.get("trades", 0),
        "playoff_pct": (simulation.get("playoffPct", 0) or 0) * 100,
        "final_standing": final_standing,
        "outcomes": _team_outcomes(payload, team_id),
    }, valid_ids


@dataclass(frozen=True)
class CommissionerSettings:
    """Narrow project-owned compatibility surface used by commissioner reads."""
    name: str | None
    team_count: int | None
    reg_season_count: int | None
    matchup_periods: dict
    veto_votes_required: int | None
    playoff_team_count: int | None
    keeper_count: int | None
    trade_deadline: int | float
    playoff_matchup_period_length: int | None
    faab: bool | None
    acquisition_budget: int | float | None
    division_map: dict
    position_slot_counts: dict


def build_commissioner_settings(payload: Any, year: int) -> CommissionerSettings:
    """Mirror the espn-api BaseSettings/football.Settings fields commissioner tools consume."""
    payload = _require_dict(payload)
    settings = _settings(payload)
    schedule = settings.get("scheduleSettings") or {}
    trade = settings.get("tradeSettings") or {}
    draft = settings.get("draftSettings") or {}
    acquisition = settings.get("acquisitionSettings") or {}

    # Reuse the same project-owned roster-slot translation as public league settings.
    slot_counts = build_league_settings(payload, 0, year)["roster_slot_counts"]

    division_map = {}
    for division in schedule.get("divisions") or []:
        if isinstance(division, dict):
            division_map[division.get("id", 0)] = division.get("name")

    matchup_periods = schedule.get("matchupPeriods") or {}
    if not isinstance(matchup_periods, dict):
        raise ESPNLeaguePayloadError("ESPN league settings contain invalid matchupPeriods")

    return CommissionerSettings(
        name=settings.get("name"),
        team_count=settings.get("size"),
        reg_season_count=schedule.get("matchupPeriodCount"),
        matchup_periods=matchup_periods,
        veto_votes_required=trade.get("vetoVotesRequired"),
        playoff_team_count=schedule.get("playoffTeamCount"),
        keeper_count=draft.get("keeperCount"),
        trade_deadline=trade.get("deadlineDate", 0),
        playoff_matchup_period_length=schedule.get("playoffMatchupPeriodLength", 0),
        faab=acquisition.get("isUsingAcquisitionBudget"),
        acquisition_budget=acquisition.get("acquisitionBudget", 0),
        division_map=division_map,
        position_slot_counts=slot_counts,
    )
