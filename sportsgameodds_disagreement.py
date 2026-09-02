"""Pure cross-book disagreement detection for sportsbook market data.

This module ranks descriptive market disagreement without network I/O and
without turning line/price differences into expected value or wager advice.
Game-market and player-prop ranking intentionally keep posted-line disagreement
separate from same-line price disagreement.
"""

from __future__ import annotations

from typing import Any, Iterable

from sportsgameodds_comparison import (
    build_game_market_comparison,
    build_player_prop_comparison,
    normalize_comparison_market,
)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_float(value: float) -> int | float:
    return int(value) if value.is_integer() else round(value, 6)


def _same_line_probability_spread(line_groups: Iterable[dict[str, Any]]) -> float:
    widest = 0.0
    for group in line_groups:
        if not isinstance(group, dict):
            continue
        probabilities = []
        for offer in group.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            probability = _as_float(offer.get("impliedProbability"))
            if probability is not None:
                probabilities.append(probability)
        if len(probabilities) >= 2:
            widest = max(widest, max(probabilities) - min(probabilities))
    return widest


def _side_disagreement(side: dict[str, Any]) -> dict[str, Any]:
    line_range = side.get("lineRange") if isinstance(side.get("lineRange"), dict) else None
    probability_range = (
        side.get("impliedProbabilityRange")
        if isinstance(side.get("impliedProbabilityRange"), dict)
        else None
    )
    line_spread = _as_float(line_range.get("spread")) if line_range else None
    probability_spread = _as_float(probability_range.get("spread")) if probability_range else None
    same_line_probability_spread = _same_line_probability_spread(side.get("lineGroups") or [])
    return {
        "sideID": side.get("sideID"),
        "bookmakerCount": int(side.get("bookmakerCount") or 0),
        "lineRange": line_range,
        "consensusPostedLine": side.get("consensusPostedLine"),
        "mostFavorablePostedLine": side.get("mostFavorablePostedLine"),
        "impliedProbabilityRange": probability_range,
        "sameLinePriceProbabilitySpread": round(same_line_probability_spread, 6),
        "sameLinePriceProbabilitySpreadPctPts": round(same_line_probability_spread * 100.0, 3),
        "_lineSpread": line_spread or 0.0,
        "_probabilitySpread": probability_spread or 0.0,
    }


def _finalize_disagreement_row(
    *,
    event_summary: dict[str, Any],
    market_label: str,
    side_rows: list[dict[str, Any]],
    stat_id: Any = None,
    market_name: Any = None,
    bet_type: Any = None,
) -> dict[str, Any] | None:
    if not side_rows:
        return None
    max_line_spread = max((row["_lineSpread"] for row in side_rows), default=0.0)
    max_probability_spread = max((row["_probabilitySpread"] for row in side_rows), default=0.0)
    max_same_line_probability_spread = max(
        (row["sameLinePriceProbabilitySpread"] for row in side_rows), default=0.0
    )
    max_bookmaker_count = max((row["bookmakerCount"] for row in side_rows), default=0)
    for row in side_rows:
        row.pop("_lineSpread", None)
        row.pop("_probabilitySpread", None)
    return {
        "event": event_summary,
        "market": market_label,
        "statID": stat_id,
        "marketName": market_name,
        "betTypeID": bet_type,
        "bookmakerCount": max_bookmaker_count,
        "maxPostedLineSpread": _clean_float(max_line_spread),
        "maxImpliedProbabilitySpread": round(max_probability_spread, 6),
        "maxImpliedProbabilitySpreadPctPts": round(max_probability_spread * 100.0, 3),
        "maxSameLinePriceProbabilitySpread": round(max_same_line_probability_spread, 6),
        "maxSameLinePriceProbabilitySpreadPctPts": round(max_same_line_probability_spread * 100.0, 3),
        "sides": side_rows,
    }


