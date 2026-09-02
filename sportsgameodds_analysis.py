"""Pure normalization helpers for SportsGameOdds market data.

These helpers turn the provider's detailed odd-by-odd payload into compact,
fantasy-oriented market signals. They do not perform network I/O and do not
make wagering recommendations.
"""

from __future__ import annotations

from statistics import median
from typing import Any


_RELEVANT_STAT_TOKENS = (
    "passing",
    "rushing",
    "receiving",
    "reception",
    "touchdowns",
    "fantasyscore",
)

_EXCLUDED_STAT_IDS = {
    "defense_interceptions",
    "firstTouchdown",
    "lastTouchdown",
}

# Provider generic touchdown O/U markets can mean player-scored TDs rather
# than passing TDs and can carry sparse/extreme prices. The clearer yes/no
# anytime-TD market remains available, while passing_touchdowns is preserved
# independently for QBs.
_EXCLUDED_MARKET_PAIRS = {
    ("touchdowns", "ou"),
}

_POSITION_CORE_MARKETS = {
    "QB": {
        ("passing_yards", "ou"),
        ("passing_touchdowns", "ou"),
        ("passing_attempts", "ou"),
        ("passing_completions", "ou"),
        ("rushing_yards", "ou"),
    },
    "RB": {
        ("rushing_yards", "ou"),
        ("rushing_attempts", "ou"),
        ("receiving_yards", "ou"),
        ("receptions", "ou"),
        ("touchdowns", "yn"),
    },
    "WR": {
        ("receiving_yards", "ou"),
        ("receptions", "ou"),
        ("touchdowns", "yn"),
    },
    "TE": {
        ("receiving_yards", "ou"),
        ("receptions", "ou"),
        ("touchdowns", "yn"),
    },
}


def _is_fantasy_relevant_stat(stat_id: str | None) -> bool:
    if not stat_id:
        return False
    if stat_id in _EXCLUDED_STAT_IDS:
        return False
    lowered = stat_id.lower()
    return any(token in lowered for token in _RELEVANT_STAT_TOKENS)


def _number_or_text(value: Any) -> Any:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return value
    if parsed.is_integer():
        return int(parsed)
    return parsed


def _consensus_line(bookmakers: dict[str, dict[str, Any]]) -> Any:
    """Median currently available line, one observation per sportsbook."""
    values = []
    for book in bookmakers.values():
        line = book.get("line")
        if line is None:
            continue
        try:
            values.append(float(line))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    result = median(values)
    return int(result) if float(result).is_integer() else result


