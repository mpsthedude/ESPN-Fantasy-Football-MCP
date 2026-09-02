"""Project-owned parsing for ESPN Fantasy Football matchup reads.

The normal matchup tool only needs league schedule metadata, team names, and
week-specific home/away scores. It does not need espn-api BoxScore player
objects, pro schedules, or positional-rating requests.

ESPN's Fantasy endpoint is undocumented. These mappings are therefore treated
as an integration contract and covered by deterministic offline tests.
"""

from __future__ import annotations

from typing import Any


MATCHUP_CONTEXT_VIEWS = ("mSettings", "mTeam", "mMatchupScore")
MATCHUP_SCORE_VIEWS = ("mTeam", "mMatchupScore", "mScoreboard")


class ESPNMatchupPayloadError(ValueError):
    """The ESPN response did not contain the expected matchup shape."""


def _require_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ESPNMatchupPayloadError("ESPN returned an unexpected matchup payload")
    return payload


def _team_name(team: dict) -> str:
    name = team.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    location = str(team.get("location") or "Unknown").strip()
    nickname = str(team.get("nickname") or "Unknown").strip()
    return f"{location} {nickname}".strip()


def _teams_by_id(payload: dict) -> dict[int, str]:
    result: dict[int, str] = {}
    teams = payload.get("teams") or []
    if not isinstance(teams, list):
        raise ESPNMatchupPayloadError("ESPN matchup payload has invalid teams")
    for team in teams:
        if isinstance(team, dict) and isinstance(team.get("id"), int):
            result[team["id"]] = _team_name(team)
    return result


def valid_scoring_weeks(payload: Any) -> list[int]:
    """Return ESPN-configured scoring weeks without hardcoded NFL limits."""
    payload = _require_dict(payload)
    settings = payload.get("settings") or {}
    schedule_settings = settings.get("scheduleSettings") or {}
    matchup_periods = schedule_settings.get("matchupPeriods") or {}

    weeks: set[int] = set()
    if isinstance(matchup_periods, dict):
        for period_weeks in matchup_periods.values():
            if not isinstance(period_weeks, (list, tuple, set)):
                continue
            for week in period_weeks:
                if isinstance(week, int) and not isinstance(week, bool) and week >= 1:
                    weeks.add(week)

    if not weeks:
        status = payload.get("status") or {}
        first = status.get("firstScoringPeriod", 1) or 1
        final = status.get("finalScoringPeriod")
        if isinstance(first, int) and isinstance(final, int) and final >= first >= 1:
            weeks.update(range(first, final + 1))

    return sorted(weeks)


def current_scoring_week(payload: Any) -> int:
    """Match espn-api's current_week semantics from the raw league payload."""
    payload = _require_dict(payload)
    status = payload.get("status") or {}
    scoring_period = payload.get("scoringPeriodId", 0) or 0
    final = status.get("finalScoringPeriod")
    try:
        week = int(scoring_period)
    except (TypeError, ValueError):
        week = 0
    if isinstance(final, int) and week > final:
        week = final
    return week


def matchup_period_for_week(payload: Any, week: int) -> int:
    """Map a scoring week to ESPN's matchup period, including multi-week playoffs."""
    payload = _require_dict(payload)
    settings = payload.get("settings") or {}
    schedule_settings = settings.get("scheduleSettings") or {}
    matchup_periods = schedule_settings.get("matchupPeriods") or {}

    if isinstance(matchup_periods, dict):
        for period_id, period_weeks in matchup_periods.items():
            if isinstance(period_weeks, (list, tuple, set)) and week in period_weeks:
                try:
                    return int(period_id)
                except (TypeError, ValueError):
                    break

    status = payload.get("status") or {}
    current_matchup = status.get("currentMatchupPeriod")
    if week == current_scoring_week(payload) and isinstance(current_matchup, int) and current_matchup > 0:
        return current_matchup
    return week


def resolve_matchup_request(payload: Any, requested_week: int | None) -> tuple[int | None, int | None, list[int]]:
    """Resolve default week, validate it, and return its ESPN matchup period."""
    valid_weeks = valid_scoring_weeks(payload)
    week = current_scoring_week(payload) if requested_week is None else requested_week
    if isinstance(week, bool) or not isinstance(week, int) or week not in valid_weeks:
        return None, None, valid_weeks
    return week, matchup_period_for_week(payload, week), valid_weeks


def build_matchup_info(payload: Any, week: int) -> list[dict]:
    """Build the legacy get_matchup_info response from a filtered ESPN payload."""
    payload = _require_dict(payload)
    team_names = _teams_by_id(payload)
    schedule = payload.get("schedule")
    if not isinstance(schedule, list):
        raise ESPNMatchupPayloadError("ESPN matchup payload is missing schedule")

    result = []
    for matchup in schedule:
        if not isinstance(matchup, dict):
            continue
        home = matchup.get("home") or {}
        away = matchup.get("away") or {}
        home_id = home.get("teamId")
        away_id = away.get("teamId")
        home_score = home.get("totalPoints", 0) or 0
        away_score = (away.get("totalPoints", 0) or 0) if away else 0

        if home_id is None:
            continue
        home_name = team_names.get(home_id, f"Team {home_id}")
        away_name = team_names.get(away_id, f"Team {away_id}") if away_id is not None else "BYE"
        if away_id is None:
            away_score = 0

        winner = "HOME" if home_score > away_score else "AWAY" if away_score > home_score else "TIE"
        result.append({
            "home_team": home_name,
            "home_score": home_score,
            "away_team": away_name,
            "away_score": away_score,
            "winner": winner,
        })
    return result


def build_commissioner_matchup_evidence(payload: Any, week: int, target_team_ids: set[int]) -> dict | None:
    """Return the first ESPN scoreboard row involving a commissioner target.

    This is intentionally separate from :func:`build_matchup_info` because the
    commissioner case-file contract requires factual ESPN team IDs as well as
    names/scores. Schedule order is preserved and no winner/interpretation is
    added. A missing away side mirrors ``espn-api`` Matchup's 0/0 placeholder
    semantics while leaving the unresolved team name as ``None``.
    """
    payload = _require_dict(payload)
    team_names = _teams_by_id(payload)
    schedule = payload.get("schedule")
    if not isinstance(schedule, list):
        raise ESPNMatchupPayloadError("ESPN matchup payload is missing schedule")
    if not isinstance(target_team_ids, set):
        target_team_ids = set(target_team_ids or [])

    for matchup in schedule:
        if not isinstance(matchup, dict):
            continue
        home = matchup.get("home")
        away = matchup.get("away")
        home = home if isinstance(home, dict) else {}
        away = away if isinstance(away, dict) else {}
        home_id = home.get("teamId", 0) if home else 0
        away_id = away.get("teamId", 0) if away else 0
        if home_id not in target_team_ids and away_id not in target_team_ids:
            continue
        return {
            "week": week,
            "source": f"espn_scoreboard_week_{week}",
            "home_team_id": home_id,
            "home_team_name": team_names.get(home_id),
            "home_score": home.get("totalPoints", 0) if home else 0,
            "away_team_id": away_id,
            "away_team_name": team_names.get(away_id),
            "away_score": away.get("totalPoints", 0) if away else 0,
        }
    return None