def _event_disagreement(event: dict[str, Any], *, market: str, bookmakers: Iterable[str]) -> dict[str, Any] | None:
    event_id = event.get("eventID")
    comparison = build_game_market_comparison(
        {"data": [event]},
        market=market,
        bookmakers_requested=bookmakers,
        event_id=str(event_id) if event_id is not None else None,
    )
    markets = comparison.get("markets") or []
    if not markets or not isinstance(markets[0], dict):
        return None
    side_rows = [
        _side_disagreement(side)
        for side in markets[0].get("sides") or []
        if isinstance(side, dict)
    ]
    event_summary = comparison.get("event") or {
        "eventID": event_id,
        "sportID": event.get("sportID"),
        "leagueID": event.get("leagueID"),
        "startsAt": event.get("startsAt"),
        "teams": event.get("teams"),
    }
    return _finalize_disagreement_row(
        event_summary=event_summary,
        market_label=market,
        side_rows=side_rows,
    )


def rank_slate_market_disagreements(
    slate: dict[str, Any],
    *,
    market: str,
    min_bookmakers: int = 2,
    top_n: int = 10,
) -> dict[str, Any]:
    """Rank disagreement for one game market across one already-fetched slate page."""
    market_kind = normalize_comparison_market(market)
    if market_kind == "player_prop":
        raise ValueError("player_prop disagreement requires the player-prop disagreement path")

    minimum_books = max(2, min(int(min_bookmakers), 20))
    result_limit = max(1, min(int(top_n), 50))
    books = [str(value) for value in slate.get("bookmakers") or []]

    ranked = []
    events_scanned = 0
    markets_with_offers = 0
    for event in slate.get("events") or []:
        if not isinstance(event, dict):
            continue
        events_scanned += 1
        row = _event_disagreement(event, market=market_kind, bookmakers=books)
        if row is None:
            continue
        markets_with_offers += 1
        if row["bookmakerCount"] < minimum_books:
            continue
        if market_kind == "moneyline":
            has_disagreement = row["maxImpliedProbabilitySpread"] > 0
        else:
            has_disagreement = (
                row["maxPostedLineSpread"] > 0
                or row["maxSameLinePriceProbabilitySpread"] > 0
            )
        if has_disagreement:
            ranked.append(row)

    if market_kind == "moneyline":
        ranked.sort(
            key=lambda row: (
                row["maxImpliedProbabilitySpread"],
                row["bookmakerCount"],
                str((row.get("event") or {}).get("eventID")),
            ),
            reverse=True,
        )
        ranking_basis = (
            "Moneylines rank by maximum implied-probability spread across books, "
            "then bookmaker count."
        )
    else:
        ranked.sort(
            key=lambda row: (
                float(row["maxPostedLineSpread"]),
                row["maxSameLinePriceProbabilitySpread"],
                row["bookmakerCount"],
                str((row.get("event") or {}).get("eventID")),
            ),
            reverse=True,
        )
        ranking_basis = (
            "Spreads/totals rank by maximum posted-line range first, then by "
            "same-line implied-probability spread, then bookmaker count."
        )

    return {
        "requestedMarket": market_kind,
        "eventsScanned": events_scanned,
        "marketsWithOffers": markets_with_offers,
        "disagreementsFound": len(ranked),
        "minBookmakers": minimum_books,
        "results": ranked[:result_limit],
        "nextCursor": slate.get("nextCursor"),
        "notice": slate.get("notice"),
        "rankingBasis": ranking_basis,
        "interpretation": (
            "This output surfaces cross-book disagreement only. It does not estimate edge, "
            "expected value, win probability, or recommend a wager. Pagination remains explicit; "
            "only the supplied slate page is ranked."
        ),
    }


