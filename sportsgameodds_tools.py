"""Register SportsGameOdds tools onto an existing FastMCP server.

This module is intentionally provider-focused and side-effect free until
`register_sportsgameodds_tools(mcp)` is called. The production fantasy-football
MCP can therefore expose ESPN, FantasyPros, and multi-sport sportsbook data
through one server without modifying the large ESPN implementation module.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, Optional

from sportsgameodds_analysis import build_fantasy_market_signal, compact_player_prop_snapshot
from sportsgameodds_comparison import (
    build_game_market_comparison,
    build_player_prop_comparison,
    game_market_odd_ids,
    normalize_comparison_market,
)
from sportsgameodds_client import (
    DEFAULT_BOOKMAKERS,
    SportsGameOddsClient,
    SportsGameOddsError,
    normalize_bookmakers,
)


# Common league aliases accepted by the generic sportsbook tools. The API can
# support additional leagueIDs too; callers may pass a provider leagueID not in
# this map and it will be normalized to uppercase rather than rejected.
LEAGUE_ALIASES = {
    "NFL": "NFL",
    "NCAAF": "NCAAF",
    "CFB": "NCAAF",
    "COLLEGE FOOTBALL": "NCAAF",
    "NBA": "NBA",
    "NCAAB": "NCAAB",
    "CBB": "NCAAB",
    "COLLEGE BASKETBALL": "NCAAB",
    "NHL": "NHL",
    "MLB": "MLB",
    "WNBA": "WNBA",
    "MLS": "MLS",
    "EPL": "EPL",
    "PGA": "PGA_MEN",
    "PGA_MEN": "PGA_MEN",
}

COMMON_LEAGUES = (
    {"leagueID": "NFL", "name": "NFL", "sportID": "FOOTBALL"},
    {"leagueID": "NCAAF", "name": "College Football", "sportID": "FOOTBALL"},
    {"leagueID": "NBA", "name": "NBA", "sportID": "BASKETBALL"},
    {"leagueID": "NCAAB", "name": "College Basketball", "sportID": "BASKETBALL"},
    {"leagueID": "NHL", "name": "NHL", "sportID": "HOCKEY"},
    {"leagueID": "MLB", "name": "MLB", "sportID": "BASEBALL"},
    {"leagueID": "WNBA", "name": "WNBA", "sportID": "BASKETBALL"},
    {"leagueID": "MLS", "name": "MLS", "sportID": "SOCCER"},
    {"leagueID": "EPL", "name": "Premier League", "sportID": "SOCCER"},
    {"leagueID": "PGA_MEN", "name": "PGA Men", "sportID": "GOLF"},
)


def _log_error(message: str) -> None:
    print(message, file=sys.stderr)


def _client() -> SportsGameOddsClient:
    return SportsGameOddsClient()


def _error(action: str, exc: Exception) -> dict:
    if isinstance(exc, SportsGameOddsError):
        _log_error(f"SportsGameOdds {action} failed: {exc}")
        return {"error": "sportsgameodds_error", "message": str(exc)}
    if isinstance(exc, ValueError):
        return {"error": "invalid_argument", "message": str(exc)}
    _log_error(f"SportsGameOdds {action} failed: {exc.__class__.__name__}")
    traceback.print_exc(file=sys.stderr)
    return {"error": "request_failed", "message": f"Unable to {action}."}


def _normalize_league(league: str) -> str:
    value = (league or "").strip()
    if not value:
        raise ValueError("league is required")
    key = " ".join(value.upper().replace("_", " ").split())
    return LEAGUE_ALIASES.get(key, value.upper().replace(" ", "_"))


def _generic_slate(
    client: SportsGameOddsClient,
    *,
    league: Optional[str] = None,
    sport: Optional[str] = None,
    team_id: Optional[str] = None,
    bookmakers: Optional[str] = None,
    starts_after: Optional[str] = None,
    starts_before: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Normalize MCP scope aliases and delegate provider work to the client."""
    if bool(league) == bool(sport):
        raise ValueError("Provide exactly one of league or sport.")
    league_id = _normalize_league(league) if league else None
    sport_id = sport.strip().upper().replace(" ", "_") if sport else None
    return client.sportsbook_slate(
        league=league_id,
        sport=sport_id,
        team_id=team_id,
        bookmakers=bookmakers,
        starts_after=starts_after,
        starts_before=starts_before,
        cursor=cursor,
        limit=limit,
    )


