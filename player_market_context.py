"""Pure composition of sportsbook disagreement and fantasy-player evidence.

This module performs no network I/O. It combines an already-ranked
SportsGameOdds player-prop disagreement result with cache-only FantasyPros
intelligence and optional ESPN league player data. The result is descriptive
context for an MCP host to reason over; it does not estimate causality,
expected value, win probability, or a recommended wager.
"""

from __future__ import annotations

from typing import Any


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_disagreement_summary(disagreement: dict[str, Any]) -> dict[str, Any]:
    groups = disagreement.get("groups") if isinstance(disagreement.get("groups"), dict) else {}
    rows: list[dict[str, Any]] = []
    bet_types: list[str] = []
    for bet_type, values in groups.items():
        if not isinstance(values, list):
            continue
        if values:
            bet_types.append(str(bet_type))
        rows.extend(row for row in values if isinstance(row, dict))

    stat_ids = []
    for row in rows:
        value = row.get("statID")
        if value is not None and str(value) not in stat_ids:
            stat_ids.append(str(value))

    return {
        "marketsWithDisagreement": len(rows),
        "betTypes": sorted(bet_types),
        "statIDs": stat_ids,
        "maxPostedLineSpread": max(
            (_as_float(row.get("maxPostedLineSpread")) or 0.0 for row in rows),
            default=0.0,
        ),
        "maxSameLinePriceProbabilitySpreadPctPts": max(
            (_as_float(row.get("maxSameLinePriceProbabilitySpreadPctPts")) or 0.0 for row in rows),
            default=0.0,
        ),
        "maxImpliedProbabilitySpreadPctPts": max(
            (_as_float(row.get("maxImpliedProbabilitySpreadPctPts")) or 0.0 for row in rows),
            default=0.0,
        ),
    }


def _stale_fantasypros_datasets(freshness: dict[str, Any] | None) -> list[str]:
    if not isinstance(freshness, dict):
        return []
    stale = []
    for name, row in freshness.items():
        if isinstance(row, dict) and row.get("status") in {"stale", "missing", "unreadable_timestamp"}:
            stale.append(str(name))
    return sorted(stale)


