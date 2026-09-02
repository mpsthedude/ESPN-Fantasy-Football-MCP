"""Project-owned parsing for completed ESPN fantasy draft results.

This module intentionally covers only the factual completed-draft surface used
by get_draft_results. Live draft-board/recommendation behavior remains a
separate contract and is not migrated here.

ESPN's Fantasy endpoints are undocumented. Keep these field mappings covered
by deterministic fixture-style tests and change them deliberately when ESPN
changes its payload.
"""

from __future__ import annotations

from typing import Any


DRAFT_RESULT_VIEWS = ("mDraftDetail", "mTeam")
DRAFT_PLAYER_VIEWS = ("players_wl",)
DRAFT_PLAYER_FILTER = {"filterActive": {"value": True}}


class ESPNDraftPayloadError(ValueError):
    """The ESPN response did not contain the expected draft-result shape."""


def _require_dict(payload: Any, label: str) -> dict:
    if not isinstance(payload, dict):
        raise ESPNDraftPayloadError(f"ESPN returned an unexpected {label} payload")
    return payload


def _team_name(team: dict) -> str:
    name = team.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    location = str(team.get("location") or "Unknown").strip()
    nickname = str(team.get("nickname") or "Unknown").strip()
    return f"{location} {nickname}".strip()


def _teams_by_id(payload: dict) -> dict[int, str]:
    teams = payload.get("teams", [])
    if not isinstance(teams, list):
        raise ESPNDraftPayloadError("ESPN draft payload has invalid teams")
    result: dict[int, str] = {}
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_id = team.get("id")
        if isinstance(team_id, int) and not isinstance(team_id, bool):
            result[team_id] = _team_name(team)
    return result


def _player_names_by_id(players_payload: Any) -> dict[int, str]:
    if not isinstance(players_payload, list):
        raise ESPNDraftPayloadError("ESPN returned an unexpected draft player payload")
    result: dict[int, str] = {}
    for player in players_payload:
        if not isinstance(player, dict):
            continue
        player_id = player.get("id")
        name = player.get("fullName")
        if isinstance(player_id, int) and not isinstance(player_id, bool) and isinstance(name, str):
            result[player_id] = name
    return result


def build_draft_results(
    draft_payload: Any,
    players_payload: Any,
    league_id: int,
    year: int,
) -> dict:
    """Build the existing get_draft_results response from raw ESPN payloads."""
    payload = _require_dict(draft_payload, "draft")
    draft_detail = payload.get("draftDetail", {})
    if not isinstance(draft_detail, dict):
        raise ESPNDraftPayloadError("ESPN draft payload has invalid draftDetail")

    if not draft_detail.get("drafted"):
        return {
            "league_id": league_id,
            "year": year,
            "drafted": False,
            "picks": [],
            "message": "This league has not completed a draft yet for the selected year.",
        }

    raw_picks = draft_detail.get("picks", [])
    if not isinstance(raw_picks, list):
        raise ESPNDraftPayloadError("ESPN draft payload has invalid picks")

    teams = _teams_by_id(payload)
    player_names = _player_names_by_id(players_payload)
    picks = []
    for pick in raw_picks:
        if not isinstance(pick, dict):
            raise ESPNDraftPayloadError("ESPN draft payload contains an invalid pick")
        team_id = pick.get("teamId")
        nominating_team_id = pick.get("nominatingTeamId")
        player_id = pick.get("playerId")
        picks.append(
            {
                "round": pick.get("roundId"),
                "pick_in_round": pick.get("roundPickNumber"),
                # espn-api 0.46's BaseLeague._fetch_draft initializes the
                # player name to an empty string when its active-player map
                # does not contain the drafted player. Preserve that contract.
                "player_name": player_names.get(player_id, ""),
                "team_id": team_id if team_id in teams else None,
                "team_name": teams.get(team_id),
                "keeper": pick.get("keeper"),
                "bid_amount": pick.get("bidAmount"),
                "nominating_team_id": nominating_team_id if nominating_team_id in teams else None,
                "nominating_team_name": teams.get(nominating_team_id),
            }
        )

    return {
        "league_id": league_id,
        "year": year,
        "drafted": True,
        "pick_count": len(picks),
        "picks": picks,
    }