def _prop_market_row(
    snapshot: dict[str, Any],
    *,
    event: dict[str, Any],
    market: dict[str, Any],
) -> dict[str, Any] | None:
    stat_id = str(market.get("statID") or "")
    bet_type = str(market.get("betTypeID") or "").lower()
    if not stat_id or not bet_type:
        return None
    comparison = build_player_prop_comparison(
        snapshot,
        stat_id=stat_id,
        bet_type=bet_type,
        event_id=str(event.get("eventID")) if event.get("eventID") is not None else None,
    )
    markets = comparison.get("markets") or []
    if not markets or not isinstance(markets[0], dict):
        return None
    side_rows = [
        _side_disagreement(side)
        for side in markets[0].get("sides") or []
        if isinstance(side, dict)
    ]
    event_summary = comparison.get("event") or {
        "eventID": event.get("eventID"),
        "startsAt": event.get("startsAt"),
        "teams": event.get("teams"),
    }
    return _finalize_disagreement_row(
        event_summary=event_summary,
        market_label="player_prop",
        side_rows=side_rows,
        stat_id=market.get("statID"),
        market_name=market.get("marketName"),
        bet_type=market.get("betTypeID"),
    )


def rank_player_prop_disagreements(
    snapshot: dict[str, Any],
    *,
    stat_id: str | None = None,
    bet_type: str | None = None,
    min_bookmakers: int = 2,
    top_n: int = 10,
) -> dict[str, Any]:
    """Rank one player's exact-event prop disagreements within each bet type."""
    minimum_books = max(2, min(int(min_bookmakers), 20))
    result_limit = max(1, min(int(top_n), 50))
    requested_stat = (stat_id or "").strip().lower() or None
    requested_bet_type = (bet_type or "").strip().lower() or None

    groups: dict[str, list[dict[str, Any]]] = {}
    markets_scanned = 0
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            market_stat = str(market.get("statID") or "").lower()
            market_bet_type = str(market.get("betTypeID") or "").lower()
            if requested_stat and market_stat != requested_stat:
                continue
            if requested_bet_type and market_bet_type != requested_bet_type:
                continue
            markets_scanned += 1
            row = _prop_market_row(snapshot, event=event, market=market)
            if row is None or row["bookmakerCount"] < minimum_books:
                continue
            if market_bet_type in {"ou", "sp"}:
                has_disagreement = (
                    row["maxPostedLineSpread"] > 0
                    or row["maxSameLinePriceProbabilitySpread"] > 0
                )
            else:
                has_disagreement = row["maxImpliedProbabilitySpread"] > 0
            if has_disagreement:
                groups.setdefault(market_bet_type or "unknown", []).append(row)

    ranking_basis = {}
    for group_name, rows in groups.items():
        if group_name in {"ou", "sp"}:
            rows.sort(
                key=lambda row: (
                    float(row["maxPostedLineSpread"]),
                    row["maxSameLinePriceProbabilitySpread"],
                    row["bookmakerCount"],
                    str(row.get("statID")),
                ),
                reverse=True,
            )
            ranking_basis[group_name] = (
                "Ranks by posted-line range first, then same-line implied-probability spread, "
                "then bookmaker count."
            )
        else:
            rows.sort(
                key=lambda row: (
                    row["maxImpliedProbabilitySpread"],
                    row["bookmakerCount"],
                    str(row.get("statID")),
                ),
                reverse=True,
            )
            ranking_basis[group_name] = (
                "Ranks by implied-probability spread across books, then bookmaker count."
            )
        groups[group_name] = rows[:result_limit]

    return {
        "player": snapshot.get("player"),
        "requestedStatID": stat_id,
        "requestedBetType": bet_type,
        "bookmakersRequested": snapshot.get("bookmakersRequested") or [],
        "marketsScanned": markets_scanned,
        "minBookmakers": minimum_books,
        "groups": groups,
        "rankingBasis": ranking_basis,
        "notice": snapshot.get("notice"),
        "interpretation": (
            "Player-prop disagreements are ranked only within the same bet type. "
            "This output does not estimate edge, expected value, win probability, or recommend a wager."
        ),
    }