def _generic_player_props(
    client: SportsGameOddsClient,
    *,
    player_name: str,
    league: str,
    team_id: str,
    event_id: Optional[str] = None,
    stat_id: Optional[str] = None,
    bookmakers: Optional[str] = None,
    include_alt_lines: bool = False,
    limit: int = 4,
) -> dict:
    """Normalize MCP aliases, then compact the client's provider-neutral raw props."""
    league_id = _normalize_league(league)
    team_value = (team_id or "").strip()
    if not team_value:
        raise ValueError(
            "team_id is required for generic player props. Use a teamID returned by get_sportsbook_slate."
        )
    raw = client.sportsbook_player_props(
        player_name=player_name,
        league=league_id,
        team_id=team_value,
        event_id=event_id,
        stat_id=stat_id,
        bookmakers=bookmakers,
        include_alt_lines=include_alt_lines,
        limit=limit,
    )
    # Generic sports should retain all full-game markets; football-only fantasy
    # relevance filtering remains an NFL compatibility concern.
    return compact_player_prop_snapshot(raw, relevant_only=False)


def _compare_sportsbook_market(
    client: SportsGameOddsClient,
    *,
    event_id: str,
    league: str,
    market: str,
    bookmakers: Optional[str] = None,
    player_name: Optional[str] = None,
    team_id: Optional[str] = None,
    stat_id: Optional[str] = None,
    bet_type: Optional[str] = None,
) -> dict:
    """Compare one exact-event game market or player prop across sportsbooks."""
    event_value = (event_id or "").strip()
    if not event_value:
        raise ValueError("event_id is required for market comparison")
    league_id = _normalize_league(league)
    market_kind = normalize_comparison_market(market)
    books = normalize_bookmakers(bookmakers)

    if market_kind != "player_prop":
        payload = client.events(
            eventID=event_value,
            leagueID=league_id,
            oddsAvailable="true",
            oddID=",".join(game_market_odd_ids(market_kind)),
            bookmakerID=",".join(books),
            includeAltLines="false",
            limit=1,
        )
        return build_game_market_comparison(
            payload,
            market=market_kind,
            bookmakers_requested=books,
            event_id=event_value,
        )

    player_value = (player_name or "").strip()
    team_value = (team_id or "").strip()
    stat_value = (stat_id or "").strip()
    if not player_value:
        raise ValueError("player_name is required for player_prop comparison")
    if not team_value:
        raise ValueError("team_id is required for player_prop comparison")
    if not stat_value:
        raise ValueError("stat_id is required for player_prop comparison")

    raw = client.sportsbook_player_props(
        player_name=player_value,
        league=league_id,
        team_id=team_value,
        event_id=event_value,
        stat_id=stat_value,
        bookmakers=books,
        include_alt_lines=False,
        limit=1,
    )
    snapshot = compact_player_prop_snapshot(raw, relevant_only=False)
    return build_player_prop_comparison(
        snapshot,
        stat_id=stat_value,
        bet_type=bet_type,
        event_id=event_value,
    )