def _line_range(bookmakers: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    values = []
    for book in bookmakers.values():
        try:
            if book.get("line") is not None:
                values.append(float(book["line"]))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    low, high = min(values), max(values)
    def clean(value: float) -> int | float:
        return int(value) if value.is_integer() else value
    return {"min": clean(low), "max": clean(high), "spread": clean(high - low)}


def _american_probability(value: Any) -> float | None:
    if value is None:
        return None
    try:
        odds = int(str(value).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return (-odds) / ((-odds) + 100.0)


def _fair_direction(fair_prices: dict[str, Any]) -> dict[str, Any] | None:
    """Describe which side the provider's fair prices imply is more likely.

    This is descriptive market context only, not a betting recommendation.
    A small probability gap is labelled balanced to avoid overstating noise.
    """
    pairs = (("over", "under"), ("yes", "no"))
    for first, second in pairs:
        p1 = _american_probability(fair_prices.get(first))
        p2 = _american_probability(fair_prices.get(second))
        if p1 is None or p2 is None:
            continue
        gap = abs(p1 - p2)
        favored = "balanced" if gap < 0.03 else (first if p1 > p2 else second)
        return {
            "favoredSide": favored,
            "probabilities": {first: round(p1, 4), second: round(p2, 4)},
            "probabilityGap": round(gap, 4),
        }
    return None


def compact_player_prop_snapshot(raw: dict[str, Any], *, relevant_only: bool = True) -> dict[str, Any]:
    """Collapse raw SGO player props into paired, game-level market snapshots.

    - Only full-game markets are included.
    - Only bookmaker entries marked available=true are included.
    - Markets with no currently available selected-book prices disappear.
    - Over/under or yes/no sides are paired into one market.
    - Generic touchdown O/U is omitted because its semantics differ from
      passing touchdowns; anytime touchdown yes/no remains available.
    - `consensusLine` is the median of one current line per sportsbook and is
      descriptive only; it is not a projection or a recommended wager.
    """
    compact_events = []

    for event in raw.get("events") or []:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for prop in event.get("props") or []:
            if prop.get("periodID") != "game":
                continue
            stat_id = prop.get("statID")
            bet_type = str(prop.get("betTypeID") or "")
            if relevant_only and not _is_fantasy_relevant_stat(stat_id):
                continue
            if (str(stat_id), bet_type) in _EXCLUDED_MARKET_PAIRS:
                continue

            books = prop.get("byBookmaker") or {}
            available_books = {
                book_id: book
                for book_id, book in books.items()
                if isinstance(book, dict) and book.get("available") is True
            }
            if not available_books:
                continue

            key = (str(stat_id), bet_type, str(prop.get("marketName")))
            market = grouped.setdefault(
                key,
                {
                    "statID": stat_id,
                    "marketName": prop.get("marketName"),
                    "betTypeID": prop.get("betTypeID"),
                    "periodID": "game",
                    "fairLine": _number_or_text(prop.get("fairOverUnder")),
                    "fairPrices": {},
                    "bookmakers": {},
                },
            )

            side = str(prop.get("sideID") or "unknown")
            if prop.get("fairOdds") is not None:
                market["fairPrices"][side] = prop.get("fairOdds")
            if market.get("fairLine") is None and prop.get("fairOverUnder") is not None:
                market["fairLine"] = _number_or_text(prop.get("fairOverUnder"))

            for book_id, book in available_books.items():
                book_market = market["bookmakers"].setdefault(
                    book_id,
                    {
                        "line": _number_or_text(book.get("overUnder") or book.get("spread")),
                        "prices": {},
                        "lastUpdatedAt": book.get("lastUpdatedAt"),
                    },
                )
                line = book.get("overUnder") or book.get("spread")
                if line is not None:
                    book_market["line"] = _number_or_text(line)
                if book.get("odds") is not None:
                    book_market["prices"][side] = book.get("odds")
                updated = book.get("lastUpdatedAt")
                if updated and (not book_market.get("lastUpdatedAt") or updated > book_market["lastUpdatedAt"]):
                    book_market["lastUpdatedAt"] = updated

        markets = []
        for market in grouped.values():
            market["consensusLine"] = _consensus_line(market["bookmakers"])
            markets.append(market)
        markets.sort(key=lambda item: (str(item.get("statID")), str(item.get("betTypeID"))))

        if markets:
            compact_events.append(
                {
                    "eventID": event.get("eventID"),
                    "startsAt": event.get("startsAt"),
                    "teams": event.get("teams"),
                    "markets": markets,
                }
            )

    return {
        "player": raw.get("player"),
        "requestedStatID": raw.get("requestedStatID"),
        "bookmakersRequested": raw.get("bookmakers") or [],
        "events": compact_events,
        "notice": raw.get("notice"),
        "interpretation": (
            "consensusLine is the median of currently available sportsbook lines; "
            "fairLine/fairPrices come from SportsGameOdds. These are market data, not fantasy projections."
        ),
    }


def build_fantasy_market_signal(snapshot: dict[str, Any], *, position: str | None = None) -> dict[str, Any]:
    """Extract a small position-aware market signal from a compact snapshot."""
    normalized_position = (position or "").strip().upper() or None
    core_pairs = _POSITION_CORE_MARKETS.get(normalized_position)
    events_out = []

    for event in snapshot.get("events") or []:
        signals = []
        for market in event.get("markets") or []:
            pair = (str(market.get("statID")), str(market.get("betTypeID")))
            if core_pairs is not None and pair not in core_pairs:
                continue
            signal = {
                "statID": market.get("statID"),
                "marketName": market.get("marketName"),
                "marketType": market.get("betTypeID"),
                "consensusLine": market.get("consensusLine"),
                "fairLine": market.get("fairLine"),
                "fairDirection": _fair_direction(market.get("fairPrices") or {}),
                "bookmakerCount": len(market.get("bookmakers") or {}),
                "lineRange": _line_range(market.get("bookmakers") or {}),
                "bookmakers": market.get("bookmakers") or {},
            }
            signals.append(signal)
        if signals:
            events_out.append(
                {
                    "eventID": event.get("eventID"),
                    "startsAt": event.get("startsAt"),
                    "teams": event.get("teams"),
                    "signals": signals,
                }
            )

    return {
        "player": snapshot.get("player"),
        "position": normalized_position,
        "bookmakersRequested": snapshot.get("bookmakersRequested") or [],
        "events": events_out,
        "interpretation": (
            "Sportsbook lines are treated as independent market evidence for fantasy analysis, "
            "not as fantasy projections or wagering recommendations. lineRange highlights book "
            "disagreement; fairDirection describes the provider's fair-price lean when both sides exist."
        ),
    }