def build_player_market_context(
    disagreement: dict[str, Any],
    fantasypros: dict[str, Any],
    *,
    scoring: str = "PPR",
    espn: dict[str, Any] | None = None,
    fantasypros_freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine already-fetched evidence into a non-causal context bundle."""
    market_summary = _market_disagreement_summary(disagreement)
    fp = fantasypros if isinstance(fantasypros, dict) else {}
    espn_row = espn if isinstance(espn, dict) else None
    signals: list[dict[str, Any]] = []

    if market_summary["marketsWithDisagreement"]:
        signals.append(
            {
                "type": "cross_book_market_disagreement",
                "source": "SportsGameOdds",
                "evidence": market_summary,
                "whyItMayMatter": (
                    "Sportsbooks are currently posting materially different lines and/or prices. "
                    "This establishes market uncertainty but does not identify which book is correct."
                ),
            }
        )

    match_confidence = fp.get("match_confidence")
    if match_confidence not in {"none", "ambiguous", None}:
        injury_status = fp.get("injury_status")
        if injury_status:
            signals.append(
                {
                    "type": "fantasypros_injury_context",
                    "source": "FantasyPros",
                    "evidence": {
                        "status": injury_status,
                        "comment": fp.get("injury_comment"),
                    },
                    "whyItMayMatter": (
                        "Availability or workload uncertainty can contribute to projection and prop-pricing dispersion. "
                        "This is contextual evidence, not proof that it caused the sportsbook disagreement."
                    ),
                }
            )

        rank_min = _as_float(fp.get("rank_min"))
        rank_max = _as_float(fp.get("rank_max"))
        rank_std = _as_float(fp.get("rank_std"))
        if rank_min is not None and rank_max is not None and rank_max > rank_min:
            signals.append(
                {
                    "type": "fantasypros_expert_rank_dispersion",
                    "source": "FantasyPros",
                    "evidence": {
                        "rankMin": rank_min,
                        "rankMax": rank_max,
                        "rankRangeWidth": round(rank_max - rank_min, 3),
                        "rankStd": rank_std,
                        "ecr": fp.get("ecr"),
                        "posRank": fp.get("pos_rank"),
                    },
                    "whyItMayMatter": (
                        "A wider expert-ranking range is independent evidence that the player's outlook is not uniformly assessed. "
                        "No threshold is converted into a betting score."
                    ),
                }
            )

        news = fp.get("recent_news") if isinstance(fp.get("recent_news"), list) else []
        compact_news = []
        for item in news[:3]:
            if not isinstance(item, dict):
                continue
            compact_news.append(
                {
                    "created": item.get("created"),
                    "title": item.get("title"),
                    "impact": item.get("impact"),
                }
            )
        if compact_news:
            signals.append(
                {
                    "type": "recent_player_news",
                    "source": "FantasyPros",
                    "evidence": compact_news,
                    "whyItMayMatter": (
                        "Recent player news can be relevant to market uncertainty. The tool surfaces it for inspection "
                        "without asserting that the news caused any line movement."
                    ),
                }
            )

    if espn_row and espn_row.get("status") == "matched":
        player = espn_row.get("player") if isinstance(espn_row.get("player"), dict) else {}
        if player.get("injured") is True:
            signals.append(
                {
                    "type": "espn_injury_flag",
                    "source": "ESPN",
                    "evidence": {"injured": True},
                    "whyItMayMatter": (
                        "ESPN also flags the player as injured in the requested fantasy league context."
                    ),
                }
            )
        if player.get("injured") is True and fp.get("injury_status"):
            signals.append(
                {
                    "type": "cross_source_injury_corroboration",
                    "source": "ESPN + FantasyPros",
                    "evidence": {
                        "espnInjured": True,
                        "fantasyProsStatus": fp.get("injury_status"),
                    },
                    "whyItMayMatter": (
                        "Two independent fantasy-data surfaces currently flag injury context, increasing confidence "
                        "that availability/workload uncertainty is real even though its market impact is not quantified."
                    ),
                }
            )

    stale_datasets = _stale_fantasypros_datasets(fantasypros_freshness)
    return {
        "player": {
            "requestedName": disagreement.get("playerName") or (disagreement.get("player") or {}).get("name"),
            "fantasyProsName": fp.get("name"),
            "team": fp.get("team"),
            "position": fp.get("position"),
        },
        "sportsbook": {
            "eventID": disagreement.get("eventID"),
            "leagueID": disagreement.get("leagueID"),
            "teamID": disagreement.get("teamID"),
            "summary": market_summary,
            "disagreement": disagreement,
        },
        "fantasyPros": {
            "source": "cache_only",
            "scoring": scoring.upper(),
            "matchMethod": fp.get("match_method"),
            "matchConfidence": match_confidence,
            "ecr": fp.get("ecr"),
            "posRank": fp.get("pos_rank"),
            "tier": fp.get("tier"),
            "rankMin": fp.get("rank_min"),
            "rankMax": fp.get("rank_max"),
            "rankStd": fp.get("rank_std"),
            "adp": fp.get("adp"),
            "projectedPoints": fp.get("projected_points"),
            "injuryStatus": fp.get("injury_status"),
            "injuryComment": fp.get("injury_comment"),
            "cacheTimestamps": fp.get("cache_timestamps"),
            "freshness": fantasypros_freshness,
        },
        "espn": espn_row or {"status": "not_requested"},
        "explanatorySignals": signals,
        "dataQuality": {
            "fantasyProsMatchConfidence": match_confidence,
            "fantasyProsStaleOrMissingDatasets": stale_datasets,
            "espnStatus": (espn_row or {}).get("status", "not_requested"),
            "sportsbookMarketsWithDisagreement": market_summary["marketsWithDisagreement"],
        },
        "interpretation": (
            "These are possible explanatory signals around observed sportsbook disagreement, not a causal model. "
            "The output does not calculate expected value, fair odds, win probability, or recommend/place a wager."
        ),
    }
