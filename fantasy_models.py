"""Platform-neutral fantasy football data model.

This module defines the normalized representation shared by the analysis and
trade-evaluation layers. Fields are factual platform state only; recommendation,
ranking, and market-intelligence logic belongs elsewhere.

No network calls, no analysis, no FantasyPros logic belongs in this module.
"""
from dataclasses import dataclass, field
from typing import Optional

# Yahoo commonly uses compound string keys; ESPN uses integers. Do not
# coerce ESPN integer IDs to strings just for uniformity -- preserve the
# source value and its original type.
FantasyId = str | int


@dataclass
class FantasyPlayer:
    name: str
    position: Optional[str]
    pro_team: Optional[str]
    season_projected_points: Optional[float]
    season_total_points: Optional[float]
    # Current roster-state facts used by trade/lineup consumers. Optional so
    # adapters for platforms/surfaces that do not expose them stay valid.
    lineup_slot: Optional[str] = None
    injury_status: Optional[str] = None


@dataclass
class FantasyTeam:
    team_id: FantasyId
    team_name: Optional[str]
    roster: list[FantasyPlayer] = field(default_factory=list)


@dataclass
class LeagueSnapshot:
    platform: str
    league_id: FantasyId
    year: int
    scoring_bucket: str
    roster_slot_counts: dict
    teams: list[FantasyTeam] = field(default_factory=list)
