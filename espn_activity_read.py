"""Project-owned parsing for ESPN Fantasy Football transaction activity.

This module mirrors the factual subset of ``espn-api`` League.recent_activity /
Activity behavior used by the commissioner tools. HTTP/session behavior remains
in :mod:`espn_transport`; this module is pure request-filter construction and
payload translation.

ESPN's Fantasy API is undocumented. Treat the communication endpoint, message
IDs, and payload shapes here as fixture-tested integration contracts.
"""

from __future__ import annotations

import datetime
from typing import Any


ACTIVITY_VIEWS = ("kona_league_communication",)
ACTIVE_PLAYER_VIEWS = ("players_wl",)
ACTIVITY_MESSAGE_TYPE_IDS = (178, 180, 179, 239, 181, 244)

_MESSAGE_ACTIONS = {
    178: "FA ADDED",
    180: "WAIVER ADDED",
    179: "DROPPED",
    181: "DROPPED",
    239: "DROPPED",
}
_ACTION_TYPES = {
    "FA ADDED": "free_agent_add",
    "WAIVER ADDED": "waiver_add",
    "DROPPED": "drop",
    "TRADE_SENT": "trade",
    "TRADE_RECEIVED": "trade",
}


class ESPNActivityPayloadError(ValueError):
    """ESPN returned an activity payload we cannot safely interpret."""


def build_activity_filter(size: int, offset: int) -> dict:
    """Reproduce espn-api 0.46.x's mixed recent-activity filter contract."""
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("size must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    return {
        "topics": {
            "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
            "limit": size,
            "limitPerMessageSet": {"value": 25},
            "offset": offset,
            "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
            "sortFor": {"sortPriority": 2, "sortAsc": False},
            "filterIncludeMessageTypeIds": {"value": list(ACTIVITY_MESSAGE_TYPE_IDS)},
        }
    }


def build_active_player_filter() -> dict:
    """Mirror the active-player lookup used by espn-api League construction."""
    return {"filterActive": {"value": True}}


def build_active_player_name_map(payload: Any) -> dict[int, str]:
    if not isinstance(payload, list):
        raise ESPNActivityPayloadError("ESPN returned an unexpected active-player payload")
    result: dict[int, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        player_id = row.get("id")
        name = row.get("fullName")
        if isinstance(player_id, int) and isinstance(name, str) and name:
            result[player_id] = name
    return result


def _team_maps(league) -> tuple[dict[int, Any], dict[int, str]]:
    teams_by_id: dict[int, Any] = {}
    roster_names: dict[int, str] = {}
    for team in getattr(league, "teams", []) or []:
        team_id = getattr(team, "team_id", None)
        if isinstance(team_id, int):
            teams_by_id[team_id] = team
        for player in getattr(team, "roster", []) or []:
            player_id = getattr(player, "playerId", None)
            player_name = getattr(player, "name", None)
            if isinstance(player_id, int) and isinstance(player_name, str) and player_name:
                roster_names[player_id] = player_name
    return teams_by_id, roster_names


def _team_fields(team) -> tuple[int | None, str | None]:
    if team is None:
        return None, None
    return getattr(team, "team_id", None), getattr(team, "team_name", None)


def _resolve_player_name(player_id: Any, roster_names: dict[int, str], active_names: dict[int, str]) -> str | None:
    if not isinstance(player_id, int):
        return None
    return roster_names.get(player_id) or active_names.get(player_id)


def _normalized_action(source_action: str, team, player_id: Any, player_name: str | None, bid_amount: Any = 0) -> dict:
    action_type = _ACTION_TYPES.get(source_action, "unknown")
    team_id, team_name = _team_fields(team)
    return {
        "action_type": action_type,
        "team_id": team_id,
        "team_name": team_name,
        "player_id": player_id if isinstance(player_id, int) else None,
        "player_name": player_name,
        "bid_amount": bid_amount if action_type == "waiver_add" else None,
        "source_action": source_action,
    }


def _derive_event_type(action_types: set[str]) -> str:
    if "trade" in action_types:
        return "trade"
    if "waiver_add" in action_types and action_types <= {"waiver_add", "drop"}:
        return "waiver"
    if "free_agent_add" in action_types and action_types <= {"free_agent_add", "drop"}:
        return "free_agent"
    if action_types == {"drop"}:
        return "drop"
    if action_types == {"unknown"}:
        return "unknown"
    return "mixed"


def build_activity_events(payload: Any, league, active_player_names: dict[int, str] | None = None) -> list[dict]:
    """Build normalized commissioner events directly from ESPN communication JSON.

    Event and message order are preserved exactly as ESPN returns them. Trades
    reproduce espn-api's two-action expansion (TRADE_SENT then TRADE_RECEIVED).
    Current-roster names take precedence over the active-player map; unresolved
    historical names remain ``None`` rather than triggering hidden per-player
    network calls.
    """
    if not isinstance(payload, dict):
        raise ESPNActivityPayloadError("ESPN returned an unexpected activity payload")
    topics = payload.get("topics")
    if not isinstance(topics, list):
        raise ESPNActivityPayloadError("ESPN activity payload is missing topics")

    teams_by_id, roster_names = _team_maps(league)
    active_names = active_player_names or {}
    events: list[dict] = []

    for topic in topics:
        if not isinstance(topic, dict):
            continue
        timestamp_ms = topic.get("date")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
            raise ESPNActivityPayloadError("ESPN activity topic has an invalid date")
        messages = topic.get("messages")
        if not isinstance(messages, list):
            raise ESPNActivityPayloadError("ESPN activity topic is missing messages")

        actions: list[dict] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("messageTypeId")
            player_id = msg.get("targetId")
            player_name = _resolve_player_name(player_id, roster_names, active_names)

            if msg_id == 244:
                from_team = teams_by_id.get(msg.get("from"))
                to_team = teams_by_id.get(msg.get("to"))
                actions.append(_normalized_action("TRADE_SENT", from_team, player_id, player_name, 0))
                if to_team is not None:
                    actions.append(_normalized_action("TRADE_RECEIVED", to_team, player_id, player_name, 0))
                continue

            source_action = _MESSAGE_ACTIONS.get(msg_id, "UNKNOWN")
            team_ref = msg.get("for") if msg_id == 239 else msg.get("to")
            team = teams_by_id.get(team_ref)
            bid = msg.get("from", 0) if source_action == "WAIVER ADDED" else 0
            actions.append(_normalized_action(source_action, team, player_id, player_name, bid))

        action_types = {action["action_type"] for action in actions}
        event_type = _derive_event_type(action_types)
        has_add = "waiver_add" in action_types or "free_agent_add" in action_types
        paired_add_drop = bool(has_add and "drop" in action_types)
        timestamp_utc = datetime.datetime.fromtimestamp(
            timestamp_ms / 1000.0, tz=datetime.timezone.utc
        ).isoformat()
        events.append({
            "timestamp_ms": timestamp_ms,
            "timestamp_utc": timestamp_utc,
            "source": "espn_recent_activity",
            "event_type": event_type,
            "actions": actions,
            "paired_add_drop": paired_add_drop,
            "paired_add_drop_basis": "same_espn_activity_object" if paired_add_drop else None,
        })

    return events
