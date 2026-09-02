"""Pure sportsbook market comparison helpers.

This module compares currently available SportsGameOdds offers without network
I/O and without making wagering recommendations. It keeps line and price
comparisons separate so offers on different spreads/totals are not treated as
if they were identical propositions.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Iterable


GAME_MARKET_ODD_IDS = {
    "moneyline": (
        "points-home-game-ml-home",
        "points-away-game-ml-away",
    ),
    "spread": (
        "points-home-game-sp-home",
        "points-away-game-sp-away",
    ),
    "total": (
        "points-all-game-ou-over",
        "points-all-game-ou-under",
    ),
}

GAME_MARKET_ALIASES = {
    "ml": "moneyline",
    "moneyline": "moneyline",
    "money line": "moneyline",
    "sp": "spread",
    "spread": "spread",
    "point spread": "spread",
    "ou": "total",
    "o/u": "total",
    "over under": "total",
    "over/under": "total",
    "total": "total",
    "totals": "total",
    "player prop": "player_prop",
    "player_prop": "player_prop",
    "player-prop": "player_prop",
    "prop": "player_prop",
}


def normalize_comparison_market(market: str) -> str:
    value = " ".join((market or "").strip().lower().replace("_", " ").split())
    normalized = GAME_MARKET_ALIASES.get(value)
    if normalized is None:
        allowed = "moneyline, spread, total, or player_prop"
        raise ValueError(f"market must be {allowed}")
    return normalized


def game_market_odd_ids(market: str) -> tuple[str, ...]:
    normalized = normalize_comparison_market(market)
    if normalized == "player_prop":
        raise ValueError("player_prop does not use fixed game oddIDs")
    return GAME_MARKET_ODD_IDS[normalized]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        iterable: Iterable[Any] = data
    elif isinstance(data, dict):
        iterable = data.values()
    else:
        return []
    return [row for row in iterable if isinstance(row, dict)]


def _number(value: Any) -> int | float | Any:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return value
    return int(parsed) if parsed.is_integer() else parsed


def _american_odds(value: Any) -> int | None:
    if value is None:
        return None
    try:
        odds = int(str(value).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    return odds if odds != 0 else None


def _implied_probability(value: Any) -> float | None:
    odds = _american_odds(value)
    if odds is None:
        return None
    if odds > 0:
        probability = 100.0 / (odds + 100.0)
    else:
        probability = (-odds) / ((-odds) + 100.0)
    return round(probability, 6)


def _book_line(book: dict[str, Any], bet_type: str) -> Any:
    if bet_type == "sp":
        return _number(book.get("spread"))
    if bet_type == "ou":
        return _number(book.get("overUnder"))
    return None


def _fair_line(odd: dict[str, Any], bet_type: str) -> Any:
    if bet_type == "sp":
        return _number(odd.get("fairSpread"))
    if bet_type == "ou":
        return _number(odd.get("fairOverUnder"))
    return None


def _book_consensus_line(odd: dict[str, Any], bet_type: str) -> Any:
    if bet_type == "sp":
        return _number(odd.get("bookSpread"))
    if bet_type == "ou":
        return _number(odd.get("bookOverUnder"))
    return None


def _numeric_lines(offers: list[dict[str, Any]]) -> list[float]:
    values = []
    for offer in offers:
        line = offer.get("line")
        try:
            if line is not None:
                values.append(float(line))
        except (TypeError, ValueError):
            continue
    return values


def _clean_float(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _line_range(offers: list[dict[str, Any]]) -> dict[str, Any] | None:
    values = _numeric_lines(offers)
    if not values:
        return None
    low, high = min(values), max(values)
    return {
        "min": _clean_float(low),
        "max": _clean_float(high),
        "spread": _clean_float(high - low),
    }


def _consensus_line(offers: list[dict[str, Any]]) -> Any:
    values = _numeric_lines(offers)
    if not values:
        return None
    return _clean_float(float(median(values)))


def _probability_range(offers: list[dict[str, Any]]) -> dict[str, float] | None:
    values = [offer["impliedProbability"] for offer in offers if offer.get("impliedProbability") is not None]
    if not values:
        return None
    low, high = min(values), max(values)
    return {"min": round(low, 6), "max": round(high, 6), "spread": round(high - low, 6)}


def _best_price_offer(offers: list[dict[str, Any]]) -> dict[str, Any] | None:
    priced = [(odds, offer) for offer in offers if (odds := _american_odds(offer.get("odds"))) is not None]
    if not priced:
        return None
    return dict(max(priced, key=lambda pair: pair[0])[1])


def _line_groups(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for offer in offers:
        line = offer.get("line")
        key = (type(line).__name__, repr(line))
        groups.setdefault(key, []).append(offer)

    output = []
    for grouped in groups.values():
        line = grouped[0].get("line")
        output.append(
            {
                "line": line,
                "bookmakerCount": len(grouped),
                "bestPriceOffer": _best_price_offer(grouped),
                "offers": sorted(grouped, key=lambda item: str(item.get("bookmakerID"))),
            }
        )

    def sort_key(group: dict[str, Any]) -> tuple[int, float | str]:
        line = group.get("line")
        try:
            return (0, float(line))
        except (TypeError, ValueError):
            return (1, str(line))

    output.sort(key=sort_key)
    return output


def _most_favorable_line(side: str, bet_type: str, offers: list[dict[str, Any]]) -> Any:
    values = _numeric_lines(offers)
    if not values:
        return None
    if bet_type == "sp":
        # A larger signed spread is always the more favorable posted handicap
        # for the selected team side (+3 > +2.5; -2.5 > -3).
        return _clean_float(max(values))
    if bet_type == "ou":
        if side == "over":
            return _clean_float(min(values))
        if side == "under":
            return _clean_float(max(values))
    return None


def _comparison_from_offers(
    *,
    side: str,
    bet_type: str,
    offers: list[dict[str, Any]],
    fair_odds: Any = None,
    fair_line: Any = None,
    book_odds: Any = None,
    book_line: Any = None,
    stat_entity_id: Any = None,
) -> dict[str, Any]:
    no_line_market = bet_type in {"ml", "yn"}
    return {
        "sideID": side,
        "statEntityID": stat_entity_id,
        "bookmakerCount": len(offers),
        "fairOdds": fair_odds,
        "fairImpliedProbability": _implied_probability(fair_odds),
        "fairLine": fair_line,
        "bookConsensusOdds": book_odds,
        "bookConsensusLine": book_line,
        "consensusPostedLine": _consensus_line(offers),
        "lineRange": _line_range(offers),
        "mostFavorablePostedLine": _most_favorable_line(side, bet_type, offers),
        "bestPostedPrice": _best_price_offer(offers) if no_line_market else None,
        "impliedProbabilityRange": _probability_range(offers),
        "lineGroups": _line_groups(offers),
    }


def _offers_from_odd(odd: dict[str, Any]) -> list[dict[str, Any]]:
    bet_type = str(odd.get("betTypeID") or "")
    offers = []
    for bookmaker_id, raw_book in _mapping(odd.get("byBookmaker")).items():
        if not isinstance(raw_book, dict) or raw_book.get("available") is not True:
            continue
        offers.append(
            {
                "bookmakerID": str(bookmaker_id),
                "line": _book_line(raw_book, bet_type),
                "odds": raw_book.get("odds"),
                "impliedProbability": _implied_probability(raw_book.get("odds")),
                "lastUpdatedAt": raw_book.get("lastUpdatedAt"),
            }
        )
    return offers


def build_game_market_comparison(
    payload: dict[str, Any],
    *,
    market: str,
    bookmakers_requested: Iterable[str] = (),
    event_id: str | None = None,
) -> dict[str, Any]:
    """Compare one main game market from an exact-event provider response."""
    normalized_market = normalize_comparison_market(market)
    if normalized_market == "player_prop":
        raise ValueError("build_game_market_comparison requires a game market")

    rows = _rows(payload)
    event = None
    if event_id:
        event = next((row for row in rows if str(row.get("eventID")) == str(event_id)), None)
    if event is None and rows:
        event = rows[0]

    event_summary = None
    markets = []
    if event is not None:
        status = _mapping(event.get("status"))
        event_summary = {
            "eventID": event.get("eventID"),
            "sportID": event.get("sportID"),
            "leagueID": event.get("leagueID"),
            "startsAt": status.get("startsAt"),
            "status": event.get("status"),
            "teams": event.get("teams"),
        }
        odds = _mapping(event.get("odds"))
        selected_odds = [
            odds[odd_id]
            for odd_id in GAME_MARKET_ODD_IDS[normalized_market]
            if isinstance(odds.get(odd_id), dict)
        ]
        if selected_odds:
            sides = []
            for odd in selected_odds:
                bet_type = str(odd.get("betTypeID") or "")
                offers = _offers_from_odd(odd)
                sides.append(
                    _comparison_from_offers(
                        side=str(odd.get("sideID") or "unknown"),
                        bet_type=bet_type,
                        offers=offers,
                        fair_odds=odd.get("fairOdds"),
                        fair_line=_fair_line(odd, bet_type),
                        book_odds=odd.get("bookOdds"),
                        book_line=_book_consensus_line(odd, bet_type),
                        stat_entity_id=odd.get("statEntityID"),
                    )
                )
            first = selected_odds[0]
            markets.append(
                {
                    "marketName": first.get("marketName") or normalized_market,
                    "statID": first.get("statID"),
                    "periodID": first.get("periodID") or "game",
                    "betTypeID": first.get("betTypeID"),
                    "sides": sides,
                }
            )

    return {
        "scope": "game",
        "requestedMarket": normalized_market,
        "bookmakersRequested": list(bookmakers_requested),
        "event": event_summary,
        "markets": markets,
        "notice": payload.get("notice"),
        "interpretation": (
            "Offers are grouped by identical posted line before price comparison. "
            "bestPriceOffer inside a line group is the highest American price for that exact line. "
            "mostFavorablePostedLine describes the handicap/total only and does not account for price. "
            "Fair fields are SportsGameOdds consensus estimates; this output is market comparison data, "
            "not a wagering recommendation."
        ),
    }


def _offers_from_compact_market(market: dict[str, Any], side: str) -> list[dict[str, Any]]:
    offers = []
    for bookmaker_id, raw_book in _mapping(market.get("bookmakers")).items():
        prices = _mapping(raw_book.get("prices")) if isinstance(raw_book, dict) else {}
        odds = prices.get(side)
        if odds is None:
            continue
        offers.append(
            {
                "bookmakerID": str(bookmaker_id),
                "line": raw_book.get("line"),
                "odds": odds,
                "impliedProbability": _implied_probability(odds),
                "lastUpdatedAt": raw_book.get("lastUpdatedAt"),
            }
        )
    return offers


def build_player_prop_comparison(
    snapshot: dict[str, Any],
    *,
    stat_id: str,
    bet_type: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Compare one player's compact prop markets across sportsbooks."""
    requested_stat = (stat_id or "").strip()
    if not requested_stat:
        raise ValueError("stat_id is required for player_prop comparison")
    requested_bet_type = (bet_type or "").strip().lower() or None

    events = [event for event in snapshot.get("events") or [] if isinstance(event, dict)]
    event = None
    if event_id:
        event = next((item for item in events if str(item.get("eventID")) == str(event_id)), None)
    if event is None and events:
        event = events[0]

    event_summary = None
    output_markets = []
    if event is not None:
        event_summary = {
            "eventID": event.get("eventID"),
            "startsAt": event.get("startsAt"),
            "teams": event.get("teams"),
        }
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if str(market.get("statID") or "").lower() != requested_stat.lower():
                continue
            market_bet_type = str(market.get("betTypeID") or "").lower()
            if requested_bet_type and market_bet_type != requested_bet_type:
                continue

            fair_prices = _mapping(market.get("fairPrices"))
            side_ids = set(fair_prices)
            for raw_book in _mapping(market.get("bookmakers")).values():
                if isinstance(raw_book, dict):
                    side_ids.update(_mapping(raw_book.get("prices")))

            sides = []
            for side in sorted(str(value) for value in side_ids):
                offers = _offers_from_compact_market(market, side)
                sides.append(
                    _comparison_from_offers(
                        side=side,
                        bet_type=market_bet_type,
                        offers=offers,
                        fair_odds=fair_prices.get(side),
                        fair_line=market.get("fairLine"),
                    )
                )
            output_markets.append(
                {
                    "marketName": market.get("marketName"),
                    "statID": market.get("statID"),
                    "periodID": market.get("periodID"),
                    "betTypeID": market.get("betTypeID"),
                    "sides": sides,
                }
            )

    return {
        "scope": "player_prop",
        "player": snapshot.get("player"),
        "requestedStatID": requested_stat,
        "requestedBetTypeID": requested_bet_type,
        "bookmakersRequested": snapshot.get("bookmakersRequested") or [],
        "event": event_summary,
        "markets": output_markets,
        "notice": snapshot.get("notice"),
        "interpretation": (
            "Player-prop offers are grouped by identical posted line before price comparison. "
            "For over markets a lower posted line is more favorable to the over; for under markets "
            "a higher line is more favorable to the under. Price and line are intentionally reported "
            "separately rather than collapsed into a wagering recommendation."
        ),
    }
