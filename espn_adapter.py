"""ESPN to normalized model translation boundary.

This module performs PURE TRANSLATION ONLY. It supports both the legacy
``espn-api`` object graph and project-owned raw ESPN league payloads while
producing the same platform-neutral FantasyTeam/FantasyPlayer/LeagueSnapshot
models consumed by the analysis layer.

It must never:
  - call FantasyPros
  - rank players
  - assign lineups
  - inspect caches
  - calculate VOR
  - classify assets
  - mutate ESPN objects/payloads
  - make network calls

The wrapper-backed functions remain temporarily for callers that have not yet
migrated. New ESPN analysis code should prefer the raw-payload functions so the
project owns the request + parsing contract end to end.
"""
from fantasy_models import FantasyPlayer, FantasyTeam, LeagueSnapshot
from espn_roster_read import parse_roster_entry


def build_espn_teams(league) -> list[FantasyTeam]:
    """Translate raw espn_api League.teams into list[FantasyTeam].

    Preserves team order, player order, ID types, and the exact defensive
    getattr(..., None) semantics of the legacy roster-dict construction --
    missing ESPN attributes become None, never fabricated. Makes no API
    calls, inspects no league settings/scoring, calls no FantasyPros.
    """
    teams = []
    for team in league.teams:
        roster = []
        for p in team.roster:
            roster.append(FantasyPlayer(
                name=getattr(p, "name", None),
                position=getattr(p, "position", None),
                pro_team=getattr(p, "proTeam", None),
                season_projected_points=getattr(p, "projected_total_points", None),
                season_total_points=getattr(p, "total_points", None),
                lineup_slot=getattr(p, "lineupSlot", None),
                injury_status=getattr(p, "injuryStatus", None),
            ))
        teams.append(FantasyTeam(
            team_id=getattr(team, "team_id", None),
            team_name=getattr(team, "team_name", None),
            roster=roster,
        ))
    return teams


def build_espn_league_snapshot(league, league_id, year, slot_counts, scoring_bucket) -> LeagueSnapshot:
    """Translate a raw espn_api League object into a LeagueSnapshot.

    Internal deduplication only: team/player translation is delegated to
    build_espn_teams(). External signature and returned semantics are
    unchanged from the original implementation.
    """
    return LeagueSnapshot(
        platform="espn",
        league_id=league_id,
        year=year,
        scoring_bucket=scoring_bucket,
        roster_slot_counts=slot_counts,
        teams=build_espn_teams(league),
    )


def _payload_team_name(team: dict):
    """Mirror espn-api Team.team_name semantics for a raw team object."""
    team_name = team.get("name", "Unknown")
    if team_name == "Unknown":
        team_name = "%s %s" % (team.get("location", "Unknown"), team.get("nickname", "Unknown"))
    return team_name


def _raw_player_data(entry: dict) -> dict:
    pool_entry = entry.get("playerPoolEntry") or {}
    player = pool_entry.get("player") or {} if isinstance(pool_entry, dict) else {}
    return player if isinstance(player, dict) else {}


def build_espn_teams_from_payload(payload: dict, year: int) -> list[FantasyTeam]:
    """Translate a project-owned raw ESPN league payload into FantasyTeam rows.

    This intentionally mirrors the subset of ``espn-api`` Team/Player behavior
    consumed by analysis/trade surfaces:
      * teams are sorted by integer team ID;
      * roster order is preserved;
      * player name/position/pro-team come from the same roster-entry parser
        already used by the direct roster MCP tools;
      * season actual/projected totals use scoring-period 0 and default to 0,
        matching ``espn-api`` Player.total_points/projected_total_points;
      * lineup slot and injury status preserve current ESPN roster state.

    No settings interpretation is performed here; callers continue to supply
    slot counts and scoring bucket separately.
    """
    if not isinstance(payload, dict):
        raise ValueError("ESPN returned an unexpected league payload")
    raw_teams = payload.get("teams")
    if not isinstance(raw_teams, list):
        raise ValueError("ESPN league payload is missing teams")

    teams: list[FantasyTeam] = []
    valid_teams = [team for team in raw_teams if isinstance(team, dict)]
    valid_teams.sort(key=lambda team: team.get("id") if isinstance(team.get("id"), int) else 10**9)

    for team in valid_teams:
        roster_container = team.get("roster") or {}
        entries = roster_container.get("entries", []) if isinstance(roster_container, dict) else []
        if not isinstance(entries, list):
            raise ValueError("ESPN team roster entries have an unexpected shape")

        roster: list[FantasyPlayer] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            player = parse_roster_entry(entry, year)
            raw_player = _raw_player_data(entry)
            roster.append(FantasyPlayer(
                name=player.get("name"),
                position=player.get("position"),
                pro_team=player.get("proTeam"),
                season_projected_points=player.get("projected_points", 0),
                season_total_points=player.get("points", 0),
                lineup_slot=player.get("lineup_slot"),
                injury_status=raw_player.get("injuryStatus"),
            ))

        teams.append(FantasyTeam(
            team_id=team.get("id"),
            team_name=_payload_team_name(team),
            roster=roster,
        ))

    return teams


def build_espn_league_snapshot_from_payload(payload: dict, league_id, year: int,
                                             slot_counts: dict, scoring_bucket: str) -> LeagueSnapshot:
    """Build the analysis domain snapshot directly from a raw ESPN payload."""
    return LeagueSnapshot(
        platform="espn",
        league_id=league_id,
        year=year,
        scoring_bucket=scoring_bucket,
        roster_slot_counts=slot_counts,
        teams=build_espn_teams_from_payload(payload, year),
    )
