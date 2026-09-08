"""Project-owned parsing and validation for ESPN completed and live drafts.

ESPN's Fantasy endpoints are undocumented. Keep the observed field contracts
covered with deterministic tests and validate against live reads deliberately.
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

from espn_roster_read import parse_roster_entry


DRAFT_RESULT_VIEWS = ("mDraftDetail", "mTeam")
DRAFT_PLAYER_VIEWS = ("players_wl",)
DRAFT_PLAYER_FILTER = {"filterActive": {"value": True}}


class ESPNDraftPayloadError(ValueError):
    """The ESPN response did not contain the expected draft-result shape."""


def validate_live_snapshot(payload: Any, previous: dict | None = None) -> dict:
    """Reject incomplete snapshots instead of presenting an older board as fresh."""
    if not isinstance(payload, dict):
        raise ESPNDraftPayloadError("Missing live draft payload")
    detail = payload.get("draftDetail")
    if not isinstance(detail, dict) or any(type(detail.get(k)) is not bool for k in ("drafted", "inProgress")):
        raise ESPNDraftPayloadError("Missing live draft status")
    picks = detail.get("picks")
    teams = payload.get("teams")
    if not isinstance(picks, list) or not picks or not isinstance(teams, list) or not teams:
        raise ESPNDraftPayloadError("Missing live draft picks or teams")
    slots = set()
    for pick in picks:
        if not isinstance(pick, dict) or any(type(pick.get(k)) is not int for k in ("overallPickNumber", "teamId", "roundId", "roundPickNumber")):
            raise ESPNDraftPayloadError("Invalid live draft slot")
        if pick["overallPickNumber"] in slots:
            raise ESPNDraftPayloadError("Duplicate live draft slot")
        slots.add(pick["overallPickNumber"])
    if previous is not None:
        old_slots = {p["overallPickNumber"] for p in previous["draftDetail"]["picks"]}
        if slots != old_slots:
            raise ESPNDraftPayloadError("Draft skeleton changed during the request; retry")
        if payload.get("settings") != previous.get("settings"):
            raise ESPNDraftPayloadError("League settings changed during the request; retry")
        if {t.get("id") for t in teams} != {t.get("id") for t in previous["teams"]}:
            raise ESPNDraftPayloadError("Incomplete team snapshot; retry")
    return payload


def build_live_player_pool(payload: Any, year: int) -> list:
    """Parse all active draft candidates, independent of carried roster status.

    Availability is determined separately from the latest draft picks. Before
    keeper assignment this universe is explicitly provisional.
    """
    entries = payload.get("players") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ESPNDraftPayloadError("Missing draft player pool")
    players = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("player"), dict):
            raise ESPNDraftPayloadError("Invalid draft player entry")
        player = entry["player"]
        pid = player.get("id")
        if type(pid) is not int or not player.get("fullName") or pid in seen:
            raise ESPNDraftPayloadError("Invalid or duplicate draft player identity")
        seen.add(pid)
        parsed = parse_roster_entry({"playerPoolEntry": entry}, year)
        players.append(SimpleNamespace(
            playerId=pid, name=parsed["name"], position=parsed["position"],
            proTeam=parsed["proTeam"], injuryStatus=parsed["injury_status"],
            stats=parsed["stats"],
        ))
    return players


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
