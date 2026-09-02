"""Register bounded NFL player-prop market context enrichment."""

from __future__ import annotations

from typing import Optional

import fantasypros_client as fp_client
from espn_fantasy_server import CURRENT_YEAR, SESSION_ID, api
from espn_roster_read import ROSTER_VIEWS, build_player_stats
from player_market_context import build_player_market_context
from sportsgameodds_client import SportsGameOddsClient
from sportsgameodds_disagreement_tools import _find_sportsbook_player_prop_disagreements
from sportsgameodds_tools import _error, _normalize_league


def _client() -> SportsGameOddsClient:
    return SportsGameOddsClient()


def _validate_optional_positive_int(value: Optional[int], name: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer when provided")
    return value


def _espn_player_context(player_name: str, league_id: int, year: int) -> dict:
    """Perform at most one ESPN roster read and degrade safely on failure."""
    try:
        transport = api.get_transport(SESSION_ID)
        payload = transport.fetch_league(league_id, year, views=ROSTER_VIEWS)
        player = build_player_stats(payload, player_name, year)
    except Exception:
        return {
            "status": "read_failed",
            "leagueID": league_id,
            "year": year,
            "message": "ESPN enrichment could not be loaded; sportsbook and FantasyPros context remain usable.",
        }
    if player is None:
        return {
            "status": "player_not_found",
            "leagueID": league_id,
            "year": year,
            "message": "The requested player was not found on a roster in this ESPN league snapshot.",
        }
    return {
        "status": "matched",
        "leagueID": league_id,
        "year": year,
        "player": player,
    }


def _get_player_prop_market_context(
    client: SportsGameOddsClient,
    *,
    event_id: str,
    player_name: str,
    league: str,
    team_id: str,
    espn_league_id: Optional[int] = None,
    espn_year: Optional[int] = None,
    scoring: str = "PPR",
    stat_id: Optional[str] = None,
    bet_type: Optional[str] = None,
    bookmakers: Optional[str] = None,
    top_n: int = 5,
    min_bookmakers: int = 2,
) -> dict:
    """Compose bounded SGO disagreement with cache-only FP and optional ESPN."""
    event_value = (event_id or "").strip()
    player_value = (player_name or "").strip()
    team_value = (team_id or "").strip()
    if not event_value:
        raise ValueError("event_id is required for player market context")
    if not player_value:
        raise ValueError("player_name is required for player market context")
    if not team_value:
        raise ValueError("team_id is required for player market context")

    league_id = _normalize_league(league)
    if league_id != "NFL":
        raise ValueError(
            "player market context currently supports NFL only because FantasyPros enrichment is NFL-specific"
        )
    scoring_error = fp_client.validate_scoring(scoring)
    if scoring_error:
        raise ValueError(scoring_error)
    requested_espn_league = _validate_optional_positive_int(espn_league_id, "espn_league_id")
    requested_espn_year = _validate_optional_positive_int(espn_year, "espn_year")
    if requested_espn_year is not None and requested_espn_league is None:
        raise ValueError("espn_year requires espn_league_id")

    disagreement = _find_sportsbook_player_prop_disagreements(
        client,
        event_id=event_value,
        player_name=player_value,
        league=league_id,
        team_id=team_value,
        stat_id=stat_id,
        bet_type=bet_type,
        bookmakers=bookmakers,
        top_n=top_n,
        min_bookmakers=min_bookmakers,
    )

    espn = None
    if requested_espn_league is not None:
        espn = _espn_player_context(
            player_value,
            requested_espn_league,
            requested_espn_year or CURRENT_YEAR,
        )

    espn_player = (espn or {}).get("player") if isinstance((espn or {}).get("player"), dict) else {}
    sportsbook_player = disagreement.get("player") if isinstance(disagreement.get("player"), dict) else {}
    fp_team = espn_player.get("team") or sportsbook_player.get("team")
    fp_position = espn_player.get("position") or sportsbook_player.get("position")

    try:
        fantasypros = fp_client.build_player_intelligence(
            player_value,
            team=fp_team,
            position=fp_position,
            scoring=scoring.upper(),
        )
        positions = [fp_position] if fp_position in fp_client.VALID_POSITIONS else []
        fantasypros_freshness = fp_client.get_cache_freshness_report(positions, scoring.upper())
    except Exception:
        fantasypros = {
            "query": {"name": player_value, "team": fp_team, "position": fp_position},
            "match_method": "cache_read_failed",
            "match_confidence": "none",
            "message": "FantasyPros cache enrichment could not be loaded.",
        }
        fantasypros_freshness = None

    result = build_player_market_context(
        disagreement,
        fantasypros,
        scoring=scoring,
        espn=espn,
        fantasypros_freshness=fantasypros_freshness,
    )
    result["providerCost"] = {
        "sportsGameOdds": {
            "exactEventPropPath": 1,
            "maxPlayerRosterFallbacks": 1,
            "hiddenPagination": False,
        },
        "fantasyProsLiveRequests": 0,
        "espnRosterReads": 1 if requested_espn_league is not None else 0,
    }
    return result


def register_player_market_context_tools(mcp) -> None:
    """Register read-only cross-provider fantasy/sportsbook context."""

    @mcp.tool()
    async def get_player_prop_market_context(
        event_id: str,
        player_name: str,
        league: str,
        team_id: str,
        espn_league_id: Optional[int] = None,
        espn_year: Optional[int] = None,
        scoring: str = "PPR",
        stat_id: Optional[str] = None,
        bet_type: Optional[str] = None,
        bookmakers: Optional[str] = None,
        top_n: int = 5,
        min_bookmakers: int = 2,
    ) -> dict:
        """Combine NFL prop disagreement with FantasyPros and optional ESPN evidence.

        Requires the exact SportsGameOdds `event_id`, player name, `league="NFL"`,
        and provider `team_id`. The sportsbook path is the same bounded exact-event
        disagreement path: no hidden pagination and at most the existing single
        player-roster fallback. FantasyPros intelligence is cache-only and consumes
        zero live FantasyPros requests. If `espn_league_id` is supplied, one ESPN
        roster snapshot is read to add that league's player stats/injury flag;
        ESPN enrichment failure degrades gracefully instead of discarding the
        sportsbook result.

        The output surfaces possible explanatory context such as injury flags,
        recent news, and expert-ranking dispersion. It does not claim those signals
        caused sportsbook differences and does not calculate expected value, fair
        odds, win probability, or a wager recommendation.
        """
        try:
            return _get_player_prop_market_context(
                _client(),
                event_id=event_id,
                player_name=player_name,
                league=league,
                team_id=team_id,
                espn_league_id=espn_league_id,
                espn_year=espn_year,
                scoring=scoring,
                stat_id=stat_id,
                bet_type=bet_type,
                bookmakers=bookmakers,
                top_n=top_n,
                min_bookmakers=min_bookmakers,
            )
        except Exception as exc:
            return _error("get player prop market context", exc)