def register_sportsgameodds_tools(mcp) -> None:
    """Register the read-only sportsbook/fantasy-market tool surface."""

    @mcp.tool()
    async def get_sportsbook_usage() -> dict:
        """Return SportsGameOdds plan and rate-limit usage without account identifiers."""
        try:
            payload = _client().usage()
            data = payload.get("data") or {}
            return {
                "tier": data.get("tier"),
                "isActive": data.get("isActive"),
                "rateLimits": data.get("rateLimits"),
            }
        except Exception as exc:
            return _error("retrieve usage", exc)

    @mcp.tool()
    async def find_sportsbook_team(
        team_name: str,
        league: str,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        """Resolve a human team name to a SportsGameOdds teamID within one league page.

        Examples include team_name="Broncos", "DEN", "Denver Broncos", or
        "Lakers". If no confident match is found and `nextCursor` is present,
        call again with that cursor unchanged. Fresh provider pages are merged
        into a 24-hour local metadata cache under the project application home;
        cache hits make no provider request. The default page size is 100 objects.
        """
        try:
            return _client().sportsbook_team_search(
                team_name=team_name,
                league=_normalize_league(league),
                cursor=cursor,
                limit=limit,
            )
        except Exception as exc:
            return _error("resolve sportsbook team", exc)

    @mcp.tool()
    async def get_sportsbook_slate(
        league: Optional[str] = None,
        sport: Optional[str] = None,
        team_id: Optional[str] = None,
        bookmakers: Optional[str] = None,
        starts_after: Optional[str] = None,
        starts_before: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Get one page of current game odds for any SportsGameOdds league or sport.

        Provide exactly one of `league` or `sport`. Examples: league="NCAAF"
        for college football, league="NBA", league="NCAAB", league="NHL",
        league="MLB", or sport="FOOTBALL" to span football leagues. Returns
        core moneyline, spread, and total markets from selected sportsbooks.
        Optional `team_id` should be a provider teamID returned by the slate or
        `find_sportsbook_team` and narrows the provider query to that team.
        `starts_after` and `starts_before` accept provider-compatible ISO-8601
        date/time strings and can bound the slate to a requested day or window.
        If `nextCursor` is returned, pass it back unchanged as `cursor` while
        keeping all other query arguments, including the date bounds, the same.
        Omit `cursor` for page one.
        """
        try:
            return _generic_slate(
                _client(), league=league, sport=sport, team_id=team_id, bookmakers=bookmakers,
                starts_after=starts_after, starts_before=starts_before, cursor=cursor, limit=limit
            )
        except Exception as exc:
            return _error("retrieve sportsbook slate", exc)

    @mcp.tool()
    async def get_sportsbook_player_props(
        player_name: str,
        league: str,
        team_id: str,
        event_id: Optional[str] = None,
        stat_id: Optional[str] = None,
        bookmakers: Optional[str] = None,
        include_alt_lines: bool = False,
    ) -> dict:
        """Get current full-game player props for any supported league.

        `league` is a provider leagueID such as NFL, NCAAF, NBA, NCAAB, NHL,
        MLB, or WNBA. `team_id` should be a SportsGameOdds teamID returned by
        get_sportsbook_slate. Optional `event_id` should be an eventID from
        that slate; when supplied, SportsGameOdds targets that exact event. The
        result keeps only currently available full-game markets and pairs
        opposing sides into compact markets.
        """
        try:
            return _generic_player_props(
                _client(),
                player_name=player_name,
                league=league,
                team_id=team_id,
                event_id=event_id,
                stat_id=stat_id,
                bookmakers=bookmakers,
                include_alt_lines=include_alt_lines,
            )
        except Exception as exc:
            return _error("retrieve sportsbook player props", exc)

    @mcp.tool()
    async def compare_sportsbook_market(
        event_id: str,
        league: str,
        market: str,
        bookmakers: Optional[str] = None,
        player_name: Optional[str] = None,
        team_id: Optional[str] = None,
        stat_id: Optional[str] = None,
        bet_type: Optional[str] = None,
    ) -> dict:
        """Compare one exact-event sportsbook market across selected books.

        `market` accepts moneyline, spread, total, or player_prop. Game-market
        comparisons require only `event_id`, `league`, and `market` and make one
        targeted event request. For `player_prop`, also provide `player_name`,
        provider `team_id`, and `stat_id`; optional `bet_type` can narrow a stat
        with multiple market types (for example `ou` or `yn`). Prices are only
        ranked against offers on the same posted line. Different spread/total/
        prop lines remain separate so the tool does not manufacture a false
        single "best bet". This tool is read-only and cannot place wagers.
        """
        try:
            return _compare_sportsbook_market(
                _client(),
                event_id=event_id,
                league=league,
                market=market,
                bookmakers=bookmakers,
                player_name=player_name,
                team_id=team_id,
                stat_id=stat_id,
                bet_type=bet_type,
            )
        except Exception as exc:
            return _error("compare sportsbook market", exc)

    @mcp.tool()
    async def get_nfl_sportsbook_slate(
        bookmakers: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Get current NFL moneylines, spreads, and game totals from selected sportsbooks."""
        try:
            return _client().nfl_slate(
                bookmakers=normalize_bookmakers(bookmakers),
                limit=limit,
            )
        except Exception as exc:
            return _error("retrieve NFL sportsbook slate", exc)

    @mcp.tool()
    async def get_nfl_player_props(
        player_name: str,
        team: str,
        stat_id: Optional[str] = None,
        bookmakers: Optional[str] = None,
        include_alt_lines: bool = False,
    ) -> dict:
        """Get compact, fantasy-relevant full-game NFL player props.

        `team` should be an NFL abbreviation such as DEN. Empty/unavailable
        markets, quarter/half specialty props, and ambiguous generic touchdown
        O/U markets are omitted. This tool is read-only and cannot place bets.
        """
        try:
            raw = _client().nfl_player_props(
                player_name=player_name,
                team=team,
                stat_id=stat_id,
                bookmakers=normalize_bookmakers(bookmakers),
                include_alt_lines=include_alt_lines,
            )
            return compact_player_prop_snapshot(raw)
        except Exception as exc:
            return _error("retrieve NFL player props", exc)

    @mcp.tool()
    async def get_fantasy_market_signal(
        player_name: str,
        team: str,
        position: Optional[str] = None,
        bookmakers: Optional[str] = None,
    ) -> dict:
        """Return a compact NFL sportsbook market signal for fantasy analysis.

        Position-aware filtering keeps the most useful markets for QB/RB/WR/TE
        while preserving sportsbook disagreement, consensus lines, and fair
        price direction. The result is independent market evidence, not a
        fantasy projection and not a wagering recommendation.
        """
        try:
            raw = _client().nfl_player_props(
                player_name=player_name,
                team=team,
                bookmakers=normalize_bookmakers(bookmakers),
                include_alt_lines=False,
            )
            snapshot = compact_player_prop_snapshot(raw)
            signal = build_fantasy_market_signal(snapshot, position=position)
            if position and signal.get("player") and signal["player"].get("position") is None:
                signal["player"] = dict(signal["player"])
                signal["player"]["position"] = position.strip().upper()
                signal["player"]["positionSource"] = "caller_or_fantasy_provider"
            return signal
        except Exception as exc:
            return _error("build fantasy market signal", exc)

    @mcp.tool()
    async def get_supported_sportsbook_leagues() -> dict:
        """Return common league IDs understood by the generic sportsbook tools."""
        return {
            "commonLeagues": list(COMMON_LEAGUES),
            "aliases": dict(LEAGUE_ALIASES),
            "note": (
                "SportsGameOdds supports additional leagues. A provider leagueID may be passed "
                "directly to get_sportsbook_slate even when it is not listed here. Coverage "
                "depends on the configured API plan and current bookmaker markets."
            ),
        }

    @mcp.tool()
    async def get_supported_sportsbooks() -> dict:
        """Return the default SportsGameOdds bookmaker IDs used by this MCP."""
        return {
            "bookmakers": list(DEFAULT_BOOKMAKERS),
            "note": (
                "Coverage varies by plan, sport, market, and event. These are "
                "provider IDs, not a guarantee every book has every market."
            ),
        }
