"""Register bounded SportsGameOdds market-disagreement detection."""

from __future__ import annotations

from typing import Optional

from sportsgameodds_analysis import compact_player_prop_snapshot
from sportsgameodds_client import SportsGameOddsClient
from sportsgameodds_comparison import normalize_comparison_market
from sportsgameodds_disagreement import (
    rank_player_prop_disagreements,
    rank_slate_market_disagreements,
)
from sportsgameodds_tools import _error, _generic_slate, _normalize_league


def _client() -> SportsGameOddsClient:
    return SportsGameOddsClient()


def _find_sportsbook_market_disagreements(
    client: SportsGameOddsClient,
    *,
    market: str,
    league: Optional[str] = None,
    sport: Optional[str] = None,
    team_id: Optional[str] = None,
    bookmakers: Optional[str] = None,
    starts_after: Optional[str] = None,
    starts_before: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 20,
    top_n: int = 10,
    min_bookmakers: int = 2,
) -> dict:
    """Fetch exactly one slate page and rank cross-book disagreement locally."""
    market_kind = normalize_comparison_market(market)
    if market_kind == "player_prop":
        raise ValueError("player_prop disagreement requires the player-prop disagreement path")

    slate = _generic_slate(
        client,
        league=league,
        sport=sport,
        team_id=team_id,
        bookmakers=bookmakers,
        starts_after=starts_after,
        starts_before=starts_before,
        cursor=cursor,
        limit=limit,
    )
    ranked = rank_slate_market_disagreements(
        slate,
        market=market_kind,
        min_bookmakers=min_bookmakers,
        top_n=top_n,
    )
    ranked.update(
        {
            "leagueID": slate.get("leagueID"),
            "sportID": slate.get("sportID"),
            "teamID": slate.get("teamID"),
            "bookmakers": slate.get("bookmakers") or [],
            "startsAfter": slate.get("startsAfter"),
            "startsBefore": slate.get("startsBefore"),
        }
    )
    return ranked


def _find_sportsbook_player_prop_disagreements(
    client: SportsGameOddsClient,
    *,
    event_id: str,
    player_name: str,
    league: str,
    team_id: str,
    stat_id: Optional[str] = None,
    bet_type: Optional[str] = None,
    bookmakers: Optional[str] = None,
    top_n: int = 10,
    min_bookmakers: int = 2,
) -> dict:
    """Fetch one exact-event player prop set and rank disagreement locally."""
    event_value = (event_id or "").strip()
    player_value = (player_name or "").strip()
    team_value = (team_id or "").strip()
    if not event_value:
        raise ValueError("event_id is required for player-prop disagreement")
    if not player_value:
        raise ValueError("player_name is required for player-prop disagreement")
    if not team_value:
        raise ValueError("team_id is required for player-prop disagreement")
    league_id = _normalize_league(league)

    raw = client.sportsbook_player_props(
        player_name=player_value,
        league=league_id,
        team_id=team_value,
        event_id=event_value,
        stat_id=(stat_id or "").strip() or None,
        bookmakers=bookmakers,
        include_alt_lines=False,
        limit=1,
    )
    snapshot = compact_player_prop_snapshot(raw, relevant_only=False)
    ranked = rank_player_prop_disagreements(
        snapshot,
        stat_id=stat_id,
        bet_type=bet_type,
        min_bookmakers=min_bookmakers,
        top_n=top_n,
    )
    ranked.update(
        {
            "eventID": event_value,
            "leagueID": league_id,
            "teamID": team_value,
            "playerName": player_value,
        }
    )
    return ranked


def register_sportsgameodds_disagreement_tools(mcp) -> None:
    """Register read-only sportsbook disagreement discovery."""

    @mcp.tool()
    async def find_sportsbook_market_disagreements(
        market: str,
        league: Optional[str] = None,
        sport: Optional[str] = None,
        team_id: Optional[str] = None,
        bookmakers: Optional[str] = None,
        starts_after: Optional[str] = None,
        starts_before: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
        top_n: int = 10,
        min_bookmakers: int = 2,
    ) -> dict:
        """Rank cross-book disagreement for one game market on one slate page.

        `market` must be moneyline, spread, or total. Provide exactly one of
        `league` or `sport`, with the same optional team/date/bookmaker/cursor
        scope supported by `get_sportsbook_slate`. The tool makes exactly one
        bounded slate-page provider read and performs ranking locally. It does
        not silently follow `nextCursor`.

        Moneylines rank by implied-probability spread. Spreads and totals rank
        by posted-line range first, then price disagreement among books offering
        the identical line. Results describe market disagreement only; they do
        not estimate expected value or recommend/place a wager.
        """
        try:
            return _find_sportsbook_market_disagreements(
                _client(),
                market=market,
                league=league,
                sport=sport,
                team_id=team_id,
                bookmakers=bookmakers,
                starts_after=starts_after,
                starts_before=starts_before,
                cursor=cursor,
                limit=limit,
                top_n=top_n,
                min_bookmakers=min_bookmakers,
            )
        except Exception as exc:
            return _error("find sportsbook market disagreements", exc)

    @mcp.tool()
    async def find_sportsbook_player_prop_disagreements(
        event_id: str,
        player_name: str,
        league: str,
        team_id: str,
        stat_id: Optional[str] = None,
        bet_type: Optional[str] = None,
        bookmakers: Optional[str] = None,
        top_n: int = 10,
        min_bookmakers: int = 2,
    ) -> dict:
        """Rank cross-book disagreement in one player's exact-event prop markets.

        `event_id`, `player_name`, `league`, and provider `team_id` are required
        so the provider read stays exact and bounded. Optional `stat_id` and
        `bet_type` narrow the markets. Results are grouped/ranked within each
        bet type, so O/U line disagreement is never mixed with yes/no price
        disagreement. The provider path performs one exact event read and only
        the existing single player-roster fallback when embedded player identity
        is unavailable. No hidden pagination or wager action is performed.
        """
        try:
            return _find_sportsbook_player_prop_disagreements(
                _client(),
                event_id=event_id,
                player_name=player_name,
                league=league,
                team_id=team_id,
                stat_id=stat_id,
                bet_type=bet_type,
                bookmakers=bookmakers,
                top_n=top_n,
                min_bookmakers=min_bookmakers,
            )
        except Exception as exc:
            return _error("find sportsbook player-prop disagreements", exc)
