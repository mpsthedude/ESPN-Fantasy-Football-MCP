from mcp.server.fastmcp import FastMCP

import app_config
import sys
import os
import datetime
import logging
import traceback
import re
import math
import json
import hashlib
import fantasypros_client as fp_client
from contextvars import ContextVar
import functools
from types import SimpleNamespace
import league_registry
import commissioner_config
import draft_strategy_store
from fantasy_models import FantasyPlayer, FantasyTeam, LeagueSnapshot
from espn_adapter import (build_espn_league_snapshot, build_espn_teams, build_espn_league_snapshot_from_payload)
from espn_transport import ESPNAccessError, ESPNTransport
from espn_session import ESPNSessionManager
from espn_league_read import (CORE_LEAGUE_VIEWS, build_league_info, build_league_settings, build_standings, build_team_info, resolve_my_team_from_payload)
from espn_roster_read import (ROSTER_VIEWS, COMMISSIONER_CURRENT_VIEWS, build_all_rosters, build_player_stats, build_team_roster, build_lineup_team, build_commissioner_snapshot, parse_roster_entry)
from espn_matchup_read import (MATCHUP_CONTEXT_VIEWS, MATCHUP_SCORE_VIEWS, build_matchup_info, build_commissioner_matchup_evidence, resolve_matchup_request)
from espn_free_agent_read import (FREE_AGENT_CONTEXT_VIEWS, FREE_AGENT_VIEWS, PRO_SCHEDULE_VIEWS, build_free_agent_filter, build_free_agents, resolve_free_agent_week)
from espn_historical_lineup_read import (HISTORICAL_LINEUP_VIEWS, HISTORICAL_PRO_SCHEDULE_VIEWS, build_historical_lineup_boxes)
from espn_draft_read import (DRAFT_PLAYER_FILTER, DRAFT_PLAYER_VIEWS, DRAFT_RESULT_VIEWS, build_draft_results)
from espn_snapshot_read import (SNAPSHOT_VIEWS, build_league_snapshot_base)
from espn_activity_read import (ACTIVITY_VIEWS, ACTIVE_PLAYER_VIEWS, build_activity_filter, build_active_player_filter, build_active_player_name_map, build_activity_events)

# Add stderr logging for MCP hosts to see. Redact configured/runtime
# secret values defensively before anything reaches stderr. This protects
# against third-party exception messages accidentally serializing cookies.
def _redact_runtime_secrets(message):
    text = str(message)
    secrets = set()
    for env_name in ("ESPN_S2", "ESPN_SWID", "SWID", "FANTASYPROS_API_KEY", "SPORTSGAMEODDS_API_KEY"):
        value = os.environ.get(env_name)
        if value:
            secrets.add(value)

    api_obj = globals().get("api")
    for creds in getattr(api_obj, "credentials", {}).values() if api_obj is not None else []:
        if isinstance(creds, dict):
            for key in ("espn_s2", "swid"):
                value = creds.get(key)
                if value:
                    secrets.add(value)

    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def log_error(message):
    print(_redact_runtime_secrets(message), file=sys.stderr)

def _default_football_season(now=None) -> int:
    moment = now or datetime.datetime.now()
    return moment.year - (1 if moment.month < 7 else 0)


mcp = FastMCP("fantasy-football-mcp")
CURRENT_YEAR = _default_football_season()

ESPNFantasyFootballAPI = ESPNSessionManager
api = ESPNSessionManager()
SESSION_ID = "primary"

# --- Shared helpers for new tools (compatibility tool surface) ---
def _resolve_year(year):
    """Resolve an optional year argument to CURRENT_YEAR when None."""
    return year if year is not None else CURRENT_YEAR

def _fetch_core_league_payload(league_id: int, year: int) -> dict:
    """Fetch one raw core ESPN league snapshot through the project transport."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=CORE_LEAGUE_VIEWS)

def _fetch_roster_payload(league_id: int, year: int) -> dict:
    """Fetch roster/player data through the project-owned ESPN transport."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=ROSTER_VIEWS)

def _fetch_snapshot_payload(league_id: int, year: int) -> dict:
    """Fetch the compact league snapshot surfaces in one project-owned read."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=SNAPSHOT_VIEWS)

def _fetch_commissioner_current_payload(league_id: int, year: int) -> dict:
    """Fetch only current settings/team/roster data required by basic commissioner reads."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=COMMISSIONER_CURRENT_VIEWS)

def _fetch_activity_page_payload(league_id: int, year: int, size: int, offset: int) -> dict:
    """Fetch one bounded ESPN commissioner activity page through project transport."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league_communication(
        league_id, year, views=ACTIVITY_VIEWS, fantasy_filter=build_activity_filter(size, offset))

def _fetch_activity_player_payload(year: int) -> list[dict]:
    """Fetch the active ESPN player-name map once per bounded activity scan."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_players(
        year, views=ACTIVE_PLAYER_VIEWS, fantasy_filter=build_active_player_filter())

def _fetch_matchup_context_payload(league_id: int, year: int) -> dict:
    """Fetch ESPN settings/team context needed to resolve a scoring week."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=MATCHUP_CONTEXT_VIEWS)

def _fetch_matchup_score_payload(league_id: int, year: int, week: int, matchup_period: int) -> dict:
    """Fetch one week of scoreboard data using ESPN's matchup-period filter."""
    transport = api.get_transport(SESSION_ID)
    fantasy_filter = {"schedule": {"filterMatchupPeriodIds": {"value": [matchup_period]}}}
    return transport.fetch_league(
        league_id,
        year,
        views=MATCHUP_SCORE_VIEWS,
        scoring_period_id=week,
        fantasy_filter=fantasy_filter,
    )

def _fetch_free_agent_context_payload(league_id: int, year: int) -> dict:
    """Fetch the minimal league context needed to resolve the default waiver week."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=FREE_AGENT_CONTEXT_VIEWS)

def _fetch_free_agent_player_payload(league_id: int, year: int, week: int,
                                     size: int, position: str = None) -> dict:
    """Fetch ESPN free-agent/waiver players through the project transport."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(
        league_id,
        year,
        views=FREE_AGENT_VIEWS,
        scoring_period_id=week,
        fantasy_filter=build_free_agent_filter(size, position),
    )

def _fetch_pro_schedule_payload(year: int) -> dict:
    """Fetch ESPN's pro-team schedule used for opponent/bye status."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_season(year, views=PRO_SCHEDULE_VIEWS)

def _fetch_historical_lineup_boxes(league_id: int, year: int, week: int, settings):
    """Fetch one historical lineup week without third-party box-score objects.

    Commissioner tools still use the cached League for metadata/team
    identity in this slice, but historical lineup and bye-week evidence
    come from project-owned ESPN requests and parsing.
    """
    matchup_period = None
    matchup_periods = getattr(settings, "matchup_periods", {}) or {}
    if isinstance(matchup_periods, dict):
        for period_id, period_weeks in matchup_periods.items():
            if isinstance(period_weeks, (list, tuple, set)) and week in period_weeks:
                try:
                    matchup_period = int(period_id)
                except (TypeError, ValueError):
                    matchup_period = None
                break
    if matchup_period is None:
        matchup_period = week

    transport = api.get_transport(SESSION_ID)
    fantasy_filter = {"schedule": {"filterMatchupPeriodIds": {"value": [matchup_period]}}}
    scoreboard_payload = transport.fetch_league(
        league_id,
        year,
        views=HISTORICAL_LINEUP_VIEWS,
        scoring_period_id=week,
        fantasy_filter=fantasy_filter,
    )
    pro_schedule_payload = transport.fetch_season(
        year, views=HISTORICAL_PRO_SCHEDULE_VIEWS
    )
    return build_historical_lineup_boxes(scoreboard_payload, pro_schedule_payload, week)

def _fetch_draft_result_payload(league_id: int, year: int) -> dict:
    """Fetch completed-draft metadata/team identities through project transport."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=DRAFT_RESULT_VIEWS)

def _fetch_draft_player_payload(year: int) -> list[dict]:
    """Fetch the active season player map used for completed draft names."""
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_players(
        year,
        views=DRAFT_PLAYER_VIEWS,
        fantasy_filter=DRAFT_PLAYER_FILTER,
    )

def _is_private_league_error(e: Exception) -> bool:
    """Classify project-transport authentication/access failures safely."""
    return ((isinstance(e, ESPNAccessError) and e.status_code in (401, 403)) or
            "401" in str(e) or "Private" in str(e) or "cannot be accessed" in str(e))

def _safe_print_exc() -> None:
    """Print a traceback unless it is an ESPN private-auth failure.

    Some historical older wrapper releases embedded raw cookie values in the
    exception message. Never serialize that exception through traceback.
    """
    current = sys.exc_info()[1]
    if current is not None and _is_private_league_error(current):
        log_error("Traceback suppressed for ESPN private league authentication/access failure")
        return
    traceback.print_exc(file=sys.stderr)

def _compatibility_string_error(log_label: str, return_label: str, e: Exception) -> str:
    """Secret-safe error boundary for the string-returning compatibility tools."""
    if _is_private_league_error(e):
        log_error(f"{log_label}: private league authentication/access failed")
        return ("This appears to be a private league. Please use the authenticate tool first with your "
                "ESPN_S2 and SWID cookies to access private leagues.")
    log_error(f"{log_label}: {str(e)}")
    _safe_print_exc()
    return f"{return_label}: {str(e)}"

def _error_response(action: str, e: Exception) -> dict:
    """Consistent, structured error payload for the new dict-returning tools.
    SECURITY (2026-08-15): classifies private/auth errors FIRST and never
    evaluates str(e) or calls traceback.print_exc() on that branch - both
    could serialize third-party/network exception details. Non-private branch behavior
    (log_error text, traceback, returned message) is byte-for-byte
    unchanged from before this fix."""
    if _is_private_league_error(e):
        log_error(f"Error {action}: private league authentication/access failed")
        return {
            "error": "private_league_auth_required",
            "message": ("This appears to be a private league. Please use the authenticate "
                        "tool first with your ESPN_S2 and SWID cookies to access private leagues.")
        }
    log_error(f"Error {action}: {str(e)}")
    _safe_print_exc()
    return {"error": "request_failed", "message": f"Error {action}: {str(e)}"}

def _validate_bounded_int(value, name: str, min_val: int, max_val: int, default: int):
    """Validate an optional int param against [min_val, max_val]. Returns (value, error_message)."""
    if value is None:
        return default, None
    if not isinstance(value, int) or isinstance(value, bool):
        return None, f"{name} must be an integer between {min_val} and {max_val} (got {value!r})."
    if value < min_val or value > max_val:
        return None, f"{name} must be between {min_val} and {max_val} (got {value})."
    return value, None

def _valid_scoring_weeks(league) -> list[int]:
    """Return ESPN-configured scoring weeks for this league.

    Prefer scheduleSettings.matchupPeriods as parsed by a former wrapper. Fall
    back to ESPN's first/final scoring-period metadata if a historical or
    unusual payload omits matchupPeriods. Never hardcode an NFL week cap.
    """
    weeks = set()
    matchup_periods = getattr(getattr(league, "settings", None), "matchup_periods", {}) or {}
    for period_weeks in matchup_periods.values():
        if isinstance(period_weeks, (list, tuple, set)):
            values = period_weeks
        else:
            values = [period_weeks]
        for value in values:
            try:
                week = int(value)
            except (TypeError, ValueError):
                continue
            if week >= 1:
                weeks.add(week)

    if not weeks:
        first = getattr(league, "firstScoringPeriod", 1) or 1
        final = getattr(league, "finalScoringPeriod", None)
        if isinstance(first, int) and isinstance(final, int) and final >= first >= 1:
            weeks.update(range(first, final + 1))

    return sorted(weeks)

def _resolve_free_agent_week(requested_week, league):
    """Resolve the scoring-period week to query for free agents.

    League.free_agents() treats week=0 as unset (Python falsy) and falls back to
    league.current_week, which is also 0 during the preseason. To avoid sending
    scoringPeriodId=0 to ESPN, fall back to week 1 as the preseason baseline
    whenever both the requested week and league.current_week resolve to 0 or less.
    """
    if requested_week is not None and requested_week >= 1:
        return requested_week
    current = getattr(league, "current_week", 0) or 0
    if current >= 1:
        return current
    return 1  # preseason fallback

def _normalize_pro_opponent(value):
    """BoxPlayer defaults pro_opponent to the literal string 'None' on bye weeks
    instead of Python None. Normalize for clean JSON output."""
    return None if value == "None" else value

# --- Helpers added for rank_waiver_targets (existing 19 tools untouched) ---

def _detect_league_scoring_bucket(scoring_rules: list) -> str:
    """Derive PPR/HALF/STD from the league's real REC rule instead of
    assuming PPR. Falls back to STD if no REC rule is found."""
    for rule in scoring_rules or []:
        if rule.get("abbr") == "REC":
            pts = rule.get("points", 0) or 0
            if pts >= 1.0:
                return "PPR"
            if pts >= 0.5:
                return "HALF"
            return "STD"
    return "STD"

def _find_team_by_id(league, team_id: int):
    """Never assume team_id equals list index - ESPN team IDs are not
    guaranteed to match list order in every league."""
    return next((t for t in league.teams if t.team_id == team_id), None)

def _get_roster_snapshot(league, team_id: int):
    """Returns (roster_list, error_message, team_obj). Uses
    _find_team_by_id rather than a positional index lookup."""
    team = _find_team_by_id(league, team_id)
    if team is None:
        valid_ids = sorted(t.team_id for t in league.teams)
        return None, f"No team with team_id={team_id} in this league. Valid team_ids: {valid_ids}", None
    roster = [{
        "name": getattr(p, "name", None),
        "position": getattr(p, "position", None),
        "proTeam": getattr(p, "proTeam", None),
        "projected_points": getattr(p, "projected_total_points", None),
        "points": getattr(p, "total_points", None),
    } for p in team.roster]
    return roster, None, team

FLEX_COMPONENT_POSITIONS = {"QB", "RB", "WR", "TE"}

def _parse_flex_eligibility(slot_key: str):
    """Returns eligible positions if slot_key is flex-style, else None.
    Guards against single-position abbreviations that happen to contain
    a slash (e.g. 'D/ST') by requiring every split component to be a
    real flex-eligible skill position."""
    if slot_key == "OP":
        return ["QB", "RB", "WR", "TE"]
    if "/" in slot_key:
        parts = slot_key.split("/")
        if all(p in FLEX_COMPONENT_POSITIONS for p in parts):
            return parts
    return None

FLEX_EXCLUDED_SLOT_KEYS = {"BE", "IR", ""}
KNOWN_DIRECT_POSITION_ORDER = ["QB", "RB", "WR", "TE", "D/ST", "K"]

def _assign_best_lineup(roster: list, slot_counts: dict, value_field: str = "projected_points") -> dict:
    """Reusable greedy best-lineup assignment. Accepts value_field so
    callers can run this against either ESPN projected_points
    (informational) or a FantasyPros-based evaluation value (decision-
    driving), never mixing the two within a single call. Reusable by
    future analyze_my_team / evaluate_trade / optimize_lineup tools."""
    def val(p):
        v = p.get(value_field)
        return v if v is not None else -1

    remaining = list(roster)
    starters, gaps = {}, []
    direct_keys = [k for k in slot_counts if k not in FLEX_EXCLUDED_SLOT_KEYS
                    and _parse_flex_eligibility(k) is None and (slot_counts.get(k) or 0) > 0]
    ordered_direct = [k for k in KNOWN_DIRECT_POSITION_ORDER if k in direct_keys] + \
                      sorted(k for k in direct_keys if k not in KNOWN_DIRECT_POSITION_ORDER)

    for pos in ordered_direct:
        required = slot_counts.get(pos, 0)
        candidates = sorted([p for p in remaining if p.get("position") == pos], key=val, reverse=True)
        assigned = candidates[:required]
        starters[pos] = assigned
        for p in assigned:
            remaining.remove(p)
        if len(assigned) < required:
            gaps.append({"slot": pos, "required": required, "filled": len(assigned), "missing": required - len(assigned)})

    flex_starters = []
    flex_keys = [k for k in slot_counts if _parse_flex_eligibility(k) is not None and (slot_counts.get(k) or 0) > 0]
    for slot_key in flex_keys:
        eligible = _parse_flex_eligibility(slot_key)
        required = slot_counts.get(slot_key, 0)
        candidates = sorted([p for p in remaining if p.get("position") in eligible], key=val, reverse=True)
        assigned = candidates[:required]
        flex_starters.extend(assigned)
        for p in assigned:
            remaining.remove(p)
        if len(assigned) < required:
            gaps.append({"slot": slot_key, "required": required, "filled": len(assigned), "missing": required - len(assigned)})

    return {"feasible": len(gaps) == 0, "starters": starters, "flex_starters": flex_starters,
             "bench": remaining, "gaps": gaps, "value_field_used": value_field}

def _can_fill_required_skill_slots(roster: list, slot_counts: dict, value_field: str = "projected_points") -> dict:
    """Reusable feasibility-only wrapper, designed for analyze_my_team /
    evaluate_trade / optimize_lineup reuse."""
    result = _assign_best_lineup(roster, slot_counts, value_field)
    return {"feasible": result["feasible"], "gaps": result["gaps"]}

def _flatten_starters(lineup_result: dict) -> list:
    flat = []
    for players in lineup_result.get("starters", {}).values():
        flat.extend(players)
    flat.extend(lineup_result.get("flex_starters", []))
    return flat

def _build_fp_eval_roster(roster: list):
    """Attaches _fp_eval_value = _fp_intel.projected_points to each row
    for FP-based lineup analysis. Returns (rows, warning) where warning
    is set if too many core-position players lack FP projection data
    to trust the resulting lineup placement."""
    rows = []
    missing_core = 0
    core_total = 0
    for p in roster:
        fp = p.get("_fp_intel") or {}
        row = dict(p)
        row["_fp_eval_value"] = fp.get("projected_points")
        rows.append(row)
        if p.get("position") in ("QB", "RB", "WR", "TE"):
            core_total += 1
            if fp.get("projected_points") is None:
                missing_core += 1
    warning = None
    if core_total > 0 and missing_core / core_total > 0.3:
        warning = (f"{missing_core} of {core_total} core-position roster players lack a FantasyPros "
                   f"projection; FP-based lineup placement may be unreliable.")
    return rows, warning

def _describe_roster_utility(before_lineup: dict, after_lineup: dict, add_name: str,
                               position_needs: dict, add_position: str) -> str:
    """5-way label derived by diffing FP-value lineups before/after the
    simulated add, not a coarse 3-way guess."""
    after_starters = {p.get("name") for p in _flatten_starters(after_lineup)}
    if add_name not in after_starters:
        return "does_not_crack_lineup"
    direct_before = len(before_lineup.get("starters", {}).get(add_position, []))
    direct_required = position_needs.get(add_position, {}).get("direct_starter_slots", 0)
    in_direct_after = any(p.get("name") == add_name for p in after_lineup.get("starters", {}).get(add_position, []))
    if in_direct_after and direct_before < direct_required:
        return "fills_direct_starting_gap"
    if in_direct_after and direct_before >= direct_required:
        return "improves_starting_rotation"
    if any(p.get("name") == add_name for p in after_lineup.get("flex_starters", [])):
        return "enters_flex_starting_lineup"
    return "bench_depth_upgrade"

def _classify_lineup_impact(roster_utility: str, direction: str) -> str:
    """Derives lineup_impact from the existing FP-value roster_utility
    label plus the asset-value direction. Distinguishes 'this is a good
    transaction' (direction) from 'this changes who starts' (lineup_impact) -
    e.g. Kyler Murray can be a strong_upgrade asset swap vs. a dead TE
    spot while still being bench_only_upgrade because Stafford remains
    the FP-value starting QB."""
    if roster_utility == "fills_direct_starting_gap":
        return "direct_starter_upgrade"
    if roster_utility == "enters_flex_starting_lineup":
        return "flex_starter_upgrade"
    if roster_utility == "improves_starting_rotation":
        return "starting_rotation_upgrade"
    # does_not_crack_lineup / bench_depth_upgrade (unreachable edge case, kept for safety)
    if direction in ("strong_upgrade", "upgrade"):
        return "bench_only_upgrade"
    return "no_lineup_improvement"

def _describe_expendability(is_zero_espn_projection: bool, is_current_starter: bool, fp_tier) -> str:
    reasons = []
    if is_zero_espn_projection:
        reasons.append("zero ESPN season projection (a dead roster spot)")
    if is_current_starter:
        reasons.append("currently occupies a starting/FLEX slot in the FantasyPros-based lineup, so this is a real trade-off")
    else:
        reasons.append("not currently in the best FantasyPros-value starting lineup")
    if fp_tier is not None and fp_tier >= 6:
        reasons.append(f"FantasyPros tier {fp_tier} (deep speculative external value)")
    return "; ".join(reasons)

def _evaluate_drop_options(roster_fp_rows: list, add_position: str, add_eval_value, slot_counts: dict) -> list:
    """Whole-roster search (no same-position preference). Feasibility
    simulated on FP-value rows, never ESPN-vs-FP blended. Hard-excludes
    any drop that breaks feasibility."""
    sim_add = {"name": "__ADD_CANDIDATE__", "position": add_position, "_fp_eval_value": add_eval_value}
    current_lineup = _assign_best_lineup(roster_fp_rows, slot_counts, value_field="_fp_eval_value")
    current_starter_names = {p.get("name") for p in _flatten_starters(current_lineup)}

    options = []
    for i, drop in enumerate(roster_fp_rows):
        simulated = roster_fp_rows[:i] + roster_fp_rows[i + 1:] + [sim_add]
        feasibility = _assign_best_lineup(simulated, slot_counts, value_field="_fp_eval_value")
        if not feasibility["feasible"]:
            continue

        drop_fp = drop.get("_fp_intel", {}) or {}
        is_zero_espn = (drop.get("projected_points") or 0) <= 0
        is_current_starter = drop.get("name") in current_starter_names
        fp_tier, fp_ecr = drop_fp.get("tier"), drop_fp.get("ecr")
        value_missing = (drop_fp.get("match_confidence") not in ("ambiguous", "none")
                          and drop_fp.get("projected_points") is None and fp_ecr is None)

        options.append({
            "candidate": drop, "feasible": True,
            "is_zero_espn_projection": is_zero_espn, "is_current_starter": is_current_starter,
            "fp_tier": fp_tier, "fp_ecr": fp_ecr, "drop_external_value_missing": value_missing,
            "expendability_reason": _describe_expendability(is_zero_espn, is_current_starter, fp_tier),
        })

    options.sort(key=lambda o: (
        0 if o["is_zero_espn_projection"] else 1,
        1 if o["is_current_starter"] else 0,
        -(o["fp_tier"] if o["fp_tier"] is not None else 0),
        -(o["fp_ecr"] if o["fp_ecr"] is not None else 0),
    ))
    return options

def _check_required_fp_caches(positions: list, scoring_bucket: str) -> list:
    """Scoring-aware; zero-cost presence check only, never triggers a
    live call itself."""
    warnings = []
    if fp_client.get_players_cache() is None:
        warnings.append("players cache is missing (ADP unavailable). Run refresh_fantasypros_cache first.")
    for pos in positions:
        if fp_client.get_rankings_cache(pos, scoring_bucket) is None:
            warnings.append(f"rankings_{pos}_{scoring_bucket} cache is missing.")
        if fp_client.get_projections_cache(pos, scoring_bucket, week=0) is None:
            warnings.append(f"projections_{pos}_{scoring_bucket}_wk0 cache is missing.")
    if fp_client.get_injuries_cache() is None:
        warnings.append("injuries cache is missing.")
    return warnings

def _build_recommendation_reason(add_name, add_position, add_intel, drop_name,
                                   drop_expendability_desc, ownership_band, roster_utility, signals) -> str:
    """Explicitly surfaces injury_signal and drop_external_value_missing,
    and uses the richer 5-way roster_utility label."""
    fragments = []
    pct = add_intel.get("espn_ownership_pct")
    ecr, tier = add_intel.get("ecr"), add_intel.get("tier")
    quality = fp_client.describe_player_quality(tier)

    if ownership_band in ("extreme", "strong", "moderate") and pct is not None:
        fragments.append(f"{add_name} is a {ownership_band} availability anomaly ({pct}% ESPN owned globally)")
    if ecr is not None:
        tier_part = f", tier-{tier} ({quality})" if tier is not None else ""
        fragments.append(f"carries {add_position} ECR {ecr}{tier_part} external value")

    utility_phrases = {
        "fills_direct_starting_gap": "would fill an open direct starting slot",
        "enters_flex_starting_lineup": "would start immediately in a FLEX slot",
        "improves_starting_rotation": "would outperform and displace a current starter",
        "bench_depth_upgrade": "would improve bench depth but not start immediately",
        "does_not_crack_lineup": "would not crack the starting lineup even after the move",
    }
    fragments.append(utility_phrases.get(roster_utility, "roster fit is unclear"))

    if signals.get("drop_external_value_missing"):
        fragments.append(f"dropping {drop_name} is supported by ESPN-side evidence only, since FantasyPros "
                          f"has no usable projection/ECR for that player at this position ({drop_expendability_desc})")
    else:
        fragments.append(f"dropping {drop_name} is supported because {drop_expendability_desc}")

    injury = signals.get("injury_signal") or {}
    if injury.get("label") == "materially_reduced":
        fragments.append(f"CAUTION: {injury.get('note')}")
    elif injury.get("label") == "caution":
        fragments.append(injury.get("note", ""))

    if signals.get("direction") == "insufficient_data":
        fragments.append("FantasyPros identity matching is unresolved, so verify manually before acting")

    text = "; ".join(f for f in fragments if f) + "."
    return text[0].upper() + text[1:]

# --- Helpers added for analyze_my_team (existing 20 tools untouched) ---

def _rank_across_league(values_by_team_id: dict, higher_is_better: bool = True) -> dict:
    """Rank complete-data teams relative to one another.

    - Teams with value=None are excluded entirely from ranking/median/leader math.
    - Values are rounded to 2 decimals BEFORE ranking so floating-point noise
      (e.g. 293.9800000001 vs 293.98) never splits an intended tie into two
      dense ranks.
    - Dense ranking: tied values share a rank; next distinct value gets rank+1.
    - Percentile is based on the share of OTHER ranked teams strictly worse:
        leader = 100.0, lowest = 0.0, tied teams share the same percentile.
      When only one team is ranked, percentile = 100.0.
    - gap_to_median is direction-aware: positive = better than median, negative = worse.
    - gap_to_leader is direction-aware: 0 = tied for leader, negative = behind leader.
    - Ties for the lead are returned as leader_team_ids (plural, sorted) -
      never resolved by dict/iteration order.
    - coverage_pct = 100 * ranked_team_count / league_size, rounded to 1
      decimal. Makes it impossible to mistake a reduced ranked pool
      (e.g. "7/7") for the entire league (e.g. a 12-team league) -
      callers should surface this alongside every league_rank.
    """
    import statistics

    normalized = {tid: (round(v, 2) if v is not None else None) for tid, v in values_by_team_id.items()}
    ranked_items = [(tid, v) for tid, v in normalized.items() if v is not None]
    excluded_team_ids = sorted(tid for tid, v in normalized.items() if v is None)

    if not ranked_items:
        return {
            "ranked_team_count": 0, "league_size": len(normalized),
            "coverage_pct": 0.0,
            "excluded_team_ids": excluded_team_ids,
            "median": None, "leader_value": None, "leader_team_ids": [],
            "per_team": {tid: {"rank": None, "percentile": None,
                                "gap_to_median": None, "gap_to_leader": None}
                         for tid in normalized},
        }

    distinct_sorted = sorted({v for _, v in ranked_items}, reverse=higher_is_better)
    dense_rank_by_value = {v: i + 1 for i, v in enumerate(distinct_sorted)}

    n_ranked = len(ranked_items)
    median_val = statistics.median(v for _, v in ranked_items)
    leader_value = distinct_sorted[0]
    leader_team_ids = sorted(tid for tid, v in ranked_items if v == leader_value)

    per_team = {}
    for tid, v in ranked_items:
        rank = dense_rank_by_value[v]
        if n_ranked == 1:
            percentile = 100.0
        else:
            strictly_worse = sum(1 for _, other in ranked_items
                                  if (other < v if higher_is_better else other > v))
            percentile = round(100.0 * strictly_worse / (n_ranked - 1), 1)

        if higher_is_better:
            gap_to_median, gap_to_leader = v - median_val, v - leader_value
        else:
            gap_to_median, gap_to_leader = median_val - v, leader_value - v

        per_team[tid] = {"rank": rank, "percentile": percentile,
                          "gap_to_median": round(gap_to_median, 2), "gap_to_leader": round(gap_to_leader, 2)}

    for tid in excluded_team_ids:
        per_team[tid] = {"rank": None, "percentile": None, "gap_to_median": None, "gap_to_leader": None}

    return {
        "ranked_team_count": n_ranked, "league_size": len(normalized),
        "coverage_pct": round(100.0 * n_ranked / len(normalized), 1) if normalized else 0.0,
        "excluded_team_ids": excluded_team_ids,
        "median": round(median_val, 2), "leader_value": round(leader_value, 2),
        "leader_team_ids": leader_team_ids,
        "per_team": per_team,
    }

def _relative_label(rank, ranked_team_count):
    """Proportional quartile band, sized to the number of teams actually
    RANKED (not raw league_size) - a team excluded for insufficient data
    has no rank and gets 'unavailable', never a fabricated position.
    Band size = ceil(ranked_team_count / 4), so this degrades sanely for
    any league size (8-team -> bands of 2; 14-team -> bands of 3-4)."""
    if rank is None or not ranked_team_count:
        return "unavailable"
    band = max(1, math.ceil(ranked_team_count / 4))
    if rank <= band:
        return "strong"
    if rank <= band * 2:
        return "above_average"
    if rank <= band * 3:
        return "below_average"
    return "weak"

def _parse_positional_rank(pos_rank_str):
    """Parses FantasyPros pos_rank strings like 'RB20' -> 20 (int). Returns
    None if unparseable/missing. Never falls back to overall ECR - if this
    returns None, the caller must treat positional rank as unavailable,
    not substitute a different metric silently."""
    if not pos_rank_str:
        return None
    m = re.search(r"(\d+)$", str(pos_rank_str))
    return int(m.group(1)) if m else None

def _position_has_flex_exposure(position: str, slot_counts: dict) -> bool:
    """True if this position can realistically compete for extra playing
    time in THIS league's actual slot configuration. RB/WR/TE always have
    a plausible bench-to-start path (bye/injury replacement of direct
    slots), so they're always lineup-relevant. QB only counts as
    lineup-relevant bench depth if a real superflex/OP slot exists for it
    (via the frozen _parse_flex_eligibility) - otherwise a backup QB has
    no route to ever start."""
    if position in ("RB", "WR", "TE"):
        return True
    if position == "QB":
        for slot_key, count in slot_counts.items():
            if count and count > 0:
                eligible = _parse_flex_eligibility(slot_key)
                if eligible and "QB" in eligible:
                    return True
        return False
    return False

def _core_offense_projection(lineup_fp: dict, roster_fp_rows: list, slot_counts: dict) -> dict:
    """FantasyPros-based core-offense total: direct QB/RB/WR/TE starters +
    FLEX/OP starters (K/D-ST excluded - FP has no comparable data for
    them). Coverage is gated on ALL core-position players - starters AND
    BENCH - not just the ones assigned to start. A missing bench player
    could have legitimately won a starting/FLEX role had their true FP
    value been known instead of falling to the -1 sorting sentinel; their
    absence therefore taints the reliability of this team's ENTIRE
    FP-value lineup assignment, not just the specific slots they'd occupy.
    Legal roster feasibility is computed independently by
    _assign_best_lineup and is never affected by this coverage check."""
    combined_starters = []
    for pos in ("QB", "RB", "WR", "TE"):
        combined_starters.extend(lineup_fp.get("starters", {}).get(pos, []))
    combined_starters.extend(lineup_fp.get("flex_starters", []))
    starter_names = {p.get("name") for p in combined_starters}

    all_core_players = [p for p in roster_fp_rows if p.get("position") in ("QB", "RB", "WR", "TE")]
    missing_any_core = [{"name": p.get("name"), "position": p.get("position"),
                          "in_starting_lineup": p.get("name") in starter_names}
                         for p in all_core_players if p.get("_fp_eval_value") is None]

    valued_starters = [p for p in combined_starters if p.get("_fp_eval_value") is not None]
    known_total = round(sum(p["_fp_eval_value"] for p in valued_starters), 2)

    return {
        "known_projection_total": known_total,
        "missing_projection_players": missing_any_core,
        "considered_starters": len(combined_starters),
        "valued_starters": len(valued_starters),
        "coverage_complete": len(missing_any_core) == 0,
    }

def _bench_depth_metrics(lineup_fp: dict, slot_counts: dict) -> dict:
    """Two distinct bench metrics:
      - bench_asset_projection_total: ALL core-position (QB/RB/WR/TE) bench
        FP value - context only, NEVER used for cross-team ranking.
      - lineup_relevant_bench_projection_total: the metric actually used
        for the ranked bench-depth comparison. Excludes bench value at
        positions with no realistic path to a starting/FLEX role (e.g. a
        backup QB in a 1-QB, no-OP league contributes $0 here, so QB2
        asset value can never dominate the ranked depth metric)."""
    bench = lineup_fp.get("bench", [])
    core_bench = [p for p in bench if p.get("position") in ("QB", "RB", "WR", "TE")]
    valued = [p for p in core_bench if p.get("_fp_eval_value") is not None]
    unvalued = [p for p in core_bench if p.get("_fp_eval_value") is None]

    bench_asset_total = round(sum(p["_fp_eval_value"] for p in valued), 2)

    relevant_valued = [p for p in valued if _position_has_flex_exposure(p.get("position"), slot_counts)]
    relevant_unvalued = [p for p in unvalued if _position_has_flex_exposure(p.get("position"), slot_counts)]
    lineup_relevant_total = round(sum(p["_fp_eval_value"] for p in relevant_valued), 2)

    starter_caliber = sum(1 for p in valued
                          if fp_client.describe_player_quality((p.get("_fp_intel") or {}).get("tier"))
                          in ("elite", "strong", "solid_starter", "flex_caliber"))

    return {
        "bench_asset_projection_total": bench_asset_total,
        "lineup_relevant_bench_projection_total": lineup_relevant_total,
        "valued_bench_count": len(valued),
        "unvalued_bench_count": len(unvalued),
        "unvalued_bench_players": [{"name": p.get("name"), "position": p.get("position")} for p in unvalued],
        "starter_or_flex_caliber_count": starter_caliber,
        "coverage_complete": len(relevant_unvalued) == 0,
        "excluded_non_lineup_relevant_players": [
            {"name": p.get("name"), "position": p.get("position")}
            for p in core_bench if not _position_has_flex_exposure(p.get("position"), slot_counts)
        ],
    }

def _analyze_position_strength(position: str, target_snapshot: dict, slot_counts: dict) -> dict:
    """Position-level strength/depth facts for one team, pre-ranking.
    Coverage checks BENCH players eligible for this slot too (same
    position for direct; any flex-eligible position for FLEX) - not just
    the players actually assigned to start - since an unvalued bench
    contender could have displaced a valued starter had their true value
    been known. league_rank/relative_label/depth_label are filled in by
    the caller after _rank_across_league runs across all teams."""
    lineup_fp = target_snapshot["lineup_fp"]

    if position == "FLEX":
        starters = lineup_fp.get("flex_starters", [])
        flex_eligible_positions = set()
        for slot_key, count in slot_counts.items():
            if count and count > 0:
                eligible = _parse_flex_eligibility(slot_key)
                if eligible:
                    flex_eligible_positions.update(eligible)
        bench_candidates = [p for p in lineup_fp.get("bench", []) if p.get("position") in flex_eligible_positions]
    else:
        starters = lineup_fp.get("starters", {}).get(position, [])
        bench_candidates = [p for p in lineup_fp.get("bench", []) if p.get("position") == position]

    missing_in_lineup = [{"name": p.get("name"), "position": p.get("position"), "in_starting_lineup": True}
                          for p in starters if p.get("_fp_eval_value") is None]
    missing_on_bench = [{"name": p.get("name"), "position": p.get("position"), "in_starting_lineup": False}
                         for p in bench_candidates if p.get("_fp_eval_value") is None]
    missing_all = missing_in_lineup + missing_on_bench

    valued_starters = [p for p in starters if p.get("_fp_eval_value") is not None]
    starter_known_total = round(sum(p["_fp_eval_value"] for p in valued_starters), 2)

    bench_at_pos = [p for p in lineup_fp.get("bench", []) if p.get("position") == position] if position != "FLEX" else []
    bench_valued = [p for p in bench_at_pos if p.get("_fp_eval_value") is not None]
    bench_known_total = round(sum(p["_fp_eval_value"] for p in bench_valued), 2)

    return {
        "direct_starters": [{"name": p.get("name"), "fp_projection": p.get("_fp_eval_value")} for p in starters],
        "bench_depth": [{"name": p.get("name"), "fp_projection": p.get("_fp_eval_value")} for p in bench_at_pos],
        "starter_projection_total_known": starter_known_total,
        "missing_projection_players": missing_all,
        "starter_coverage_complete": len(missing_all) == 0,
        "bench_projection_total_known": bench_known_total,
        "notes": (["One or more bench players eligible for this slot lack a FantasyPros projection; "
                    "the assigned lineup for this slot cannot be verified as FP-optimal."] if missing_on_bench else []),
        "league_rank": None, "league_size": None, "ranked_team_count": None,
        "relative_label": "unavailable", "depth_label": "unavailable",
    }

def _identify_core_assets(target_snapshot: dict, slot_counts: dict) -> list:
    """Core-asset detection using the ACTUAL positional rank field
    (pos_rank, e.g. 'RB20' -> 20), never raw overall ECR. Raw
    espn_ownership_pct may appear as context; ownership_anomaly_band is
    NEVER called here - that helper describes free-agent availability
    anomalies and is meaningless for a rostered player.

    Starters are not automatically core just for strong tier/rank: we
    simulate removing the player and check who realistically fills their
    vacated position. If a near-equivalent replacement already exists on
    the roster (drop-off <= fp_client.MEANINGFUL_PROJECTION_DELTA), the
    player is NOT classified as core despite strong external value and a
    starting role - the roster already has adequate depth there."""
    lineup_fp = target_snapshot["lineup_fp"]
    starter_names = {p.get("name") for p in _flatten_starters(lineup_fp)}
    roster_fp_rows = target_snapshot["roster_fp_rows"]

    core = []
    for i, p in enumerate(roster_fp_rows):
        fp_intel = p.get("_fp_intel") or {}
        tier = fp_intel.get("tier")
        pos_rank_int = _parse_positional_rank(fp_intel.get("pos_rank"))
        is_starter = p.get("name") in starter_names
        eval_value = p.get("_fp_eval_value")

        if is_starter and ((tier is not None and tier <= 3) or (pos_rank_int is not None and pos_rank_int <= 12)):
            simulated = roster_fp_rows[:i] + roster_fp_rows[i + 1:]
            after_removal = _assign_best_lineup(simulated, slot_counts, value_field="_fp_eval_value")

            after_names_at_pos = ({q.get("name") for q in after_removal.get("starters", {}).get(p.get("position"), [])}
                                   | {q.get("name") for q in after_removal.get("flex_starters", [])
                                      if q.get("position") == p.get("position")})
            before_names_at_pos = ({q.get("name") for q in lineup_fp.get("starters", {}).get(p.get("position"), [])}
                                    | {q.get("name") for q in lineup_fp.get("flex_starters", [])
                                       if q.get("position") == p.get("position")})
            new_entrants = after_names_at_pos - (before_names_at_pos - {p.get("name")})
            replacement_rows = [q for q in simulated if q.get("name") in new_entrants]
            replacement_value = max(
                (q.get("_fp_eval_value") for q in replacement_rows if q.get("_fp_eval_value") is not None),
                default=None)

            near_equivalent_replacement_exists = (
                eval_value is not None and replacement_value is not None
                and (eval_value - replacement_value) <= fp_client.MEANINGFUL_PROJECTION_DELTA
            )
            if near_equivalent_replacement_exists:
                continue

            fields_used = []
            if tier is not None:
                fields_used.append(f"tier={tier}")
            if pos_rank_int is not None:
                fields_used.append(f"pos_rank={fp_intel.get('pos_rank')}")
            replacement_note = (f"no near-equivalent roster replacement (best available drop-off would be "
                                 f"{round(eval_value - replacement_value, 2)} pts)"
                                 if eval_value is not None and replacement_value is not None
                                 else "no roster replacement available at this position at all")
            core.append({
                "player": p.get("name"), "position": p.get("position"),
                "lineup_role": "direct_starter" if p.get("name") in
                    {q.get("name") for pos_list in lineup_fp.get("starters", {}).values() for q in pos_list}
                    else "flex_starter",
                "external_quality": fp_client.describe_player_quality(tier),
                "espn_ownership_pct_context": fp_intel.get("espn_ownership_pct"),
                "why_core": f"Currently a lineup starter; " + ", ".join(fields_used) + f"; {replacement_note}.",
                "confidence": fp_intel.get("match_confidence"),
            })
            continue

        if not is_starter and tier is not None and tier <= 2 and eval_value is not None:
            same_pos_others = [q for q in roster_fp_rows
                                if q.get("position") == p.get("position") and q.get("name") != p.get("name")]
            better_or_equal_exists = any((q.get("_fp_eval_value") or -1) >= eval_value for q in same_pos_others)
            if not better_or_equal_exists:
                core.append({
                    "player": p.get("name"), "position": p.get("position"),
                    "lineup_role": "bench",
                    "external_quality": fp_client.describe_player_quality(tier),
                    "espn_ownership_pct_context": fp_intel.get("espn_ownership_pct"),
                    "why_core": f"Bench player, but tier={tier} ({fp_client.describe_player_quality(tier)}) "
                                f"with no equal-or-better roster replacement at {p.get('position')}.",
                    "confidence": fp_intel.get("match_confidence"),
                })
    return core

def _identify_expendable_assets(target_snapshot: dict, slot_counts: dict, core_asset_names: set) -> list:
    """Non-core players who are realistic drop/trade/replace candidates.
    Roster legality after removal is NOT the sole gate: a team's only K,
    D/ST, QB, or a weak TE can still be genuinely expendable even though
    dropping them without a replacement would break feasibility - that's
    just the normal 'drop weak player, add a waiver player' workflow, not
    proof of untouchability. drop_without_replacement_feasible and
    replacement_required are reported separately. Zero ESPN projection is
    no longer treated ALONE as sufficient 'dead roster spot' evidence -
    it's combined with weak FantasyPros external value (tier>=5 or no
    usable FP data) before that framing is used."""
    lineup_fp = target_snapshot["lineup_fp"]
    starter_names = {p.get("name") for p in _flatten_starters(lineup_fp)}
    roster_fp_rows = target_snapshot["roster_fp_rows"]

    expendable = []
    for i, p in enumerate(roster_fp_rows):
        if p.get("name") in core_asset_names:
            continue

        simulated = roster_fp_rows[:i] + roster_fp_rows[i + 1:]
        feasibility = _assign_best_lineup(simulated, slot_counts, value_field="_fp_eval_value")
        drop_without_replacement_feasible = feasibility["feasible"]
        replacement_required = not drop_without_replacement_feasible

        is_starter = p.get("name") in starter_names
        zero_espn = (p.get("projected_points") or 0) <= 0
        fp_intel = p.get("_fp_intel") or {}
        tier = fp_intel.get("tier")
        weak_external_value = (tier is None) or (tier >= 5)

        qualifies = (not is_starter) or (is_starter and weak_external_value)
        if not qualifies:
            continue

        reasons = []
        if zero_espn and weak_external_value:
            reasons.append("zero ESPN season projection combined with weak/no FantasyPros external value "
                           "(a likely dead roster spot)")
        elif zero_espn:
            reasons.append("zero ESPN season projection (context only - not alone treated as a dead spot "
                           "without corroborating weak FantasyPros value)")
        if not is_starter:
            reasons.append("not currently in the best FantasyPros-value starting lineup")
        elif weak_external_value:
            reasons.append(f"occupies a starting/required slot but external value is weak "
                           f"(tier={tier if tier is not None else 'unknown'})")
        reasons.append("dropping without a same-slot replacement would break lineup feasibility - a "
                        "waiver/trade replacement at this position would be required" if replacement_required
                        else "removing this player does not break lineup feasibility even without a replacement")

        expendable.append({
            "player": p.get("name"), "position": p.get("position"),
            "current_lineup_role": "starter_or_flex" if is_starter else "bench",
            "external_quality": fp_client.describe_player_quality(tier),
            "why_expendable": "; ".join(reasons) + ".",
            "drop_without_replacement_feasible": drop_without_replacement_feasible,
            "replacement_required": replacement_required,
            "confidence": fp_intel.get("match_confidence"),
        })
    return expendable

def _identify_trade_surplus(target_snapshot: dict, position_analysis: dict, slot_counts: dict) -> list:
    """Conservative trade-surplus detection. A position only qualifies if
    BOTH starter strength AND depth are strong/above_average (ordinary
    healthy depth is NOT surplus), at least one useful (flex-caliber-or-
    better) player currently sits outside the best starting/FLEX rotation,
    and removing that player would not break lineup feasibility. Labeled
    trade_surplus_candidate throughout - never an assertion the player
    "should" be traded."""
    lineup_fp = target_snapshot["lineup_fp"]
    roster_fp_rows = target_snapshot["roster_fp_rows"]
    starter_names = {p.get("name") for p in _flatten_starters(lineup_fp)}

    surplus = []
    for position in ("QB", "RB", "WR", "TE"):
        pa = position_analysis.get(position, {})
        strength_label = pa.get("relative_label")
        depth_label = pa.get("depth_label")

        if strength_label not in ("strong", "above_average") or depth_label not in ("strong", "above_average"):
            continue

        position_players = [p for p in roster_fp_rows if p.get("position") == position]
        currently_startable = [p for p in position_players if p.get("name") in starter_names]
        useful_rostered = [p for p in position_players
                            if fp_client.describe_player_quality((p.get("_fp_intel") or {}).get("tier"))
                            in ("elite", "strong", "solid_starter", "flex_caliber")]
        outside_rotation_useful = [p for p in useful_rostered if p.get("name") not in starter_names]

        if not outside_rotation_useful:
            continue

        candidates = []
        for p in outside_rotation_useful:
            idx = roster_fp_rows.index(p)
            simulated = roster_fp_rows[:idx] + roster_fp_rows[idx + 1:]
            feasibility = _assign_best_lineup(simulated, slot_counts, value_field="_fp_eval_value")
            if feasibility["feasible"]:
                candidates.append({
                    "player": p.get("name"),
                    "external_quality": fp_client.describe_player_quality((p.get("_fp_intel") or {}).get("tier")),
                    "fp_projection": p.get("_fp_eval_value"),
                })

        if not candidates:
            continue

        surplus.append({
            "position": position,
            "surplus_candidates": candidates,
            "position_strength": strength_label,
            "depth_strength": depth_label,
            "currently_startable_count": len(currently_startable),
            "useful_rostered_count": len(useful_rostered),
            "why_surplus": (f"{position} starter strength is {strength_label} and depth is {depth_label} "
                             f"(both above league median); {len(useful_rostered)} flex-caliber-or-better "
                             f"{position}s rostered vs {len(currently_startable)} currently starting; at "
                             f"least one useful player sits outside the best rotation and could be moved "
                             f"without breaking lineup feasibility."),
            "confidence": "medium",
        })
    return surplus

def _identify_positional_needs(position_analysis: dict, target_snapshot: dict, slot_counts: dict) -> list:
    """Rule-based need severity from transparent, already-computed
    evidence. Guards against manufacturing a need purely from backup-
    quantity at a position with no real starting exposure - backup QB
    count in a 1-QB, no-OP league is never elevated above 'none' on that
    basis alone, since severity is driven by the STARTER's own
    league-relative strength/depth, never bench count.

    Explicit single-starter/no-flex-exposure guard (derived generically
    from slot_counts + _parse_flex_eligibility via the frozen
    _position_has_flex_exposure - never hardcoded to a specific league
    or specifically to "QB" by name): when a position has exactly ONE
    direct starting slot and no real flex/OP route for that position
    (the standard case for QB in a 1-QB, no-superflex league),
    "my starter is relatively weak" and "I lack a backup" are DIFFERENT
    problems - only starter viability/health drives severity in that
    case; backup depth/quantity is informational only. A league with 2+
    direct starters at that position, or a real superflex/OP slot
    (has_flex_exposure=True), is NOT subject to this guard and falls
    through to the standard depth-aware logic below, so QB depth still
    matters normally in a superflex league."""
    needs = []

    for position in ("QB", "RB", "WR", "TE"):
        direct_slots = slot_counts.get(position, 0)
        has_flex_exposure = _position_has_flex_exposure(position, slot_counts)
        if direct_slots == 0 and not has_flex_exposure:
            continue

        pa = position_analysis.get(position, {})
        strength_label = pa.get("relative_label")
        depth_label = pa.get("depth_label")
        coverage_complete = pa.get("starter_coverage_complete", True)

        starters_here = pa.get("direct_starters", [])
        starter_names_here = {s.get("name") for s in starters_here}
        materially_reduced_starter = False
        for p in target_snapshot["roster_fp_rows"]:
            if p.get("name") in starter_names_here:
                fp_intel = p.get("_fp_intel") or {}
                signal = fp_client.classify_injury_signal(fp_intel.get("injury_status"))
                if signal.get("label") == "materially_reduced":
                    materially_reduced_starter = True

        bench_replacement_exists = any(
            b.get("fp_projection") is not None and b.get("fp_projection") > 0
            for b in pa.get("bench_depth", []))

        if not coverage_complete:
            needs.append({
                "position": position, "severity": "unknown",
                "evidence": (f"{position} coverage is insufficient_data (a rostered player affecting this "
                             f"position's assignment lacks a FantasyPros projection) - need severity cannot "
                             f"be reliably assessed yet."),
            })
            continue

        # --- Single-starter, no-flex-exposure guard ---
        is_single_starter_no_flex_position = (direct_slots == 1 and not has_flex_exposure)
        if is_single_starter_no_flex_position:
            no_viable_starter = len(starters_here) < direct_slots
            if no_viable_starter:
                severity = "urgent"
                evidence = (f"{position} has no viable legal starter filling the required direct slot in "
                            f"this single-starter, no-flex-eligible league configuration.")
            elif materially_reduced_starter and not bench_replacement_exists:
                severity = "urgent"
                evidence = (f"The starting {position} carries a materially reduced injury status with no "
                            f"viable bench replacement rostered, in a single-starter, no-flex-eligible "
                            f"league configuration where a backup cannot otherwise be substituted into "
                            f"the lineup.")
            elif strength_label in ("weak", "below_average"):
                severity = "meaningful"
                evidence = (f"{position} starter strength ranks {strength_label} among fully covered "
                            f"league teams (league_rank {pa.get('league_rank')} of "
                            f"{pa.get('ranked_team_count')} ranked); however, the roster has a healthy, "
                            f"viable starter and this is a single-starter, no-flex-eligible league "
                            f"configuration at {position} - backup depth does not change this assessment.")
            else:
                severity = "none"
                evidence = (f"{position} starter strength is {strength_label} and the roster has a "
                            f"healthy, viable starter in this single-starter, no-flex-eligible league "
                            f"configuration.")
            needs.append({"position": position, "severity": severity, "evidence": evidence})
            continue

        # --- Standard depth-aware logic (2+ direct starters, or real flex/OP exposure) ---
        if strength_label == "weak" and (depth_label == "weak" or materially_reduced_starter) and not bench_replacement_exists:
            severity = "urgent"
            evidence = (f"{position} starter strength ranks weak in the league"
                        + (f"; a starter carries a materially reduced injury status" if materially_reduced_starter else "")
                        + f"; depth is {depth_label} with no viable bench replacement rostered.")
        elif strength_label in ("weak", "below_average") and depth_label in ("weak", "below_average"):
            severity = "meaningful"
            evidence = f"{position} starter strength is {strength_label} and depth is also {depth_label}."
        elif depth_label == "weak" and strength_label not in ("weak", "below_average"):
            severity = "minor"
            evidence = (f"{position} starting strength is {strength_label} (adequate), but bench depth at "
                        f"this position is weak - a single injury/bye would create risk.")
        else:
            severity = "none"
            evidence = f"{position} starter strength is {strength_label}, depth is {depth_label}."

        needs.append({"position": position, "severity": severity, "evidence": evidence})

    return needs


# --- Helpers added for evaluate_trade (existing 21 tools untouched) ---

def _extract_roster_dicts(team) -> list:
    """Normalize factual roster rows from either compatibility wrapper or domain teams.

    The trade evidence layer intentionally consumes this stable dict shape;
    platform translation stays outside the recommendation logic.
    """
    return [{
        "name": getattr(p, "name", None), "position": getattr(p, "position", None),
        "proTeam": getattr(p, "pro_team", getattr(p, "proTeam", None)),
        "projected_points": getattr(p, "season_projected_points", getattr(p, "projected_total_points", None)),
        "points": getattr(p, "season_total_points", getattr(p, "total_points", None)),
        "lineup_slot": getattr(p, "lineup_slot", getattr(p, "lineupSlot", None)),
        "espn_injury_status": getattr(p, "injury_status", getattr(p, "injuryStatus", None)),
    } for p in team.roster]

def _match_name_in_roster(roster_dicts: list, query_name: str) -> dict:
    q_exact = (query_name or "").strip().casefold()
    exact_matches = [p for p in roster_dicts if (p.get("name") or "").strip().casefold() == q_exact]
    if len(exact_matches) == 1:
        return {"match_type": "exact", "candidates": exact_matches}
    if len(exact_matches) > 1:
        return {"match_type": "ambiguous", "candidates": exact_matches}
    q_norm = fp_client.normalize_player_name(query_name)
    norm_matches = [p for p in roster_dicts if fp_client.normalize_player_name(p.get("name")) == q_norm]
    if len(norm_matches) == 1:
        return {"match_type": "normalized", "candidates": norm_matches}
    if len(norm_matches) > 1:
        return {"match_type": "ambiguous", "candidates": norm_matches}
    return {"match_type": "none", "candidates": []}

def _resolve_trade_players(league, team_id_val: int, players_out: list, players_in: list) -> dict:
    if not isinstance(players_out, list) or not (1 <= len(players_out) <= 3):
        return {"error": "invalid_parameter", "message": "players_out must be a list of 1-3 player names."}
    if not isinstance(players_in, list) or not (1 <= len(players_in) <= 3):
        return {"error": "invalid_parameter", "message": "players_in must be a list of 1-3 player names."}
    if not all(isinstance(n, str) and n.strip() for n in players_out):
        return {"error": "invalid_parameter", "message": "players_out must contain only non-empty strings."}
    if not all(isinstance(n, str) and n.strip() for n in players_in):
        return {"error": "invalid_parameter", "message": "players_in must contain only non-empty strings."}

    norm_out = [fp_client.normalize_player_name(n) for n in players_out]
    norm_in = [fp_client.normalize_player_name(n) for n in players_in]
    if len(set(norm_out)) != len(norm_out):
        return {"error": "invalid_parameter", "message": "players_out contains duplicate player names."}
    if len(set(norm_in)) != len(norm_in):
        return {"error": "invalid_parameter", "message": "players_in contains duplicate player names."}
    if set(norm_out) & set(norm_in):
        return {"error": "invalid_parameter", "message": "A player name appears in both players_out and players_in."}

    target_team = _find_team_by_id(league, team_id_val)
    if target_team is None:
        valid_ids = sorted(t.team_id for t in league.teams)
        return {"error": "invalid_parameter",
                 "message": f"No team with team_id={team_id_val} in this league. Valid team_ids: {valid_ids}"}

    target_roster_before = _extract_roster_dicts(target_team)

    players_out_resolved = []
    for name in players_out:
        match = _match_name_in_roster(target_roster_before, name)
        if match["match_type"] == "none":
            return {"error": "player_not_on_roster",
                     "message": f"'{name}' is not on team_id={team_id_val}'s current roster."}
        if match["match_type"] == "ambiguous":
            return {"error": "ambiguous_outgoing_player",
                     "message": f"'{name}' matches multiple players on this roster; cannot auto-resolve.",
                     "candidates": match["candidates"]}
        players_out_resolved.append(match["candidates"][0])

    other_teams = [t for t in league.teams if t.team_id != team_id_val]
    other_rosters = {t.team_id: _extract_roster_dicts(t) for t in other_teams}

    players_in_resolved = []
    owning_team_ids = set()
    for name in players_in:
        target_match = _match_name_in_roster(target_roster_before, name)
        if target_match["match_type"] == "ambiguous":
            return {"error": "ambiguous_incoming_player",
                     "message": f"'{name}' matches multiple players on team_id={team_id_val}'s own roster; "
                                 "cannot determine identity safely.",
                     "candidates": target_match["candidates"]}
        if target_match["match_type"] in ("exact", "normalized"):
            return {"error": "player_already_on_target_roster",
                     "message": f"'{name}' is already on team_id={team_id_val}'s roster."}

        found_on = []
        for other_id, other_roster in other_rosters.items():
            m = _match_name_in_roster(other_roster, name)
            if m["match_type"] in ("exact", "normalized"):
                found_on.append((other_id, m["candidates"][0]))
            elif m["match_type"] == "ambiguous":
                return {"error": "ambiguous_incoming_player",
                         "message": f"'{name}' matches multiple players on team_id={other_id}'s roster.",
                         "candidates": m["candidates"]}

        if not found_on:
            return {"error": "player_not_on_any_opposing_roster",
                     "message": f"'{name}' was not found on any opposing team's roster in this league. "
                                 "This tool evaluates roster-to-roster trades only - free-agent availability "
                                 "is never checked."}
        if len(found_on) > 1:
            return {"error": "ambiguous_incoming_player",
                     "message": f"'{name}' matches players on multiple different rosters "
                                 f"({[tid for tid, _ in found_on]}); cannot determine trade partner without disambiguation.",
                     "candidates": [c for _, c in found_on]}

        owning_team_id, resolved_player = found_on[0]
        owning_team_ids.add(owning_team_id)
        players_in_resolved.append(resolved_player)

    if len(owning_team_ids) > 1:
        return {"error": "multi_team_trade_not_supported",
                 "message": f"Incoming players are owned by multiple different teams ({sorted(owning_team_ids)}); "
                             "this tool supports a single two-team trade only.",
                 "owning_team_ids": sorted(owning_team_ids)}

    partner_team_id = next(iter(owning_team_ids))
    partner_team = _find_team_by_id(league, partner_team_id)
    partner_roster_before = other_rosters[partner_team_id]

    return {
        "target_team": target_team, "target_roster_before": target_roster_before,
        "players_out_resolved": players_out_resolved, "players_in_resolved": players_in_resolved,
        "partner_team_id": partner_team_id, "partner_team": partner_team,
        "partner_roster_before": partner_roster_before,
    }

def _simulate_trade_roster(roster_before: list, players_out_resolved: list, players_in_resolved: list) -> list:
    out_names = {p["name"] for p in players_out_resolved}
    remaining = [p for p in roster_before if p["name"] not in out_names]
    incoming = [dict(p, lineup_slot=None) for p in players_in_resolved]
    return remaining + incoming

def _check_roster_size_limit(roster_after: list, roster_before: list, players_out_names: set,
                               slot_counts: dict) -> dict:
    active_roster_capacity = sum(v for k, v in slot_counts.items() if k != "IR")
    ir_slots_configured = slot_counts.get("IR", 0)
    active_roster_count_before = sum(1 for p in roster_before if p.get("lineup_slot") != "IR")
    ir_occupants_preserved = [p["name"] for p in roster_before
                                if p.get("lineup_slot") == "IR" and p["name"] not in players_out_names]
    active_roster_count_after = len(roster_after) - len(ir_occupants_preserved)
    open_roster_spots_after = max(0, active_roster_capacity - active_roster_count_after)
    size_feasible = active_roster_count_after <= active_roster_capacity
    if size_feasible:
        reason = (f"Fits modeled active roster capacity ({active_roster_count_after}/{active_roster_capacity}); "
                   f"{open_roster_spots_after} open spot(s) after this trade.")
    else:
        reason = (f"Proposed package does not fit modeled active roster capacity "
                   f"({active_roster_count_after}/{active_roster_capacity}). Incoming players are never "
                   f"assumed IR-eligible, so this cannot be resolved via an unused IR slot.")
    return {
        "active_roster_count_before": active_roster_count_before,
        "active_roster_count_after": active_roster_count_after,
        "active_roster_capacity": active_roster_capacity,
        "open_roster_spots_after": open_roster_spots_after,
        "ir_slots_configured": ir_slots_configured,
        "ir_occupants_preserved": ir_occupants_preserved,
        "size_feasible": size_feasible,
        "modeled_transaction_size_feasible": size_feasible,
        "reason": reason,
    }

def _build_snapshot_from_roster(roster_dicts: list, slot_counts: dict, scoring_bucket: str,
                                  team_id, team_name) -> dict:
    roster = [dict(p) for p in roster_dicts]
    for p in roster:
        p["_fp_intel"] = fp_client.build_player_intelligence(
            p.get("name"), p.get("proTeam"), p.get("position"), scoring=scoring_bucket)
    roster_fp_rows, fp_reliability_warning = _build_fp_eval_roster(roster)
    lineup_espn = _assign_best_lineup(roster, slot_counts, value_field="projected_points")
    lineup_fp = _assign_best_lineup(roster_fp_rows, slot_counts, value_field="_fp_eval_value")
    return {
        "team_id": team_id, "team_name": team_name,
        "roster": roster, "roster_fp_rows": roster_fp_rows,
        "lineup_espn": lineup_espn, "lineup_fp": lineup_fp,
        "fp_reliability_warning": fp_reliability_warning,
    }

def _build_team_snapshot(team, slot_counts: dict, scoring_bucket: str) -> dict:
    return _build_snapshot_from_roster(_extract_roster_dicts(team), slot_counts, scoring_bucket,
                                         team.team_id, team.team_name)

def _compare_market_value(players_out_resolved: list, players_in_resolved: list) -> dict:
    out_intel = [p["_fp_intel"] for p in players_out_resolved]
    in_intel = [p["_fp_intel"] for p in players_in_resolved]

    def no_signal(intel):
        return (intel.get("projected_points") is None and intel.get("ecr") is None
                and intel.get("tier") is None and intel.get("adp") is None)
    out_no_signal = sum(1 for i in out_intel if no_signal(i))
    in_no_signal = sum(1 for i in in_intel if no_signal(i))

    proj_vals_out = [i.get("projected_points") for i in out_intel]
    proj_vals_in = [i.get("projected_points") for i in in_intel]
    ecr_vals_out = [i.get("ecr") for i in out_intel]
    ecr_vals_in = [i.get("ecr") for i in in_intel]
    tier_vals_out = [i.get("tier") for i in out_intel]
    tier_vals_in = [i.get("tier") for i in in_intel]
    adp_vals_out = [i.get("adp") for i in out_intel]
    adp_vals_in = [i.get("adp") for i in in_intel]
    proj_coverage_complete = all(v is not None for v in proj_vals_out + proj_vals_in)
    ecr_coverage_complete = all(v is not None for v in ecr_vals_out + ecr_vals_in)
    tier_coverage_complete = all(v is not None for v in tier_vals_out + tier_vals_in)
    adp_coverage_complete = all(v is not None for v in adp_vals_out + adp_vals_in)
    signal_coverage = {"projection": proj_coverage_complete, "ecr": ecr_coverage_complete,
                         "tier": tier_coverage_complete, "adp": adp_coverage_complete}

    package_size_in, package_size_out = len(players_in_resolved), len(players_out_resolved)

    def _asset_row(player_dict, intel):
        return {"player": player_dict["name"], "position": player_dict.get("position"),
                 "proTeam": player_dict.get("proTeam"), "match_confidence": intel.get("match_confidence"),
                 "ecr": intel.get("ecr"), "pos_rank": intel.get("pos_rank"), "tier": intel.get("tier"),
                 "adp": intel.get("adp"), "fp_projected_points": intel.get("projected_points"),
                 "injury_status": intel.get("injury_status"), "espn_ownership_pct": intel.get("espn_ownership_pct")}
    asset_quality_context = {"outgoing": [_asset_row(p, i) for p, i in zip(players_out_resolved, out_intel)],
                               "incoming": [_asset_row(p, i) for p, i in zip(players_in_resolved, in_intel)]}

    if out_no_signal >= max(1, len(out_intel) / 2) or in_no_signal >= max(1, len(in_intel) / 2):
        early_best_asset_side = "not_applicable" if package_size_in == package_size_out else "unknown"
        return {
            "assessment": "insufficient_data",
            "projection_value_signal": {"outgoing_projection_total": None, "incoming_projection_total": None,
                                          "projection_delta": None, "projection_direction": None, "coverage_complete": False},
            "signal_coverage": signal_coverage,
            "asset_quality_context": asset_quality_context,
            "consolidation_context": {"package_size_in": package_size_in, "package_size_out": package_size_out,
                                        "best_asset_side": early_best_asset_side,
                                        "note": ("Equal-count trade; consolidation/star-premium comparison not applicable."
                                                  if early_best_asset_side == "not_applicable"
                                                  else "Insufficient data for consolidation comparison.")},
            "reason": "At least half of the players on one side lack any usable FantasyPros market signal "
                       "(no projection, ECR, tier, or ADP) - too little reliable data for a responsible comparison.",
        }

    if proj_coverage_complete:
        outgoing_total, incoming_total = round(sum(proj_vals_out), 2), round(sum(proj_vals_in), 2)
        delta = round(incoming_total - outgoing_total, 2)
        direction = ("incoming_projection_advantage" if delta > 0
                      else "outgoing_projection_advantage" if delta < 0 else "even")
    else:
        outgoing_total = incoming_total = delta = direction = None
    projection_value_signal = {"outgoing_projection_total": outgoing_total, "incoming_projection_total": incoming_total,
                                 "projection_delta": delta, "projection_direction": direction,
                                 "coverage_complete": proj_coverage_complete}

    if package_size_in == package_size_out:
        best_asset_side = "not_applicable"
        consolidation_note = "Equal-count trade; consolidation/star-premium comparison not applicable."
    elif not tier_coverage_complete:
        best_asset_side = "unknown"
        consolidation_note = "Insufficient comparable tier data on at least one side; consolidation conclusion withheld."
    else:
        best_out, best_in = min(tier_vals_out), min(tier_vals_in)
        if best_in < best_out:
            best_asset_side = "incoming"
            consolidation_note = f"Incoming side holds the best individual asset (tier {best_in} vs {best_out})."
        elif best_out < best_in:
            best_asset_side = "outgoing"
            consolidation_note = f"Outgoing side holds the best individual asset (tier {best_out} vs {best_in})."
        else:
            best_asset_side = "tie"
            consolidation_note = f"Both sides' best individual asset shares tier {best_in}."
    consolidation_context = {"package_size_in": package_size_in, "package_size_out": package_size_out,
                               "best_asset_side": best_asset_side, "note": consolidation_note}

    votes_in = votes_out = 0
    vote_notes = []
    if proj_coverage_complete and direction != "even":
        if direction == "incoming_projection_advantage":
            votes_in += 1
        else:
            votes_out += 1
        vote_notes.append(f"season projection {'favors incoming' if direction=='incoming_projection_advantage' else 'favors outgoing'} ({delta:+.2f} pts)")

    if ecr_coverage_complete:
        best_ecr_out, best_ecr_in = min(ecr_vals_out), min(ecr_vals_in)
        if best_ecr_out != best_ecr_in:
            if best_ecr_in < best_ecr_out:
                votes_in += 1
            else:
                votes_out += 1
            vote_notes.append(f"ECR favors {'incoming' if best_ecr_in < best_ecr_out else 'outgoing'} ({best_ecr_in} vs {best_ecr_out})")

    if tier_coverage_complete:
        best_tier_out, best_tier_in = min(tier_vals_out), min(tier_vals_in)
        if best_tier_out != best_tier_in:
            if best_tier_in < best_tier_out:
                votes_in += 1
            else:
                votes_out += 1
            vote_notes.append(f"tier favors {'incoming' if best_tier_in < best_tier_out else 'outgoing'} (tier {best_tier_in} vs {best_tier_out})")

    if adp_coverage_complete:
        best_adp_out, best_adp_in = min(adp_vals_out), min(adp_vals_in)
        if best_adp_out != best_adp_in:
            if best_adp_in < best_adp_out:
                votes_in += 1
            else:
                votes_out += 1
            vote_notes.append(f"ADP favors {'incoming' if best_adp_in < best_adp_out else 'outgoing'} ({best_adp_in} vs {best_adp_out})")

    net = votes_in - votes_out
    if net >= 3:
        assessment = "strong_incoming_advantage"
    elif net >= 1:
        assessment = "incoming_advantage"
    elif net <= -3:
        assessment = "strong_outgoing_advantage"
    elif net <= -1:
        assessment = "outgoing_advantage"
    else:
        assessment = "roughly_even"

    if best_asset_side in ("incoming", "outgoing") and package_size_in != package_size_out:
        override_side = best_asset_side
        if assessment == "roughly_even":
            assessment = "incoming_advantage" if override_side == "incoming" else "outgoing_advantage"
            vote_notes.append(f"consolidation override: roughly-even signals nudged toward {override_side} "
                                f"(holds the best individual asset in this unequal package)")
        elif (net == 1 and override_side == "outgoing") or (net == -1 and override_side == "incoming"):
            assessment = "roughly_even"
            vote_notes.append(f"consolidation override: a weak 1-signal lean against {override_side} was reversed to roughly_even")

    reason = (f"{votes_in} of up to 4 comparable market signals favor incoming, {votes_out} favor outgoing"
               + (f" ({'; '.join(vote_notes)})" if vote_notes else "")
               + f". Package sizes: {package_size_out}-for-{package_size_in}.")

    return {
        "assessment": assessment,
        "projection_value_signal": projection_value_signal,
        "signal_coverage": signal_coverage,
        "asset_quality_context": asset_quality_context,
        "consolidation_context": consolidation_context,
        "reason": reason,
    }

def _build_slot_map(lineup_fp: dict) -> dict:
    slot_map = {}
    for pos, players in lineup_fp.get("starters", {}).items():
        for p in players:
            slot_map[p.get("name")] = pos
    for p in lineup_fp.get("flex_starters", []):
        slot_map[p.get("name")] = "FLEX"
    return slot_map

def _compare_lineups(lineup_fp_before: dict, lineup_fp_after: dict,
                       core_offense_before: dict, core_offense_after: dict) -> dict:
    before_map = _build_slot_map(lineup_fp_before)
    after_map = _build_slot_map(lineup_fp_after)

    entering = sorted(set(after_map) - set(before_map))
    leaving = sorted(set(before_map) - set(after_map))
    changing = [
        {"player": name, "before_slot": before_map[name], "after_slot": after_map[name]}
        for name in sorted(set(before_map) & set(after_map)) if before_map[name] != after_map[name]
    ]

    def touches_flex(before_slot, after_slot):
        return before_slot == "FLEX" or after_slot == "FLEX"

    def touches_direct(before_slot, after_slot):
        return (before_slot is not None and before_slot != "FLEX") or (after_slot is not None and after_slot != "FLEX")

    all_events = ([{"player": n, "before_slot": None, "after_slot": after_map[n]} for n in entering]
                   + [{"player": n, "before_slot": before_map[n], "after_slot": None} for n in leaving]
                   + changing)
    direct_slot_changes = [e for e in all_events if touches_direct(e["before_slot"], e["after_slot"])]
    flex_changes = [e for e in all_events if touches_flex(e["before_slot"], e["after_slot"])]

    coverage_complete = core_offense_before["coverage_complete"] and core_offense_after["coverage_complete"]
    result = {
        "before_slot_by_player": before_map, "after_slot_by_player": after_map,
        "players_entering_lineup": entering, "players_leaving_lineup": leaving,
        "players_changing_slots": changing,
        "direct_slot_changes": direct_slot_changes, "flex_changes": flex_changes,
        "core_offense_projection_before": core_offense_before["known_projection_total"],
        "core_offense_projection_after": core_offense_after["known_projection_total"],
        "coverage_complete": coverage_complete,
    }
    if not coverage_complete:
        result["projection_delta"] = None
        result["classification"] = "insufficient_data"
        result["structural_effect"] = "starting_lineup_changed" if (entering or leaving or changing) else "no_starting_change"
        return result

    delta = round(core_offense_after["known_projection_total"] - core_offense_before["known_projection_total"], 2)
    result["projection_delta"] = delta
    result["structural_effect"] = None
    if delta >= fp_client.STRONG_PROJECTION_DELTA:
        result["classification"] = "major_starting_upgrade"
    elif delta >= fp_client.MEANINGFUL_PROJECTION_DELTA:
        result["classification"] = "starting_upgrade"
    elif delta > 0:
        result["classification"] = "minor_starting_upgrade"
    elif delta > -fp_client.MEANINGFUL_PROJECTION_DELTA:
        result["classification"] = "no_starting_change"
    else:
        result["classification"] = "starting_downgrade"
    return result

def _compare_depth(snapshot_before: dict, snapshot_after: dict, slot_counts: dict,
                     roster_size_check: dict) -> dict:
    depth_before = _bench_depth_metrics(snapshot_before["lineup_fp"], slot_counts)
    depth_after = _bench_depth_metrics(snapshot_after["lineup_fp"], slot_counts)
    coverage_complete = depth_before["coverage_complete"] and depth_after["coverage_complete"]
    delta = round(depth_after["lineup_relevant_bench_projection_total"]
                   - depth_before["lineup_relevant_bench_projection_total"], 2) if coverage_complete else None

    positions_gaining, positions_losing, positions_incomplete = [], [], []
    for pos in ("QB", "RB", "WR", "TE"):
        pa_before = _analyze_position_strength(pos, snapshot_before, slot_counts)
        pa_after = _analyze_position_strength(pos, snapshot_after, slot_counts)
        depth_cov_before = all(b["fp_projection"] is not None for b in pa_before["bench_depth"])
        depth_cov_after = all(b["fp_projection"] is not None for b in pa_after["bench_depth"])
        if depth_cov_before and depth_cov_after:
            d = pa_after["bench_projection_total_known"] - pa_before["bench_projection_total_known"]
            if d > 0:
                positions_gaining.append(pos)
            elif d < 0:
                positions_losing.append(pos)
        else:
            positions_incomplete.append(pos)

    if not coverage_complete:
        depth_effect = "insufficient_data"
    elif delta >= fp_client.STRONG_PROJECTION_DELTA:
        depth_effect = "major_depth_improvement"
    elif delta >= fp_client.MEANINGFUL_PROJECTION_DELTA:
        depth_effect = "depth_improvement"
    elif delta > -fp_client.MEANINGFUL_PROJECTION_DELTA:
        depth_effect = "neutral"
    elif delta > -fp_client.STRONG_PROJECTION_DELTA:
        depth_effect = "depth_reduction"
    else:
        depth_effect = "major_depth_reduction"

    risk_notes = []
    if coverage_complete and depth_effect in ("depth_reduction", "major_depth_reduction"):
        note = (f"Computed lineup-relevant bench depth falls from {depth_before['lineup_relevant_bench_projection_total']} "
                 f"to {depth_after['lineup_relevant_bench_projection_total']} FP season points after the optimized "
                 f"lineup is rebuilt, leaving less immediate replacement flexibility.")
        if roster_size_check.get("open_roster_spots_after", 0) > 0:
            note += (f" This trade creates {roster_size_check['open_roster_spots_after']} open active roster "
                      f"spot(s); no replacement player has been assumed.")
        risk_notes.append(note)
    if coverage_complete and depth_after["starter_or_flex_caliber_count"] < depth_before["starter_or_flex_caliber_count"]:
        risk_notes.append(f"Flex-caliber-or-better bench count falls from {depth_before['starter_or_flex_caliber_count']} "
                            f"to {depth_after['starter_or_flex_caliber_count']}.")

    return {
        "depth_before": depth_before, "depth_after": depth_after,
        "positions_gaining_depth": positions_gaining, "positions_losing_depth": positions_losing,
        "positions_with_incomplete_depth_coverage": positions_incomplete,
        "depth_effect": depth_effect, "coverage_complete": coverage_complete,
        "lineup_relevant_bench_delta": delta, "risk_notes": risk_notes,
    }

_NEED_SEVERITY_ORDER = {"urgent": 0, "meaningful": 1, "minor": 2, "none": 3}

def _diff_positional_needs(needs_before: list, needs_after: list) -> list:
    before_by_pos = {n["position"]: n["severity"] for n in needs_before}
    after_by_pos = {n["position"]: n["severity"] for n in needs_after}
    diffs = []
    for pos in sorted(set(before_by_pos) | set(after_by_pos)):
        before, after = before_by_pos.get(pos, "unknown"), after_by_pos.get(pos, "unknown")
        if before == "unknown" or after == "unknown":
            effect = "insufficient_data"
        elif _NEED_SEVERITY_ORDER[after] > _NEED_SEVERITY_ORDER[before]:
            effect = "improved"
        elif _NEED_SEVERITY_ORDER[after] < _NEED_SEVERITY_ORDER[before]:
            effect = "worsened"
        else:
            effect = "unchanged"
        diffs.append({"position": pos, "before": before, "after": after, "effect": effect})
    return diffs

def _aggregate_need_overall(positional_need_changes: list) -> str:
    comparable = [e for e in positional_need_changes if e["effect"] != "insufficient_data"]
    if not comparable:
        return "unknown"
    material_improvements = [e for e in comparable if e["effect"] == "improved" and e["before"] in ("urgent", "meaningful")]
    material_worsenings = [e for e in comparable if e["effect"] == "worsened" and e["after"] in ("urgent", "meaningful")]
    has_imp, has_wor = bool(material_improvements), bool(material_worsenings)
    if has_imp and has_wor:
        return "mixed"
    if has_wor:
        return "negative"
    if has_imp:
        return "positive"
    return "neutral"

def _compare_injury_risk(players_out_resolved: list, players_in_resolved: list) -> dict:
    def effective_status(p):
        fp_status = p["_fp_intel"].get("injury_status")
        return fp_status if fp_status is not None else p.get("espn_injury_status")

    def profile(players):
        signals = [{"player": p["name"], "position": p.get("position"),
                     **fp_client.classify_injury_signal(effective_status(p))} for p in players]
        materially_reduced_count = sum(1 for s in signals if s["label"] == "materially_reduced")
        caution_count = sum(1 for s in signals if s["label"] == "caution")
        return {"materially_reduced_count": materially_reduced_count, "caution_count": caution_count}, signals

    out_profile, out_signals = profile(players_out_resolved)
    in_profile, in_signals = profile(players_in_resolved)

    unresolved = any(
        p["_fp_intel"].get("match_confidence") in ("ambiguous", "none") and p.get("espn_injury_status") is None
        for p in players_out_resolved + players_in_resolved
    )
    if unresolved:
        risk_change = "insufficient_data"
    elif in_profile["materially_reduced_count"] < out_profile["materially_reduced_count"]:
        risk_change = "risk_reduced"
    elif in_profile["materially_reduced_count"] > out_profile["materially_reduced_count"]:
        risk_change = "risk_increased"
    elif in_profile["caution_count"] < out_profile["caution_count"]:
        risk_change = "risk_reduced"
    elif in_profile["caution_count"] > out_profile["caution_count"]:
        risk_change = "risk_increased"
    else:
        risk_change = "roughly_neutral"

    return {
        "outgoing_profile": out_profile, "incoming_profile": in_profile,
        "outgoing_risks": [s for s in out_signals if s["label"] != "healthy_or_no_flag"],
        "incoming_risks": [s for s in in_signals if s["label"] != "healthy_or_no_flag"],
        "risk_change": risk_change,
    }

def _assess_bye_week_impact(before_slot_map: dict, after_slot_map: dict,
                              roster_before_by_name: dict, roster_after_by_name: dict) -> dict:
    def overlap_count(slot_map, roster_by_name):
        by_week = {}
        unknown = False
        for name in slot_map:
            p = roster_by_name.get(name)
            bye = (p or {}).get("_fp_intel", {}).get("bye_week") if p else None
            if bye is None:
                unknown = True
                continue
            by_week.setdefault(bye, []).append(name)
        affected = [{"week": wk, "players": names} for wk, names in by_week.items() if len(names) >= 2]
        return affected, unknown

    before_affected, before_unknown = overlap_count(before_slot_map, roster_before_by_name)
    after_affected, after_unknown = overlap_count(after_slot_map, roster_after_by_name)

    if before_unknown or after_unknown:
        impact = "insufficient_data"
    elif len(after_affected) < len(before_affected):
        impact = "improved"
    elif len(after_affected) > len(before_affected):
        impact = "worsened"
    else:
        impact = "unchanged"

    return {"bye_week_impact": impact, "affected_weeks_after": after_affected,
             "before_overlap_count": len(before_affected), "after_overlap_count": len(after_affected)}

def _position_quality_summary(roster_fp_rows: list, position: str) -> dict:
    relevant = [p for p in roster_fp_rows if p.get("position") == position]
    coverage_complete = all((p.get("_fp_intel") or {}).get("tier") is not None for p in relevant)
    useful_count = sum(1 for p in relevant
                        if fp_client.describe_player_quality((p.get("_fp_intel") or {}).get("tier"))
                        in ("elite", "strong", "solid_starter", "flex_caliber"))
    return {"coverage_complete": coverage_complete, "useful_count": useful_count, "player_count": len(relevant)}

def _assess_partner_fit(partner_team, players_out_resolved_bare: list, players_in_resolved_bare: list,
                          partner_snapshot_before: dict, partner_snapshot_after: dict,
                          partner_size_check: dict) -> dict:
    lineup_feasible = partner_snapshot_after["lineup_fp"]["feasible"]
    lineup_gaps_after = [g["slot"] for g in partner_snapshot_after["lineup_fp"]["gaps"]]
    core_gaps = [g for g in lineup_gaps_after if g in ("QB", "RB", "WR", "TE")]
    if not lineup_gaps_after:
        construction_severity = "none"
    elif core_gaps and partner_size_check["open_roster_spots_after"] == 0:
        construction_severity = "major"
    elif core_gaps:
        construction_severity = "moderate"
    else:
        construction_severity = "minor"
    partner_roster_construction_risk = bool(lineup_gaps_after)

    partner_positions_gained = sorted({p["position"] for p in players_out_resolved_bare})
    partner_positions_lost = sorted({p["position"] for p in players_in_resolved_bare})
    partner_starter_names_after = {p.get("name") for p in _flatten_starters(partner_snapshot_after["lineup_fp"])}
    incoming_enters_partner_lineup = any(p["name"] in partner_starter_names_after for p in players_out_resolved_bare)

    involved_positions = set(partner_positions_gained) | set(partner_positions_lost)
    quality_before = {pos: _position_quality_summary(partner_snapshot_before["roster_fp_rows"], pos) for pos in involved_positions}
    quality_after = {pos: _position_quality_summary(partner_snapshot_after["roster_fp_rows"], pos) for pos in involved_positions}

    creates_major_hole_positions, hole_claim_unknown = [], []
    for pos in partner_positions_lost:
        qb, qa = quality_before[pos], quality_after[pos]
        if qb["coverage_complete"] and qa["coverage_complete"]:
            if qb["useful_count"] >= 1 and qa["useful_count"] == 0:
                creates_major_hole_positions.append(pos)
        else:
            hole_claim_unknown.append(pos)
    creates_major_hole = bool(creates_major_hole_positions)

    thin_gains, redundant_gains, quality_unknown_gains = [], [], []
    for pos in partner_positions_gained:
        qb = quality_before[pos]
        if not qb["coverage_complete"]:
            quality_unknown_gains.append(pos)
        elif qb["useful_count"] <= 1:
            thin_gains.append(pos)
        else:
            redundant_gains.append(pos)

    quality_data_incomplete_positions = sorted(set(quality_unknown_gains) | set(hole_claim_unknown))

    notes = []
    if not partner_size_check["size_feasible"]:
        logical_fit = "poor_fit"
        notes.append(f"Partner would not fit modeled active roster capacity as proposed "
                      f"({partner_size_check['active_roster_count_after']}/{partner_size_check['active_roster_capacity']}).")
    elif construction_severity == "major":
        logical_fit = "poor_fit"
        notes.append(f"Partner's post-trade roster leaves a lineup gap at {', '.join(core_gaps)} requiring a "
                      f"follow-up roster move to fill.")
    elif quality_data_incomplete_positions:
        logical_fit = "unknown"
        notes.append(f"Partner roster-quality (FantasyPros tier) data is incomplete at "
                      f"{', '.join(quality_data_incomplete_positions)}; fit cannot be reliably assessed from tier data alone.")
    else:
        if thin_gains and not creates_major_hole:
            logical_fit = "plausible"
            notes.append(f"Partner is thin at {', '.join(thin_gains)} before this trade; the incoming asset(s) could plausibly address that.")
        elif redundant_gains and set(redundant_gains) == set(partner_positions_gained):
            logical_fit = "poor_fit"
            notes.append(f"Partner already rosters multiple useful players at {', '.join(redundant_gains)}; this trade would add redundant depth.")
        else:
            logical_fit = "unknown"
            notes.append("Partner-fit evidence is mixed or inconclusive from roster-only signals.")

    if construction_severity in ("moderate", "minor"):
        notes.append(f"Partner's post-trade lineup has a {construction_severity} gap at {', '.join(lineup_gaps_after)}, "
                      f"requiring a follow-up roster move.")

    partner_fp_reliability_warnings = sorted(set(
        w for w in [partner_snapshot_before.get("fp_reliability_warning"),
                     partner_snapshot_after.get("fp_reliability_warning")] if w))

    return {
        "partner_team_id": partner_team.team_id, "partner_team_name": partner_team.team_name,
        "partner_positions_gained": partner_positions_gained, "partner_positions_lost": partner_positions_lost,
        "incoming_enters_partner_lineup": incoming_enters_partner_lineup,
        "creates_major_positional_hole_for_partner": creates_major_hole,
        "partner_roster_legality": {
            "size_feasible": partner_size_check["size_feasible"],
            "modeled_transaction_size_feasible": partner_size_check["size_feasible"],
            "lineup_feasible": lineup_feasible, "lineup_gaps_after": lineup_gaps_after,
            "partner_roster_construction_risk": partner_roster_construction_risk,
            "partner_roster_construction_severity": construction_severity,
            "open_roster_spots_after": partner_size_check["open_roster_spots_after"],
        },
        "logical_fit": logical_fit, "notes": notes,
        "fp_reliability_warnings": partner_fp_reliability_warnings,
    }

def _lineup_category(classification: str) -> str:
    return {"major_starting_upgrade": "strong_positive", "starting_upgrade": "positive",
             "minor_starting_upgrade": "slight_positive", "no_starting_change": "neutral",
             "starting_downgrade": "negative", "insufficient_data": "unknown"}.get(classification, "unknown")

def _market_category(assessment: str) -> str:
    return {"strong_incoming_advantage": "strong_positive", "incoming_advantage": "positive",
             "roughly_even": "neutral", "outgoing_advantage": "negative",
             "strong_outgoing_advantage": "strong_negative", "insufficient_data": "unknown"}.get(assessment, "unknown")

def _apply_secondary_modifiers(base_verdict: str, depth_impact: dict, injury_risk: dict,
                                 roster_construction_severity: str, incoming_materially_reduced: bool) -> tuple:
    single_trigger = (depth_impact.get("depth_effect") == "major_depth_reduction"
                        or (injury_risk.get("risk_change") == "risk_increased" and incoming_materially_reduced)
                        or (roster_construction_severity == "major"))
    major_trigger_count = sum([
        depth_impact.get("depth_effect") == "major_depth_reduction",
        injury_risk.get("risk_change") == "risk_increased" and incoming_materially_reduced,
        roster_construction_severity == "major",
    ])
    major_trigger = major_trigger_count >= 2

    if base_verdict == "ACCEPT" and single_trigger:
        return "LEAN_ACCEPT", ["Downgraded from ACCEPT to LEAN_ACCEPT: a secondary risk factor "
                                 "(major bench-depth reduction, a materially-reduced incoming injury, "
                                 "and/or a major roster-construction gap) was present."]
    if base_verdict == "LEAN_ACCEPT" and single_trigger:
        return "FAIR", ["Downgraded from LEAN_ACCEPT to FAIR: a secondary risk factor was present."]
    if base_verdict == "FAIR" and major_trigger:
        return "LEAN_DECLINE", ["Downgraded from FAIR to LEAN_DECLINE: multiple major secondary risk "
                                  "factors were present simultaneously."]
    return base_verdict, []

def _compute_trade_verdict(lineup_impact: dict, market_value: dict, depth_impact: dict, need_overall: str,
                             injury_risk: dict, roster_legality: dict, roster_construction_severity: str,
                             incoming_materially_reduced: bool, league_relative_changes: dict, team_id_val) -> tuple:
    if not roster_legality["size_feasible"]:
        return "DECLINE", [f"Proposed package does not fit modeled active roster capacity "
                             f"({roster_legality['active_roster_count_after']}/{roster_legality['active_roster_capacity']})."]

    lineup_cat = _lineup_category(lineup_impact["classification"])
    market_cat = _market_category(market_value["assessment"])
    if lineup_cat == "unknown" and market_cat == "unknown":
        return "INSUFFICIENT_DATA", ["Both starting-lineup impact and market value are insufficient_data - "
                                       "too little reliable FantasyPros coverage to responsibly evaluate this trade."]

    need_bucket = {"positive": "positive", "negative": "negative"}.get(need_overall, "neutral")
    reasons = []
    if need_overall == "mixed":
        reasons.append("Positional needs are mixed (a genuine need-for-need tradeoff); treated as neutral for verdict purposes.")
    elif need_overall == "unknown":
        reasons.append("Positional need impact is unknown (insufficient FantasyPros coverage); treated as neutral for verdict purposes.")
    else:
        reasons.append(f"Positional need impact: {need_overall}.")

    try:
        if lineup_cat == "unknown":
            reasons.append("Starting-lineup impact is insufficient_data.")
            if market_cat in ("strong_positive", "positive"):
                base = "FAIR" if need_bucket == "negative" else "LEAN_ACCEPT"
            elif market_cat == "neutral":
                base = "FAIR" if need_bucket != "negative" else "LEAN_DECLINE"
            else:
                base = "FAIR" if need_bucket == "positive" else "LEAN_DECLINE"
            reasons.append(f"Market value: {market_value['assessment']}.")
        elif market_cat == "unknown":
            reasons.append("Market value is insufficient_data.")
            if lineup_cat in ("strong_positive", "positive"):
                base = "FAIR" if need_bucket == "negative" else "LEAN_ACCEPT"
            elif lineup_cat == "slight_positive":
                base = "LEAN_ACCEPT" if need_bucket == "positive" else "FAIR"
            elif lineup_cat == "neutral":
                base = "LEAN_DECLINE" if need_bucket == "negative" else "FAIR"
            else:
                base = "FAIR" if need_bucket == "positive" else "LEAN_DECLINE"
            reasons.append(f"Lineup impact: {lineup_impact['classification']}.")
        else:
            L = {"strong_positive": 2, "positive": 1, "slight_positive": 0.5, "neutral": 0, "negative": -1}[lineup_cat]
            M = {"strong_positive": 2, "positive": 1, "neutral": 0, "negative": -1, "strong_negative": -2}[market_cat]
            N = {"positive": 1, "neutral": 0, "negative": -1}[need_bucket]

            if L <= -1:
                if M <= -1:
                    base = "DECLINE"
                elif M >= 1:
                    base = {"positive": "FAIR", "neutral": "LEAN_DECLINE", "negative": "DECLINE"}[need_bucket]
                else:
                    base = "LEAN_DECLINE" if N >= 0 else "DECLINE"
            elif L == 0:
                if M >= 2:
                    base = "FAIR" if N == -1 else "LEAN_ACCEPT"
                elif M == 1:
                    base = {"positive": "LEAN_ACCEPT", "neutral": "FAIR", "negative": "FAIR"}[need_bucket]
                elif M == 0:
                    base = {"positive": "FAIR", "neutral": "FAIR", "negative": "LEAN_DECLINE"}[need_bucket]
                elif M == -1:
                    base = {"positive": "FAIR", "neutral": "LEAN_DECLINE", "negative": "LEAN_DECLINE"}[need_bucket]
                else:
                    base = {"positive": "LEAN_DECLINE", "neutral": "LEAN_DECLINE", "negative": "DECLINE"}[need_bucket]
            elif L == 0.5:
                if M >= 1:
                    base = "FAIR" if N == -1 else "LEAN_ACCEPT"
                elif M == 0:
                    base = {"positive": "LEAN_ACCEPT", "neutral": "FAIR", "negative": "LEAN_DECLINE"}[need_bucket]
                else:
                    base = "FAIR" if N == 1 else "LEAN_DECLINE"
            else:
                if M >= 1:
                    base = "LEAN_ACCEPT" if N == -1 else "ACCEPT"
                elif M == 0:
                    base = "FAIR" if N == -1 else ("ACCEPT" if L == 2 else "LEAN_ACCEPT")
                elif M == -1:
                    if L == 2:
                        base = "LEAN_ACCEPT" if N != -1 else "LEAN_DECLINE"
                    else:
                        base = "FAIR" if N != -1 else "LEAN_DECLINE"
                else:
                    if L == 2:
                        base = "FAIR" if N == 1 else "LEAN_DECLINE"
                    else:
                        base = "LEAN_DECLINE"
            reasons.append(f"Lineup impact: {lineup_impact['classification']}.")
            reasons.append(f"Market value: {market_value['assessment']}.")
    except KeyError:
        return "INSUFFICIENT_DATA", reasons + ["Unhandled evidence combination - defensive fallback triggered; "
                                                  "this branch should be logically unreachable."]

    final_verdict, modifier_reasons = _apply_secondary_modifiers(
        base, depth_impact, injury_risk, roster_construction_severity, incoming_materially_reduced)

    evidence_reasons = []
    if depth_impact["depth_effect"] in ("depth_reduction", "major_depth_reduction"):
        evidence_reasons.append(f"Depth tradeoff noted: {depth_impact['depth_effect']} "
                                  f"({depth_impact['lineup_relevant_bench_delta']:+.2f} pts lineup-relevant bench).")
    if injury_risk["risk_change"] == "risk_increased":
        evidence_reasons.append("Injury risk tradeoff noted: incoming side carries more injury concern than outgoing.")
    if roster_construction_severity != "none":
        evidence_reasons.append(f"Roster-construction tradeoff noted: {roster_construction_severity} "
                                  f"post-trade lineup gap requiring a follow-up move.")
    sl = league_relative_changes.get("starting_lineup", {})
    if (sl.get("before_rank") is not None and sl.get("after_rank") is not None
            and abs(sl["before_rank"] - sl["after_rank"]) >= 3):
        evidence_reasons.append(f"Notable league-relative shift: starting-lineup rank moves from "
                                  f"{sl['before_rank']} to {sl['after_rank']} among ranked teams.")

    return final_verdict, reasons + modifier_reasons + evidence_reasons

def _describe_best_worst_case(lineup_impact, market_value, positional_need_changes, depth_impact,
                                injury_risk, roster_legality) -> tuple:
    best_candidates = []
    if lineup_impact["classification"] in ("major_starting_upgrade", "starting_upgrade"):
        best_candidates.append(f"If the starting-lineup upgrade holds, the computed +{lineup_impact['projection_delta']} "
                                 f"pt season-projection gain is realized.")
    for e in positional_need_changes:
        if e["effect"] == "improved" and e["before"] in ("urgent", "meaningful"):
            best_candidates.append(f"The computed {e['position']} weakness ({e['before']} -> {e['after']}) remains addressed.")
    if depth_impact["depth_effect"] in ("depth_improvement", "major_depth_improvement"):
        best_candidates.append("The computed bench-depth improvement provides real replacement flexibility if needed.")
    if market_value["assessment"] in ("incoming_advantage", "strong_incoming_advantage"):
        best_candidates.append("The market-value edge shown is realized if the incoming asset(s) perform in line with their tier.")
    best_case = best_candidates[0] if best_candidates else "No material lineup upgrade is projected; the computed evidence resembles a lateral asset swap."

    worst_candidates = []
    if depth_impact["depth_effect"] in ("depth_reduction", "major_depth_reduction"):
        note = "Computed lineup-relevant bench depth is thinner, leaving less immediate replacement flexibility"
        note += (f", and {roster_legality['open_roster_spots_after']} open roster spot(s) remain unfilled."
                  if roster_legality.get("open_roster_spots_after", 0) > 0 else ".")
        worst_candidates.append(note)
    for e in positional_need_changes:
        if e["effect"] == "worsened" and e["after"] in ("urgent", "meaningful"):
            worst_candidates.append(f"The computed {e['position']} situation worsens ({e['before']} -> {e['after']}).")
    if injury_risk["risk_change"] == "risk_increased":
        worst_candidates.append("The incoming injury concern results in reduced availability or fantasy utility if it materializes.")
    if market_value["assessment"] in ("outgoing_advantage", "strong_outgoing_advantage"):
        worst_candidates.append("The market-value disadvantage shown compounds if the outgoing asset(s) outperform their tier.")
    worst_case = worst_candidates[0] if worst_candidates else "No material downside is evident from the computed evidence."
    return best_case, worst_case

# --- Helpers added for find_trade_targets (existing 22 tools untouched) ---

MAX_INCOMING_CANDIDATES_PER_PARTNER = 8
MAX_OUTGOING_POOL = 10
PREMIUM_CORE_OUTGOING_POOL_SIZE = 3
MAX_FULL_EVALUATIONS = 60
MAX_PREMIUM_FULL_EVALUATIONS = 8
MAX_FULL_EVALUATIONS_PER_PARTNER = 20
GUARANTEED_STAGE_B_PER_PARTNER = 3

_FTT_QUALITY_ORDER = {"elite": 0, "strong": 1, "solid_starter": 2, "flex_caliber": 3, "depth": 4,
                       "speculative": 5, "deep_speculative": 6, "unranked_deep_bench": 7, "unknown": 8}
_FTT_OUTGOING_QUALITY_ORDER = {"unranked_deep_bench": 0, "deep_speculative": 1, "speculative": 2, "depth": 3,
                                "flex_caliber": 4, "solid_starter": 5, "strong": 6, "elite": 7, "unknown": 8}
_FTT_TIER3_SAFETY_VALVE_QUALITIES = ("elite", "strong", "solid_starter")

_FTT_VERDICT_ORDER = {"ACCEPT": 0, "LEAN_ACCEPT": 1, "FAIR": 2, "LEAN_DECLINE": 3, "INSUFFICIENT_DATA": 4, "DECLINE": 5}
_FTT_NEED_EFFECT_ORDER = {"positive": 0, "mixed": 1, "neutral": 1, "unknown": 2, "negative": 3}
_FTT_LINEUP_ORDER = {"major_starting_upgrade": 0, "starting_upgrade": 1, "minor_starting_upgrade": 2,
                       "no_starting_change": 3, "starting_downgrade": 4, "insufficient_data": 5}
_FTT_MARKET_ORDER = {"strong_incoming_advantage": 0, "incoming_advantage": 1, "roughly_even": 2,
                       "outgoing_advantage": 3, "strong_outgoing_advantage": 4, "insufficient_data": 5}
_FTT_PARTNER_FIT_ORDER = {"plausible": 0, "unknown": 1, "poor_fit": 2}
_FTT_DEPTH_ORDER = {"major_depth_improvement": 0, "depth_improvement": 1, "neutral": 2,
                      "depth_reduction": 3, "major_depth_reduction": 4, "insufficient_data": 5}
_FTT_INJURY_ORDER = {"risk_reduced": 0, "roughly_neutral": 1, "risk_increased": 2, "insufficient_data": 3}

_FTT_LEAGUE_RELATIVE_METRIC_KEYS = (
    ["core_offense", "bench_depth"]
    + [f"starter_{pos}" for pos in ("QB", "RB", "WR", "TE")]
    + [f"depth_{pos}" for pos in ("QB", "RB", "WR", "TE")]
    + ["starter_FLEX"]
)

def _ftt_validate_position_filter(position):
    if position is None:
        return None
    if position.upper() not in ("QB", "RB", "WR", "TE"):
        return "position must be one of QB, RB, WR, TE, or omitted."
    return None

def _ftt_position_has_flex_exposure(position, slot_counts):
    return _position_has_flex_exposure(position, slot_counts)

def _ftt_enters_lineup_on_add(candidate_player, target_roster_fp_rows, slot_counts):
    """Cheap add-only lineup test: insert the candidate's already-enriched
    row into the target's CURRENT roster_fp_rows and re-run the frozen
    lineup engine once. Returns (enters_lineup: bool, displaces_direct_starter: bool)."""
    simulated = target_roster_fp_rows + [candidate_player]
    add_only_lineup = _assign_best_lineup(simulated, slot_counts, value_field="_fp_eval_value")
    starter_names = {p.get("name") for p in _flatten_starters(add_only_lineup)}
    enters_lineup = candidate_player["name"] in starter_names
    position = candidate_player.get("position")
    direct_names = {p.get("name") for p in add_only_lineup.get("starters", {}).get(position, [])}
    displaces_direct_starter = candidate_player["name"] in direct_names
    return enters_lineup, displaces_direct_starter

def _ftt_compute_incoming_candidate_facts(candidate_player, owning_team_id, baseline):
    """Returns the exact facts dict shape required. Zero FP calls -
    candidate_player already carries _fp_intel/_fp_eval_value from
    baseline enrichment."""
    intel = candidate_player.get("_fp_intel") or {}
    position = candidate_player.get("position")
    has_flex_exposure = _ftt_position_has_flex_exposure(position, baseline["slot_counts"])
    need_entry = None
    if position in ("QB", "RB", "WR", "TE"):
        need_entry = next((n for n in baseline["target_positional_needs"] if n["position"] == position), None)
    need_severity = need_entry["severity"] if need_entry else "none"

    enters_target_lineup_on_add, displaces_direct_starter = _ftt_enters_lineup_on_add(
        candidate_player, baseline["target_roster_fp_rows"], baseline["slot_counts"])

    quality = fp_client.describe_player_quality(intel.get("tier"))
    depth_label = baseline["target_position_analysis"].get(position, {}).get("depth_label", "unavailable")

    facts = {
        "position": position, "need_severity": need_severity, "has_flex_exposure": has_flex_exposure,
        "enters_target_lineup_on_add": enters_target_lineup_on_add,
        "displaces_direct_starter": displaces_direct_starter,
        "quality": quality, "tier": intel.get("tier"), "ecr": intel.get("ecr"),
        "depth_label": depth_label, "owning_team_id": owning_team_id,
    }
    facts["tier_assigned"] = _ftt_tier_for_incoming_candidate(facts)
    return facts

def _ftt_tier_for_incoming_candidate(facts):
    """Returns 1, 2, 3, or None. Single-QB/no-OP special case gates
    Tier 1/2 independently; all other positions/formats only require
    enters_target_lineup_on_add for the immediate-upgrade branch."""
    position = facts["position"]
    is_single_qb_no_flex = (position == "QB" and not facts["has_flex_exposure"])

    if is_single_qb_no_flex:
        immediate_upgrade_qualifies = facts["displaces_direct_starter"]
    else:
        immediate_upgrade_qualifies = facts["enters_target_lineup_on_add"]

    if facts["need_severity"] in ("urgent", "meaningful") and immediate_upgrade_qualifies:
        return 1

    if facts["need_severity"] == "minor" and immediate_upgrade_qualifies:
        return 2
    if (facts["need_severity"] in ("urgent", "meaningful") and facts["has_flex_exposure"]
            and not facts["enters_target_lineup_on_add"]
            and facts["depth_label"] in ("weak", "below_average")
            and facts["quality"] in ("elite", "strong", "solid_starter", "flex_caliber")):
        return 2

    if facts["need_severity"] in ("none", "unknown") and facts["enters_target_lineup_on_add"]:
        return 3
    if facts["quality"] in _FTT_TIER3_SAFETY_VALVE_QUALITIES:
        return 3

    return None

def _ftt_incoming_sort_key(candidate_record):
    """Accepts the ENTIRE candidate record (candidate_player + facts),
    not just facts, per the approved consistent shape."""
    facts = candidate_record["facts"]
    tier_assigned = facts["tier_assigned"] if facts["tier_assigned"] is not None else 99
    return (
        tier_assigned,
        0 if facts["enters_target_lineup_on_add"] else 1,
        _FTT_QUALITY_ORDER.get(facts["quality"], _FTT_QUALITY_ORDER["unknown"]),
        facts["tier"] if facts["tier"] is not None else 99,
        facts["ecr"] if facts["ecr"] is not None else 9999,
        candidate_record["candidate_player"]["name"],
    )

def _ftt_generate_incoming_candidates(baseline, position_filter):
    """Builds {partner_team_id: [candidate_record, ...]} - bounded to
    MAX_INCOMING_CANDIDATES_PER_PARTNER per partner, sorted by
    _ftt_incoming_sort_key BEFORE slicing (so the slice keeps the
    genuinely best candidates, never an arbitrary subset)."""
    candidates_by_partner = {}
    scan_team_ids = baseline["scan_team_ids"]
    for owning_team_id in scan_team_ids:
        if owning_team_id == baseline["team_id_val"]:
            continue
        owning_roster = baseline["snapshots"][owning_team_id]["roster_fp_rows"]
        records = []
        for p in owning_roster:
            pos = p.get("position")
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            if position_filter and pos != position_filter:
                continue
            facts = _ftt_compute_incoming_candidate_facts(p, owning_team_id, baseline)
            if facts["tier_assigned"] is None:
                continue
            records.append({"candidate_player": p, "facts": facts})
        records.sort(key=_ftt_incoming_sort_key)
        candidates_by_partner[owning_team_id] = records[:MAX_INCOMING_CANDIDATES_PER_PARTNER]
    return candidates_by_partner

def _ftt_outgoing_category(player, baseline):
    name = player["name"]
    if name in baseline["trade_surplus_player_names"]:
        return 1
    if name in baseline["expendable_player_names"]:
        return 2
    if name in baseline["core_asset_names"]:
        return 5
    quality = fp_client.describe_player_quality((player.get("_fp_intel") or {}).get("tier"))
    if quality in ("elite", "strong", "solid_starter", "flex_caliber"):
        return 3
    pos = player.get("position")
    pa = baseline["target_position_analysis"].get(pos, {})
    if pa.get("relative_label") in ("strong", "above_average") and pa.get("depth_label") in ("strong", "above_average"):
        return 4
    return None

def _ftt_removal_feasible(player, baseline):
    rows = baseline["target_roster_fp_rows"]
    idx = next(i for i, p in enumerate(rows) if p["name"] == player["name"])
    sim = rows[:idx] + rows[idx + 1:]
    return _assign_best_lineup(sim, baseline["slot_counts"], value_field="_fp_eval_value")["feasible"]

def _ftt_outgoing_sort_key(player, category, baseline):
    starter_names = {p.get("name") for p in _flatten_starters(baseline["target_snapshot"]["lineup_fp"])}
    outside_rotation = 0 if player["name"] not in starter_names else 1
    removal_feasible = 0 if _ftt_removal_feasible(player, baseline) else 1
    quality = fp_client.describe_player_quality((player.get("_fp_intel") or {}).get("tier"))
    outgoing_quality_rank = _FTT_OUTGOING_QUALITY_ORDER.get(quality, _FTT_OUTGOING_QUALITY_ORDER["unknown"])
    return (category, outside_rotation, removal_feasible, outgoing_quality_rank, player["name"])

def _ftt_generate_outgoing_pool(baseline):
    pool = []
    for p in baseline["target_roster_fp_rows"]:
        cat = _ftt_outgoing_category(p, baseline)
        if cat is None or cat == 5:
            continue
        pool.append((cat, p))
    pool.sort(key=lambda cp: _ftt_outgoing_sort_key(cp[1], cp[0], baseline))
    return [p for _, p in pool[:MAX_OUTGOING_POOL]]

def _ftt_generate_premium_core_outgoing_pool(baseline):
    core_players = [p for p in baseline["target_roster_fp_rows"] if p["name"] in baseline["core_asset_names"]]
    core_players.sort(key=lambda p: (
        _FTT_OUTGOING_QUALITY_ORDER.get(
            fp_client.describe_player_quality((p.get("_fp_intel") or {}).get("tier")),
            _FTT_OUTGOING_QUALITY_ORDER["unknown"]),
        0 if _ftt_removal_feasible(p, baseline) else 1,
        p["name"],
    ))
    return core_players[:PREMIUM_CORE_OUTGOING_POOL_SIZE]

def _ftt_canonical_package_key(players_out, players_in, partner_team_id):
    return (partner_team_id, frozenset(p["name"] for p in players_out), frozenset(p["name"] for p in players_in))

class _FttPackage:
    __slots__ = ("players_out", "players_in", "incoming_candidates", "partner_team_id", "is_premium")
    def __init__(self, players_out, players_in, incoming_candidates, partner_team_id, is_premium=False):
        self.players_out = players_out
        self.players_in = players_in
        self.incoming_candidates = incoming_candidates
        self.partner_team_id = partner_team_id
        self.is_premium = is_premium

def _ftt_generate_routine_packages(outgoing_pool, incoming_candidates_by_partner, max_package_size, baseline):
    seen = set()
    packages = []
    for partner_id, incoming_records in incoming_candidates_by_partner.items():
        if not incoming_records:
            continue
        for out_player in outgoing_pool:
            for rec in incoming_records:
                po, pi = [out_player], [rec["candidate_player"]]
                key = _ftt_canonical_package_key(po, pi, partner_id)
                if key in seen:
                    continue
                seen.add(key)
                packages.append(_FttPackage(po, pi, [rec], partner_id))
        if max_package_size >= 2:
            from itertools import combinations
            for out_pair in combinations(outgoing_pool[:6], 2):
                for rec in incoming_records[:5]:
                    po, pi = list(out_pair), [rec["candidate_player"]]
                    key = _ftt_canonical_package_key(po, pi, partner_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    packages.append(_FttPackage(po, pi, [rec], partner_id))
            for out_player in outgoing_pool[:6]:
                for in_pair in combinations(incoming_records[:5], 2):
                    po, pi = [out_player], [c["candidate_player"] for c in in_pair]
                    key = _ftt_canonical_package_key(po, pi, partner_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    packages.append(_FttPackage(po, pi, list(in_pair), partner_id))
            for out_pair in combinations(outgoing_pool[:5], 2):
                for in_pair in combinations(incoming_records[:5], 2):
                    po, pi = list(out_pair), [c["candidate_player"] for c in in_pair]
                    key = _ftt_canonical_package_key(po, pi, partner_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    packages.append(_FttPackage(po, pi, list(in_pair), partner_id))
        if max_package_size >= 3:
            from itertools import combinations
            for out_triple in combinations(outgoing_pool[:4], 3):
                for rec in incoming_records[:3]:
                    po, pi = list(out_triple), [rec["candidate_player"]]
                    key = _ftt_canonical_package_key(po, pi, partner_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    packages.append(_FttPackage(po, pi, [rec], partner_id))
            for out_player in outgoing_pool[:4]:
                for in_triple in combinations(incoming_records[:3], 3):
                    po, pi = [out_player], [c["candidate_player"] for c in in_triple]
                    key = _ftt_canonical_package_key(po, pi, partner_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    packages.append(_FttPackage(po, pi, list(in_triple), partner_id))
    return packages

def _ftt_generate_premium_packages(premium_core_outgoing_pool, incoming_candidates_by_partner, max_package_size, baseline):
    from itertools import combinations
    seen = set()
    packages = []
    for partner_id, incoming_records in incoming_candidates_by_partner.items():
        premium_incoming = [r for r in incoming_records
                              if r["facts"]["need_severity"] in ("urgent", "meaningful")
                              or r["facts"]["quality"] in ("elite", "strong")]
        if not premium_incoming:
            continue
        for core_player in premium_core_outgoing_pool:
            for rec in premium_incoming[:5]:
                po, pi = [core_player], [rec["candidate_player"]]
                key = _ftt_canonical_package_key(po, pi, partner_id)
                if key in seen:
                    continue
                seen.add(key)
                packages.append(_FttPackage(po, pi, [rec], partner_id, is_premium=True))
            if max_package_size >= 2:
                for in_pair in combinations(premium_incoming[:4], 2):
                    po, pi = [core_player], [c["candidate_player"] for c in in_pair]
                    key = _ftt_canonical_package_key(po, pi, partner_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    packages.append(_FttPackage(po, pi, list(in_pair), partner_id, is_premium=True))
    return packages

def _build_simulated_snapshot_from_enriched_rows(enriched_rows_before: list, outgoing_names: set,
                                                   incoming_rows: list, slot_counts: dict,
                                                   team_id, team_name) -> dict:
    """Fast simulator - never calls build_player_intelligence. Reuses
    frozen _build_fp_eval_roster and _assign_best_lineup directly, so
    _fp_eval_value derivation and the reliability-warning threshold/
    wording are never independently duplicated."""
    remaining = [dict(p) for p in enriched_rows_before if p["name"] not in outgoing_names]
    incoming = [dict(p, lineup_slot=None) for p in incoming_rows]
    roster = remaining + incoming
    roster_fp_rows, fp_reliability_warning = _build_fp_eval_roster(roster)
    lineup_espn = _assign_best_lineup(roster, slot_counts, value_field="projected_points")
    lineup_fp = _assign_best_lineup(roster_fp_rows, slot_counts, value_field="_fp_eval_value")
    return {"team_id": team_id, "team_name": team_name, "roster": roster,
             "roster_fp_rows": roster_fp_rows, "lineup_espn": lineup_espn, "lineup_fp": lineup_fp,
             "fp_reliability_warning": fp_reliability_warning}

def _ftt_stage_a_structural_check(package, baseline):
    target_rows_before = baseline["snapshots"][baseline["team_id_val"]]["roster_fp_rows"]
    target_rows_after = _simulate_trade_roster(target_rows_before, package.players_out, package.players_in)
    target_size_check = _check_roster_size_limit(
        target_rows_after, target_rows_before, {p["name"] for p in package.players_out}, baseline["slot_counts"])

    partner_rows_before = baseline["snapshots"][package.partner_team_id]["roster_fp_rows"]
    partner_rows_after = _simulate_trade_roster(partner_rows_before, package.players_in, package.players_out)
    partner_size_check = _check_roster_size_limit(
        partner_rows_after, partner_rows_before, {p["name"] for p in package.players_in}, baseline["slot_counts"])

    return target_size_check, partner_size_check

def _ftt_has_no_market_signal(intel):
    return (intel.get("projected_points") is None and intel.get("ecr") is None
            and intel.get("tier") is None and intel.get("adp") is None)

def _ftt_prefilter_package(package, baseline):
    """Returns (passes: bool, reason: str). Exactly three hard-rejection
    gates. No opaque 'looks expensive' gate exists anywhere here."""
    target_size_check, partner_size_check = _ftt_stage_a_structural_check(package, baseline)
    if not target_size_check["size_feasible"]:
        return False, "target_size_overflow"
    if not partner_size_check["size_feasible"]:
        return False, "partner_size_overflow"

    all_incoming_useless = all(
        _ftt_has_no_market_signal(c["candidate_player"].get("_fp_intel") or {})
        and c["facts"].get("tier_assigned") is None
        for c in package.incoming_candidates
    )
    if all_incoming_useless:
        return False, "no_usable_incoming_evidence"

    return True, "passed_prefilter"

def _ftt_cheap_partner_fit_hint(package, baseline):
    """Cheap Stage-A-only hint using _position_quality_summary (frozen) -
    never builds an optimized lineup. Purely for ORDERING, never a gate."""
    partner_snapshot = baseline["snapshots"][package.partner_team_id]
    positions_gained = sorted({p["position"] for p in package.players_out})
    thin = []
    redundant = []
    for pos in positions_gained:
        q = _position_quality_summary(partner_snapshot["roster_fp_rows"], pos)
        if not q["coverage_complete"]:
            continue
        if q["useful_count"] <= 1:
            thin.append(pos)
        else:
            redundant.append(pos)
    if thin:
        return "plausible"
    if redundant and set(redundant) == set(positions_gained):
        return "poor_fit"
    return "unknown"

def _ftt_prefilter_sort_key(package, baseline):
    in_facts = [c["facts"] for c in package.incoming_candidates]
    out_categories = [_ftt_outgoing_category(p, baseline) for p in package.players_out]
    out_categories = [c if c is not None else 5 for c in out_categories]

    best_incoming_tier_assigned = min(f["tier_assigned"] if f["tier_assigned"] is not None else 99 for f in in_facts)
    any_enters_lineup = any(f["enters_target_lineup_on_add"] for f in in_facts)
    best_incoming_quality = min(_FTT_QUALITY_ORDER.get(f["quality"], _FTT_QUALITY_ORDER["unknown"]) for f in in_facts)
    outgoing_category_profile = tuple(sorted(out_categories, reverse=True))
    partner_fit_rank = _FTT_PARTNER_FIT_ORDER[_ftt_cheap_partner_fit_hint(package, baseline)]
    package_simplicity = len(package.players_out) + len(package.players_in)

    return (
        best_incoming_tier_assigned,
        0 if any_enters_lineup else 1,
        best_incoming_quality,
        outgoing_category_profile,
        partner_fit_rank,
        package_simplicity,
        package.partner_team_id,
        tuple(sorted(p["name"] for p in package.players_out)) + tuple(sorted(p["name"] for p in package.players_in)),
    )

def _ftt_allocate_stage_b_budget(prefiltered_survivors_by_partner: dict, baseline, restricted_to_single_partner: bool,
                                   max_full_evaluations=MAX_FULL_EVALUATIONS,
                                   max_premium_full_evaluations=MAX_PREMIUM_FULL_EVALUATIONS,
                                   max_per_partner=MAX_FULL_EVALUATIONS_PER_PARTNER,
                                   guaranteed_per_partner=GUARANTEED_STAGE_B_PER_PARTNER):
    def _sort(survivors):
        return sorted(survivors, key=lambda p: _ftt_prefilter_sort_key(p, baseline))

    total_prefilter_survivors = sum(len(v) for v in prefiltered_survivors_by_partner.values())

    if restricted_to_single_partner:
        only_partner = next(iter(prefiltered_survivors_by_partner))
        survivors_sorted = _sort(prefiltered_survivors_by_partner[only_partner])
        premium_sorted = [p for p in survivors_sorted if p.is_premium]
        routine_sorted = [p for p in survivors_sorted if not p.is_premium]
        premium_selected = premium_sorted[:min(max_premium_full_evaluations, max_per_partner)]
        remaining_partner_budget = max(0, min(max_per_partner, max_full_evaluations) - len(premium_selected))
        routine_selected = routine_sorted[:remaining_partner_budget]
        selected = (premium_selected + routine_selected)[:max_full_evaluations]
        search_truncated = total_prefilter_survivors > len(selected)
        counts = {"total_prefilter_survivors": total_prefilter_survivors,
                   "selected_for_full_evaluation": len(selected),
                   "survivors_not_evaluated_due_to_budget": total_prefilter_survivors - len(selected)}
        return selected, search_truncated, counts

    selected = []
    per_partner_used = {}
    remaining_by_partner_routine = {}

    all_premium = []
    for partner_id, survivors in prefiltered_survivors_by_partner.items():
        for pkg in survivors:
            if pkg.is_premium:
                all_premium.append(pkg)
    all_premium_sorted = sorted(all_premium, key=lambda p: _ftt_prefilter_sort_key(p, baseline))
    premium_selected_count = 0
    for pkg in all_premium_sorted:
        if premium_selected_count >= max_premium_full_evaluations:
            break
        if per_partner_used.get(pkg.partner_team_id, 0) >= max_per_partner:
            continue
        selected.append(pkg)
        premium_selected_count += 1
        per_partner_used[pkg.partner_team_id] = per_partner_used.get(pkg.partner_team_id, 0) + 1

    for partner_id, survivors in prefiltered_survivors_by_partner.items():
        routine_for_partner = _sort([p for p in survivors if not p.is_premium])
        remaining_by_partner_routine[partner_id] = routine_for_partner

    for partner_id in sorted(remaining_by_partner_routine.keys()):
        routine_survivors = remaining_by_partner_routine[partner_id]
        already_used = per_partner_used.get(partner_id, 0)
        remaining_partner_cap = max(0, max_per_partner - already_used)
        take_n = min(guaranteed_per_partner, remaining_partner_cap)
        take = routine_survivors[:take_n]
        selected.extend(take)
        per_partner_used[partner_id] = already_used + len(take)
        remaining_by_partner_routine[partner_id] = routine_survivors[len(take):]

    remaining_budget = max_full_evaluations - len(selected)
    all_remaining = [p for plist in remaining_by_partner_routine.values() for p in plist]
    all_remaining_sorted = sorted(all_remaining, key=lambda p: _ftt_prefilter_sort_key(p, baseline))
    for pkg in all_remaining_sorted:
        if remaining_budget <= 0:
            break
        if per_partner_used.get(pkg.partner_team_id, 0) >= max_per_partner:
            continue
        selected.append(pkg)
        remaining_budget -= 1
        per_partner_used[pkg.partner_team_id] = per_partner_used.get(pkg.partner_team_id, 0) + 1

    search_truncated = total_prefilter_survivors > len(selected)
    counts = {"total_prefilter_survivors": total_prefilter_survivors,
               "selected_for_full_evaluation": len(selected),
               "survivors_not_evaluated_due_to_budget": total_prefilter_survivors - len(selected)}
    return selected, search_truncated, counts

def _ftt_build_baseline_context(league, team_id_val, partner_team_id, slot_counts, scoring_bucket):
    """CRITICAL: snapshots/metric_values/league-relative ranking ALWAYS
    cover the FULL league, regardless of partner_team_id. This is
    required for exact parity with evaluate_trade, which always ranks
    across every real team. partner_team_id restricts ONLY which
    partner(s) incoming/outgoing candidate generation considers -
    never the ranking/enrichment scope. This also satisfies the
    one-ESPN-fetch / one-enrichment-pass requirement unconditionally."""
    all_team_ids = sorted(t.team_id for t in league.teams)
    candidate_partner_ids = ([partner_team_id] if partner_team_id is not None
                               else [tid for tid in all_team_ids if tid != team_id_val])

    snapshots = {t.team_id: _build_team_snapshot(t, slot_counts, scoring_bucket) for t in league.teams}
    target_snapshot = snapshots[team_id_val]
    target_roster_fp_rows = target_snapshot["roster_fp_rows"]

    def core_offense_value(snap):
        m = _core_offense_projection(snap["lineup_fp"], snap["roster_fp_rows"], slot_counts)
        return m["known_projection_total"] if m["coverage_complete"] else None

    def bench_value(snap):
        m = _bench_depth_metrics(snap["lineup_fp"], slot_counts)
        return m["lineup_relevant_bench_projection_total"] if m["coverage_complete"] else None

    def position_starter_value(pos, snap):
        m = _analyze_position_strength(pos, snap, slot_counts)
        return m["starter_projection_total_known"] if m["starter_coverage_complete"] else None

    def position_depth_value(pos, snap):
        m = _analyze_position_strength(pos, snap, slot_counts)
        return m["bench_projection_total_known"] if all(b["fp_projection"] is not None for b in m["bench_depth"]) else None

    metric_values = {}
    metric_values["core_offense"] = {tid: core_offense_value(s) for tid, s in snapshots.items()}
    metric_values["bench_depth"] = {tid: bench_value(s) for tid, s in snapshots.items()}
    for pos in ("QB", "RB", "WR", "TE"):
        metric_values[f"starter_{pos}"] = {tid: position_starter_value(pos, s) for tid, s in snapshots.items()}
        metric_values[f"depth_{pos}"] = {tid: position_depth_value(pos, s) for tid, s in snapshots.items()}
    metric_values["starter_FLEX"] = {tid: position_starter_value("FLEX", s) for tid, s in snapshots.items()}

    target_position_analysis = {}
    for pos in ("QB", "RB", "WR", "TE", "FLEX"):
        starter_key = "starter_FLEX" if pos == "FLEX" else f"starter_{pos}"
        starter_rank = _rank_across_league(metric_values[starter_key], True)
        pa = _analyze_position_strength(pos, target_snapshot, slot_counts)
        rank_info = starter_rank["per_team"][team_id_val]
        pa["league_rank"] = rank_info["rank"]
        pa["ranked_team_count"] = starter_rank["ranked_team_count"]
        pa["relative_label"] = _relative_label(rank_info["rank"], starter_rank["ranked_team_count"])
        if pos != "FLEX":
            depth_rank = _rank_across_league(metric_values[f"depth_{pos}"], True)
            depth_info = depth_rank["per_team"][team_id_val]
            pa["depth_league_rank"] = depth_info["rank"]
            pa["depth_ranked_team_count"] = depth_rank["ranked_team_count"]
            pa["depth_label"] = _relative_label(depth_info["rank"], depth_rank["ranked_team_count"])
        else:
            pa["depth_label"] = "unavailable"
        target_position_analysis[pos] = pa

    target_positional_needs = _identify_positional_needs(target_position_analysis, target_snapshot, slot_counts)
    core_asset_list = _identify_core_assets(target_snapshot, slot_counts)
    core_asset_names = {c["player"] for c in core_asset_list}
    expendable_list = _identify_expendable_assets(target_snapshot, slot_counts, core_asset_names)
    expendable_player_names = {e["player"] for e in expendable_list}
    trade_surplus_list = _identify_trade_surplus(target_snapshot, target_position_analysis, slot_counts)
    trade_surplus_player_names = {c["player"] for s in trade_surplus_list for c in s["surplus_candidates"]}

    return {
        "team_id_val": team_id_val, "partner_team_id_restriction": partner_team_id,
        "all_team_ids": all_team_ids, "scan_team_ids": candidate_partner_ids,
        "slot_counts": slot_counts, "scoring_bucket": scoring_bucket,
        "snapshots": snapshots, "target_snapshot": target_snapshot,
        "target_roster_fp_rows": target_roster_fp_rows,
        "metric_values": metric_values,
        "target_position_analysis": target_position_analysis,
        "target_positional_needs": target_positional_needs,
        "core_asset_list": core_asset_list, "core_asset_names": core_asset_names,
        "expendable_list": expendable_list, "expendable_player_names": expendable_player_names,
        "trade_surplus_list": trade_surplus_list, "trade_surplus_player_names": trade_surplus_player_names,
    }

from collections import namedtuple as _ftt_namedtuple
_FttTeamRef = _ftt_namedtuple("_FttTeamRef", ["team_id", "team_name"])

def _ftt_league_relative(metric_key, metric_fn, baseline, target_snapshot_after, partner_snapshot_after, partner_team_id):
    static_values = dict(baseline["metric_values"][metric_key])
    team_id_val = baseline["team_id_val"]
    values_before = dict(static_values)
    values_before[partner_team_id] = metric_fn(baseline["snapshots"][partner_team_id])
    values_before[team_id_val] = metric_fn(baseline["snapshots"][team_id_val])
    values_after = dict(static_values)
    values_after[partner_team_id] = metric_fn(partner_snapshot_after)
    values_after[team_id_val] = metric_fn(target_snapshot_after)
    return _rank_across_league(values_before, higher_is_better=True), _rank_across_league(values_after, higher_is_better=True)

def _ftt_pack_relative(rank_before, rank_after, tid):
    return {"before_rank": rank_before["per_team"][tid]["rank"], "after_rank": rank_after["per_team"][tid]["rank"],
             "before_ranked_team_count": rank_before["ranked_team_count"], "after_ranked_team_count": rank_after["ranked_team_count"],
             "before_coverage_pct": rank_before["coverage_pct"], "after_coverage_pct": rank_after["coverage_pct"]}

def _ftt_core_offense_value(snap, slot_counts):
    m = _core_offense_projection(snap["lineup_fp"], snap["roster_fp_rows"], slot_counts)
    return m["known_projection_total"] if m["coverage_complete"] else None

def _ftt_bench_value(snap, slot_counts):
    m = _bench_depth_metrics(snap["lineup_fp"], slot_counts)
    return m["lineup_relevant_bench_projection_total"] if m["coverage_complete"] else None

def _ftt_position_starter_value(pos, snap, slot_counts):
    m = _analyze_position_strength(pos, snap, slot_counts)
    return m["starter_projection_total_known"] if m["starter_coverage_complete"] else None

def _ftt_position_depth_value(pos, snap, slot_counts):
    m = _analyze_position_strength(pos, snap, slot_counts)
    return m["bench_projection_total_known"] if all(b["fp_projection"] is not None for b in m["bench_depth"]) else None

def _ftt_evaluate_package_full(package, baseline):
    """Mirrors evaluate_trade's exact internal sequence. Every evidence
    call below is the frozen function, called with the same argument
    shapes evaluate_trade uses. The ONLY substitution is how the two
    participating AFTER snapshots are built (fast simulator vs. full
    _build_snapshot_from_roster)."""
    team_id_val = baseline["team_id_val"]
    partner_team_id = package.partner_team_id
    slot_counts = baseline["slot_counts"]

    out_names = {p["name"] for p in package.players_out}
    in_names = {p["name"] for p in package.players_in}

    target_rows_before = baseline["snapshots"][team_id_val]["roster_fp_rows"]
    target_rows_after = _simulate_trade_roster(target_rows_before, package.players_out, package.players_in)
    roster_size_check = _check_roster_size_limit(target_rows_after, target_rows_before, out_names, slot_counts)

    partner_rows_before = baseline["snapshots"][partner_team_id]["roster_fp_rows"]
    partner_rows_after = _simulate_trade_roster(partner_rows_before, package.players_in, package.players_out)
    partner_size_check = _check_roster_size_limit(partner_rows_after, partner_rows_before, in_names, slot_counts)

    target_snapshot_after = _build_simulated_snapshot_from_enriched_rows(
        target_rows_before, out_names, package.players_in, slot_counts, team_id_val, baseline["target_snapshot"]["team_name"])
    partner_team_name = baseline["snapshots"][partner_team_id]["team_name"]
    partner_snapshot_after = _build_simulated_snapshot_from_enriched_rows(
        partner_rows_before, in_names, package.players_out, slot_counts, partner_team_id, partner_team_name)

    lineup_feasible = target_snapshot_after["lineup_fp"]["feasible"]
    lineup_gaps_after = [g["slot"] for g in target_snapshot_after["lineup_fp"]["gaps"]]
    core_gaps = [g for g in lineup_gaps_after if g in ("QB", "RB", "WR", "TE")]
    if not lineup_gaps_after:
        roster_construction_severity = "none"
    elif core_gaps and roster_size_check["open_roster_spots_after"] == 0:
        roster_construction_severity = "major"
    elif core_gaps:
        roster_construction_severity = "moderate"
    else:
        roster_construction_severity = "minor"

    roster_legality = {
        **roster_size_check, "lineup_feasible": lineup_feasible, "lineup_gaps_after": lineup_gaps_after,
        "roster_construction_risk": bool(lineup_gaps_after), "roster_construction_severity": roster_construction_severity,
    }

    target_snapshot_before = baseline["snapshots"][team_id_val]
    partner_snapshot_before = baseline["snapshots"][partner_team_id]
    before_by_name = {p["name"]: p for p in target_snapshot_before["roster_fp_rows"]}
    after_by_name = {p["name"]: p for p in target_snapshot_after["roster_fp_rows"]}
    players_out_resolved = [before_by_name[p["name"]] for p in package.players_out]
    players_in_resolved = [after_by_name[p["name"]] for p in package.players_in]

    core_offense_before_metric = _core_offense_projection(target_snapshot_before["lineup_fp"], target_snapshot_before["roster_fp_rows"], slot_counts)
    core_offense_after_metric = _core_offense_projection(target_snapshot_after["lineup_fp"], target_snapshot_after["roster_fp_rows"], slot_counts)

    market_value = _compare_market_value(players_out_resolved, players_in_resolved)
    lineup_impact = _compare_lineups(target_snapshot_before["lineup_fp"], target_snapshot_after["lineup_fp"],
                                       core_offense_before_metric, core_offense_after_metric)
    depth_impact = _compare_depth(target_snapshot_before, target_snapshot_after, slot_counts, roster_size_check)

    core_rank_before, core_rank_after = _ftt_league_relative(
        "core_offense", lambda s: _ftt_core_offense_value(s, slot_counts), baseline, target_snapshot_after, partner_snapshot_after, partner_team_id)
    bench_rank_before, bench_rank_after = _ftt_league_relative(
        "bench_depth", lambda s: _ftt_bench_value(s, slot_counts), baseline, target_snapshot_after, partner_snapshot_after, partner_team_id)
    league_relative_changes = {
        "starting_lineup": _ftt_pack_relative(core_rank_before, core_rank_after, team_id_val),
        "bench_depth": _ftt_pack_relative(bench_rank_before, bench_rank_after, team_id_val),
    }

    position_analysis_before, position_analysis_after = {}, {}
    for pos in ("QB", "RB", "WR", "TE"):
        starter_rank_before, starter_rank_after = _ftt_league_relative(
            f"starter_{pos}", lambda s, pos=pos: _ftt_position_starter_value(pos, s, slot_counts),
            baseline, target_snapshot_after, partner_snapshot_after, partner_team_id)
        depth_rank_before, depth_rank_after = _ftt_league_relative(
            f"depth_{pos}", lambda s, pos=pos: _ftt_position_depth_value(pos, s, slot_counts),
            baseline, target_snapshot_after, partner_snapshot_after, partner_team_id)
        league_relative_changes[pos] = {
            **_ftt_pack_relative(starter_rank_before, starter_rank_after, team_id_val),
            "depth_before_rank": depth_rank_before["per_team"][team_id_val]["rank"],
            "depth_after_rank": depth_rank_after["per_team"][team_id_val]["rank"],
            "depth_before_ranked_team_count": depth_rank_before["ranked_team_count"],
            "depth_after_ranked_team_count": depth_rank_after["ranked_team_count"],
        }
        pa_before = _analyze_position_strength(pos, target_snapshot_before, slot_counts)
        pa_after = _analyze_position_strength(pos, target_snapshot_after, slot_counts)
        pa_before["league_rank"] = starter_rank_before["per_team"][team_id_val]["rank"]
        pa_before["ranked_team_count"] = starter_rank_before["ranked_team_count"]
        pa_before["relative_label"] = _relative_label(pa_before["league_rank"], pa_before["ranked_team_count"])
        pa_before["depth_league_rank"] = depth_rank_before["per_team"][team_id_val]["rank"]
        pa_before["depth_ranked_team_count"] = depth_rank_before["ranked_team_count"]
        pa_before["depth_label"] = _relative_label(pa_before["depth_league_rank"], pa_before["depth_ranked_team_count"])
        pa_after["league_rank"] = starter_rank_after["per_team"][team_id_val]["rank"]
        pa_after["ranked_team_count"] = starter_rank_after["ranked_team_count"]
        pa_after["relative_label"] = _relative_label(pa_after["league_rank"], pa_after["ranked_team_count"])
        pa_after["depth_league_rank"] = depth_rank_after["per_team"][team_id_val]["rank"]
        pa_after["depth_ranked_team_count"] = depth_rank_after["ranked_team_count"]
        pa_after["depth_label"] = _relative_label(pa_after["depth_league_rank"], pa_after["depth_ranked_team_count"])
        position_analysis_before[pos] = pa_before
        position_analysis_after[pos] = pa_after

    flex_rank_before, flex_rank_after = _ftt_league_relative(
        "starter_FLEX", lambda s: _ftt_position_starter_value("FLEX", s, slot_counts),
        baseline, target_snapshot_after, partner_snapshot_after, partner_team_id)
    league_relative_changes["FLEX"] = _ftt_pack_relative(flex_rank_before, flex_rank_after, team_id_val)

    needs_before = _identify_positional_needs(position_analysis_before, target_snapshot_before, slot_counts)
    needs_after = _identify_positional_needs(position_analysis_after, target_snapshot_after, slot_counts)
    positional_need_changes = _diff_positional_needs(needs_before, needs_after)
    need_overall = _aggregate_need_overall(positional_need_changes)

    injury_risk = _compare_injury_risk(players_out_resolved, players_in_resolved)
    incoming_materially_reduced = any(
        fp_client.classify_injury_signal(
            p["_fp_intel"].get("injury_status") or p.get("espn_injury_status"))["label"] == "materially_reduced"
        for p in players_in_resolved)

    before_slot_map = _build_slot_map(target_snapshot_before["lineup_fp"])
    after_slot_map = _build_slot_map(target_snapshot_after["lineup_fp"])
    bye_week_impact = _assess_bye_week_impact(before_slot_map, after_slot_map, before_by_name, after_by_name)

    partner_fit = _assess_partner_fit(
        _FttTeamRef(team_id=partner_team_id, team_name=partner_team_name),
        package.players_out, package.players_in, partner_snapshot_before, partner_snapshot_after, partner_size_check)

    verdict, verdict_reasons = _compute_trade_verdict(
        lineup_impact, market_value, depth_impact, need_overall, injury_risk,
        roster_legality, roster_construction_severity, incoming_materially_reduced,
        league_relative_changes, team_id_val)

    best_case, worst_case = _describe_best_worst_case(
        lineup_impact, market_value, positional_need_changes, depth_impact, injury_risk, roster_legality)

    return {
        "package": package, "partner_team_id": partner_team_id, "partner_team_name": partner_team_name,
        "proposed_trade": {"players_out": [p["name"] for p in package.players_out],
                             "players_in": [p["name"] for p in package.players_in]},
        "roster_legality": roster_legality, "market_value": market_value, "lineup_impact": lineup_impact,
        "depth_impact": depth_impact, "positional_need_changes": positional_need_changes, "need_overall": need_overall,
        "league_relative_changes": league_relative_changes, "injury_risk": injury_risk, "bye_week_impact": bye_week_impact,
        "partner_fit": partner_fit, "verdict": verdict, "verdict_reasons": verdict_reasons,
        "best_case": best_case, "worst_case": worst_case,
        "players_out_resolved": players_out_resolved, "players_in_resolved": players_in_resolved,
    }

def _ftt_sort_key_for_trade_target(evaluated):
    incoming_rows = evaluated["market_value"]["asset_quality_context"]["incoming"]
    tiers = [r["tier"] for r in incoming_rows]
    ecrs = [r["ecr"] for r in incoming_rows]
    tier_tiebreak = min(tiers) if tiers and all(t is not None for t in tiers) else 99
    ecr_tiebreak = min(ecrs) if ecrs and all(e is not None for e in ecrs) else 9999
    return (
        _FTT_VERDICT_ORDER[evaluated["verdict"]],
        _FTT_NEED_EFFECT_ORDER.get(evaluated["need_overall"], 2),
        _FTT_LINEUP_ORDER[evaluated["lineup_impact"]["classification"]],
        _FTT_MARKET_ORDER[evaluated["market_value"]["assessment"]],
        _FTT_PARTNER_FIT_ORDER[evaluated["partner_fit"]["logical_fit"]],
        _FTT_DEPTH_ORDER[evaluated["depth_impact"]["depth_effect"]],
        _FTT_INJURY_ORDER[evaluated["injury_risk"]["risk_change"]],
        tier_tiebreak, ecr_tiebreak,
        evaluated["partner_team_id"],
        tuple(sorted(evaluated["proposed_trade"]["players_in"])),
    )

def _ftt_is_hard_excluded(evaluated):
    return (not evaluated["roster_legality"]["size_feasible"]
            or not evaluated["partner_fit"]["partner_roster_legality"]["size_feasible"]
            or evaluated["partner_fit"]["partner_roster_legality"]["partner_roster_construction_severity"] == "major"
            or evaluated["verdict"] == "DECLINE")

def _ftt_hard_exclusion_reason(evaluated):
    if not evaluated["roster_legality"]["size_feasible"]:
        return "rejected_partner_structure_or_size"  # target size - counted under structure bucket
    if not evaluated["partner_fit"]["partner_roster_legality"]["size_feasible"]:
        return "rejected_partner_structure_or_size"
    if evaluated["partner_fit"]["partner_roster_legality"]["partner_roster_construction_severity"] == "major":
        return "rejected_partner_structure_or_size"
    if evaluated["verdict"] == "DECLINE":
        return "rejected_decline"
    return None

def _ftt_is_eligible_for_trade_targets(evaluated):
    if _ftt_is_hard_excluded(evaluated):
        return False
    v = evaluated["verdict"]
    if v == "INSUFFICIENT_DATA":
        return False
    if v in ("ACCEPT", "LEAN_ACCEPT"):
        return True
    if v == "FAIR":
        strategic_positive = (evaluated["need_overall"] == "positive"
                                or evaluated["lineup_impact"]["classification"] in
                                   ("major_starting_upgrade", "starting_upgrade", "minor_starting_upgrade")
                                or evaluated["market_value"]["assessment"] in
                                   ("incoming_advantage", "strong_incoming_advantage"))
        no_quality_poor_fit = evaluated["partner_fit"]["logical_fit"] != "poor_fit"
        return strategic_positive and no_quality_poor_fit
    return False

def _ftt_derive_recommendation_label(evaluated):
    verdict = evaluated["verdict"]
    partner_fit = evaluated["partner_fit"]["logical_fit"]
    if verdict == "ACCEPT" and partner_fit == "plausible":
        return "priority_target"
    if verdict in ("ACCEPT", "LEAN_ACCEPT") and partner_fit in ("plausible", "unknown"):
        return "strong_target"
    if verdict == "FAIR" and partner_fit in ("plausible", "unknown"):
        return "exploratory_target"
    if verdict == "LEAN_DECLINE":
        return "stretch_target"
    if partner_fit == "poor_fit" and verdict in ("ACCEPT", "LEAN_ACCEPT", "FAIR"):
        return "stretch_target"
    return "stretch_target"

def _ftt_determine_primary_target_player(package):
    best = min(package.incoming_candidates, key=_ftt_incoming_sort_key)
    p = best["candidate_player"]
    return {"player": p["name"], "position": p.get("position"), "nfl_team": p.get("proTeam"),
             "partner_team_id": package.partner_team_id}

def _ftt_primary_target_key(primary_target):
    return (primary_target["partner_team_id"], primary_target["player"],
             primary_target["position"], primary_target["nfl_team"])

def _ftt_group_and_select_best(evaluated_candidates, limit):
    by_primary = {}
    for c in evaluated_candidates:
        primary = _ftt_determine_primary_target_player(c["package"])
        c["primary_target"] = primary
        key = _ftt_primary_target_key(primary)
        if key not in by_primary or _ftt_sort_key_for_trade_target(c) < _ftt_sort_key_for_trade_target(by_primary[key]):
            by_primary[key] = c
    return sorted(by_primary.values(), key=_ftt_sort_key_for_trade_target)[:limit]

def _ftt_select_final_targets(evaluated_candidates, limit):
    normal_eligible = [c for c in evaluated_candidates if _ftt_is_eligible_for_trade_targets(c)]
    stretch_pool = [c for c in evaluated_candidates
                     if not _ftt_is_hard_excluded(c) and c["verdict"] == "LEAN_DECLINE"
                     and (c["need_overall"] == "positive" or
                          c["lineup_impact"]["classification"] in ("major_starting_upgrade", "starting_upgrade"))]

    grouped_normal = _ftt_group_and_select_best(normal_eligible, limit)
    if len(grouped_normal) < limit:
        existing_keys = {_ftt_primary_target_key(_ftt_determine_primary_target_player(c["package"])) for c in grouped_normal}
        stretch_candidates = [c for c in stretch_pool
                                if _ftt_primary_target_key(_ftt_determine_primary_target_player(c["package"])) not in existing_keys]
        grouped_stretch = _ftt_group_and_select_best(stretch_candidates, limit - len(grouped_normal))
        grouped_normal.extend(grouped_stretch)
    return sorted(grouped_normal, key=_ftt_sort_key_for_trade_target)[:limit]

def _ftt_build_why_target(evaluated):
    reasons = []
    need_entries = [n for n in evaluated["positional_need_changes"] if n["effect"] == "improved"]
    for n in need_entries:
        reasons.append(f"Would upgrade {n['position']} from a {n['before']} need to {n['after']}.")
    li = evaluated["lineup_impact"]
    if li["classification"] in ("major_starting_upgrade", "starting_upgrade", "minor_starting_upgrade"):
        entering = li.get("players_entering_lineup", [])
        if entering:
            reasons.append(f"{', '.join(entering)} would enter the starting/FLEX lineup, "
                             f"a {li['classification'].replace('_', ' ')} (+{li.get('projection_delta')} pts).")
    if not reasons:
        reasons.append("Represents a plausible roster-fit opportunity based on computed evidence.")
    return reasons

def _ftt_build_why_this_offer(evaluated, baseline):
    reasons = []
    for p in evaluated["package"].players_out:
        if p["name"] in baseline["trade_surplus_player_names"]:
            reasons.append(f"{p['name']} is a trade_surplus_candidate - target has depth/strength at {p.get('position')}.")
        elif p["name"] in baseline["expendable_player_names"]:
            reasons.append(f"{p['name']} is an expendable/replaceable asset on the target roster.")
        else:
            reasons.append(f"{p['name']} is offered as a useful non-core asset with computed roster removal feasibility considered.")
    pf = evaluated["partner_fit"]
    if pf["logical_fit"] == "plausible":
        reasons.append(f"Partner appears thin at {', '.join(pf['partner_positions_gained'])}, "
                         f"so the incoming asset(s) could plausibly address that.")
    return reasons

@mcp.tool(name="authenticate")
async def authenticate(espn_s2: str | None = None, swid: str | None = None) -> str:
    """Use configured ESPN credentials or explicitly replace the active pair."""
    if espn_s2 is None and swid is None:
        try:
            api.prime(SESSION_ID)
        except Exception as exc:
            return f"Authentication error: {exc}"
        creds = api.credentials.get(SESSION_ID) or {}
        if creds.get("espn_s2") and creds.get("swid"):
            return ("ESPN authentication is already active from server-side configuration. "
                    "No manual credential call is required.")
        return (
            "ESPN authentication is not active. Configure ESPN_S2 plus ESPN_SWID/SWID "
            "or supply both values explicitly."
        )
    if espn_s2 is None or swid is None:
        return "Authentication error: provide both espn_s2 and swid together, or omit both."
    try:
        api.store_credentials(SESSION_ID, espn_s2, swid)
    except ValueError as exc:
        return f"Authentication error: {exc}"
    return "Authentication successful. Explicit credentials are active for this process."


def _compatibility_text_failure(action: str, exc: Exception) -> str:
    return _compatibility_string_error(f"ESPN {action} failed", f"ESPN {action} failed", exc)


@mcp.tool(name="get_league_info")
async def get_league_info(league_id: int, year: int = CURRENT_YEAR) -> str:
    """Return basic league identity and season context."""
    try:
        payload = _fetch_core_league_payload(league_id, year)
        result = build_league_info(payload, year)
    except Exception as exc:
        return _compatibility_text_failure("league info read", exc)
    return str(result)


@mcp.tool(name="get_team_roster")
async def get_team_roster(league_id: int, team_id: int, year: int = CURRENT_YEAR) -> str:
    """Return the current roster for an ESPN fantasy team ID."""
    try:
        payload = _fetch_roster_payload(league_id, year)
        result, valid_ids = build_team_roster(payload, team_id, year)
    except Exception as exc:
        return _compatibility_text_failure("team roster read", exc)
    return str(result) if result is not None else f"Invalid team_id. Valid team_ids: {valid_ids}"


@mcp.tool(name="get_team_info")
async def get_team_info(league_id: int, team_id: int, year: int = CURRENT_YEAR) -> str:
    """Return record and transaction context for an ESPN fantasy team."""
    try:
        payload = _fetch_core_league_payload(league_id, year)
        result, valid_ids = build_team_info(payload, team_id)
    except Exception as exc:
        return _compatibility_text_failure("team info read", exc)
    return str(result) if result is not None else f"Invalid team_id. Valid team_ids: {valid_ids}"


@mcp.tool(name="get_player_stats")
async def get_player_stats(league_id: int, player_name: str, year: int = CURRENT_YEAR) -> str:
    """Return ESPN roster statistics for a case-insensitive player-name match."""
    try:
        payload = _fetch_roster_payload(league_id, year)
        result = build_player_stats(payload, player_name, year)
    except Exception as exc:
        return _compatibility_text_failure("player stats read", exc)
    return str(result) if result is not None else f"Player '{player_name}' not found in league {league_id}"


@mcp.tool(name="get_league_standings")
async def get_league_standings(league_id: int, year: int = CURRENT_YEAR) -> str:
    """Return ESPN's league standings order."""
    try:
        result = build_standings(_fetch_core_league_payload(league_id, year))
    except Exception as exc:
        return _compatibility_text_failure("standings read", exc)
    return str(result)


@mcp.tool(name="get_matchup_info")
async def get_matchup_info(league_id: int, week: int = None, year: int = CURRENT_YEAR) -> str:
    """Return matchup scores for a valid ESPN scoring week."""
    try:
        context = _fetch_matchup_context_payload(league_id, year)
        resolved_week, matchup_period, valid_weeks = resolve_matchup_request(context, week)
        if resolved_week is None:
            return f"Invalid week number. Valid scoring weeks: {valid_weeks}"
        payload = _fetch_matchup_score_payload(league_id, year, resolved_week, matchup_period)
        result = build_matchup_info(payload, resolved_week)
    except Exception as exc:
        return _compatibility_text_failure("matchup read", exc)
    return str(result)


@mcp.tool(name="logout")
async def logout() -> str:
    """Forget active in-memory ESPN credentials for this process."""
    removed = api.clear_credentials(SESSION_ID)
    return "Authentication credentials have been cleared." if removed else "No active ESPN credentials were stored."


@mcp.tool()
async def get_league_settings(league_id: int, year: int = None) -> dict:
    """Get exact roster slot requirements and scoring rules reported by ESPN."""
    try:
        resolved_year = _resolve_year(year)
        log_error(f"Getting league settings for league {league_id}, year {resolved_year}")
        payload = _fetch_core_league_payload(league_id, resolved_year)
        return build_league_settings(payload, league_id, resolved_year)
    except Exception as e:
        return _error_response("retrieving league settings", e)

@mcp.tool()
async def get_free_agents(league_id: int, week: int = None, position: str = None,
                           size: int = 50, year: int = None) -> dict:
    """Get available free agents / waiver players for a league.

    This path reads ESPN directly through the project transport. During
    preseason, a missing/zero current scoring period resolves to week 1.

    Args:
        league_id: The ESPN fantasy football league ID
        week: Week to check (defaults to ESPN current week, or week 1 preseason)
        position: Optional position filter, e.g. "RB", "WR", "QB", "TE", "K", "D/ST"
        size: Max number of free agents to return (bounded 1-200, default 50)
        year: Optional year (defaults to current season if omitted)
    """
    try:
        validated_size, size_err = _validate_bounded_int(size, "size", 1, 200, 50)
        if size_err:
            return {"error": "invalid_parameter", "message": size_err}

        resolved_year = _resolve_year(year)
        log_error(f"Getting free agents for league {league_id}, year {resolved_year}")
        if resolved_year < 2019:
            raise ValueError("Cant use free agents before 2019")

        if week in (None, 0, False):
            context = _fetch_free_agent_context_payload(league_id, resolved_year)
            fa_week = resolve_free_agent_week(context, week)
        else:
            fa_week = resolve_free_agent_week({}, week)

        player_payload = _fetch_free_agent_player_payload(
            league_id, resolved_year, fa_week, validated_size, position
        )
        raw_players = player_payload.get("players") if isinstance(player_payload, dict) else None
        if raw_players == []:
            return {
                "league_id": league_id, "year": resolved_year, "week_used": fa_week,
                "position_filter": position, "count": 0, "free_agents": [],
                "message": "No free agents found for the given filters."
            }

        schedule_payload = _fetch_pro_schedule_payload(resolved_year)
        results = build_free_agents(player_payload, schedule_payload, resolved_year, fa_week)
        if not results:
            return {
                "league_id": league_id, "year": resolved_year, "week_used": fa_week,
                "position_filter": position, "count": 0, "free_agents": [],
                "message": "No free agents found for the given filters."
            }

        return {
            "league_id": league_id, "year": resolved_year, "week_used": fa_week,
            "position_filter": position, "count": len(results), "free_agents": results
        }
    except Exception as e:
        return _error_response("retrieving free agents", e)

@mcp.tool()
async def get_draft_results(league_id: int, year: int = None) -> dict:
    """Get completed ESPN draft results without constructing an espn-api League.

    Returns round/pick, player, drafting team, keeper/bid fields, and
    nominating team using the same public response shape as before. Live
    draft-board/recommendation tools remain on their separate draft-state
    path and are intentionally not changed by this migration.

    Args:
        league_id: The ESPN fantasy football league ID
        year: Optional year (defaults to current season if omitted)
    """
    try:
        resolved_year = _resolve_year(year)
        log_error(f"Getting draft results for league {league_id}, year {resolved_year}")
        draft_payload = _fetch_draft_result_payload(league_id, resolved_year)
        draft_detail = draft_payload.get("draftDetail") if isinstance(draft_payload, dict) else None
        if not isinstance(draft_detail, dict) or not draft_detail.get("drafted"):
            return build_draft_results(draft_payload, [], league_id, resolved_year)

        players_payload = _fetch_draft_player_payload(resolved_year)
        return build_draft_results(draft_payload, players_payload, league_id, resolved_year)
    except Exception as e:
        return _error_response("retrieving draft results", e)

@mcp.tool()
async def get_all_rosters(league_id: int, year: int = None, detailed: bool = False) -> dict:
    """Get every team's current roster in a single project-owned ESPN read.

    Args:
        league_id: The ESPN fantasy football league ID
        year: Optional year (defaults to current season if omitted)
        detailed: If True, include full per-player stat blobs (large payload).
                  Default False returns a lightweight summary per player.
    """
    try:
        resolved_year = _resolve_year(year)
        log_error(f"Getting all rosters for league {league_id}, year {resolved_year}")
        payload = _fetch_roster_payload(league_id, resolved_year)
        return build_all_rosters(payload, league_id, resolved_year, detailed=detailed)
    except Exception as e:
        return _error_response("retrieving all rosters", e)

@mcp.tool()
async def get_league_snapshot(league_id: int, year: int = None,
                               free_agent_limit: int = 25) -> dict:
    """Get a compact, AI-analysis-ready league snapshot from project-owned ESPN reads.

    The base snapshot (settings, standings, rosters, draft status) is fetched
    in one direct ESPN league request. Free-agent enrichment remains a
    separate filtered read and is non-fatal, preserving the prior snapshot
    contract when that optional surface is unavailable.

    Args:
        league_id: The ESPN fantasy football league ID
        year: Optional year (defaults to current season if omitted)
        free_agent_limit: Max free agents to include (bounded 0-100, default 25)
    """
    try:
        validated_limit, limit_err = _validate_bounded_int(free_agent_limit, "free_agent_limit", 0, 100, 25)
        if limit_err:
            return {"error": "invalid_parameter", "message": limit_err}

        resolved_year = _resolve_year(year)
        log_error(f"Building league snapshot for league {league_id}, year {resolved_year}")
        payload = _fetch_snapshot_payload(league_id, resolved_year)
        result = build_league_snapshot_base(payload, league_id, resolved_year)

        free_agents_out = []
        free_agents_available = True
        free_agents_error = None
        fa_week = resolve_free_agent_week(payload, None)
        if validated_limit == 0:
            free_agents_available = False
        else:
            try:
                player_payload = _fetch_free_agent_player_payload(
                    league_id, resolved_year, fa_week, validated_limit
                )
                schedule_payload = _fetch_pro_schedule_payload(resolved_year)
                fa_players = build_free_agents(
                    player_payload, schedule_payload, resolved_year, fa_week
                )
                free_agents_out = [{
                    "name": p.get("name"),
                    "position": p.get("position"),
                    "proTeam": p.get("proTeam"),
                    "projected_points": p.get("projected_points"),
                } for p in fa_players]
                if not fa_players:
                    free_agents_available = False
            except Exception as fa_err:
                free_agents_available = False
                if _is_private_league_error(fa_err):
                    log_error("Snapshot free agent lookup failed: private league authentication/access failed")
                    free_agents_error = "private_league_auth_required"
                else:
                    log_error(f"Snapshot free agent lookup failed: {fa_err}")
                    free_agents_error = str(fa_err)

        result.update({
            "free_agents_week_used": fa_week,
            "free_agents_available": free_agents_available,
            "free_agents_error": free_agents_error,
            "free_agents_top": free_agents_out,
        })
        return result
    except Exception as e:
        return _error_response("building league snapshot", e)

# --- REGISTRY-AWARE FANTASYPROS CACHE REFRESH (2026-08-15, additive to
# the existing 27 tools - refresh_fantasypros_cache's SIGNATURE/BEHAVIOR
# is redesigned here, no other tool is touched, tool count stays 27).
# fantasypros_client.py is NOT modified - refresh_selected() already
# fully supports scoring="PPR"/"HALF"/"STD" and an independent
# datasets/positions subset; this block is pure server-side
# orchestration around that existing, frozen, unchanged helper. ---

_CANONICAL_SCORING_ORDER = ["PPR", "HALF", "STD"]
_SHARED_FP_DATASETS = ("players", "injuries", "news")  # scoring-independent
_SCORING_FP_DATASETS = ("rankings", "projections")      # scoring-dependent

def _discover_registered_scoring_buckets() -> dict:
    """Discover scoring buckets for enabled registry leagues via direct ESPN reads.

    ESPN remains the source of truth; the registry stores navigation only.
    Each league is isolated so one inaccessible/private league never drops
    successful discoveries for the others. No wrapper League objects are
    constructed for this metadata-only operation.
    """
    try:
        registry = league_registry.load_registry()
    except league_registry.RegistryError as e:
        return {"registry_error": str(e), "buckets": [], "leagues": [], "failures": []}

    leagues_out, failures, seen_buckets = [], [], set()
    for alias, entry in league_registry.list_enabled_leagues(registry):
        league_id = entry["league_id"]
        try:
            payload = _fetch_core_league_payload(league_id, CURRENT_YEAR)
            settings_result = build_league_settings(payload, league_id, CURRENT_YEAR)
            bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))
            leagues_out.append({"alias": alias, "league_id": league_id, "scoring_bucket": bucket})
            seen_buckets.add(bucket)
        except Exception as e:
            if _is_private_league_error(e):
                status = "authentication_required"
            else:
                status = "inaccessible"
            failures.append({"alias": alias, "league_id": league_id, "status": status})

    ordered_buckets = [b for b in _CANONICAL_SCORING_ORDER if b in seen_buckets]
    return {"registry_error": None, "buckets": ordered_buckets, "leagues": leagues_out, "failures": failures}

def _plan_fp_refresh(shared_wanted: list, scoring_wanted: list, buckets: list,
                       positions: list, force: bool) -> dict:
    """Zero-network-cost planning pass. Sums fp_client's own pure local
    estimator ONCE for the shared-dataset group and ONCE PER unique
    required scoring bucket for the scoring-dependent group - shared
    datasets are never double-counted across buckets. Returns the
    complete aggregate plan so the caller can quota-check the ENTIRE
    multi-bucket operation before a single live FantasyPros request."""
    shared_estimate = (fp_client._estimate_batch_requests(shared_wanted, positions, _CANONICAL_SCORING_ORDER[0], force)
                         if shared_wanted else 0)
    per_bucket_estimate = {}
    for bucket in buckets:
        per_bucket_estimate[bucket] = (fp_client._estimate_batch_requests(scoring_wanted, positions, bucket, force)
                                         if scoring_wanted else 0)
    total = shared_estimate + sum(per_bucket_estimate.values())
    return {"shared_estimate": shared_estimate, "per_bucket_estimate": per_bucket_estimate, "total_estimated_requests": total}

@mcp.tool()
async def refresh_fantasypros_cache(datasets: list = None, positions: list = None,
                                     scoring: str = "REGISTERED", force: bool = False,
                                     allow_soft_limit_override: bool = False,
                                     dry_run: bool = False) -> dict:
    """Refresh FantasyPros cached data (players, rankings, projections,
    injuries, news). Cached reads never consume API requests; only a
    stale/missing dataset triggers a live call. The ENTIRE refresh plan
    (across every required scoring bucket) is quota-checked before the
    first live request - if the complete plan cannot safely fit, ZERO
    FantasyPros calls are made.

    Args:
        datasets: Optional subset, e.g. ["players","rankings","injuries"].
                  Defaults to all five.
        positions: Optional subset of QB/RB/WR/TE for rankings/projections.
                   Defaults to all four core positions.
        scoring: "REGISTERED" (default) discovers every ENABLED league in
                 the local league registry, dynamically resolves each
                 one's real ESPN scoring bucket, deduplicates the
                 required set, and refreshes shared datasets once plus
                 scoring-dependent datasets once per unique bucket - so a
                 future league needs only a registry entry, never a code
                 change. Explicit "PPR"/"HALF"/"STD" (case-insensitive)
                 refreshes exactly that one bucket only, independent of
                 what leagues are registered - useful for diagnostics/
                 manual prep/testing.
        force: If True, refresh even if cache is fresh (cache-staleness
               override only - does not affect quota enforcement).
        allow_soft_limit_override: If True, permit this batch to cross
               the daily soft limit (45) as long as it stays under the
               hard limit (50).
        dry_run: If True, performs registry discovery + full planning +
                 quota-feasibility check but makes ZERO live requests,
                 ZERO cache writes, and ZERO quota-ledger changes.
    """
    try:
        ds_err = fp_client.validate_datasets(datasets)
        if ds_err:
            return {"error": "invalid_parameter", "message": ds_err}

        scoring_mode = (scoring or "REGISTERED").strip().upper()
        if scoring_mode not in ("REGISTERED",) and scoring_mode not in fp_client.VALID_SCORING:
            return {"error": "invalid_parameter",
                    "message": f"scoring must be one of ['REGISTERED'] + {sorted(fp_client.VALID_SCORING)} (got {scoring!r})."}

        if positions is not None:
            pos_err = None
            for p in positions:
                e = fp_client.validate_position(p) if hasattr(fp_client, "validate_position") else None
                if e:
                    pos_err = e
                    break
            if pos_err:
                return {"error": "invalid_parameter", "message": pos_err}
        positions_to_use = positions or fp_client.CORE_POSITIONS

        wanted = datasets or fp_client.DATASET_NAMES
        shared_wanted = [d for d in wanted if d in _SHARED_FP_DATASETS]
        scoring_wanted = [d for d in wanted if d in _SCORING_FP_DATASETS]

        usage_before = fp_client.get_usage_summary()
        used = usage_before["requests_used_today"]

        if scoring_mode == "REGISTERED":
            discovery = _discover_registered_scoring_buckets()
            if discovery["registry_error"]:
                return {"error": "registry_error", "message": discovery["registry_error"]}
            if discovery["failures"]:
                return {
                    "error": "scoring_discovery_incomplete",
                    "message": "One or more enabled registered leagues could not be resolved; "
                                "refusing to begin a REGISTERED refresh so no league is silently "
                                "excluded from cache coverage. Zero FantasyPros requests were made.",
                    "failures": discovery["failures"],
                    "resolved_leagues": discovery["leagues"],
                }
            registered_leagues = discovery["leagues"]
            buckets = discovery["buckets"]
        else:
            registered_leagues = None
            buckets = [scoring_mode]

        plan = _plan_fp_refresh(shared_wanted, scoring_wanted, buckets, positions_to_use, force)
        total_estimated = plan["total_estimated_requests"]

        if used + total_estimated > fp_client.DAILY_REQUEST_LIMIT:
            return {
                "error": "quota_plan_blocked", "mode": scoring_mode,
                "message": f"The complete plan across {buckets} could require up to {total_estimated} live "
                            f"requests, but only {fp_client.DAILY_REQUEST_LIMIT - used} remain today "
                            f"(used {used}/{fp_client.DAILY_REQUEST_LIMIT}). Zero FantasyPros requests were made.",
                "required_scoring_buckets": buckets, "plan": plan,
                "quota": {"before": used, "hard_limit": fp_client.DAILY_REQUEST_LIMIT},
            }
        if used + total_estimated > fp_client.DAILY_SOFT_LIMIT and not allow_soft_limit_override:
            return {
                "error": "quota_plan_blocked", "mode": scoring_mode,
                "message": f"The complete plan across {buckets} could require up to {total_estimated} live "
                            f"requests, pushing usage from {used} to {used + total_estimated}, crossing the "
                            f"soft limit of {fp_client.DAILY_SOFT_LIMIT}. Pass allow_soft_limit_override=True "
                            f"to proceed. Zero FantasyPros requests were made.",
                "required_scoring_buckets": buckets, "plan": plan,
                "quota": {"before": used, "soft_limit": fp_client.DAILY_SOFT_LIMIT, "hard_limit": fp_client.DAILY_REQUEST_LIMIT},
            }

        if dry_run:
            return {
                "mode": scoring_mode, "status": "dry_run",
                "registered_leagues": registered_leagues,
                "required_scoring_buckets": buckets, "plan": plan,
                "quota": {"before": used, "consumed": 0, "after": used, "hard_limit": fp_client.DAILY_REQUEST_LIMIT},
                "warnings": [],
            }

        refreshed_all, served_from_cache_all, failures_all = [], [], []

        if shared_wanted:
            r = fp_client.refresh_selected(datasets=shared_wanted, force=force,
                                             allow_soft_limit_override=allow_soft_limit_override,
                                             scoring=_CANONICAL_SCORING_ORDER[0], positions=positions_to_use)
            refreshed_all += r.get("datasets_refreshed_live", [])
            served_from_cache_all += r.get("datasets_served_from_cache", [])
            failures_all += r.get("failures", [])

        for bucket in buckets:
            if not scoring_wanted:
                continue
            r = fp_client.refresh_selected(datasets=scoring_wanted, force=force,
                                             allow_soft_limit_override=allow_soft_limit_override,
                                             scoring=bucket, positions=positions_to_use)
            refreshed_all += r.get("datasets_refreshed_live", [])
            served_from_cache_all += r.get("datasets_served_from_cache", [])
            failures_all += r.get("failures", [])

        usage_after = fp_client.get_usage_summary()
        consumed = usage_after["requests_used_today"] - used

        return {
            "mode": scoring_mode,
            "registered_leagues": registered_leagues,
            "required_scoring_buckets": buckets,
            "plan": plan,
            "datasets_refreshed_live": refreshed_all,
            "datasets_served_from_cache": served_from_cache_all,
            "failures": failures_all,
            "quota": {"before": used, "consumed": consumed, "after": usage_after["requests_used_today"],
                       "soft_limit": fp_client.DAILY_SOFT_LIMIT, "hard_limit": fp_client.DAILY_REQUEST_LIMIT},
            "status": "success" if not failures_all else "partial_failure",
            "warnings": [],
        }
    except fp_client.FantasyProsConfigError as e:
        return {"error": "configuration_error", "message": str(e)}
    except fp_client.FantasyProsQuotaError as e:
        return e.details
    except Exception as e:
        return _error_response("refreshing FantasyPros cache", e)

@mcp.tool()
async def get_player_intelligence(player_name: str, team: str = None, position: str = None) -> dict:
    """Compact combined FantasyPros view for one player: ECR, positional
    rank, tier, ADP, projection, injury status, recent news, bye week,
    ESPN ownership %, and match confidence. Reads from cache only - never
    makes a live API call.

    Args:
        player_name: Player's name
        team: Optional NFL team abbreviation to disambiguate (yields
              HIGH confidence when combined with position)
        position: Optional position (QB/RB/WR/TE) to disambiguate
    """
    try:
        if position is not None:
            pos_err = fp_client.validate_position(position)
            if pos_err:
                return {"error": "invalid_parameter", "message": pos_err}
        return fp_client.build_player_intelligence(player_name, team, position)
    except Exception as e:
        return _error_response("retrieving player intelligence", e)

@mcp.tool()
async def get_consensus_rankings(position: str, scoring: str = "PPR", limit: int = None) -> dict:
    """Get cached FantasyPros consensus rankings for a position. Reads
    from cache only - never makes a live API call.

    Args:
        position: QB, RB, WR, or TE
        scoring: Scoring format, default PPR
        limit: Optional max rows to return (1-200)
    """
    try:
        pos_err = fp_client.validate_position(position)
        if pos_err:
            return {"error": "invalid_parameter", "message": pos_err}
        scoring_err = fp_client.validate_scoring(scoring)
        if scoring_err:
            return {"error": "invalid_parameter", "message": scoring_err}
        limit_err = fp_client.validate_limit(limit)
        if limit_err:
            return {"error": "invalid_parameter", "message": limit_err}
        return fp_client.get_rankings_list(position, scoring, limit)
    except Exception as e:
        return _error_response("retrieving consensus rankings", e)

@mcp.tool()
async def get_adp(position: str, limit: int = None) -> dict:
    """Get ADP for a position from the cached FantasyPros players dataset
    (true ADP; not the ownership % found in consensus-rankings). Reads
    from cache only - never makes a live API call.

    Args:
        position: QB, RB, WR, or TE
        limit: Optional max rows to return (1-200)
    """
    try:
        pos_err = fp_client.validate_position(position)
        if pos_err:
            return {"error": "invalid_parameter", "message": pos_err}
        limit_err = fp_client.validate_limit(limit)
        if limit_err:
            return {"error": "invalid_parameter", "message": limit_err}
        return fp_client.get_adp_list(position, "PPR", limit)
    except Exception as e:
        return _error_response("retrieving ADP", e)

@mcp.tool()
async def enrich_espn_free_agents(league_id: int, position: str = None, limit: int = 25,
                                   year: int = None) -> dict:
    """Join direct ESPN free agents against cached FantasyPros market intelligence.

    ESPN availability/projection facts come from the already project-owned
    ``get_free_agents`` path. FantasyPros enrichment remains cache-only and
    never triggers a live FantasyPros request.

    Args:
        league_id: The ESPN fantasy football league ID
        position: Optional ESPN position filter
        limit: Max free agents to enrich (bounded 1-100, default 25)
        year: Optional year (defaults to current season)
    """
    try:
        validated_limit, limit_err = _validate_bounded_int(limit, "limit", 1, 100, 25)
        if limit_err:
            return {"error": "invalid_parameter", "message": limit_err}
        resolved_year = _resolve_year(year)

        fa_result = await get_free_agents(
            league_id, week=None, position=position, size=validated_limit, year=resolved_year
        )
        if "error" in fa_result:
            return fa_result

        enriched = []
        for player in fa_result.get("free_agents", []):
            intel = fp_client.build_player_intelligence(
                player.get("name"), player.get("proTeam"), player.get("position")
            )
            enriched.append({
                "player": player.get("name"),
                "position": player.get("position"),
                "nfl_team": player.get("proTeam"),
                "espn_projected_points": player.get("projected_points"),
                "fp_ecr": intel.get("ecr"),
                "fp_pos_rank": intel.get("pos_rank"),
                "fp_tier": intel.get("tier"),
                "fp_adp": intel.get("adp"),
                "fp_projected_points": intel.get("projected_points"),
                "injury_status": intel.get("injury_status"),
                "espn_ownership_pct": intel.get("espn_ownership_pct"),
                "match_method": intel.get("match_method"),
                "match_confidence": intel.get("match_confidence"),
            })

        return {
            "league_id": league_id,
            "year": resolved_year,
            "week_used": fa_result.get("week_used"),
            "position_filter": position,
            "count": len(enriched),
            "free_agents": enriched,
        }
    except Exception as e:
        return _error_response("enriching ESPN free agents", e)

@mcp.tool()
async def compare_players(players: list) -> dict:
    """Compare 2-4 players using cached FantasyPros intelligence. Runs
    entirely from cache; no confirmed live 'compare' endpoint exists on
    the validated FantasyPros public v2 API surface.

    Args:
        players: List of 2-4 player names to compare
    """
    try:
        players_err = fp_client.validate_compare_players(players)
        if players_err:
            return {"error": "invalid_parameter", "message": players_err}
        return fp_client.compare_players_from_cache(players)
    except Exception as e:
        return _error_response("comparing players", e)

# --- TOOL #20-23 STANDALONE TIER-1 PERFORMANCE (2026-08-14): generic,
# additive per-call FantasyPros parsed-cache scope decorator. Reuses
# the EXACT SAME infrastructure as Tool #25's per-brief cache -
# _GFB_FP_PARSED_CACHE_CONTEXT - which is defined later in this file
# and resolved by NAME at call time inside the wrapper body (safe:
# Python only needs the ContextVar to exist by the time the wrapper
# actually RUNS on the first real tool invocation, long after the
# whole module has finished loading - not at decoration time here).
# No second _read_cache monkeypatch, no new ContextVar, no process-
# level cache is introduced. NESTED-SCOPE SAFE: if this decorator
# runs from INSIDE an already-active scope (e.g. Tool #25 calling
# analyze_my_team/rank_waiver_targets/find_trade_targets directly),
# it detects the existing non-None cache and simply calls straight
# through - creating nothing, resetting nothing - so Tool #25's
# single shared ~13-file brief-level cache is completely preserved.
# Only when NO outer scope is active does it create a brand-new
# per-call cache, torn down via ContextVar.reset in a finally block
# the instant that one standalone call completes, errors, or is
# cancelled - so every standalone invocation starts fully fresh.
def _with_fp_parsed_cache_scope(func):
    @functools.wraps(func)
    async def _fp_scope_wrapper(*args, **kwargs):
        existing_cache = _GFB_FP_PARSED_CACHE_CONTEXT.get()
        if existing_cache is not None:
            return await func(*args, **kwargs)
        token = _GFB_FP_PARSED_CACHE_CONTEXT.set({})
        try:
            return await func(*args, **kwargs)
        finally:
            _GFB_FP_PARSED_CACHE_CONTEXT.reset(token)
    return _fp_scope_wrapper

@mcp.tool()
@_with_fp_parsed_cache_scope
async def rank_waiver_targets(league_id: int, team_id: int, position: str = None,
                               limit: int = 10, year: int = None) -> dict:
    """Rank the best available waiver-wire targets for a specific team.
    Lineup placement decisions use FantasyPros-only season projections
    (never blended with ESPN numbers). Drop-candidate search covers the
    WHOLE roster, not just the add's position. Uses cached FantasyPros
    data only - zero live FantasyPros API calls, ever.

    Args:
        league_id: The ESPN fantasy football league ID
        team_id: The team ID to build recommendations for (real ESPN team_id, not list position)
        position: Optional filter, QB/RB/WR/TE only
        limit: Max ranked recommendations to return (bounded 1-50, default 10)
        year: Optional year (defaults to current season if omitted)
    """
    try:
        league_id_val, league_err = _validate_bounded_int(league_id, "league_id", 1, 999_999_999, league_id)
        if league_err:
            return {"error": "invalid_parameter", "message": league_err}
        team_id_val, team_err = _validate_bounded_int(team_id, "team_id", 1, 999, team_id)
        if team_err:
            return {"error": "invalid_parameter", "message": team_err}
        limit_val, limit_err = _validate_bounded_int(limit, "limit", 1, 50, 10)
        if limit_err:
            return {"error": "invalid_parameter", "message": limit_err}
        if position is not None:
            pos_err = fp_client.validate_position(position)
            if pos_err:
                return {"error": "invalid_parameter", "message": pos_err}

        resolved_year = _resolve_year(year)
        payload = _fetch_snapshot_payload(league_id_val, resolved_year)
        settings_result = build_league_settings(payload, league_id_val, resolved_year)
        slot_counts = settings_result.get("roster_slot_counts", {})
        scoring_bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))

        positions_in_scope = [position.upper()] if position else fp_client.CORE_POSITIONS
        cache_warnings = _check_required_fp_caches(positions_in_scope, scoring_bucket)
        if cache_warnings:
            return {"error": "cache_incomplete", "scoring_bucket_detected": scoring_bucket,
                    "message": f"Required FantasyPros cache data is missing for {scoring_bucket}. "
                               "No live FantasyPros calls were made.",
                    "warnings": cache_warnings, "next_step": "Call refresh_fantasypros_cache() then retry."}

        data_freshness = fp_client.get_cache_freshness_report(positions_in_scope, scoring_bucket)
        freshness_warnings = [f"{k} is stale (age {v['age_seconds']}s, ttl {v['ttl_seconds']}s)"
                                for k, v in data_freshness.items() if v.get("is_stale")]

        team_roster, valid_team_ids = build_team_roster(payload, team_id_val, resolved_year)
        if team_roster is None:
            return {
                "error": "invalid_parameter",
                "message": f"No team with team_id={team_id_val} in this league. Valid team_ids: {valid_team_ids}"
            }
        team_name = team_roster.get("team_name")
        roster = [{
            "name": player.get("name"),
            "position": player.get("position"),
            "proTeam": player.get("proTeam"),
            "projected_points": player.get("projected_points"),
            "points": player.get("points"),
        } for player in team_roster.get("roster", [])]

        for p in roster:
            p["_fp_intel"] = fp_client.build_player_intelligence(
                p.get("name"), p.get("proTeam"), p.get("position"), scoring=scoring_bucket)

        roster_fp_rows, fp_reliability_warning = _build_fp_eval_roster(roster)
        lineup_espn_before = _assign_best_lineup(roster, slot_counts, value_field="projected_points")
        lineup_fp_before = _assign_best_lineup(roster_fp_rows, slot_counts, value_field="_fp_eval_value")

        position_needs = {pos: {
            "direct_starter_slots": slot_counts.get(pos, 0),
            "filled_in_fp_lineup": len(lineup_fp_before["starters"].get(pos, [])),
        } for pos in ["QB", "RB", "WR", "TE"]}

        fa_result = await get_free_agents(league_id_val, week=None, position=position, size=100, year=resolved_year)
        if "error" in fa_result:
            return fa_result

        pool_by_position = {}
        for fa in fa_result.get("free_agents", []):
            fa_pos = fa.get("position")
            if fa_pos not in ("QB", "RB", "WR", "TE"):
                continue
            add_intel = fp_client.build_player_intelligence(fa.get("name"), fa.get("proTeam"), fa_pos, scoring=scoring_bucket)
            pool_by_position.setdefault(fa_pos, []).append((fa, add_intel))

        recommendations = []
        for fa_pos, candidates in pool_by_position.items():
            candidates.sort(key=lambda c: c[1].get("ecr") if c[1].get("ecr") is not None else 9999)
            for idx, (fa, add_intel) in enumerate(candidates):
                add_eval_value = add_intel.get("projected_points")
                drop_options = _evaluate_drop_options(roster_fp_rows, fa_pos, add_eval_value, slot_counts)
                if not drop_options:
                    continue
                best_drop = drop_options[0]
                drop_player = best_drop["candidate"]
                drop_intel = drop_player.get("_fp_intel", {}) or {}

                sim_add_row = {"name": fa.get("name"), "position": fa_pos, "_fp_eval_value": add_eval_value}
                simulated_roster = [r for r in roster_fp_rows if r.get("name") != drop_player.get("name")] + [sim_add_row]
                lineup_fp_after = _assign_best_lineup(simulated_roster, slot_counts, value_field="_fp_eval_value")
                can_enter_rotation = fa.get("name") in {p.get("name") for p in _flatten_starters(lineup_fp_after)}
                roster_utility = _describe_roster_utility(lineup_fp_before, lineup_fp_after, fa.get("name"),
                                                             position_needs, fa_pos)

                next_best = candidates[idx + 1] if idx + 1 < len(candidates) else None
                scarcity_note = (f"Ranked #{idx+1} of {len(candidates)} available {fa_pos} free agents by ECR"
                                  + (f"; next-best is {next_best[0].get('name')} (ECR {next_best[1].get('ecr')})"
                                     if next_best else "; no other comparable free agent at this position."))

                ownership_band = fp_client.ownership_anomaly_band(add_intel.get("espn_ownership_pct"))
                add_intel_with_band = {**add_intel, "_ownership_band": ownership_band}
                drop_is_expendable = best_drop["is_zero_espn_projection"] or not best_drop["is_current_starter"]

                signals = fp_client.assess_upgrade_signals(
                    add_intel_with_band, drop_intel, roster_utility, can_enter_rotation,
                    drop_is_expendable, best_drop["is_zero_espn_projection"], True, scarcity_note)

                lineup_impact = _classify_lineup_impact(roster_utility, signals["direction"])

                if add_intel.get("match_confidence") in ("ambiguous", "none"):
                    rec_confidence = "unreliable_match"
                elif signals["direction"] in ("strong_upgrade", "upgrade"):
                    rec_confidence = "high" if roster_utility in ("fills_direct_starting_gap", "enters_flex_starting_lineup", "improves_starting_rotation") else "medium"
                elif signals["direction"] == "marginal":
                    rec_confidence = "low"
                else:
                    rec_confidence = "not_recommended"

                reason = _build_recommendation_reason(
                    fa.get("name"), fa_pos, add_intel,
                    drop_player.get("name"), best_drop["expendability_reason"], ownership_band, roster_utility, signals)

                recommendations.append({
                    "add_player": fa.get("name"), "add_position": fa_pos,
                    "recommended_drop": drop_player.get("name"),
                    "reason": reason,
                    "player_quality": fp_client.describe_player_quality(add_intel.get("tier")),
                    "roster_utility": roster_utility,
                    "lineup_impact": lineup_impact,
                    "ecr": add_intel.get("ecr"), "adp": add_intel.get("adp"),
                    "fantasypros_tier": add_intel.get("tier"),
                    "espn_ownership_pct": add_intel.get("espn_ownership_pct"),
                    "ownership_anomaly_band": ownership_band,
                    "fantasypros_projection": add_intel.get("projected_points"),
                    "espn_projection_weekly": fa.get("projected_points"),
                    "injury_status": add_intel.get("injury_status"),
                    "match_confidence": add_intel.get("match_confidence"),
                    "signals": signals,
                    "recommendation_confidence": rec_confidence,
                })

        direction_order = {"strong_upgrade": 0, "upgrade": 1, "marginal": 2, "downgrade": 3, "insufficient_data": 4}
        lineup_impact_order = {"direct_starter_upgrade": 0, "flex_starter_upgrade": 1,
                                 "starting_rotation_upgrade": 2, "bench_only_upgrade": 3, "no_lineup_improvement": 4}
        ownership_order = {"extreme": 0, "strong": 1, "moderate": 2, "none": 3, "unknown": 4}

        def sort_key(r):
            s = r["signals"]
            return (
                direction_order.get(s["direction"], 5),
                lineup_impact_order.get(r["lineup_impact"], 5),
                0 if s.get("drop_is_expendable") else 1,
                r["ecr"] if r["ecr"] is not None else 9999,
                ownership_order.get(r["ownership_anomaly_band"], 5),
            )
        recommendations.sort(key=sort_key)
        for i, r in enumerate(recommendations[:limit_val], start=1):
            r["rank"] = i
        top = recommendations[:limit_val]

        has_starting_upgrade = any(
            r["signals"]["direction"] in ("strong_upgrade", "upgrade")
            and r["lineup_impact"] in ("direct_starter_upgrade", "flex_starter_upgrade", "starting_rotation_upgrade")
            for r in top)
        has_bench_upgrade = any(
            r["signals"]["direction"] in ("strong_upgrade", "upgrade")
            and r["lineup_impact"] == "bench_only_upgrade"
            for r in top)

        if has_starting_upgrade:
            overall = "starting_lineup_upgrade_available"
        elif has_bench_upgrade:
            overall = "bench_value_upgrade_available"
        else:
            overall = "no_clear_upgrade_available"

        warnings_out = list(freshness_warnings)
        if fp_reliability_warning:
            warnings_out.append(fp_reliability_warning)

        return {
            "league_id": league_id_val, "team_id": team_id_val, "team_name": team_name,
            "year": resolved_year, "scoring_bucket_detected": scoring_bucket, "position_filter": position,
            "count": len(top), "overall_recommendation": overall,
            "data_freshness": data_freshness, "warnings": warnings_out,
            "lineup_before": {"espn_view_informational": lineup_espn_before, "fp_view_decision_driving": lineup_fp_before},
            "recommendations": top,
            "methodology_notes": [
                "Lineup placement (can_enter_rotation, roster_utility) uses FantasyPros season "
                "projections for BOTH the roster and the add candidate - never ESPN-vs-FP blended.",
                "espn_projection_weekly is informational context only, never used to decide rotation.",
                "drop_external_value_missing=true means the drop has a resolved FantasyPros identity "
                "but no usable ECR/projection at that position (e.g. K/D-ST); classification then relies "
                "on ESPN zero-projection/non-starter facts only, with no fabricated FP delta.",
            ],
        }
    except Exception as e:
        return _error_response("ranking waiver targets", e)

@mcp.tool()
@_with_fp_parsed_cache_scope
async def analyze_my_team(league_id: int, team_id: int, year: int = None) -> dict:
    """League-relative roster analysis: how strong is this team relative to
    every other real team in this specific league, where are its meaningful
    weaknesses/surpluses, which players are core vs expendable, and what
    should the manager prioritize next. Builds one ESPN league fetch and
    iterates all teams locally (zero extra ESPN calls). Uses cached
    FantasyPros data only - zero live FantasyPros API calls, ever. Lineup
    placement and all cross-team comparisons use FantasyPros season
    projections on BOTH sides - never ESPN-vs-FP blended. A team/metric
    with any missing but lineup-relevant FantasyPros projection returns
    rank_status="insufficient_data" rather than being silently compared
    against complete-data teams.

    Args:
        league_id: The ESPN fantasy football league ID
        team_id: The team ID to analyze (real ESPN team_id, not list position)
        year: Optional year (defaults to current season if omitted)
    """
    try:
        league_id_val, league_err = _validate_bounded_int(league_id, "league_id", 1, 999_999_999, league_id)
        if league_err:
            return {"error": "invalid_parameter", "message": league_err}
        team_id_val, team_err = _validate_bounded_int(team_id, "team_id", 1, 999, team_id)
        if team_err:
            return {"error": "invalid_parameter", "message": team_err}

        resolved_year = _resolve_year(year)
        payload = _fetch_snapshot_payload(league_id_val, resolved_year)
        settings_result = build_league_settings(payload, league_id_val, resolved_year)
        slot_counts = settings_result.get("roster_slot_counts", {})
        scoring_bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))
        espn_snapshot = build_espn_league_snapshot_from_payload(
            payload, league_id_val, resolved_year, slot_counts, scoring_bucket)

        positions_in_scope = fp_client.CORE_POSITIONS
        cache_warnings = _check_required_fp_caches(positions_in_scope, scoring_bucket)
        if cache_warnings:
            return {"error": "cache_incomplete", "scoring_bucket_detected": scoring_bucket,
                     "message": f"Required FantasyPros cache data is missing for {scoring_bucket}. "
                                "No live FantasyPros calls were made.",
                     "warnings": cache_warnings, "next_step": "Call refresh_fantasypros_cache() then retry."}

        data_freshness = fp_client.get_cache_freshness_report(positions_in_scope, scoring_bucket)
        freshness_warnings = [f"{k} is stale (age {v['age_seconds']}s, ttl {v['ttl_seconds']}s)"
                                for k, v in data_freshness.items() if v.get("is_stale")]

        target_team = next((team for team in espn_snapshot.teams if team.team_id == team_id_val), None)
        if target_team is None:
            valid_ids = sorted(team.team_id for team in espn_snapshot.teams)
            return {"error": "invalid_parameter",
                     "message": f"No team with team_id={team_id_val} in this league. Valid team_ids: {valid_ids}"}

        snapshots = {}
        for team in espn_snapshot.teams:
            roster = [{
                "name": p.name, "position": p.position,
                "proTeam": p.pro_team,
                "projected_points": p.season_projected_points,
                "points": p.season_total_points,
            } for p in team.roster]
            for p in roster:
                p["_fp_intel"] = fp_client.build_player_intelligence(
                    p.get("name"), p.get("proTeam"), p.get("position"), scoring=scoring_bucket)
            roster_fp_rows, fp_reliability_warning = _build_fp_eval_roster(roster)
            lineup_espn = _assign_best_lineup(roster, slot_counts, value_field="projected_points")
            lineup_fp = _assign_best_lineup(roster_fp_rows, slot_counts, value_field="_fp_eval_value")
            snapshots[team.team_id] = {
                "team_id": team.team_id, "team_name": team.team_name,
                "roster": roster, "roster_fp_rows": roster_fp_rows,
                "lineup_espn": lineup_espn, "lineup_fp": lineup_fp,
                "fp_reliability_warning": fp_reliability_warning,
            }

        target_snapshot = snapshots[team_id_val]

        core_offense_by_team = {tid: _core_offense_projection(s["lineup_fp"], s["roster_fp_rows"], slot_counts)
                                  for tid, s in snapshots.items()}
        core_offense_values = {tid: (m["known_projection_total"] if m["coverage_complete"] else None)
                                 for tid, m in core_offense_by_team.items()}
        core_offense_rank = _rank_across_league(core_offense_values, higher_is_better=True)
        target_core = core_offense_by_team[team_id_val]
        target_core_rank_info = core_offense_rank["per_team"][team_id_val]

        bench_by_team = {tid: _bench_depth_metrics(s["lineup_fp"], slot_counts) for tid, s in snapshots.items()}
        bench_values = {tid: (m["lineup_relevant_bench_projection_total"] if m["coverage_complete"] else None)
                          for tid, m in bench_by_team.items()}
        bench_rank = _rank_across_league(bench_values, higher_is_better=True)
        target_bench = bench_by_team[team_id_val]
        target_bench_rank_info = bench_rank["per_team"][team_id_val]

        position_analysis = {}
        for position in ("QB", "RB", "WR", "TE", "FLEX"):
            per_team_pos = {tid: _analyze_position_strength(position, s, slot_counts) for tid, s in snapshots.items()}

            starter_values = {tid: (m["starter_projection_total_known"] if m["starter_coverage_complete"] else None)
                                for tid, m in per_team_pos.items()}
            starter_rank = _rank_across_league(starter_values, higher_is_better=True)

            depth_values = {tid: (m["bench_projection_total_known"]
                                    if all(b["fp_projection"] is not None for b in m["bench_depth"]) else None)
                              for tid, m in per_team_pos.items()}
            depth_rank = _rank_across_league(depth_values, higher_is_better=True)

            target_pos = per_team_pos[team_id_val]
            rank_info = starter_rank["per_team"][team_id_val]
            depth_info = depth_rank["per_team"][team_id_val]
            target_pos["league_rank"] = rank_info["rank"]
            target_pos["league_size"] = starter_rank["league_size"]
            target_pos["ranked_team_count"] = starter_rank["ranked_team_count"]
            target_pos["percentile"] = rank_info["percentile"]
            target_pos["gap_to_median"] = rank_info["gap_to_median"]
            target_pos["gap_to_leader"] = rank_info["gap_to_leader"]
            target_pos["relative_label"] = _relative_label(rank_info["rank"], starter_rank["ranked_team_count"])
            target_pos["depth_label"] = _relative_label(depth_info["rank"], depth_rank["ranked_team_count"])
            target_pos["starter_ranking_coverage_pct"] = starter_rank["coverage_pct"]
            target_pos["depth_ranking_coverage_pct"] = depth_rank["coverage_pct"]
            target_pos["depth_ranked_team_count"] = depth_rank["ranked_team_count"]
            position_analysis[position] = target_pos

        core_asset_list = _identify_core_assets(target_snapshot, slot_counts)
        core_asset_names = {c["player"] for c in core_asset_list}
        expendable_list = _identify_expendable_assets(target_snapshot, slot_counts, core_asset_names)
        trade_surplus_list = _identify_trade_surplus(target_snapshot, position_analysis, slot_counts)
        positional_needs_list = _identify_positional_needs(position_analysis, target_snapshot, slot_counts)

        starter_names_target = {p.get("name") for p in _flatten_starters(target_snapshot["lineup_fp"])}
        injury_risks = []
        for p in target_snapshot["roster_fp_rows"]:
            fp_intel = p.get("_fp_intel") or {}
            signal = fp_client.classify_injury_signal(fp_intel.get("injury_status"))
            is_data_concern = fp_intel.get("match_confidence") in ("none", "ambiguous")
            if signal["label"] in ("materially_reduced", "caution") or is_data_concern:
                injury_risks.append({
                    "player": p.get("name"), "position": p.get("position"),
                    "lineup_role": "starter_or_flex" if p.get("name") in starter_names_target else "bench",
                    "injury_label": signal["label"], "note": signal["note"],
                    "data_quality_concern": is_data_concern, "match_confidence": fp_intel.get("match_confidence"),
                })

        severity_order = {"urgent": 0, "meaningful": 1, "minor": 2, "unknown": 3, "none": 4}
        waiver_priority_positions = [n["position"] for n in
            sorted(positional_needs_list, key=lambda n: severity_order.get(n["severity"], 5))
            if n["severity"] in ("urgent", "meaningful")]

        priorities = []
        for n in positional_needs_list:
            if n["severity"] == "urgent":
                priorities.append(f"Address {n['position']} urgently: {n['evidence']}")
        for n in positional_needs_list:
            if n["severity"] == "meaningful":
                priorities.append(f"Improve {n['position']}: {n['evidence']}")
        for s in trade_surplus_list:
            top_candidate = s["surplus_candidates"][0]["player"] if s["surplus_candidates"] else None
            if top_candidate:
                priorities.append(f"Consider shopping surplus at {s['position']}: "
                                    f"{top_candidate} is a trade_surplus_candidate.")
        for r in injury_risks:
            if r["injury_label"] == "materially_reduced":
                priorities.append(f"Monitor {r['player']} ({r['position']}) - {r['note']}")
        strong_positions = [pos for pos in ("QB", "RB", "WR", "TE", "FLEX")
                             if position_analysis[pos].get("relative_label") == "strong"]
        if strong_positions:
            priorities.append(f"Hold strength at {', '.join(strong_positions)} - ranked strong league-wide.")
        recommended_priorities = priorities[:5]

        league_size_val = len(espn_snapshot.teams)

        def _fmt_rank_fragment(label, rank, ranked_team_count, coverage_pct):
            """Makes a reduced ranked pool impossible to mistake for the
            whole league - e.g. never lets '7/7' read as 7th place in a
            7-team league when league_size is actually 12."""
            if rank is None:
                return f"{label} rank unavailable (insufficient FantasyPros coverage)"
            return (f"{label} {rank}/{ranked_team_count} ranked teams "
                    f"({ranked_team_count} of {league_size_val} covered; {coverage_pct}%)")

        summary_parts = [
            _fmt_rank_fragment("Starting lineup", target_core_rank_info["rank"],
                                core_offense_rank["ranked_team_count"], core_offense_rank["coverage_pct"]),
        ]
        for pos in ("QB", "RB", "WR", "TE", "FLEX"):
            pa = position_analysis[pos]
            summary_parts.append(_fmt_rank_fragment(pos, pa["league_rank"], pa["ranked_team_count"],
                                                      pa["starter_ranking_coverage_pct"]))
        summary_parts.append(_fmt_rank_fragment("Bench depth", target_bench_rank_info["rank"],
                                                   bench_rank["ranked_team_count"], bench_rank["coverage_pct"]))
        summary_basis = "; ".join(summary_parts) + "."

        league_outlook = {
            "starting_lineup_rank": target_core_rank_info["rank"],
            "starting_lineup_rank_status": "ranked" if target_core["coverage_complete"] else "insufficient_data",
            "bench_depth_rank": target_bench_rank_info["rank"],
            "bench_depth_rank_status": "ranked" if target_bench["coverage_complete"] else "insufficient_data",
            "qb_rank": position_analysis["QB"]["league_rank"],
            "rb_rank": position_analysis["RB"]["league_rank"],
            "wr_rank": position_analysis["WR"]["league_rank"],
            "te_rank": position_analysis["TE"]["league_rank"],
            "flex_rank": position_analysis["FLEX"]["league_rank"],
            "summary_basis": summary_basis,
        }

        warnings_out = list(freshness_warnings)
        if target_snapshot["fp_reliability_warning"]:
            warnings_out.append(target_snapshot["fp_reliability_warning"])
        if not target_core["coverage_complete"]:
            warnings_out.append("Starting lineup core-offense projection is incomplete - one or more "
                                  "core-position players (starter or lineup-contending bench) lack a "
                                  "FantasyPros projection; league_rank withheld.")
        if not target_bench["coverage_complete"]:
            warnings_out.append("Bench-depth projection is incomplete for lineup-relevant positions; "
                                  "league_rank withheld.")
        for pos in ("QB", "RB", "WR", "TE", "FLEX"):
            if not position_analysis[pos].get("starter_coverage_complete", True):
                warnings_out.append(f"{pos} starter/bench coverage incomplete; league_rank withheld for this position.")

        return {
            "league_id": league_id_val, "team_id": team_id_val, "team_name": target_team.team_name,
            "year": resolved_year, "scoring_bucket": scoring_bucket, "league_size": len(espn_snapshot.teams),
            "league_outlook": league_outlook,
            "starting_lineup": {
                "fp_value_lineup": target_snapshot["lineup_fp"],
                "known_projection_total": target_core["known_projection_total"],
                "missing_projection_players": target_core["missing_projection_players"],
                "coverage_complete": target_core["coverage_complete"],
                "league_rank": target_core_rank_info["rank"], "rank_status": league_outlook["starting_lineup_rank_status"],
                "ranked_team_count": core_offense_rank["ranked_team_count"], "league_size": len(espn_snapshot.teams),
                "ranking_coverage_pct": core_offense_rank["coverage_pct"],
                "league_median": core_offense_rank["median"], "gap_to_median": target_core_rank_info["gap_to_median"],
                "gap_to_leader": target_core_rank_info["gap_to_leader"],
            },
            "bench_analysis": {
                "bench_asset_projection_total": target_bench["bench_asset_projection_total"],
                "lineup_relevant_bench_projection_total": target_bench["lineup_relevant_bench_projection_total"],
                "valued_bench_count": target_bench["valued_bench_count"],
                "unvalued_bench_count": target_bench["unvalued_bench_count"],
                "starter_or_flex_caliber_count": target_bench["starter_or_flex_caliber_count"],
                "coverage_complete": target_bench["coverage_complete"],
                "league_rank": target_bench_rank_info["rank"], "rank_status": league_outlook["bench_depth_rank_status"],
                "ranked_team_count": bench_rank["ranked_team_count"], "league_size": len(espn_snapshot.teams),
                "ranking_coverage_pct": bench_rank["coverage_pct"], "league_median": bench_rank["median"],
            },
            "position_analysis": position_analysis,
            "core_assets": core_asset_list,
            "expendable_assets": expendable_list,
            "trade_surplus": trade_surplus_list,
            "positional_needs": positional_needs_list,
            "injury_risks": injury_risks,
            "waiver_priority_positions": waiver_priority_positions,
            "recommended_priorities": recommended_priorities,
            "data_freshness": data_freshness,
            "warnings": warnings_out,
            "methodology_notes": [
                "core_offense_projection excludes K/D-ST - FantasyPros has no comparable projections for those positions.",
                "Coverage for every ranked metric is gated on ALL relevant players (starters AND lineup-contending "
                "bench) - a single missing FantasyPros projection withholds that league_rank rather than silently "
                "treating the missing value as zero or comparing a partial total against complete-data teams.",
                "Bench-depth ranking uses lineup_relevant_bench_projection_total, not raw bench asset value - a "
                "backup QB in a single-QB, no-superflex league contributes $0 to the ranked bench metric even "
                "though its raw asset value is separately reported in bench_asset_projection_total.",
                "FLEX strength is the literal post-direct-slot leftover engine output from _assign_best_lineup, "
                "never a raw RB+WR+TE count.",
                "Core-asset classification uses the FantasyPros positional rank field (pos_rank), never raw overall "
                "ECR, and applies a removal-simulation near-equivalent-replacement check to starters as well as "
                "bench players before declaring a player irreplaceable.",
                "ownership_anomaly_band is never applied to rostered players - that helper only describes "
                "free-agent availability anomalies.",
                "No composite numeric or categorical team-strength score is produced in this version - "
                "league_outlook returns transparent component ranks only.",
                "League ranks only include teams with complete FantasyPros coverage for that specific "
                "metric. ranked_team_count (and ranking_coverage_pct) may therefore be smaller than "
                "league_size - e.g. '7/7 ranked teams' means 7th among 7 teams with complete coverage, "
                "NOT 7th place in a 7-team league.",
                "A position with exactly one direct starting slot and no real flex/OP route for that "
                "position (e.g. QB in most standard leagues) uses a single-starter guard for positional "
                "need severity: only starter viability/health drives severity there, never backup "
                "quantity. A league with 2+ direct starters or a real superflex/OP slot is not subject "
                "to this guard and uses standard depth-aware severity logic instead.",
            ],
        }
    except Exception as e:
        return _error_response("analyzing team", e)

@mcp.tool()
@_with_fp_parsed_cache_scope
async def evaluate_trade(league_id: int, team_id: int, players_out: list, players_in: list,
                          year: int = None) -> dict:
    """Evaluates a proposed roster-to-roster trade from team_id's perspective:
    does it improve the team, and why. Distinguishes market/asset value from
    team-specific fit, starting-lineup impact, bench/depth impact, positional
    need impact, injury risk, and roster-construction feasibility. Never
    checks free-agent availability - both sides must be real rostered players
    in this league. Supports 1-3 players out and 1-3 players in; rejects
    multi-team proposals. Uses cached FantasyPros data only - zero live calls.
    Reuses the frozen lineup/ranking/need-classification infrastructure
    behind analyze_my_team and rank_waiver_targets without modifying either.
    Issues exactly ONE ESPN league fetch.

    Args:
        league_id: The ESPN fantasy football league ID
        team_id: The team ID proposing/evaluating the trade (real ESPN team_id)
        players_out: List of 1-3 player names currently on team_id's roster
        players_in: List of 1-3 player names currently on ONE other roster in this league
        year: Optional year (defaults to current season if omitted)
    """
    try:
        league_id_val, league_err = _validate_bounded_int(league_id, "league_id", 1, 999_999_999, league_id)
        if league_err:
            return {"error": "invalid_parameter", "message": league_err}
        team_id_val, team_err = _validate_bounded_int(team_id, "team_id", 1, 999, team_id)
        if team_err:
            return {"error": "invalid_parameter", "message": team_err}

        resolved_year = _resolve_year(year)
        payload = _fetch_snapshot_payload(league_id_val, resolved_year)
        settings_result = build_league_settings(payload, league_id_val, resolved_year)
        slot_counts = settings_result.get("roster_slot_counts", {})
        scoring_bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))
        espn_snapshot = build_espn_league_snapshot_from_payload(
            payload, league_id_val, resolved_year, slot_counts, scoring_bucket)

        positions_in_scope = fp_client.CORE_POSITIONS
        cache_warnings = _check_required_fp_caches(positions_in_scope, scoring_bucket)
        if cache_warnings:
            return {"error": "cache_incomplete", "scoring_bucket_detected": scoring_bucket,
                     "message": f"Required FantasyPros cache data is missing for {scoring_bucket}. "
                                 "No live FantasyPros calls were made.",
                     "warnings": cache_warnings, "next_step": "Call refresh_fantasypros_cache() then retry."}

        data_freshness = fp_client.get_cache_freshness_report(positions_in_scope, scoring_bucket)
        freshness_warnings = [f"{k} is stale (age {v['age_seconds']}s, ttl {v['ttl_seconds']}s)"
                                for k, v in data_freshness.items() if v.get("is_stale")]

        resolved = _resolve_trade_players(espn_snapshot, team_id_val, players_out, players_in)
        if "error" in resolved:
            return resolved

        target_team = resolved["target_team"]
        target_roster_before = resolved["target_roster_before"]
        players_out_resolved_bare = resolved["players_out_resolved"]
        players_in_resolved_bare = resolved["players_in_resolved"]
        partner_team = resolved["partner_team"]
        partner_team_id = resolved["partner_team_id"]
        players_out_names = {p["name"] for p in players_out_resolved_bare}

        target_roster_after = _simulate_trade_roster(target_roster_before, players_out_resolved_bare, players_in_resolved_bare)
        roster_size_check = _check_roster_size_limit(target_roster_after, target_roster_before, players_out_names, slot_counts)

        target_snapshot_before = _build_snapshot_from_roster(target_roster_before, slot_counts, scoring_bucket,
                                                                 target_team.team_id, target_team.team_name)
        target_snapshot_after = _build_snapshot_from_roster(target_roster_after, slot_counts, scoring_bucket,
                                                                target_team.team_id, target_team.team_name)

        lineup_feasible = target_snapshot_after["lineup_fp"]["feasible"]
        lineup_gaps_after = [g["slot"] for g in target_snapshot_after["lineup_fp"]["gaps"]]
        core_gaps = [g for g in lineup_gaps_after if g in ("QB", "RB", "WR", "TE")]
        if not lineup_gaps_after:
            roster_construction_severity = "none"
        elif core_gaps and roster_size_check["open_roster_spots_after"] == 0:
            roster_construction_severity = "major"
        elif core_gaps:
            roster_construction_severity = "moderate"
        else:
            roster_construction_severity = "minor"

        roster_legality = {
            **roster_size_check,
            "lineup_feasible": lineup_feasible,
            "lineup_gaps_after": lineup_gaps_after,
            "roster_construction_risk": bool(lineup_gaps_after),
            "roster_construction_severity": roster_construction_severity,
        }

        before_by_name = {p["name"]: p for p in target_snapshot_before["roster_fp_rows"]}
        after_by_name = {p["name"]: p for p in target_snapshot_after["roster_fp_rows"]}
        players_out_resolved = [before_by_name[p["name"]] for p in players_out_resolved_bare]
        players_in_resolved = [after_by_name[p["name"]] for p in players_in_resolved_bare]

        partner_roster_before = resolved["partner_roster_before"]
        partner_roster_after = _simulate_trade_roster(partner_roster_before, players_in_resolved_bare, players_out_resolved_bare)
        partner_size_check = _check_roster_size_limit(
            partner_roster_after, partner_roster_before, {p["name"] for p in players_in_resolved_bare}, slot_counts)
        partner_snapshot_before = _build_snapshot_from_roster(partner_roster_before, slot_counts, scoring_bucket,
                                                                  partner_team.team_id, partner_team.team_name)
        partner_snapshot_after = _build_snapshot_from_roster(partner_roster_after, slot_counts, scoring_bucket,
                                                                 partner_team.team_id, partner_team.team_name)

        static_teams = [t for t in espn_snapshot.teams if t.team_id not in (team_id_val, partner_team_id)]
        static_snapshots = {t.team_id: _build_team_snapshot(t, slot_counts, scoring_bucket) for t in static_teams}

        def league_relative(metric_fn):
            static_values = {tid: metric_fn(snap) for tid, snap in static_snapshots.items()}
            values_before = {**static_values, partner_team_id: metric_fn(partner_snapshot_before),
                               team_id_val: metric_fn(target_snapshot_before)}
            values_after = {**static_values, partner_team_id: metric_fn(partner_snapshot_after),
                              team_id_val: metric_fn(target_snapshot_after)}
            return (_rank_across_league(values_before, higher_is_better=True),
                    _rank_across_league(values_after, higher_is_better=True))

        def _pack_relative(rank_before, rank_after, tid):
            return {"before_rank": rank_before["per_team"][tid]["rank"], "after_rank": rank_after["per_team"][tid]["rank"],
                     "before_ranked_team_count": rank_before["ranked_team_count"], "after_ranked_team_count": rank_after["ranked_team_count"],
                     "before_coverage_pct": rank_before["coverage_pct"], "after_coverage_pct": rank_after["coverage_pct"]}

        def core_offense_value(snap):
            m = _core_offense_projection(snap["lineup_fp"], snap["roster_fp_rows"], slot_counts)
            return m["known_projection_total"] if m["coverage_complete"] else None

        def bench_value(snap):
            m = _bench_depth_metrics(snap["lineup_fp"], slot_counts)
            return m["lineup_relevant_bench_projection_total"] if m["coverage_complete"] else None

        def position_starter_value(pos, snap):
            m = _analyze_position_strength(pos, snap, slot_counts)
            return m["starter_projection_total_known"] if m["starter_coverage_complete"] else None

        def position_depth_value(pos, snap):
            m = _analyze_position_strength(pos, snap, slot_counts)
            return m["bench_projection_total_known"] if all(b["fp_projection"] is not None for b in m["bench_depth"]) else None

        core_rank_before, core_rank_after = league_relative(core_offense_value)
        bench_rank_before, bench_rank_after = league_relative(bench_value)
        league_relative_changes = {
            "starting_lineup": _pack_relative(core_rank_before, core_rank_after, team_id_val),
            "bench_depth": _pack_relative(bench_rank_before, bench_rank_after, team_id_val),
        }

        position_analysis_before, position_analysis_after = {}, {}
        for pos in ("QB", "RB", "WR", "TE"):
            starter_rank_before, starter_rank_after = league_relative(lambda snap, pos=pos: position_starter_value(pos, snap))
            depth_rank_before, depth_rank_after = league_relative(lambda snap, pos=pos: position_depth_value(pos, snap))
            league_relative_changes[pos] = {
                **_pack_relative(starter_rank_before, starter_rank_after, team_id_val),
                "depth_before_rank": depth_rank_before["per_team"][team_id_val]["rank"],
                "depth_after_rank": depth_rank_after["per_team"][team_id_val]["rank"],
                "depth_before_ranked_team_count": depth_rank_before["ranked_team_count"],
                "depth_after_ranked_team_count": depth_rank_after["ranked_team_count"],
            }

            pa_before = _analyze_position_strength(pos, target_snapshot_before, slot_counts)
            pa_after = _analyze_position_strength(pos, target_snapshot_after, slot_counts)
            pa_before["league_rank"] = starter_rank_before["per_team"][team_id_val]["rank"]
            pa_before["ranked_team_count"] = starter_rank_before["ranked_team_count"]
            pa_before["relative_label"] = _relative_label(pa_before["league_rank"], pa_before["ranked_team_count"])
            pa_before["depth_league_rank"] = depth_rank_before["per_team"][team_id_val]["rank"]
            pa_before["depth_ranked_team_count"] = depth_rank_before["ranked_team_count"]
            pa_before["depth_label"] = _relative_label(pa_before["depth_league_rank"], pa_before["depth_ranked_team_count"])

            pa_after["league_rank"] = starter_rank_after["per_team"][team_id_val]["rank"]
            pa_after["ranked_team_count"] = starter_rank_after["ranked_team_count"]
            pa_after["relative_label"] = _relative_label(pa_after["league_rank"], pa_after["ranked_team_count"])
            pa_after["depth_league_rank"] = depth_rank_after["per_team"][team_id_val]["rank"]
            pa_after["depth_ranked_team_count"] = depth_rank_after["ranked_team_count"]
            pa_after["depth_label"] = _relative_label(pa_after["depth_league_rank"], pa_after["depth_ranked_team_count"])

            position_analysis_before[pos] = pa_before
            position_analysis_after[pos] = pa_after

        flex_starter_rank_before, flex_starter_rank_after = league_relative(lambda snap: position_starter_value("FLEX", snap))
        league_relative_changes["FLEX"] = _pack_relative(flex_starter_rank_before, flex_starter_rank_after, team_id_val)

        needs_before = _identify_positional_needs(position_analysis_before, target_snapshot_before, slot_counts)
        needs_after = _identify_positional_needs(position_analysis_after, target_snapshot_after, slot_counts)
        positional_need_changes = _diff_positional_needs(needs_before, needs_after)
        need_overall = _aggregate_need_overall(positional_need_changes)

        partner_fit = _assess_partner_fit(partner_team, players_out_resolved_bare, players_in_resolved_bare,
                                            partner_snapshot_before, partner_snapshot_after, partner_size_check)

        core_offense_before_metric = _core_offense_projection(target_snapshot_before["lineup_fp"], target_snapshot_before["roster_fp_rows"], slot_counts)
        core_offense_after_metric = _core_offense_projection(target_snapshot_after["lineup_fp"], target_snapshot_after["roster_fp_rows"], slot_counts)

        market_value = _compare_market_value(players_out_resolved, players_in_resolved)
        lineup_impact = _compare_lineups(target_snapshot_before["lineup_fp"], target_snapshot_after["lineup_fp"],
                                           core_offense_before_metric, core_offense_after_metric)
        depth_impact = _compare_depth(target_snapshot_before, target_snapshot_after, slot_counts, roster_size_check)

        injury_risk = _compare_injury_risk(players_out_resolved, players_in_resolved)
        incoming_materially_reduced = any(
            fp_client.classify_injury_signal(
                p["_fp_intel"].get("injury_status") or p.get("espn_injury_status"))["label"] == "materially_reduced"
            for p in players_in_resolved)

        before_slot_map = _build_slot_map(target_snapshot_before["lineup_fp"])
        after_slot_map = _build_slot_map(target_snapshot_after["lineup_fp"])
        bye_week_impact = _assess_bye_week_impact(before_slot_map, after_slot_map, before_by_name, after_by_name)

        verdict, verdict_reasons = _compute_trade_verdict(
            lineup_impact, market_value, depth_impact, need_overall, injury_risk,
            roster_legality, roster_construction_severity, incoming_materially_reduced,
            league_relative_changes, team_id_val)

        best_case, worst_case = _describe_best_worst_case(
            lineup_impact, market_value, positional_need_changes, depth_impact, injury_risk, roster_legality)

        league_size = len(espn_snapshot.teams)
        league_relative_incomplete_coverage = {}
        target_excluded_from_metric = {}
        for metric_name in ("starting_lineup", "bench_depth"):
            vals = league_relative_changes[metric_name]
            league_relative_incomplete_coverage[metric_name] = {
                "before": vals["before_ranked_team_count"] < league_size,
                "after": vals["after_ranked_team_count"] < league_size,
            }
            target_excluded_from_metric[metric_name] = {"before": vals["before_rank"] is None, "after": vals["after_rank"] is None}
        for pos in ("QB", "RB", "WR", "TE"):
            vals = league_relative_changes[pos]
            league_relative_incomplete_coverage[pos] = {
                "starter_before": vals["before_ranked_team_count"] < league_size,
                "starter_after": vals["after_ranked_team_count"] < league_size,
                "depth_before": vals["depth_before_ranked_team_count"] < league_size,
                "depth_after": vals["depth_after_ranked_team_count"] < league_size,
            }
            target_excluded_from_metric[pos] = {
                "starter_before": vals["before_rank"] is None, "starter_after": vals["after_rank"] is None,
                "depth_before": vals["depth_before_rank"] is None, "depth_after": vals["depth_after_rank"] is None,
            }
        flex_vals = league_relative_changes["FLEX"]
        league_relative_incomplete_coverage["FLEX"] = {
            "starter_before": flex_vals["before_ranked_team_count"] < league_size,
            "starter_after": flex_vals["after_ranked_team_count"] < league_size,
        }
        target_excluded_from_metric["FLEX"] = {
            "starter_before": flex_vals["before_rank"] is None, "starter_after": flex_vals["after_rank"] is None,
        }

        data_quality = {
            "market_projection_coverage_complete": market_value["projection_value_signal"]["coverage_complete"],
            "market_assessment_evaluable": market_value["assessment"] != "insufficient_data",
            "market_signal_coverage": market_value["signal_coverage"],
            "lineup_value_complete": lineup_impact["coverage_complete"],
            "depth_value_complete": depth_impact["coverage_complete"],
            "players_with_missing_fp_projection": sorted(
                p["name"] for p in players_out_resolved + players_in_resolved
                if p["_fp_intel"].get("projected_points") is None),
            "players_with_low_or_unresolved_fp_match": sorted(
                p["name"] for p in players_out_resolved + players_in_resolved
                if p["_fp_intel"].get("match_confidence") in ("low", "ambiguous", "none")),
            "league_relative_incomplete_coverage": league_relative_incomplete_coverage,
            "target_excluded_from_metric": target_excluded_from_metric,
        }

        warnings_out = list(freshness_warnings)
        if target_snapshot_before["fp_reliability_warning"]:
            warnings_out.append(f"[target before] {target_snapshot_before['fp_reliability_warning']}")
        if target_snapshot_after["fp_reliability_warning"]:
            warnings_out.append(f"[target after] {target_snapshot_after['fp_reliability_warning']}")
        for w in partner_fit.get("fp_reliability_warnings", []):
            warnings_out.append(f"[partner] {w}")
        if roster_construction_severity != "none":
            warnings_out.append(f"Post-trade lineup has a {roster_construction_severity} construction gap at "
                                  f"{', '.join(lineup_gaps_after)} - a follow-up roster move would be required; "
                                  f"no replacement has been assumed.")

        return {
            "league_id": league_id_val, "team_id": team_id_val, "team_name": target_team.team_name,
            "partner_team_id": partner_fit["partner_team_id"], "partner_team_name": partner_fit["partner_team_name"],
            "year": resolved_year, "scoring_bucket": scoring_bucket,
            "trade": {"players_out": [p["name"] for p in players_out_resolved_bare],
                       "players_in": [p["name"] for p in players_in_resolved_bare]},
            "roster_legality": roster_legality,
            "market_value": market_value,
            "lineup_impact": lineup_impact,
            "depth_impact": depth_impact,
            "positional_need_changes": positional_need_changes, "need_overall": need_overall,
            "league_relative_changes": league_relative_changes,
            "injury_risk": injury_risk,
            "bye_week_impact": bye_week_impact,
            "partner_fit": partner_fit,
            "verdict": verdict, "verdict_reasons": verdict_reasons,
            "best_case": best_case, "worst_case": worst_case,
            "data_quality": data_quality,
            "data_freshness": data_freshness, "warnings": warnings_out,
            "methodology_notes": [
                "size_feasible/modeled_transaction_size_feasible describe fit against modeled active roster "
                "capacity only - never a claim about whether ESPN itself would accept or reject the transaction.",
                "A post-trade lineup gap (lineup_feasible=false) never forces DECLINE by itself - it is surfaced "
                "as roster_construction_risk/roster_construction_severity and weighed as evidence.",
                "Market value never derives its assessment from a projection sum alone - up to 4 comparable "
                "market signals are vote-counted, each requiring FULL package coverage independently before "
                "it can vote; best_asset_side requires full tier coverage on both sides for unequal trades.",
                "Lineup impact is derived from full before/after slot maps, not name-set differences - a player "
                "moving between direct and FLEX slots is traced explicitly via players_changing_slots.",
                "Positional-need aggregation preserves 'unknown' explicitly; unknown contributes no directional "
                "signal to the verdict ladder. 'mixed' is preserved explicitly and contributes no net directional "
                "need signal - neither state is silently reclassified as neutral in the output, only in the "
                "internal ladder-routing step.",
                "Roster-size validation treats IR as conditional capacity: existing legitimate IR occupants are "
                "preserved; incoming players are never assumed IR-eligible regardless of injury status.",
                "QB/RB/WR/TE receive independently-computed starter and depth league ranks; FLEX receives a "
                "starter rank only - the frozen positional-strength helper has no bench set for FLEX, so no "
                "FLEX depth metric is fabricated.",
                "partner_fit simulates the partner's own before/after roster but is a roster-only realism signal, "
                "never a second full trade verdict, never a prediction that the partner will accept. Missing "
                "FantasyPros tier data at a position never behaves like replacement-level talent - it withholds "
                "the thin/redundant/major-hole claim and returns logical_fit=unknown instead.",
                "Bye-week impact is context only and never affects the verdict tier.",
                "This tool issues exactly one project-owned ESPN league snapshot request; settings and rosters "
                "are parsed from that same raw payload.",
            ],
        }
    except Exception as e:
        return _error_response("evaluating trade", e)


@mcp.tool()
@_with_fp_parsed_cache_scope
async def find_trade_targets(league_id: int, team_id: int, position: str = None, partner_team_id: int = None,
                              limit: int = 10, max_package_size: int = 2, year: int = None) -> dict:
    """Identifies realistic trade targets on actual opposing rosters that
    would improve team_id's fantasy outlook. Never searches free agents/
    waivers. Never calls the public evaluate_trade tool internally - it
    reuses the exact frozen trade-evidence helpers directly (parity is
    verified by a dedicated regression test) to avoid repeated ESPN
    fetches and re-enrichment. Bounded, budgeted search - not exhaustive;
    see search_summary for exact coverage. Uses cached FantasyPros data
    only - zero live calls. Issues exactly ONE project-owned ESPN league snapshot request.

    Args:
        league_id: The ESPN fantasy football league ID
        team_id: The team ID to find trade targets for (real ESPN team_id)
        position: Optional incoming-position filter: QB, RB, WR, or TE only
        partner_team_id: Optional restriction to one opposing team (real ESPN team_id)
        limit: Max recommended trade targets to return (bounded 1-20, default 10)
        max_package_size: Max players per side of a package (bounded 1-3, default 2)
        year: Optional year (defaults to current season if omitted)
    """
    try:
        league_id_val, league_err = _validate_bounded_int(league_id, "league_id", 1, 999_999_999, league_id)
        if league_err:
            return {"error": "invalid_parameter", "message": league_err}
        team_id_val, team_err = _validate_bounded_int(team_id, "team_id", 1, 999, team_id)
        if team_err:
            return {"error": "invalid_parameter", "message": team_err}
        limit_val, limit_err = _validate_bounded_int(limit, "limit", 1, 20, 10)
        if limit_err:
            return {"error": "invalid_parameter", "message": limit_err}
        max_pkg_val, max_pkg_err = _validate_bounded_int(max_package_size, "max_package_size", 1, 3, 2)
        if max_pkg_err:
            return {"error": "invalid_parameter", "message": max_pkg_err}
        pos_err = _ftt_validate_position_filter(position)
        if pos_err:
            return {"error": "invalid_parameter", "message": pos_err}
        position_filter = position.upper() if position else None

        partner_team_id_val = None
        if partner_team_id is not None:
            partner_team_id_val, partner_err = _validate_bounded_int(partner_team_id, "partner_team_id", 1, 999, partner_team_id)
            if partner_err:
                return {"error": "invalid_parameter", "message": partner_err}
            if partner_team_id_val == team_id_val:
                return {"error": "invalid_parameter", "message": "partner_team_id cannot equal team_id."}

        resolved_year = _resolve_year(year)
        payload = _fetch_snapshot_payload(league_id_val, resolved_year)
        settings_result = build_league_settings(payload, league_id_val, resolved_year)
        slot_counts = settings_result.get("roster_slot_counts", {})
        scoring_bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))
        espn_snapshot = build_espn_league_snapshot_from_payload(
            payload, league_id_val, resolved_year, slot_counts, scoring_bucket)

        positions_in_scope = fp_client.CORE_POSITIONS
        cache_warnings = _check_required_fp_caches(positions_in_scope, scoring_bucket)
        if cache_warnings:
            return {"error": "cache_incomplete", "scoring_bucket_detected": scoring_bucket,
                     "message": f"Required FantasyPros cache data is missing for {scoring_bucket}. "
                                 "No live FantasyPros calls were made.",
                     "warnings": cache_warnings, "next_step": "Call refresh_fantasypros_cache() then retry."}

        data_freshness = fp_client.get_cache_freshness_report(positions_in_scope, scoring_bucket)
        freshness_warnings = [f"{k} is stale (age {v['age_seconds']}s, ttl {v['ttl_seconds']}s)"
                                for k, v in data_freshness.items() if v.get("is_stale")]

        target_team = _find_team_by_id(espn_snapshot, team_id_val)
        if target_team is None:
            valid_ids = sorted(t.team_id for t in espn_snapshot.teams)
            return {"error": "invalid_parameter",
                     "message": f"No team with team_id={team_id_val} in this league. Valid team_ids: {valid_ids}"}
        if partner_team_id_val is not None:
            partner_team_obj = _find_team_by_id(espn_snapshot, partner_team_id_val)
            if partner_team_obj is None:
                valid_ids = sorted(t.team_id for t in espn_snapshot.teams)
                return {"error": "invalid_parameter",
                         "message": f"No team with team_id={partner_team_id_val} in this league. Valid team_ids: {valid_ids}"}

        baseline = _ftt_build_baseline_context(espn_snapshot, team_id_val, partner_team_id_val, slot_counts, scoring_bucket)

        incoming_candidates_by_partner = _ftt_generate_incoming_candidates(baseline, position_filter)
        incoming_players_screened = sum(len(v) for v in incoming_candidates_by_partner.values())
        outgoing_pool = _ftt_generate_outgoing_pool(baseline)
        premium_core_outgoing_pool = _ftt_generate_premium_core_outgoing_pool(baseline)

        routine_packages = _ftt_generate_routine_packages(outgoing_pool, incoming_candidates_by_partner, max_pkg_val, baseline)
        premium_packages = _ftt_generate_premium_packages(premium_core_outgoing_pool, incoming_candidates_by_partner, max_pkg_val, baseline)
        all_packages = routine_packages + premium_packages
        package_candidates_generated = len(all_packages)

        survivors_by_partner = {}
        rejected_partner_structure = 0
        rejected_no_evidence = 0
        for pkg in all_packages:
            passes, reason = _ftt_prefilter_package(pkg, baseline)
            if not passes:
                if reason == "no_usable_incoming_evidence":
                    rejected_no_evidence += 1
                else:
                    rejected_partner_structure += 1
                continue
            survivors_by_partner.setdefault(pkg.partner_team_id, []).append(pkg)
        package_candidates_prefiltered = sum(len(v) for v in survivors_by_partner.values())

        restricted_to_single_partner = partner_team_id_val is not None
        if not survivors_by_partner:
            selected_packages, search_truncated, budget_counts = [], False, {
                "total_prefilter_survivors": 0, "selected_for_full_evaluation": 0, "survivors_not_evaluated_due_to_budget": 0}
        else:
            selected_packages, search_truncated, budget_counts = _ftt_allocate_stage_b_budget(
                survivors_by_partner, baseline, restricted_to_single_partner)

        evaluated_candidates = [_ftt_evaluate_package_full(pkg, baseline) for pkg in selected_packages]
        full_trade_evaluations = len(evaluated_candidates)

        rejected_decline = 0
        rejected_partner_structure_post = 0
        rejected_insufficient_data = 0
        unresolved_targets = []
        for c in evaluated_candidates:
            if _ftt_is_hard_excluded(c):
                reason = _ftt_hard_exclusion_reason(c)
                if reason == "rejected_decline":
                    rejected_decline += 1
                else:
                    rejected_partner_structure_post += 1
                continue
            if c["verdict"] == "INSUFFICIENT_DATA":
                rejected_insufficient_data += 1
                if len(unresolved_targets) < limit_val:
                    primary = _ftt_determine_primary_target_player(c["package"])
                    unresolved_targets.append({
                        "partner_team_id": c["partner_team_id"], "target_players": [p["name"] for p in c["package"].players_in],
                        "reason": "insufficient_data",
                        "note": f"Market value assessment={c['market_value']['assessment']}, "
                                 f"lineup classification={c['lineup_impact']['classification']} - "
                                 f"insufficient FantasyPros coverage to evaluate reliably.",
                    })

        final_targets_raw = _ftt_select_final_targets(evaluated_candidates, limit_val)

        trade_targets = []
        for i, c in enumerate(final_targets_raw, start=1):
            data_quality = {
                "market_projection_coverage_complete": c["market_value"]["projection_value_signal"]["coverage_complete"],
                "market_assessment_evaluable": c["market_value"]["assessment"] != "insufficient_data",
                "market_signal_coverage": c["market_value"]["signal_coverage"],
                "lineup_value_complete": c["lineup_impact"]["coverage_complete"],
                "depth_value_complete": c["depth_impact"]["coverage_complete"],
            }
            warnings_for_candidate = []
            if c["roster_legality"]["roster_construction_severity"] != "none":
                warnings_for_candidate.append(f"Post-trade lineup has a {c['roster_legality']['roster_construction_severity']} "
                                                 f"construction gap requiring a follow-up roster move.")
            trade_targets.append({
                "rank": i, "recommendation": _ftt_derive_recommendation_label(c),
                "primary_target": c["primary_target"],
                "partner_team_id": c["partner_team_id"], "partner_team_name": c["partner_team_name"],
                "target_players": [p["name"] for p in c["package"].players_in],
                "proposed_trade": c["proposed_trade"],
                "why_target": _ftt_build_why_target(c), "why_this_offer": _ftt_build_why_this_offer(c, baseline),
                "verdict": c["verdict"], "verdict_reasons": c["verdict_reasons"],
                "market_value": c["market_value"], "lineup_impact": c["lineup_impact"], "depth_impact": c["depth_impact"],
                "positional_need_changes": c["positional_need_changes"], "need_overall": c["need_overall"],
                "injury_risk": c["injury_risk"], "league_relative_changes": c["league_relative_changes"],
                "partner_fit": c["partner_fit"], "roster_legality": c["roster_legality"],
                "best_case": c["best_case"], "worst_case": c["worst_case"],
                "data_quality": data_quality, "warnings": warnings_for_candidate,
            })

        target_search_profile = {
            "positional_needs": baseline["target_positional_needs"],
            "position_strengths": {pos: {"relative_label": baseline["target_position_analysis"][pos].get("relative_label"),
                                           "depth_label": baseline["target_position_analysis"][pos].get("depth_label")}
                                     for pos in ("QB", "RB", "WR", "TE", "FLEX")},
            "trade_surplus_candidates": baseline["trade_surplus_list"],
            "expendable_assets": baseline["expendable_list"],
            "core_assets": baseline["core_asset_list"],
        }

        search_summary = {
            "partners_scanned": len(incoming_candidates_by_partner),
            "incoming_players_screened": incoming_players_screened,
            "package_candidates_generated": package_candidates_generated,
            "package_candidates_prefiltered": package_candidates_prefiltered,
            "full_trade_evaluations": full_trade_evaluations,
            "rejected_decline": rejected_decline,
            "rejected_partner_structure": rejected_partner_structure + rejected_partner_structure_post,
            "rejected_insufficient_data": rejected_insufficient_data,
            "search_truncated": search_truncated,
            "evaluation_budget": {
                "max_full_evaluations": MAX_FULL_EVALUATIONS, "max_full_evaluations_per_partner": MAX_FULL_EVALUATIONS_PER_PARTNER,
                "max_premium_full_evaluations": MAX_PREMIUM_FULL_EVALUATIONS,
                "max_incoming_candidates_per_partner": MAX_INCOMING_CANDIDATES_PER_PARTNER, "max_outgoing_pool": MAX_OUTGOING_POOL,
            },
            "budget_counts": budget_counts,
        }

        return {
            "league_id": league_id_val, "team_id": team_id_val, "team_name": target_team.team_name,
            "year": resolved_year, "scoring_bucket": scoring_bucket,
            "filters": {"position": position, "partner_team_id": partner_team_id_val, "limit": limit_val, "max_package_size": max_pkg_val},
            "target_search_profile": target_search_profile,
            "search_summary": search_summary,
            "trade_targets": trade_targets,
            "unresolved_targets": unresolved_targets,
            "data_freshness": data_freshness, "warnings": freshness_warnings,
            "methodology_notes": [
                "find_trade_targets never calls the public evaluate_trade tool internally - it reuses the same "
                "frozen evidence helpers directly to avoid repeated ESPN fetches and re-enrichment. Parity with "
                "evaluate_trade is verified by an explicit regression test.",
                "Search is bounded, not exhaustive - see search_summary.evaluation_budget and search_truncated.",
                "K/D-ST are never searched as incoming trade targets - FantasyPros has no season projection/tier "
                "data for those positions.",
                "DECLINE and INSUFFICIENT_DATA verdicts are excluded from trade_targets by default; "
                "INSUFFICIENT_DATA packages appear in unresolved_targets instead, never silently dropped.",
                "recommendation label is derived transparently from verdict + partner_fit + roster_construction_severity "
                "- never a hidden second score.",
                "primary_target groups final recommendations by (partner_team_id, player, position, nfl_team) - "
                "X, X+Y, and X+Z packages targeting the same primary player collapse to one best recommendation.",
                "League-relative ranking always covers the FULL league regardless of partner_team_id restriction - "
                "that filter only restricts which partner(s) candidate generation considers.",
            ],
        }
    except Exception as e:
        return _error_response("finding trade targets", e)

# --- Helpers added for optimize_lineup (existing 23 tools untouched) ---

WEEKLY_CLOSE_CALL_THRESHOLD = 2.0

_OL_AVAILABLE_STATUSES = {"ACTIVE", "NORMAL"}
_OL_CAUTION_STATUSES = {"QUESTIONABLE"}
_OL_UNAVAILABLE_STATUSES = {"OUT", "INJURY_RESERVE"}

def _ol_validate_week(week):
    """Returns (week_or_None, error_message). Accepts None or an int 1-18.
    Rejects bools explicitly (bool is a subclass of int in Python)."""
    if week is None:
        return None, None
    if isinstance(week, bool) or not isinstance(week, int):
        return None, f"week must be an integer between 1 and 18, or omitted (got {week!r})."
    if not (1 <= week <= 18):
        return None, f"week must be between 1 and 18 (got {week})."
    return week, None

def _ol_classify_availability(injury_status):
    """Categorical weekly availability from VERIFIED observed ESPN injuryStatus
    values only. Any unrecognized status is 'unknown' - never silently hard-excluded."""
    if injury_status in _OL_AVAILABLE_STATUSES:
        return "available"
    if injury_status in _OL_CAUTION_STATUSES:
        return "caution"
    if injury_status in _OL_UNAVAILABLE_STATUSES:
        return "unavailable"
    return "unknown"

def _ol_extract_week_projection(player_row, week):
    """ESPN weekly projection ONLY from player.stats[week]['projected_points'].
    Never falls back to stats[0] (season) or projected_total_points. Missing -> None."""
    stats = player_row.get("_ol_raw_stats") or {}
    week_stats = stats.get(week)
    if not week_stats:
        return None
    return week_stats.get("projected_points")

def _ol_week_key_present(working_roster, week):
    """True if the requested week bucket is actually loaded in this single
    ESPN fetch's player.stats data for at least one rostered player."""
    for p in working_roster:
        stats = p.get("_ol_raw_stats") or {}
        if week in stats:
            return True
    return False

def _ol_schedule_sufficient(schedule):
    """Deterministic completeness rule: normalize keys to ints 1-18, require
    exactly 17 distinct regular-season week keys before trusting a missing
    key as a verified bye (live-verified against real 17-game schedules)."""
    normalized = set()
    for k in (schedule or {}):
        try:
            wk = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= wk <= 18:
            normalized.add(wk)
    return len(normalized) == 17, normalized

def _ol_determine_bye_status(player_row, week):
    """Returns (bye_status, source). ESPN schedule is primary and only trusted
    when sufficiently complete (17 distinct weeks). FantasyPros bye_week is a
    fallback only when the ESPN schedule itself is insufficient. Never invents
    a bye when neither source verifies it."""
    schedule = player_row.get("_ol_raw_schedule") or {}
    sufficient, normalized_weeks = _ol_schedule_sufficient(schedule)
    if sufficient:
        return ("bye" if week not in normalized_weeks else "not_bye"), "espn_schedule"
    fp_bye = (player_row.get("_fp_intel") or {}).get("bye_week")
    if fp_bye is not None:
        return ("bye" if fp_bye == week else "not_bye"), "fantasypros_cache"
    return "unknown", None

def _ol_is_hard_excluded(player_row):
    """IR-slot exclusion is independent of injuryStatus text (live-verified:
    real IR occupants are frequently ACTIVE/QUESTIONABLE, never assume
    correlation). Verified bye and hard-unavailable injury status also exclude."""
    if player_row.get("lineup_slot") == "IR":
        return True
    if player_row.get("_ol_availability") == "unavailable":
        return True
    if player_row.get("_ol_bye_status") == "bye":
        return True
    return False

def _ol_eligible_for_any_slot(position, slot_counts):
    """True if this position can legally fill at least one configured direct
    or flex/OP starting slot, using the frozen flex parser."""
    if (slot_counts.get(position) or 0) > 0:
        return True
    for slot_key, count in slot_counts.items():
        if (count or 0) <= 0:
            continue
        eligible = _parse_flex_eligibility(slot_key)
        if eligible and position in eligible:
            return True
    return False

def _ol_share_legal_opportunity(pos_a, pos_b, slot_counts):
    """True if two positions could legally compete for the same configured
    starting opportunity (same direct position, or a shared flex/OP slot)."""
    if pos_a == pos_b:
        return True
    for slot_key, count in slot_counts.items():
        if (count or 0) <= 0:
            continue
        eligible = _parse_flex_eligibility(slot_key)
        if eligible and pos_a in eligible and pos_b in eligible:
            return True
    return False


def _ol_build_current_lineup_from_slots(working_roster):
    """Current lineup = ESPN truth, reconstructed directly from the real
    lineupSlot value on each rostered player. Never passed through
    _assign_best_lineup. Distinguishes direct starters, flex/OP starters,
    bench, and IR using the frozen flex parser only for classification."""
    starters, flex_starters, bench, ir = {}, [], [], []
    for p in working_roster:
        slot = p.get("lineup_slot")
        if slot == "BE":
            bench.append(p)
        elif slot == "IR":
            ir.append(p)
        elif slot is not None and _parse_flex_eligibility(slot) is not None:
            flex_starters.append(p)
        elif slot is not None:
            starters.setdefault(slot, []).append(p)
        else:
            bench.append(p)
    return {"starters": starters, "flex_starters": flex_starters, "bench": bench, "ir": ir}

def _ol_check_current_lineup_feasibility(working_roster, slot_counts):
    """Literal ESPN-truth feasibility check: does the user's ACTUAL current
    lineupSlot assignment fill every required slot? This is NOT the same
    question as structural feasibility of the active roster (which allows
    rearrangement) - it reports the current, possibly-unoptimized state."""
    gaps = []
    exclude = {"BE", "IR", ""}
    for slot_key, required in slot_counts.items():
        if not required or slot_key in exclude:
            continue
        filled = sum(1 for p in working_roster if p.get("lineup_slot") == slot_key)
        if filled < required:
            gaps.append({"slot": slot_key, "required": required, "filled": filled, "missing": required - filled})
    return {"feasible": len(gaps) == 0, "gaps": gaps}

def _ol_build_core_offense_slot_counts(slot_counts):
    """Reduced slot map containing ONLY QB/RB/WR/TE direct slots and any
    flex/OP/slash slot the frozen _parse_flex_eligibility recognizes as
    entirely core-offensive. K/D-ST/BE/IR/any other slot is zeroed so the
    FantasyPros season-quality lineup never reports a false K/D-ST gap.
    Live-verified: the frozen parser's FLEX_COMPONENT_POSITIONS is exactly
    {QB,RB,WR,TE}, so every non-None result is already core-only by
    construction; the explicit subset check below is defensive."""
    core = {"QB", "RB", "WR", "TE"}
    result = {}
    for key, count in slot_counts.items():
        if key in ("BE", "IR"):
            result[key] = count
        elif key in core:
            result[key] = count
        else:
            eligible = _parse_flex_eligibility(key)
            result[key] = count if (eligible and set(eligible) <= core) else 0
    return result

def _ol_build_slot_map(lineup_dict):
    """Same conceptual pattern as frozen _build_slot_map: collapses every
    flex/OP starter into the label 'FLEX' for diffing purposes. Works on
    both the ESPN-truth current lineup dict and the frozen _assign_best_lineup
    output, since both share the same starters/flex_starters shape."""
    m = {}
    for pos, players in lineup_dict.get("starters", {}).items():
        for p in players:
            m[p.get("name")] = pos
    for p in lineup_dict.get("flex_starters", []):
        m[p.get("name")] = "FLEX"
    return m

def _ol_diff_lineups(before_map, after_map):
    """Diffs current (ESPN-truth) vs recommended starter sets. If the
    starter PLAYER SET is unchanged, suppresses cosmetic slot-label churn
    entirely (e.g. WR<->FLEX reshuffling among the same starters) rather
    than reporting a meaningless permutation as starters_changing_slots.
    Returns (moves_dict, is_cosmetic_or_identical)."""
    entering = sorted(set(after_map) - set(before_map))
    leaving = sorted(set(before_map) - set(after_map))
    if not entering and not leaving:
        return {
            "entering_starting_lineup": [],
            "leaving_starting_lineup": [],
            "starters_changing_slots": [],
            "unchanged_starters": sorted(set(before_map) | set(after_map)),
        }, True
    changing = [
        {"player": n, "from": before_map[n], "to": after_map[n]}
        for n in sorted(set(before_map) & set(after_map)) if before_map[n] != after_map[n]
    ]
    unchanged = sorted(n for n in (set(before_map) & set(after_map)) if before_map[n] == after_map[n])
    return {
        "entering_starting_lineup": entering,
        "leaving_starting_lineup": leaving,
        "starters_changing_slots": changing,
        "unchanged_starters": unchanged,
    }, False

def _ol_compute_close_calls(entering, leaving, before_map, after_map, name_to_row, slot_counts):
    """Deterministic close-call pairing only, never a fabricated cross-product.
    Simple 1-for-1: pair when exactly one entrant and one leaver share a legal
    configured starting opportunity. Multi-player: pair only when the slot
    chain makes the replacement structurally unambiguous (exactly one leaver
    vacated the exact slot an entrant now occupies); ambiguous groups emit no pair."""
    close_calls = []

    def _try_pair(enter_name, leave_name):
        enter_row, leave_row = name_to_row.get(enter_name), name_to_row.get(leave_name)
        if not enter_row or not leave_row:
            return
        if not _ol_share_legal_opportunity(enter_row.get("position"), leave_row.get("position"), slot_counts):
            return
        ep, lp = enter_row.get("_espn_week_projection"), leave_row.get("_espn_week_projection")
        if ep is None or lp is None:
            return
        delta = round(ep - lp, 2)
        if abs(delta) <= WEEKLY_CLOSE_CALL_THRESHOLD:
            close_calls.append({
                "player_started": enter_name, "player_benched": leave_name,
                "espn_week_projection_difference": delta, "decision": "close_call",
            })

    if len(entering) == 1 and len(leaving) == 1:
        _try_pair(entering[0], leaving[0])
    else:
        slot_of_leaving = {}
        for n in leaving:
            slot_of_leaving.setdefault(before_map[n], []).append(n)
        for n in entering:
            slot = after_map[n]
            candidates = slot_of_leaving.get(slot, [])
            if len(candidates) == 1:
                _try_pair(n, candidates[0])
    return close_calls

def _ol_compare_weekly_vs_season(weekly_after_map, season_map, name_to_row,
                                   weekly_evaluable, season_evaluable):
    """QB/RB/WR/TE only - K/D-ST are structurally absent from season_map by
    construction (_ol_build_core_offense_slot_counts), so they can never
    appear in this comparison. No cross-provider score; categorical only."""
    core_positions = {"QB", "RB", "WR", "TE"}
    weekly_core_names = ({n for n in weekly_after_map
                           if (name_to_row.get(n) or {}).get("position") in core_positions}
                          if weekly_evaluable else None)
    season_core_names = set(season_map) if season_evaluable else None

    if weekly_core_names is None and season_core_names is None:
        return "insufficient_data", []
    if weekly_core_names is None or season_core_names is None:
        return "partially_evaluable", []
    if weekly_core_names == season_core_names:
        return "agree", []
    disagreements = []
    for n in sorted(weekly_core_names - season_core_names):
        disagreements.append({"espn_weekly_favors": n,
                                "note": f"{n} is started by ESPN's weekly projection but not by "
                                        f"FantasyPros' season-long quality lineup."})
    for n in sorted(season_core_names - weekly_core_names):
        disagreements.append({"fantasypros_season_favors": n,
                                "note": f"{n} is favored by FantasyPros' season-long quality context "
                                        f"but is not started by ESPN's weekly projection."})
    return "disagree", disagreements

@mcp.tool()
async def optimize_lineup(league_id: int, team_id: int, week: int = None, year: int = None) -> dict:
    """Answers 'what lineup should I start this week?' using the team's ACTUAL
    ESPN roster and current lineupSlot assignment. Weekly ESPN projections
    (player.stats[week]['projected_points']) drive the primary recommendation;
    cached FantasyPros season-long quality is secondary context only - never
    blended, never overriding the weekly ESPN recommendation. Not a waiver
    search, trade search, or transaction tool. Uses two explicit project-owned
    ESPN reads: one league roster/settings snapshot and one pro-team schedule
    read for ESPN-primary bye verification. Uses cached FantasyPros data only -
    zero live FantasyPros calls.

    Args:
        league_id: The ESPN fantasy football league ID
        team_id: The team ID to optimize (real ESPN team_id, not list position)
        week: Optional NFL scoring week (1-18). If omitted, resolves the
              current ESPN scoring period. An explicit week is NEVER
              substituted with another week's projections.
        year: Optional year (defaults to current season if omitted)
    """
    try:
        league_id_val, league_err = _validate_bounded_int(league_id, "league_id", 1, 999_999_999, league_id)
        if league_err:
            return {"error": "invalid_parameter", "message": league_err}
        team_id_val, team_err = _validate_bounded_int(team_id, "team_id", 1, 999, team_id)
        if team_err:
            return {"error": "invalid_parameter", "message": team_err}
        week_val, week_err = _ol_validate_week(week)
        if week_err:
            return {"error": "invalid_parameter", "message": week_err}

        resolved_year = _resolve_year(year)
        payload = _fetch_snapshot_payload(league_id_val, resolved_year)
        settings_result = build_league_settings(payload, league_id_val, resolved_year)
        slot_counts = settings_result.get("roster_slot_counts", {})
        scoring_bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))
        schedule_payload = _fetch_pro_schedule_payload(resolved_year)
        lineup_team, valid_ids = build_lineup_team(
            payload, schedule_payload, team_id_val, resolved_year)
        if lineup_team is None:
            return {"error": "invalid_parameter",
                     "message": f"No team with team_id={team_id_val} in this league. Valid team_ids: {valid_ids}"}

        positions_in_scope = fp_client.CORE_POSITIONS
        cache_warnings = _check_required_fp_caches(positions_in_scope, scoring_bucket)
        if cache_warnings:
            return {"error": "cache_incomplete", "scoring_bucket_detected": scoring_bucket,
                     "message": f"Required FantasyPros cache data is missing for {scoring_bucket}. "
                                 "No live FantasyPros calls were made.",
                     "warnings": cache_warnings, "next_step": "Call refresh_fantasypros_cache() then retry."}
        data_freshness = fp_client.get_cache_freshness_report(positions_in_scope, scoring_bucket)
        freshness_warnings = [f"{k} is stale (age {v['age_seconds']}s, ttl {v['ttl_seconds']}s)"
                                for k, v in data_freshness.items() if v.get("is_stale")]

        # Week resolution - explicit week is NEVER substituted; omitted week
        # resolves the current ESPN scoring period via the frozen resolver.
        if week_val is None:
            resolved_week = resolve_free_agent_week(payload, None)
            requested_week = resolved_week
        else:
            resolved_week = week_val
            requested_week = week_val

        # Project-owned parser already returns the exact factual shape the
        # frozen optimizer consumes; no wrapper Player objects are involved.
        raw_roster = lineup_team["roster"]

        # FantasyPros enrichment - cache only, one pass per rostered
        # QB/RB/WR/TE player; K/D-ST get an empty intel dict (no invented FP value).
        for p in raw_roster:
            if p["position"] in ("QB", "RB", "WR", "TE"):
                p["_fp_intel"] = fp_client.build_player_intelligence(
                    p.get("name"), p.get("proTeam"), p.get("position"), scoring=scoring_bucket)
            else:
                p["_fp_intel"] = {}

        working_roster, fp_reliability_warning = _build_fp_eval_roster(raw_roster)  # frozen, unmodified

        espn_projection_week_available = resolved_week if _ol_week_key_present(working_roster, resolved_week) else None
        weekly_projection_available = espn_projection_week_available is not None

        for p in working_roster:
            p["_espn_week_projection"] = (_ol_extract_week_projection(p, resolved_week)
                                            if weekly_projection_available else None)
            p["_ol_availability"] = _ol_classify_availability(p.get("injury_status"))
            p["_ol_bye_status"], p["_ol_bye_source"] = _ol_determine_bye_status(p, resolved_week)
            if p["position"] in ("K", "D/ST"):
                p["_ol_external_quality"] = "unavailable"
            else:
                p["_ol_external_quality"] = fp_client.describe_player_quality((p.get("_fp_intel") or {}).get("tier"))

        name_to_row = {p["name"]: p for p in working_roster}

        # --- Structural feasibility pass (always runs, independent of weekly
        # projection coverage). Every candidate shares the same constant value,
        # so no missing-data -1 sentinel issue and no player preference is implied. ---
        structural_candidate_pool = []
        for p in working_roster:
            if _ol_is_hard_excluded(p):
                continue
            row = dict(p)
            row["_lineup_structure_value"] = 0
            structural_candidate_pool.append(row)
        structural_result = _assign_best_lineup(structural_candidate_pool, slot_counts,
                                                   value_field="_lineup_structure_value")

        # --- Strict weekly decision-candidate coverage gate ---
        weekly_decision_candidate_pool = [
            p for p in working_roster
            if not _ol_is_hard_excluded(p) and _ol_eligible_for_any_slot(p["position"], slot_counts)
        ]
        missing_decision_candidate_projection_players = sorted(
            p["name"] for p in weekly_decision_candidate_pool if p.get("_espn_week_projection") is None)
        weekly_optimization_evaluable = (
            weekly_projection_available
            and structural_result["feasible"]
            and len(missing_decision_candidate_projection_players) == 0
        )

        missing_weekly_projection_players = sorted(
            p["name"] for p in working_roster if p.get("_espn_week_projection") is None)

        espn_weekly_projection_coverage = (
            "none" if not weekly_projection_available
            else ("complete" if not missing_weekly_projection_players else "partial")
        )

        # --- Weekly optimization pass (only when fully evaluable) ---
        weekly_result = None
        if weekly_optimization_evaluable:
            weekly_pool_rows = [dict(p) for p in weekly_decision_candidate_pool]
            weekly_result = _assign_best_lineup(weekly_pool_rows, slot_counts, value_field="_espn_week_projection")

        # --- Current lineup = ESPN truth (never passed through _assign_best_lineup) ---
        current_lineup_dict = _ol_build_current_lineup_from_slots(working_roster)
        current_feasibility = _ol_check_current_lineup_feasibility(working_roster, slot_counts)
        before_map = _ol_build_slot_map(current_lineup_dict)

        # --- FantasyPros season-quality context (QB/RB/WR/TE only, IR excluded,
        # weekly bye/injury exclusion NOT applied - season quality is timeframe-
        # independent, matching frozen analyze_my_team/evaluate_trade precedent) ---
        core_offense_slot_counts = _ol_build_core_offense_slot_counts(slot_counts)
        core_candidates = [dict(p) for p in working_roster
                             if p.get("lineup_slot") != "IR" and p["position"] in ("QB", "RB", "WR", "TE")]
        season_evaluable = len(core_candidates) > 0
        season_quality_result = (
            _assign_best_lineup(core_candidates, core_offense_slot_counts, value_field="_fp_eval_value")
            if season_evaluable else {"feasible": True, "starters": {}, "flex_starters": [], "bench": [], "gaps": []}
        )
        season_map = _ol_build_slot_map(season_quality_result) if season_evaluable else {}

        # --- Current vs recommended move diff (cosmetic slot-churn suppressed) ---
        if weekly_optimization_evaluable:
            after_map = _ol_build_slot_map(weekly_result)
            moves, is_cosmetic_or_identical = _ol_diff_lineups(before_map, after_map)
        else:
            after_map = {}
            moves, is_cosmetic_or_identical = {
                "entering_starting_lineup": [], "leaving_starting_lineup": [],
                "starters_changing_slots": [], "unchanged_starters": [],
            }, False

        entering = moves["entering_starting_lineup"]
        leaving = moves["leaving_starting_lineup"]

        close_calls = []
        if weekly_optimization_evaluable and not is_cosmetic_or_identical:
            close_calls = _ol_compute_close_calls(entering, leaving, before_map, after_map, name_to_row, slot_counts)

        # --- Weekly projection comparison (ESPN-only, strict complete-coverage rule) ---
        if weekly_optimization_evaluable:
            current_starter_names = set(before_map.keys())
            recommended_starter_names = set(after_map.keys())
            missing_cmp = sorted(
                n for n in (current_starter_names | recommended_starter_names)
                if (name_to_row.get(n) or {}).get("_espn_week_projection") is None
            )
            if not missing_cmp:
                current_total = round(sum(name_to_row[n]["_espn_week_projection"] for n in current_starter_names), 2)
                recommended_total = round(sum(name_to_row[n]["_espn_week_projection"] for n in recommended_starter_names), 2)
                weekly_delta = round(recommended_total - current_total, 2)
                weekly_coverage_complete = True
            else:
                current_total = recommended_total = weekly_delta = None
                weekly_coverage_complete = False
        else:
            current_total = recommended_total = weekly_delta = None
            weekly_coverage_complete = False
            missing_cmp = []

        # --- Weekly vs season-quality agreement (QB/RB/WR/TE only; K/D-ST
        # structurally absent from season_map, never compared) ---
        agreement, disagreements = _ol_compare_weekly_vs_season(
            after_map, season_map, name_to_row, weekly_optimization_evaluable, season_evaluable)

        # --- FP disagreement relevance to confidence (only if it touches an
        # actual decision-relevant player from this recommendation) ---
        disagreement_names = set()
        for d in disagreements:
            if "espn_weekly_favors" in d:
                disagreement_names.add(d["espn_weekly_favors"])
            if "fantasypros_season_favors" in d:
                disagreement_names.add(d["fantasypros_season_favors"])
        close_call_names = {cc["player_started"] for cc in close_calls} | {cc["player_benched"] for cc in close_calls}
        fp_disagreement_relevant = bool(disagreement_names & close_call_names)

        # --- Confidence (categorical only, no numeric score) ---
        decision_player_names = (set(entering) | set(leaving)) if not is_cosmetic_or_identical else set()
        decision_availabilities = [name_to_row[n]["_ol_availability"] for n in decision_player_names if n in name_to_row]
        has_unknown_decision = any(a == "unknown" for a in decision_availabilities)
        has_caution_decision = any(a == "caution" for a in decision_availabilities)

        if not weekly_optimization_evaluable:
            confidence = "low"
        elif has_unknown_decision:
            confidence = "low"
        elif has_caution_decision or fp_disagreement_relevant:
            confidence = "medium"
        else:
            confidence = "high"

        # --- Status precedence (exact order; IR activation alone never overrides) ---
        if not structural_result["feasible"]:
            status = "roster_move_required"
        elif not weekly_optimization_evaluable:
            status = "insufficient_weekly_data"
        elif is_cosmetic_or_identical:
            status = "current_lineup_already_optimal"
        elif confidence == "high":
            status = "lineup_change_recommended"
        else:
            status = "lineup_change_recommended_with_caution"

        # --- Roster actions required (IR activation is informational only) ---
        roster_actions_required = []
        if not structural_result["feasible"]:
            roster_actions_required.append({
                "type": "roster_move_required",
                "note": "The active/available roster cannot legally fill every required starting slot.",
                "gaps": structural_result["gaps"],
            })
        for p in working_roster:
            if p.get("lineup_slot") == "IR" and p.get("_ol_external_quality") in ("elite", "strong", "solid_starter"):
                roster_actions_required.append({
                    "type": "activation_opportunity",
                    "player": p["name"], "position": p["position"],
                    "note": (f"{p['name']} is on IR but rated {p['_ol_external_quality']} by FantasyPros "
                              f"season-long context; activation may be worth considering. The current "
                              f"active roster {'can' if structural_result['feasible'] else 'cannot'} still "
                              f"field a legal lineup without this player."),
                })

        injury_and_availability = [
            {"player": p["name"], "position": p["position"], "espn_injury_status": p.get("injury_status"),
              "availability": p["_ol_availability"]}
            for p in working_roster if p["_ol_availability"] != "available"
        ]
        bye_week_context = [
            {"player": p["name"], "position": p["position"], "bye_status": p["_ol_bye_status"],
              "source": p.get("_ol_bye_source")}
            for p in working_roster if p["_ol_bye_status"] in ("bye", "unknown")
        ]

        # --- Human-readable, evidence-based reasons for every move ---
        def _ol_summarize_players(players):
            return [{"name": p.get("name"), "position": p.get("position")} for p in players]

        def _ol_reason_for_entering(name):
            row = name_to_row.get(name) or {}
            proj = row.get("_espn_week_projection")
            proj_txt = f"{proj:.2f}" if proj is not None else "unavailable"
            return f"{name} enters the starting lineup at {after_map.get(name)} (ESPN Week {resolved_week} projection: {proj_txt})."

        def _ol_reason_for_leaving(name):
            row = name_to_row.get(name) or {}
            if row.get("_ol_bye_status") == "bye":
                return f"{name} is on a verified Week {resolved_week} bye and is excluded from the weekly candidate pool."
            if row.get("_ol_availability") == "unavailable":
                return f"{name} is marked {row.get('injury_status')} and is excluded from the weekly candidate pool."
            proj = row.get("_espn_week_projection")
            proj_txt = f"{proj:.2f}" if proj is not None else "unavailable"
            return f"{name} leaves the starting lineup (was {before_map.get(name)}; ESPN Week {resolved_week} projection: {proj_txt})."

        def _ol_reason_for_changing(item):
            row = name_to_row.get(item["player"]) or {}
            proj = row.get("_espn_week_projection")
            proj_txt = f"{proj:.2f}" if proj is not None else "unavailable"
            return (f"{item['player']} remains a starter but moves from {item['from']} to {item['to']} "
                    f"(ESPN Week {resolved_week} projection: {proj_txt}).")

        def _ol_reason_for_close_call(cc):
            base = (f"{cc['player_started']} is projected {cc['espn_week_projection_difference']:+.2f} points "
                     f"vs {cc['player_benched']} for Week {resolved_week} (within the "
                     f"{WEEKLY_CLOSE_CALL_THRESHOLD}-point close-call threshold).")
            if cc["player_started"] in disagreement_names or cc["player_benched"] in disagreement_names:
                base += (" FantasyPros' season-long quality context disagrees with this weekly lean; "
                          "treat this as a weekly matchup decision, not a rest-of-season ranking.")
            return base

        entering_out = [{"player": n, "position": (name_to_row.get(n) or {}).get("position"),
                           "to_slot": after_map.get(n), "reason": _ol_reason_for_entering(n)} for n in entering]
        leaving_out = [{"player": n, "position": (name_to_row.get(n) or {}).get("position"),
                          "from_slot": before_map.get(n), "reason": _ol_reason_for_leaving(n)} for n in leaving]
        changing_out = [dict(item, reason=_ol_reason_for_changing(item)) for item in moves["starters_changing_slots"]]
        close_calls_out = [dict(cc, reason=_ol_reason_for_close_call(cc)) for cc in close_calls]

        current_lineup_out = {
            "starters": {pos: _ol_summarize_players(pls) for pos, pls in current_lineup_dict["starters"].items()},
            "flex_starters": _ol_summarize_players(current_lineup_dict["flex_starters"]),
            "bench": _ol_summarize_players(current_lineup_dict["bench"]),
            "ir": _ol_summarize_players(current_lineup_dict["ir"]),
            "lineup_feasible": current_feasibility["feasible"],
            "lineup_gaps": current_feasibility["gaps"],
        }
        recommended_lineup_out = None
        if weekly_optimization_evaluable:
            recommended_lineup_out = {
                "starters": {pos: _ol_summarize_players(pls) for pos, pls in weekly_result["starters"].items()},
                "flex_starters": _ol_summarize_players(weekly_result["flex_starters"]),
                "bench": _ol_summarize_players(weekly_result["bench"]),
                "lineup_feasible": weekly_result["feasible"],
                "lineup_gaps": weekly_result["gaps"],
            }

        season_quality_lineup_out = {
            "starters": {pos: _ol_summarize_players(pls) for pos, pls in season_quality_result["starters"].items()},
            "flex_starters": _ol_summarize_players(season_quality_result["flex_starters"]),
        } if season_evaluable else None

        core_positions_qbrbwrte = ("QB", "RB", "WR", "TE")
        fp_relevant = [p for p in working_roster if p["position"] in core_positions_qbrbwrte]
        low_or_unresolved_fp_match_players = sorted(
            p["name"] for p in fp_relevant
            if (p.get("_fp_intel") or {}).get("match_confidence") in ("low", "ambiguous", "none"))
        fantasypros_match_coverage = "complete" if not low_or_unresolved_fp_match_players else "partial"

        warnings_out = list(freshness_warnings)
        if fp_reliability_warning:
            warnings_out.append(fp_reliability_warning)

        return {
            "league_id": league_id_val, "team_id": team_id_val, "team_name": lineup_team["team_name"],
            "year": resolved_year, "scoring_bucket": scoring_bucket,

            "requested_week": requested_week,
            "espn_projection_week_available": espn_projection_week_available,
            "weekly_projection_available": weekly_projection_available,

            "status": status, "confidence": confidence,

            "current_lineup": current_lineup_out,
            "recommended_lineup": recommended_lineup_out,

            "moves": {
                "entering_starting_lineup": entering_out,
                "leaving_starting_lineup": leaving_out,
                "starters_changing_slots": changing_out,
                "unchanged_starters": moves["unchanged_starters"],
            },

            "weekly_projection_comparison": {
                "provider": "ESPN", "requested_week": resolved_week,
                "current_lineup_projection": current_total, "recommended_lineup_projection": recommended_total,
                "weekly_projection_delta": weekly_delta, "coverage_complete": weekly_coverage_complete,
                "missing_projection_players": missing_cmp,
            },

            "close_calls": close_calls_out,

            "season_quality_context": {
                "provider": "FantasyPros", "timeframe": "season",
                "lineup": season_quality_lineup_out,
                "agreement_with_weekly_lineup": agreement,
                "disagreements": disagreements,
            },

            "injury_and_availability": injury_and_availability,
            "bye_week_context": bye_week_context,
            "roster_actions_required": roster_actions_required,

            "lineup_lock_status": "not_modeled",

            "data_quality": {
                "weekly_optimization_evaluable": weekly_optimization_evaluable,
                "espn_weekly_projection_coverage": espn_weekly_projection_coverage,
                "missing_weekly_projection_players": missing_weekly_projection_players,
                "missing_decision_candidate_projection_players": missing_decision_candidate_projection_players,
                "fantasypros_match_coverage": fantasypros_match_coverage,
                "low_or_unresolved_fp_match_players": low_or_unresolved_fp_match_players,
            },

            "data_freshness": data_freshness, "warnings": warnings_out,
            "methodology_notes": [
                "ESPN weekly projection is read exclusively from player.stats[week]['projected_points']; "
                "the season bucket (stats[0], equal to projected_total_points) is never substituted for it.",
                "An explicitly requested week is never substituted with another week's projections; if the "
                "requested week is not the currently-loaded ESPN scoring period, weekly optimization is "
                "skipped entirely and status becomes insufficient_weekly_data (unless structural infeasibility "
                "takes precedence).",
                "Structural lineup feasibility (roster_move_required) is evaluated independently of weekly "
                "projection coverage via a constant-value pass through the frozen lineup engine - it can "
                "still be determined even when no weekly ESPN data is available.",
                "Missing weekly projection is never treated as evidence of low weekly value: if ANY player "
                "eligible to fill a starting slot lacks a requested-week ESPN projection, the weekly "
                "optimization path does not run at all, rather than silently exposing the frozen lineup "
                "engine's internal None-to-negative-one sorting sentinel as a real projection.",
                "lineup_slot=='IR' is an independent exclusion from injuryStatus text - real IR occupants "
                "are frequently ACTIVE or QUESTIONABLE, never assume correlation.",
                "A verified bye requires either a sufficiently complete ESPN schedule (exactly 17 distinct "
                "week keys in 1-18) with the requested week missing, or a FantasyPros cache bye_week match "
                "when the ESPN schedule itself is insufficient; otherwise bye status is 'unknown', never invented.",
                "If the current starter player set already equals the optimized starter player set, the "
                "lineup is reported as already optimal even if the frozen lineup engine assigned different "
                "but equivalent direct/FLEX slot labels - cosmetic slot permutation is never presented as "
                "a recommendation.",
                "FantasyPros season-quality context is QB/RB/WR/TE only, built against a reduced core-offense "
                "slot map so K/D-ST never produce a false gap or an invented FantasyPros preference; it is "
                "secondary context and never overrides the ESPN weekly recommendation.",
                "close_calls only pair players from a structurally unambiguous entering/leaving replacement; "
                "ambiguous multi-player chains produce no fabricated pairing.",
                "lineup_lock_status is not_modeled in this version - kickoff/transaction-lock estimation "
                "is deferred.",
                "This tool issues exactly one ESPN league fetch and makes zero live FantasyPros calls.",
            ],
        }
    except Exception as e:
        return _error_response("optimizing lineup", e)

# --- Helpers added for get_fantasy_brief (existing 24 tools untouched) ---

# --- TOOL #25 PERFORMANCE PHASE 1 (2026-08-14): per-brief parsed
# FantasyPros cache. Purely additive - captures the ORIGINAL frozen
# fp_client._read_cache exactly once, then installs a single stable
# context-aware wrapper in its place. Outside a get_fantasy_brief
# invocation the ContextVar is None and the wrapper transparently
# delegates to the ORIGINAL reader with zero behavior change for any
# other tool/caller (concurrent or not). Inside one brief invocation,
# each of the FantasyPros dataset_keys actually requested is read/
# parsed from disk at most once and reused for the remainder of that
# single brief only - the cache is destroyed (via ContextVar.reset in
# a finally block on get_fantasy_brief) the instant the brief
# completes, errors, or is cancelled. No process-level or cross-
# request caching of any kind is introduced; each concurrent brief
# gets its own independent context-local dict (asyncio ContextVar
# semantics: a value set inside one Task is invisible to sibling
# Tasks). Static audit (2026-08-14) confirmed every consumer of
# _read_cache's returned objects (get_players_cache, get_rankings_cache,
# get_projections_cache, get_injuries_cache, get_news_cache,
# get_news_for_player, match_player, build_player_intelligence,
# get_rankings_list, get_adp_list, _dataset_freshness) is read-only -
# list comprehensions and dict.get() calls only, with the single
# .sort() call in fantasypros_client.py's get_adp_list operating on a
# freshly-built filtered list, never the cached object itself. Safe to
# memoize the shared parsed object for the scope of one brief.
_GFB_ORIGINAL_FP_READ_CACHE = fp_client._read_cache

_GFB_FP_PARSED_CACHE_CONTEXT = ContextVar("gfb_fp_parsed_cache", default=None)

def _gfb_contextual_fp_read_cache(dataset_key):
    brief_cache = _GFB_FP_PARSED_CACHE_CONTEXT.get()
    if brief_cache is None:
        return _GFB_ORIGINAL_FP_READ_CACHE(dataset_key)
    if dataset_key not in brief_cache:
        brief_cache[dataset_key] = _GFB_ORIGINAL_FP_READ_CACHE(dataset_key)
    return brief_cache[dataset_key]

fp_client._read_cache = _gfb_contextual_fp_read_cache

def _gfb_source_status_ok():
    return {"status": "ok", "error": None}

def _gfb_source_status_error(message):
    return {"status": "error", "error": message}

def _gfb_source_status_insufficient(message):
    return {"status": "insufficient", "error": message}

def _gfb_is_foundational_error(result):
    """A foundational error means the shared team/league context itself
    could not be established - invalid_parameter or private-league auth,
    surfaced identically by every one of the 4 frozen tools since they all
    validate league_id/team_id the same way. cache_incomplete is NOT
    foundational - it is an isolated FantasyPros-coverage limitation."""
    return isinstance(result, dict) and result.get("error") in (
        "invalid_parameter", "private_league_auth_required", "request_failed")

_GFB_WAIVER_STRONG_DIRECTIONS = ("strong_upgrade", "upgrade")
_GFB_TRADE_HIGH_VALUE_LABELS = ("priority_target", "strong_target")
_GFB_TRADE_ACCEPT_VERDICTS = ("ACCEPT", "LEAN_ACCEPT")
_GFB_TRADE_STARTING_UPGRADE_CLASSES = ("major_starting_upgrade", "starting_upgrade", "minor_starting_upgrade")

def _gfb_waiver_top_for_position(waiver_result, position):
    """Returns the first frozen waiver recommendation matching position,
    preserving frozen order (recommendations are already sorted; this is
    not a re-rank, only a position filter)."""
    if not isinstance(waiver_result, dict) or "error" in waiver_result:
        return None
    for r in waiver_result.get("recommendations", []):
        if r.get("add_position") == position:
            return r
    return None

_GFB_WAIVER_STARTING_LINEUP_IMPACTS = ("direct_starter_upgrade", "flex_starter_upgrade", "starting_rotation_upgrade")

def _gfb_waiver_qualifies_high_value(waiver_result, position):
    """CORRECTED 2026-08-14: qualification is now candidate-specific AND
    position-specific. The global overall_recommendation flag can be
    caused by a DIFFERENT position's candidate (e.g. a strong RB waiver
    triggering the league-wide flag) and is therefore NOT sufficient
    evidence that THIS position's candidate is itself a starting-lineup
    upgrade. A waiver candidate only qualifies as the primary route when
    its OWN frozen evidence shows both a strong asset direction AND a
    real starting/rotation lineup impact - bench_only_upgrade and
    no_lineup_improvement never qualify here, regardless of the global
    flag."""
    if not isinstance(waiver_result, dict) or "error" in waiver_result:
        return False, None
    top = _gfb_waiver_top_for_position(waiver_result, position)
    if top is None:
        return False, None
    if top.get("signals", {}).get("direction") not in _GFB_WAIVER_STRONG_DIRECTIONS:
        return False, None
    if top.get("lineup_impact") not in _GFB_WAIVER_STARTING_LINEUP_IMPACTS:
        return False, None
    return True, top

def _gfb_waiver_qualifies_consider(waiver_result, position):
    if not isinstance(waiver_result, dict) or "error" in waiver_result:
        return False, None
    top = _gfb_waiver_top_for_position(waiver_result, position)
    if top is None:
        return False, None
    if waiver_result.get("overall_recommendation") == "bench_value_upgrade_available":
        return True, top
    if top.get("signals", {}).get("direction") not in ("insufficient_data", "downgrade"):
        return True, top
    return False, None

def _gfb_trade_top_for_position(trade_result, position):
    if not isinstance(trade_result, dict) or "error" in trade_result:
        return None
    for t in trade_result.get("trade_targets", []):
        if t.get("primary_target", {}).get("position") == position:
            return t
    return None

def _gfb_trade_alternatives_for_position(trade_result, position, exclude_rank=None, max_count=3):
    if not isinstance(trade_result, dict) or "error" in trade_result:
        return []
    out = []
    for t in trade_result.get("trade_targets", []):
        if t.get("primary_target", {}).get("position") != position:
            continue
        if exclude_rank is not None and t.get("rank") == exclude_rank:
            continue
        out.append(t)
        if len(out) >= max_count:
            break
    return out

def _gfb_trade_qualifies_high_value(trade_result, team_result, position):
    top = _gfb_trade_top_for_position(trade_result, position)
    if top is None:
        return False, None
    if top.get("recommendation") not in _GFB_TRADE_HIGH_VALUE_LABELS:
        return False, None
    if top.get("verdict") not in _GFB_TRADE_ACCEPT_VERDICTS:
        return False, None
    need_severity = _gfb_need_severity(team_result, position)
    addresses_need = need_severity in ("meaningful", "urgent")
    need_positive = top.get("need_overall") == "positive"
    starting_upgrade = top.get("lineup_impact", {}).get("classification") in _GFB_TRADE_STARTING_UPGRADE_CLASSES
    if addresses_need or need_positive or starting_upgrade:
        return True, top
    return False, None

def _gfb_trade_qualifies_consider(trade_result, position):
    top = _gfb_trade_top_for_position(trade_result, position)
    if top is None:
        return False, None
    if top.get("recommendation") == "exploratory_target" or top.get("verdict") == "FAIR":
        return True, top
    return False, None

def _gfb_need_severity(team_result, position):
    if not isinstance(team_result, dict) or "error" in team_result:
        return "unknown"
    for n in team_result.get("positional_needs", []):
        if n.get("position") == position:
            return n.get("severity")
    return "unknown"

_GFB_CORE_ACQUISITION_POSITIONS = ("QB", "RB", "WR", "TE")

def _gfb_build_acquisition_goal(position, waiver_result, trade_result, team_result):
    """Groups waiver+trade evidence for ONE position into a single goal.
    Grouping is based purely on position (same acquisition problem), never
    on formal need severity - severity is evidence for PRIORITY only, per
    the approved guardrail. Returns None if no qualifying waiver or trade
    candidate exists at this position."""
    waiver_hv, waiver_top = _gfb_waiver_qualifies_high_value(waiver_result, position)
    trade_hv, trade_top = _gfb_trade_qualifies_high_value(trade_result, team_result, position)
    waiver_consider, waiver_top_c = (False, None) if waiver_hv else _gfb_waiver_qualifies_consider(waiver_result, position)
    trade_consider, trade_top_c = (False, None) if trade_hv else _gfb_trade_qualifies_consider(trade_result, position)

    waiver_candidate = waiver_top if waiver_hv else waiver_top_c
    trade_candidate = trade_top if trade_hv else trade_top_c
    if waiver_candidate is None and trade_candidate is None:
        return None

    need_severity = _gfb_need_severity(team_result, position)
    priority = "high_value_move" if (waiver_hv or trade_hv) else "consider"

    # --- Route arbitration (categorical, frozen-label-based only) ---
    source = ["analyze_my_team"] if need_severity != "unknown" else []
    if waiver_hv:
        route, alt_route = "waiver", "trade"
        primary_candidate, alt_candidate = waiver_candidate, trade_candidate
        source.append("rank_waiver_targets")
        if trade_candidate is not None:
            source.append("find_trade_targets")
    elif trade_hv:
        route, alt_route = "trade", "waiver"
        primary_candidate, alt_candidate = trade_candidate, waiver_candidate
        source.append("find_trade_targets")
        if waiver_candidate is not None:
            source.append("rank_waiver_targets")
    elif waiver_consider and not trade_hv:
        route, alt_route = "waiver", "trade"
        primary_candidate, alt_candidate = waiver_candidate, trade_candidate
        source.append("rank_waiver_targets")
        if trade_candidate is not None:
            source.append("find_trade_targets")
    else:
        route, alt_route = "trade", "waiver"
        primary_candidate, alt_candidate = trade_candidate, waiver_candidate
        source.append("find_trade_targets")
        if waiver_candidate is not None:
            source.append("rank_waiver_targets")

    def _describe_waiver(w):
        return {"type": "waiver", "add_player": w.get("add_player"), "drop_player": w.get("recommended_drop"),
                 "asset_classification": w.get("signals", {}).get("direction"), "lineup_impact": w.get("lineup_impact"),
                 "reason": w.get("reason")}

    def _describe_trade(t):
        return {"type": "trade", "primary_target": t.get("primary_target"), "partner_team_name": t.get("partner_team_name"),
                 "proposed_trade": t.get("proposed_trade"), "recommendation": t.get("recommendation"),
                 "verdict": t.get("verdict"), "why_target": t.get("why_target")}

    recommended_path = _describe_waiver(primary_candidate) if route == "waiver" else _describe_trade(primary_candidate)

    alternatives = []
    if alt_candidate is not None:
        alternatives.append(_describe_waiver(alt_candidate) if alt_route == "waiver" else _describe_trade(alt_candidate))
    extra_trades = _gfb_trade_alternatives_for_position(
        trade_result, position, exclude_rank=(trade_candidate.get("rank") if trade_candidate else None), max_count=2)
    for t in extra_trades:
        alternatives.append(_describe_trade(t))
    alternatives = alternatives[:3]

    why_parts = []
    if need_severity in ("meaningful", "urgent"):
        why_parts.append(f"Team analysis identifies {position} as a {need_severity} need.")
    if route == "waiver":
        why_parts.append(f"Frozen waiver analysis classifies the top {position} option as a "
                           f"{primary_candidate.get('signals', {}).get('direction')} with lineup impact "
                           f"{primary_candidate.get('lineup_impact')}.")
    else:
        why_parts.append(f"Top trade recommendation is {primary_candidate.get('recommendation')} "
                           f"with verdict {primary_candidate.get('verdict')}.")
    why = " ".join(why_parts) if why_parts else f"An acquisition opportunity exists at {position}."

    return {
        "goal": f"upgrade_{position.lower()}", "position": position, "priority": priority,
        "why": why, "recommended_path": recommended_path, "alternatives": alternatives,
        "source": sorted(set(source)),
    }

def _gfb_collect_relevant_player_names(lineup_result, team_result):
    """Union of current starters, recommended starters, core assets, and
    players directly involved in a recommended move. Used to filter
    optimize_lineup's roster-wide injury/bye lists down to decision-
    relevant entries only - optimize_lineup itself does not pre-filter
    these lists (confirmed from its live source), so Tool #25 must."""
    names = set()
    if isinstance(lineup_result, dict) and "error" not in lineup_result:
        cur = lineup_result.get("current_lineup") or {}
        for pls in (cur.get("starters") or {}).values():
            names.update(p.get("name") for p in pls)
        names.update(p.get("name") for p in (cur.get("flex_starters") or []))
        rec = lineup_result.get("recommended_lineup")
        if rec:
            for pls in (rec.get("starters") or {}).values():
                names.update(p.get("name") for p in pls)
            names.update(p.get("name") for p in (rec.get("flex_starters") or []))
        moves = lineup_result.get("moves") or {}
        names.update(e.get("player") for e in moves.get("entering_starting_lineup", []))
        names.update(e.get("player") for e in moves.get("leaving_starting_lineup", []))
        names.update(e.get("player") for e in moves.get("starters_changing_slots", []))
    if isinstance(team_result, dict) and "error" not in team_result:
        names.update(c.get("player") for c in team_result.get("core_assets", []))
    names.discard(None)
    return names

def _gfb_build_monitor_items(lineup_result, team_result, max_count=5):
    items = []
    relevant_names = _gfb_collect_relevant_player_names(lineup_result, team_result)

    if isinstance(lineup_result, dict) and "error" not in lineup_result:
        for entry in lineup_result.get("injury_and_availability", []):
            name, avail = entry.get("player"), entry.get("availability")
            if name not in relevant_names:
                continue
            if avail in ("caution", "unknown"):
                items.append({"type": "injury_availability", "player": name, "position": entry.get("position"),
                               "detail": f"{name} is {entry.get('espn_injury_status')} ({avail}).",
                               "source": ["optimize_lineup"]})
            elif avail == "unavailable":
                items.append({"type": "injury_availability", "player": name, "position": entry.get("position"),
                               "detail": f"{name} ({entry.get('espn_injury_status')}) is a core asset or "
                                          f"otherwise affects a current roster decision.",
                               "source": ["optimize_lineup"]})
        for entry in lineup_result.get("bye_week_context", []):
            name = entry.get("player")
            if name in relevant_names:
                items.append({"type": "bye_week", "player": name, "position": entry.get("position"),
                               "detail": f"{name} bye status: {entry.get('bye_status')} (affects current/recommended lineup).",
                               "source": ["optimize_lineup"]})
        for action in lineup_result.get("roster_actions_required", []):
            if action.get("type") == "activation_opportunity":
                items.append({"type": "activation_opportunity", "player": action.get("player"),
                               "detail": action.get("note"), "source": ["optimize_lineup"]})
    return items[:max_count]

_GFB_LINEUP_ACTIONABLE_STATUSES = ("roster_move_required", "lineup_change_recommended", "lineup_change_recommended_with_caution")

def _gfb_build_lineup_action(lineup_result):
    """A do_now action ONLY when Tool #24 itself reports a real actionable
    status. current_lineup_already_optimal and insufficient_weekly_data
    never manufacture a lineup action - Tool #24's status is preserved
    verbatim, never reinterpreted."""
    if not isinstance(lineup_result, dict) or "error" in lineup_result:
        return None
    status = lineup_result.get("status")
    if status not in _GFB_LINEUP_ACTIONABLE_STATUSES:
        return None
    if status == "roster_move_required":
        gaps = lineup_result.get("current_lineup", {}).get("lineup_gaps", [])
        action_text = f"Fix your active lineup: it cannot legally fill {len(gaps)} required slot(s)."
        why = "The active/available roster cannot fill every required starting slot (optimize_lineup structural check)."
    else:
        entering = lineup_result.get("moves", {}).get("entering_starting_lineup", [])
        leaving = lineup_result.get("moves", {}).get("leaving_starting_lineup", [])
        if entering and leaving:
            action_text = (f"Start {', '.join(e.get('player') for e in entering)} over "
                            f"{', '.join(l.get('player') for l in leaving)} for Week {lineup_result.get('requested_week')}.")
        elif entering:
            action_text = f"Start {', '.join(e.get('player') for e in entering)} for Week {lineup_result.get('requested_week')}."
        else:
            action_text = f"Adjust your starting lineup for Week {lineup_result.get('requested_week')}."
        delta = lineup_result.get("weekly_projection_comparison", {}).get("weekly_projection_delta")
        delta_txt = f" ESPN projects a {delta:+.1f}-point improvement." if delta is not None else ""
        caution_txt = " Proceed with caution due to injury/data uncertainty." if status == "lineup_change_recommended_with_caution" else ""
        why = f"optimize_lineup recommends this change for the requested week.{delta_txt}{caution_txt}"
    return {
        "priority": "do_now", "goal": "weekly_lineup", "action": action_text, "why": why,
        "source": ["optimize_lineup"],
        "evidence": {"status": status, "confidence": lineup_result.get("confidence"),
                      "requested_week": lineup_result.get("requested_week")},
    }

_GFB_STATUS_ORDER = {"insufficient_data": 0, "urgent_action_required": 1, "action_recommended": 2,
                       "opportunities_available": 3, "monitor_only": 4}

def _gfb_determine_brief_status(lineup_result, lineup_action, acquisition_goals, monitor_items, foundational_ok):
    if not foundational_ok:
        return "insufficient_data"
    if isinstance(lineup_result, dict) and lineup_result.get("status") == "roster_move_required":
        return "urgent_action_required"
    if lineup_action is not None:
        return "action_recommended"
    if any(g["priority"] == "high_value_move" for g in acquisition_goals):
        return "action_recommended"
    if any(g["priority"] == "consider" for g in acquisition_goals):
        return "opportunities_available"
    if monitor_items:
        return "monitor_only"
    return "monitor_only"

def _gfb_generate_headline(brief_status, lineup_result, lineup_action, acquisition_goals, monitor_items):
    hv_goals = [g for g in acquisition_goals if g["priority"] == "high_value_move"]
    consider_goals = [g for g in acquisition_goals if g["priority"] == "consider"]
    n_lineup_moves = 0
    if lineup_action is not None and isinstance(lineup_result, dict):
        n_lineup_moves = (len(lineup_result.get("moves", {}).get("entering_starting_lineup", []))
                            or (1 if lineup_result.get("status") == "roster_move_required" else 0))

    if brief_status == "urgent_action_required":
        return "Your immediate priority is fixing the active lineup so every required starting slot can be filled."
    if lineup_action is not None and hv_goals:
        return f"Make {n_lineup_moves} lineup change(s) this week, then focus on {hv_goals[0]['goal'].replace('_', ' ')}."
    if lineup_action is not None:
        return f"Make {n_lineup_moves} lineup change(s) this week; no other major roster move is more urgent."
    if isinstance(lineup_result, dict) and lineup_result.get("status") == "current_lineup_already_optimal" and hv_goals:
        return (f"Your current lineup is already optimized, but there is a meaningful roster upgrade "
                 f"available at {hv_goals[0]['position']}.")
    if isinstance(lineup_result, dict) and lineup_result.get("status") == "insufficient_weekly_data" and hv_goals:
        return "Weekly lineup projections are incomplete, but your longer-term roster priorities are still actionable."
    if consider_goals:
        return "No urgent move stands out, but there are lower-priority roster opportunities worth considering."
    if monitor_items:
        return "No immediate lineup or roster move stands out; monitor the situations listed below."
    return "Your current roster does not show a clear immediate move."

@mcp.tool()
async def get_fantasy_brief(league_id: int, team_id: int, week: int = None, year: int = None) -> dict:
    """Answers 'what should I do with my fantasy team right now?' by
    synthesizing the frozen decision engines (analyze_my_team, optimize_lineup,
    rank_waiver_targets, find_trade_targets) into one concise, prioritized
    briefing. This is a THIN ORCHESTRATION TOOL - it never re-decides
    lineups, waiver rankings, roster needs, or trade values; every fact
    traces to a named frozen source. Advisory only - makes no roster
    changes. Uses cached FantasyPros data only via its constituent tools;
    makes exactly one real ESPN league fetch (subsequent calls to the
    already-cached League object are free) plus one waiver free-agent
    lookup, identical to a standalone rank_waiver_targets call.

    Args:
        league_id: The ESPN fantasy football league ID
        team_id: The team ID to brief (real ESPN team_id, not list position)
        week: Optional NFL scoring week (1-18), exact optimize_lineup semantics -
              an explicit week is never substituted with another week's data.
        year: Optional year (defaults to current season if omitted)
    """
    _gfb_fp_cache_token = _GFB_FP_PARSED_CACHE_CONTEXT.set({})
    try:
        league_id_val, league_err = _validate_bounded_int(league_id, "league_id", 1, 999_999_999, league_id)
        if league_err:
            return {"error": "invalid_parameter", "message": league_err}
        team_id_val, team_err = _validate_bounded_int(team_id, "team_id", 1, 999, team_id)
        if team_err:
            return {"error": "invalid_parameter", "message": team_err}
        week_val, week_err = _ol_validate_week(week)
        if week_err:
            return {"error": "invalid_parameter", "message": week_err}
        resolved_year = _resolve_year(year)

        # --- Sequential direct calls to the 4 frozen public source tools.
        # api.get_league is cache-keyed by (league_id, year, credentials);
        # only the FIRST call below performs a real network fetch. ---
        team_result = await analyze_my_team(league_id=league_id_val, team_id=team_id_val, year=resolved_year)
        if _gfb_is_foundational_error(team_result):
            return {"error": team_result.get("error"), "message": team_result.get("message"),
                     "brief_status": "insufficient_data"}

        lineup_result = await optimize_lineup(league_id=league_id_val, team_id=team_id_val, week=week_val, year=resolved_year)
        waiver_result = await rank_waiver_targets(league_id=league_id_val, team_id=team_id_val, year=resolved_year)
        trade_result = await find_trade_targets(league_id=league_id_val, team_id=team_id_val, limit=10,
                                                   max_package_size=2, year=resolved_year)

        source_status = {
            "team_analysis": (_gfb_source_status_ok() if "error" not in team_result
                                else _gfb_source_status_error(team_result.get("message"))),
            "lineup": (_gfb_source_status_ok() if isinstance(lineup_result, dict) and "error" not in lineup_result
                        else _gfb_source_status_error(lineup_result.get("message") if isinstance(lineup_result, dict) else "unknown error")),
            "waivers": (_gfb_source_status_ok() if isinstance(waiver_result, dict) and "error" not in waiver_result
                         else _gfb_source_status_error(waiver_result.get("message") if isinstance(waiver_result, dict) else "unknown error")),
            "trades": (_gfb_source_status_ok() if isinstance(trade_result, dict) and "error" not in trade_result
                        else _gfb_source_status_error(trade_result.get("message") if isinstance(trade_result, dict) else "unknown error")),
        }
        if isinstance(lineup_result, dict) and lineup_result.get("status") == "insufficient_weekly_data":
            source_status["lineup"] = _gfb_source_status_insufficient("Weekly ESPN projection coverage insufficient for the requested week.")

        # --- Build lineup do_now action (Tool #24 truth, never reinterpreted) ---
        lineup_action = _gfb_build_lineup_action(lineup_result)

        # --- Build acquisition goals for each core position (grouping by
        # position only; severity affects priority, never grouping) ---
        acquisition_goals = []
        for pos in _GFB_CORE_ACQUISITION_POSITIONS:
            g = _gfb_build_acquisition_goal(pos, waiver_result, trade_result, team_result)
            if g is not None:
                acquisition_goals.append(g)

        def _goal_sort_key(g):
            sev_order = {"urgent": 0, "meaningful": 1, "minor": 2, "none": 3, "unknown": 4}
            pri_order = {"high_value_move": 0, "consider": 1}
            return (pri_order.get(g["priority"], 2), sev_order.get(_gfb_need_severity(team_result, g["position"]), 4), g["position"])
        acquisition_goals.sort(key=_goal_sort_key)

        monitor_items = _gfb_build_monitor_items(lineup_result, team_result, max_count=5)

        # --- Assemble top_actions: do_now first, then high_value_move
        # acquisition goals, then consider goals - max 3, no numeric score ---
        top_actions = []
        if lineup_action is not None:
            top_actions.append(lineup_action)
        for g in acquisition_goals:
            if g["priority"] != "high_value_move":
                continue
            top_actions.append({
                "priority": "high_value_move", "goal": g["goal"], "action": _gfb_action_text_for_goal(g),
                "why": g["why"], "source": g["source"], "evidence": {"position": g["position"]},
                "alternatives": g["alternatives"],
            })
        for g in acquisition_goals:
            if g["priority"] != "consider":
                continue
            if len(top_actions) >= 3:
                break
            top_actions.append({
                "priority": "consider", "goal": g["goal"], "action": _gfb_action_text_for_goal(g),
                "why": g["why"], "source": g["source"], "evidence": {"position": g["position"]},
                "alternatives": g["alternatives"],
            })
        top_actions = top_actions[:3]
        for i, a in enumerate(top_actions, start=1):
            a["rank"] = i

        foundational_ok = "error" not in team_result
        brief_status = _gfb_determine_brief_status(lineup_result, lineup_action, acquisition_goals, monitor_items, foundational_ok)
        headline = _gfb_generate_headline(brief_status, lineup_result, lineup_action, acquisition_goals, monitor_items)

        # --- Compact subsections (compression allowed, reinterpretation not) ---
        lineup_out = None
        if isinstance(lineup_result, dict) and "error" not in lineup_result:
            lineup_out = {
                "status": lineup_result.get("status"), "confidence": lineup_result.get("confidence"),
                "requested_week": lineup_result.get("requested_week"),
                "weekly_projection_available": lineup_result.get("weekly_projection_available"),
                "recommended_moves": lineup_result.get("moves"),
                "weekly_projection_delta": lineup_result.get("weekly_projection_comparison", {}).get("weekly_projection_delta"),
                "structural_feasible": lineup_result.get("current_lineup", {}).get("lineup_feasible"),
                "lineup_gaps": lineup_result.get("current_lineup", {}).get("lineup_gaps"),
                "warnings": lineup_result.get("warnings"),
            }

        team_snapshot_out = None
        if isinstance(team_result, dict) and "error" not in team_result:
            team_snapshot_out = {
                "needs": team_result.get("positional_needs"),
                "strengths": [pos for pos in ("QB", "RB", "WR", "TE", "FLEX")
                               if team_result.get("position_analysis", {}).get(pos, {}).get("relative_label") == "strong"],
                "core_assets": team_result.get("core_assets"),
                "trade_surplus": team_result.get("trade_surplus"),
            }

        waivers_out = None
        if isinstance(waiver_result, dict) and "error" not in waiver_result:
            waivers_out = {
                "status": waiver_result.get("overall_recommendation"),
                "top_targets": waiver_result.get("recommendations", [])[:3],
            }

        trades_out = None
        if isinstance(trade_result, dict) and "error" not in trade_result:
            trades_out = {
                "status": "search_completed",
                "search_ran": True,
                "search_truncated": trade_result.get("search_summary", {}).get("search_truncated"),
                "top_targets": trade_result.get("trade_targets", [])[:3],
            }

        warnings_out = []
        for dom, res in (("lineup", lineup_result), ("waivers", waiver_result), ("trades", trade_result)):
            if isinstance(res, dict):
                warnings_out.extend(f"[{dom}] {w}" for w in (res.get("warnings") or []))
        if isinstance(trade_result, dict) and trade_result.get("search_summary", {}).get("search_truncated"):
            warnings_out.append("[trades] Trade search used the frozen bounded evaluation budget; these are the "
                                  "top recommendations found within that search, not a claim of league-wide optimality.")

        return {
            "league_id": league_id_val, "team_id": team_id_val,
            "team_name": (team_result.get("team_name") if isinstance(team_result, dict) else None),
            "year": resolved_year, "requested_week": (lineup_result.get("requested_week") if isinstance(lineup_result, dict) else week_val),

            "brief_status": brief_status,
            "headline": headline,

            "top_actions": top_actions,

            "lineup": lineup_out,
            "team_snapshot": team_snapshot_out,
            "waivers": waivers_out,
            "trades": trades_out,

            "monitor": monitor_items,

            "source_status": source_status,

            "data_quality": {
                "lineup": (lineup_result.get("data_quality") if isinstance(lineup_result, dict) else None),
                "team_analysis": {"warnings": team_result.get("warnings")} if isinstance(team_result, dict) and "error" not in team_result else None,
                "waivers": {"data_freshness": waiver_result.get("data_freshness")} if isinstance(waiver_result, dict) and "error" not in waiver_result else None,
                "trades": {"search_summary": trade_result.get("search_summary")} if isinstance(trade_result, dict) and "error" not in trade_result else None,
            },

            "warnings": warnings_out,
            "methodology_notes": [
                "get_fantasy_brief is a thin orchestration tool - it never re-decides lineups, waiver rankings, "
                "positional needs, or trade values. Every fact traces to a named frozen source tool.",
                "Lineup advice is Tool #24 (optimize_lineup) truth, never reinterpreted; a lineup action only "
                "appears when optimize_lineup itself reports an actionable status.",
                "Acquisition goals group waiver and trade options by POSITION only - formal team-analysis "
                "need severity affects priority ordering, never whether two options are grouped together.",
                "Route arbitration (waiver vs trade primary) uses only frozen categorical labels - no numeric "
                "score is computed across domains, and ESPN weekly projections are never blended with "
                "FantasyPros season values.",
                "Trade search always runs with the normal frozen max_package_size=2 surface; results are "
                "compressed to the top 3 for display without altering their frozen order or verdict.",
                "An isolated source domain failure (waivers, trades, or lineup) never invalidates unrelated "
                "valid domains - source_status reports each domain independently.",
                "This tool is advisory only and makes zero roster/lineup/transaction changes.",
            ],
        }
    except Exception as e:
        return _error_response("building fantasy brief", e)
    finally:
        _GFB_FP_PARSED_CACHE_CONTEXT.reset(_gfb_fp_cache_token)

def _gfb_action_text_for_goal(g):
    path = g["recommended_path"]
    if path["type"] == "waiver":
        return f"Upgrade {g['position']}: add {path.get('add_player')}, drop {path.get('drop_player')} (waiver)."
    pt = path.get("primary_target", {})
    return (f"Upgrade {g['position']}: pursue a trade for {pt.get('player')} "
             f"({path.get('recommendation')}, verdict {path.get('verdict')}).")



# --- Helpers/tools added for MULTI-LEAGUE FOUNDATION PHASE 1 (2026-08-14,
# existing 25 tools untouched). Registry loading/validation lives entirely
# in league_registry.py (config-only, no ESPN/FantasyPros logic). This
# block contains ONLY the additive ESPN-context glue + the two new public
# tools. NO commissioner concepts, NO role concepts, NO write-capability
# concepts exist anywhere in this block - explicitly out of scope for
# Phase 1. Both new tools are read-only: zero ESPN writes, zero
# FantasyPros refresh calls, zero roster/lineup/trade/waiver actions. ---

def _normalize_swid(raw) -> str:
    """Canonical comparison form only - braces stripped, uppercased,
    trimmed. NEVER mutates or persists the stored credential itself;
    used solely for in-memory equality testing against team.owners[].id."""
    if not raw or not isinstance(raw, str):
        return None
    return raw.strip().strip("{}").upper()

def _resolve_my_team(league, authenticated_swid: str) -> dict:
    """Matches the authenticated SWID against every real team's
    owners[].id (live-verified against a representative live
    development league: correctly auto-resolved the authenticated
    owner's team without being told).
    Never guesses on ambiguity - returns explicit candidates instead.
    Output NEVER includes any SWID/owner-id value, only team_id/team_name."""
    norm_target = _normalize_swid(authenticated_swid)
    if not norm_target:
        return {"status": "team_not_resolved", "team_id": None, "team_name": None,
                "resolution_method": "no_credential_available", "candidates": []}

    matches = []
    for t in league.teams:
        owner_ids = [_normalize_swid(o.get("id")) for o in (getattr(t, "owners", None) or [])]
        if norm_target in owner_ids:
            matches.append(t)

    if len(matches) == 1:
        t = matches[0]
        return {"status": "resolved", "team_id": t.team_id, "team_name": t.team_name,
                "resolution_method": "owner_swid_match", "candidates": []}
    if len(matches) == 0:
        return {"status": "team_not_resolved", "team_id": None, "team_name": None,
                "resolution_method": "owner_swid_match", "candidates": []}
    return {"status": "ambiguous_team_ownership", "team_id": None, "team_name": None,
            "resolution_method": "owner_swid_match",
            "candidates": [{"team_id": t.team_id, "team_name": t.team_name} for t in matches]}

def _league_context_for_entry(alias: str, entry: dict, resolved_year: int, authenticated_swid: str) -> dict:
    """Shared ESPN-access + team-resolution logic for one registry entry.
    Used by both list_my_leagues and get_league_context so there is
    exactly ONE code path for 'what does this registered league look
    like right now' - no duplicated per-league logic between the two
    tools. Never calls FantasyPros; never re-implements PPR/HALF/STD
    detection (reuses the frozen _detect_league_scoring_bucket)."""
    league_id = entry["league_id"]
    display_name = entry.get("display_name")
    row = {"alias": alias, "league_id": league_id, "display_name": display_name,
           "league_name": None, "access_status": None, "league": None, "my_team": None}

    if entry.get("enabled", True) is False:
        row["access_status"] = "disabled"
        return row

    try:
        payload = _fetch_core_league_payload(league_id, resolved_year)
        settings_result = build_league_settings(payload, league_id, resolved_year)
        scoring_bucket = _detect_league_scoring_bucket(settings_result.get("scoring_rules", []))
        row["league_name"] = settings_result.get("league_name")
        row["league"] = {
            "name": settings_result.get("league_name"),
            "team_count": settings_result.get("team_count"),
            "scoring_type": settings_result.get("scoring_type"),
            "scoring_bucket": scoring_bucket,
            "playoff_team_count": settings_result.get("playoff_team_count"),
        }
        my_team = resolve_my_team_from_payload(payload, authenticated_swid)
        row["my_team"] = my_team
        if my_team["status"] == "resolved":
            row["access_status"] = "accessible"
        elif my_team["status"] == "ambiguous_team_ownership":
            row["access_status"] = "ambiguous_team_ownership"
        else:
            row["access_status"] = "team_not_resolved"
    except Exception as e:
        if _is_private_league_error(e):
            # Classify authentication failures before serializing exception
            # text. This keeps the same secret-safe public contract while
            # the project-owned transport owns the HTTP/session boundary.
            row["access_status"] = "authentication_required"
        elif "404" in str(e) or "season" in str(e).lower():
            row["access_status"] = "season_unavailable"
            row["_error_detail"] = str(e)[:200]
        else:
            row["access_status"] = "inaccessible"
            row["_error_detail"] = str(e)[:200]
    return row

@mcp.tool()
async def list_my_leagues(year: int = None) -> dict:
    """List every league configured in the local, non-secret league
    registry (league_registry.json), with live ESPN access status and
    automatic team-ownership resolution for each. Read-only - makes no
    ESPN writes, no FantasyPros refresh calls. A malformed registry
    file returns a structured registry_error and never affects Tools
    #1-25. An individual league's ESPN access failure never removes it
    from this list - every configured entry is always returned with an
    explicit access_status. Contains no commissioner/role/permission
    concepts (out of scope for this phase).

    Args:
        year: NFL season year. Defaults via the same _resolve_year
              semantics used by Tools #1-25 (current league year,
              falling back a season during the off-season window).
    """
    try:
        resolved_year = _resolve_year(year)
        try:
            registry = league_registry.load_registry()
        except league_registry.RegistryError as e:
            return {"error": "registry_error", "message": str(e)}

        default_alias, _ = league_registry.get_default_league(registry)
        all_entries = sorted(registry.get("leagues", {}).items())
        authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")

        leagues_out = []
        accessible_count = 0
        warnings_out = []
        for alias, entry in all_entries:
            row = _league_context_for_entry(alias, entry, resolved_year, authenticated_swid)
            detail = row.pop("_error_detail", None)
            if detail:
                warnings_out.append(f"{alias}: {detail}")
            if row["access_status"] == "accessible":
                accessible_count += 1
            leagues_out.append(row)

        return {
            "year": resolved_year, "default_league": default_alias,
            "league_count": len(leagues_out), "accessible_count": accessible_count,
            "leagues": leagues_out, "warnings": warnings_out,
        }
    except Exception as e:
        return _error_response("listing my leagues", e)

@mcp.tool()
async def get_league_context(alias: str = None, league_id: int = None, year: int = None) -> dict:
    """Resolve ONE league's identity/settings/my-team context from the
    local league registry. Exactly one of alias/league_id normally
    identifies the league; if both are supplied they must refer to the
    SAME registered entry (conflicting_parameters otherwise). If
    neither is supplied, resolves the registry's default_league. A
    league_id not present in the registry returns league_not_registered
    - this tool is a personal registry-context lookup, not a duplicate
    of the existing ad-hoc get_league_info tool. Read-only; no
    commissioner/role/permission concepts (out of scope for this phase).

    Args:
        alias: Registered league alias (case-insensitive, whitespace-trimmed)
        league_id: Registered ESPN league ID
        year: NFL season year. Defaults via the same _resolve_year
              semantics used by Tools #1-25.
    """
    try:
        resolved_year = _resolve_year(year)
        try:
            registry = league_registry.load_registry()
        except league_registry.RegistryError as e:
            return {"error": "registry_error", "message": str(e)}

        if alias is not None and league_id is not None:
            try:
                alias_norm, alias_entry = league_registry.resolve_alias(registry, alias)
            except league_registry.RegistryError as e:
                return {"error": "alias_not_found", "message": str(e)}
            try:
                id_alias_norm, _ = league_registry.resolve_league_id(registry, league_id)
            except league_registry.RegistryError as e:
                return {"error": "league_not_registered", "message": str(e)}
            if alias_norm != id_alias_norm:
                return {"error": "conflicting_parameters",
                        "message": f"alias '{alias}' resolves to '{alias_norm}' but league_id "
                                    f"{league_id} resolves to '{id_alias_norm}' - these must match."}
            resolved_alias, entry = alias_norm, alias_entry
        elif alias is not None:
            try:
                resolved_alias, entry = league_registry.resolve_alias(registry, alias)
            except league_registry.RegistryError as e:
                return {"error": "alias_not_found", "message": str(e)}
        elif league_id is not None:
            try:
                resolved_alias, entry = league_registry.resolve_league_id(registry, league_id)
            except league_registry.RegistryError as e:
                return {"error": "league_not_registered", "message": str(e)}
        else:
            resolved_alias, entry = league_registry.get_default_league(registry)

        authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")
        row = _league_context_for_entry(resolved_alias, entry, resolved_year, authenticated_swid)
        warnings_out = []
        detail = row.pop("_error_detail", None)
        if detail:
            warnings_out.append(detail)

        return {
            "alias": row["alias"], "league_id": row["league_id"],
            "display_name": row["display_name"], "year": resolved_year,
            "access_status": row["access_status"],
            "league": row["league"], "my_team": row["my_team"],
            "warnings": warnings_out,
        }
    except Exception as e:
        return _error_response("resolving league context", e)

# --- COMMISSIONER READ/AUDIT FOUNDATION - PHASE C1 (2026-08-15) ---
# get_commissioner_context is the FIRST commissioner tool. It answers
# "what commissioner league am I working with and what are its current
# administrative rules/settings?" - NOT an audit, NOT an investigation,
# NOT a write tool. Commissioner eligibility is a SEPARATE namespace
# from league_registry.json's navigation registry (by explicit design
# decision) - commissioner_config.py's guard is checked FIRST, before
# any ESPN fetch, so hunt_ball/molnar_mania are rejected without ever
# calling api.get_league() for them. Config presence means READ
# eligibility ONLY - it never implies or grants ESPN write permission;
# that is an entirely separate, unbuilt C10+ system.

def _commissioner_resolve_guard(alias, league_id):
    """Shared commissioner eligibility guard - the SAME guard EVERY
    commissioner tool (C1's get_commissioner_context and both new C2
    tools) calls before any ESPN fetch. Returns
    (resolved_alias, entry, None) on success, or
    (None, None, structured_error_dict) on failure - callers return the
    error dict directly without ever touching ESPN. Factored out here
    specifically so C2 does not duplicate C1's guard logic (per explicit
    design decision - all commissioner tools share ONE eligibility
    boundary)."""
    try:
        config = commissioner_config.load_config()
    except commissioner_config.CommissionerConfigError as e:
        return None, None, {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}

    try:
        resolved_alias, entry = commissioner_config.resolve_commissioner_league(
            config, alias=alias, league_id=league_id)
    except commissioner_config.CommissionerConfigError as e:
        msg = str(e)
        if msg.startswith("not_configured"):
            return None, None, {"status": "error", "error": "commissioner_not_configured_for_league", "message": msg}
        if msg.startswith("mismatch"):
            return None, None, {"status": "error", "error": "commissioner_league_mismatch", "message": msg}
        if msg.startswith("target_required"):
            return None, None, {"status": "error", "error": "commissioner_league_required", "message": msg}
        return None, None, {"status": "error", "error": "commissioner_config_invalid", "message": msg}

    return resolved_alias, entry, None

def _commissioner_select_teams(league, team_id):
    """Reuses the frozen _find_team_by_id resolver (never positional
    indexing) to optionally narrow to one team. Returns
    (teams_list, error_dict_or_None). teams_list is sorted by team_id
    ascending for deterministic output when team_id is None."""
    if team_id is None:
        return sorted(league.teams, key=lambda t: t.team_id), None
    team = _find_team_by_id(league, team_id)
    if team is None:
        valid_ids = sorted(t.team_id for t in league.teams)
        return None, {"status": "error", "error": "invalid_team_id",
                      "message": f"No team with team_id={team_id} in this league.",
                      "valid_team_ids": valid_ids}
    return [team], None

_COMMISSIONER_SEVERITY_RANK = {"action_required": 0, "review": 1, "monitor": 2, "info": 3}

def _commissioner_findings_sort_key(finding):
    """Deterministic ordering: severity, then finding type, then slot,
    then player name - never relies on raw dict iteration order."""
    return (_COMMISSIONER_SEVERITY_RANK.get(finding.get("severity"), 9),
            finding.get("type", ""), finding.get("slot") or "", finding.get("player_name") or "")

def _commissioner_normalize_lineup_players(roster_or_lineup, is_historical: bool):
    """Normalizes either current team.roster (list[Player]) or a
    project-owned historical lineup compatibility objects into one common shape
    for finding evaluation. on_bye is only reliably known for the
    historical project-owned lineup path (on_bye_week) - for current-scope Player
    objects it is left as None (unknown), never guessed, per the
    explicit design decision to not invent bye status from schedule
    data that is not an existing reliable primitive."""
    out = []
    for p in roster_or_lineup:
        lineup_slot = getattr(p, "slot_position", None) if is_historical else getattr(p, "lineupSlot", None)
        out.append({
            "playerId": getattr(p, "playerId", None),
            "player_name": getattr(p, "name", None),
            "lineup_slot": lineup_slot,
            "eligible_slots": list(getattr(p, "eligibleSlots", []) or []),
            "injury_status": getattr(p, "injuryStatus", None),
            "on_bye": bool(getattr(p, "on_bye_week", False)) if is_historical else None,
        })
    return out

def _commissioner_audit_team_lineup(players, slot_counts, source, week_label=None):
    """Evaluates ONE team's normalized lineup players against
    league.settings.position_slot_counts. Implements exactly the four
    objectively-supportable finding types authorized for C2 - never
    FantasyPros, never a strategy judgment. Reuses the frozen
    FLEX_EXCLUDED_SLOT_KEYS / _parse_flex_eligibility slot vocabulary
    (no competing position-slot ontology introduced)."""
    findings = []

    # 1. empty_required_starter_slot - exact slot_key match, mirrors the
    # frozen _ol_check_current_lineup_feasibility pattern (ESPN-only,
    # no FP dependency here).
    for slot_key, required in (slot_counts or {}).items():
        if not required or slot_key in FLEX_EXCLUDED_SLOT_KEYS:
            continue
        occupied = sum(1 for p in players if p["lineup_slot"] == slot_key)
        if occupied < required:
            findings.append({
                "type": "empty_required_starter_slot", "issue_class": "structural", "severity": "review",
                "slot": slot_key, "required": required, "occupied": occupied, "missing": required - occupied,
                "player_name": None, "source": source,
                "basis": f"{slot_key} starter requirement={required}; occupied={occupied}",
            })

    for p in players:
        slot = p["lineup_slot"]
        if slot is None or slot in FLEX_EXCLUDED_SLOT_KEYS:
            continue  # bench/IR/unassigned - not a starter, out of scope for these 4 findings

        # 2. assigned_player_ineligible_for_slot - reliable via ESPN eligibleSlots.
        if p["eligible_slots"] and slot not in p["eligible_slots"]:
            findings.append({
                "type": "assigned_player_ineligible_for_slot", "issue_class": "structural", "severity": "review",
                "slot": slot, "player_name": p["player_name"], "playerId": p["playerId"],
                "eligible_slots": p["eligible_slots"], "source": source,
                "basis": f"player assigned to starter slot '{slot}' but eligibleSlots={p['eligible_slots']}",
            })

        # 3. starting_player_out - exact "OUT" enumeration match, same
        # convention already used by optimize_lineup elsewhere in this file.
        if p["injury_status"] == "OUT":
            findings.append({
                "type": "starting_player_out", "issue_class": "warning", "severity": "monitor",
                "slot": slot, "player_name": p["player_name"], "playerId": p["playerId"], "source": source,
                "basis": "player injuryStatus=OUT while assigned to a starter slot",
            })

        # 4. starting_player_on_bye - ONLY reliable for the historical
        # historical lineup path (on_bye is None at current scope - never guessed).
        if p["on_bye"] is True:
            findings.append({
                "type": "starting_player_on_bye", "issue_class": "warning", "severity": "monitor",
                "slot": slot, "player_name": p["player_name"], "playerId": p["playerId"], "source": source,
                "basis": f"player on_bye_week=true (source: {source}) while assigned to a starter slot",
            })

    findings.sort(key=_commissioner_findings_sort_key)
    return findings

def _commissioner_audit_team_roster(team, slot_counts):
    """Evaluates ONE team's CURRENT roster occupancy/compliance -
    never historical, never mutates anything. total configured capacity
    is the sum of ALL configured slot counts (starters + BE + IR),
    matching actual ESPN roster-size semantics."""
    findings = []
    roster = team.roster
    configured_capacity = sum(v for v in (slot_counts or {}).values() if isinstance(v, int))
    roster_count = len(roster)
    available_spots = max(0, configured_capacity - roster_count)

    if roster_count > configured_capacity:
        findings.append({
            "type": "roster_over_capacity", "issue_class": "structural", "severity": "review",
            "roster_count": roster_count, "configured_capacity": configured_capacity, "source": "espn_current_roster",
            "basis": f"roster_count={roster_count} exceeds configured_capacity={configured_capacity}",
        })

    slot_occupancy = {}
    seen_player_ids = {}
    for p in roster:
        slot = getattr(p, "lineupSlot", None)
        slot_occupancy[slot] = slot_occupancy.get(slot, 0) + 1
        pid = getattr(p, "playerId", None)
        if pid is not None:
            seen_player_ids[pid] = seen_player_ids.get(pid, 0) + 1
        if slot not in (slot_counts or {}):
            findings.append({
                "type": "unknown_slot_assignment", "issue_class": "data_quality", "severity": "monitor",
                "slot": slot, "player_name": getattr(p, "name", None), "playerId": pid, "source": "espn_current_roster",
                "basis": f"lineupSlot='{slot}' is not present in this league's configured position_slot_counts",
            })

    for slot_key, configured in (slot_counts or {}).items():
        occupied = slot_occupancy.get(slot_key, 0)
        if slot_key == "IR":
            if occupied > (configured or 0):
                findings.append({
                    "type": "ir_over_capacity", "issue_class": "structural", "severity": "review",
                    "slot": "IR", "occupied": occupied, "configured": configured, "source": "espn_current_roster",
                    "basis": f"IR occupancy={occupied} exceeds configured IR capacity={configured}",
                })
        elif occupied > (configured or 0):
            findings.append({
                "type": "slot_over_capacity", "issue_class": "structural", "severity": "review",
                "slot": slot_key, "occupied": occupied, "configured": configured, "source": "espn_current_roster",
                "basis": f"{slot_key} occupancy={occupied} exceeds configured capacity={configured}",
            })

    for pid, count in seen_player_ids.items():
        if count > 1:
            findings.append({
                "type": "duplicate_player_record", "issue_class": "data_integrity", "severity": "review",
                "playerId": pid, "occurrences": count, "source": "espn_current_roster",
                "basis": f"player ID {pid} appears {count} times on this roster",
            })

    findings.sort(key=_commissioner_findings_sort_key)
    return {
        "roster_count": roster_count, "configured_capacity": configured_capacity,
        "available_spots": available_spots, "slot_occupancy": slot_occupancy, "findings": findings,
    }

@mcp.tool()
async def get_commissioner_context(alias: str = None, league_id: int = None, year: int = None) -> dict:
    """Resolve ONE commissioner-eligible league's identity and current
    live governance/settings snapshot. Read-only; not an audit, not an
    investigation, not a recommendation, not a write tool. Commissioner
    eligibility is checked against commissioner_config.json - a
    SEPARATE namespace/security boundary from the league_registry.json
    navigation registry - BEFORE any ESPN fetch is attempted, so a
    league that is not configured for commissioner reads (e.g. a normal
    member league you also happen to have ESPN access to) is rejected
    without ever contacting ESPN for it. Being configured here means
    ONLY "eligible for commissioner READ/AUDIT tools" - it NEVER means
    ESPN write permission has been granted or verified.

    Args:
        alias: Configured commissioner-league alias (case-insensitive).
        league_id: Configured commissioner ESPN league ID.
                   Exactly one of alias/league_id normally identifies
                   the league; if both are supplied they must resolve
                   to the SAME configured commissioner league. If
                   neither is supplied and exactly one commissioner
                   league is configured, it is used automatically.
        year: NFL season year. Defaults via the same _resolve_year
              semantics used by every other tool in this server.
    """
    try:
        resolved_alias, entry, guard_error = _commissioner_resolve_guard(alias, league_id)
        if guard_error is not None:
            return guard_error

        # Guard passed - now, and only now, do we touch ESPN through the
        # project-owned transport. No separate commissioner credentials
        # or write-capable session is introduced.
        resolved_year = _resolve_year(year)
        resolved_league_id = entry["league_id"]
        commissioner_payload = _fetch_commissioner_current_payload(resolved_league_id, resolved_year)
        league = build_commissioner_snapshot(commissioner_payload, resolved_league_id, resolved_year)
        settings = league.settings

        authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")
        my_team = resolve_my_team_from_payload(commissioner_payload, authenticated_swid)

        divisions = [{"division_id": did, "name": dname}
                     for did, dname in sorted(getattr(settings, "division_map", {}).items())]
        # position_slot_counts already comes fully normalized (ESPN slot
        # id -> human-readable label) by the frozen espn-api Settings
        # class - no second position map is introduced here.
        roster_slots = dict(getattr(settings, "position_slot_counts", {}) or {})

        return {
            "status": "ok",
            "commissioner": {
                "configured_for_commissioner_reads": True,
                "scope": "read_only",
                "alias": resolved_alias,
                "write_scope": "not_available",
            },
            "league": {
                "league_id": resolved_league_id,
                "league_name": getattr(settings, "name", None),
                "year": resolved_year,
                "current_week": getattr(league, "current_week", None),
                "team_count": getattr(settings, "team_count", None),
            },
            "my_team": {"team_id": my_team["team_id"], "team_name": my_team["team_name"]},
            "governance": {
                "regular_season_weeks": getattr(settings, "reg_season_count", None),
                "playoff_team_count": getattr(settings, "playoff_team_count", None),
                "playoff_matchup_period_length": getattr(settings, "playoff_matchup_period_length", None),
                "veto_votes_required": getattr(settings, "veto_votes_required", None),
                "trade_deadline": getattr(settings, "trade_deadline", None),
                "trade_deadline_configured": bool(getattr(settings, "trade_deadline", 0)),
                "faab": getattr(settings, "faab", None),
                "acquisition_budget": getattr(settings, "acquisition_budget", None),
                "keeper_count": getattr(settings, "keeper_count", None),
                "divisions": divisions,
                "roster_slots": roster_slots,
            },
            "source": "espn_live_settings",
        }
    except commissioner_config.CommissionerConfigError as e:
        return {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}
    except Exception as e:
        return _error_response("resolving commissioner context", e)

# --- COMMISSIONER READ/AUDIT - PHASE C2 (2026-08-15) ---
# Two new tools: commissioner_audit_lineups, commissioner_audit_rosters.
# Both reuse the exact same _commissioner_resolve_guard as C1 - guard
# FIRST, ESPN fetch SECOND, so member leagues are rejected without ever
# calling api.get_league() for them. Both are ESPN-only (zero
# FantasyPros involvement) and NEVER call league.load_roster_week
# (would mutate the shared cached League's team.roster for every team).
@mcp.tool()
async def commissioner_audit_lineups(alias: str = None, league_id: int = None, year: int = None,
                                       week: int = None, team_id: int = None) -> dict:
    """Audits commissioner-league lineup STRUCTURE for objective,
    administrative findings only - never fantasy-strategy judgment.
    Read-only; makes zero FantasyPros calls. Detects exactly four
    finding types: empty_required_starter_slot, assigned_player_
    ineligible_for_slot (via ESPN eligibleSlots), starting_player_out
    (injuryStatus == "OUT"), and starting_player_on_bye (historical
    scope only, via project-owned on_bye_week parsing). Does NOT flag ordinary
    strategic choices (weak starter, benched star, poor projection).

    Two clearly-separated, never-mixed data sources: if `week` is
    omitted or resolves to the league's current scoring period, uses
    the already-loaded team.roster/player.lineupSlot (source=
    espn_current_roster, zero extra network calls). If `week` names an
    explicit different historical week, uses the project-owned ESPN historical-lineup read
    ONLY (source=espn_box_score_week_N) - if ESPN's historical lineup
    data is unavailable (a known preseason limitation), returns a
    structured partial/unavailable response, never a crash and never a
    false "no issues" conclusion.

    Args:
        alias: Configured commissioner-league alias.
        league_id: Configured commissioner ESPN league ID.
        year: NFL season year (same _resolve_year semantics as every
              other tool).
        week: Optional explicit scoring week for historical audit.
              Omit for the current lineup.
        team_id: Optional single team ID (sparse IDs fully supported
                 via the frozen _find_team_by_id resolver). Omit for
                 all teams.
    """
    try:
        resolved_alias, entry, guard_error = _commissioner_resolve_guard(alias, league_id)
        if guard_error is not None:
            return guard_error

        resolved_year = _resolve_year(year)
        resolved_league_id = entry["league_id"]
        commissioner_payload = _fetch_commissioner_current_payload(resolved_league_id, resolved_year)
        league = build_commissioner_snapshot(commissioner_payload, resolved_league_id, resolved_year)
        settings = league.settings
        slot_counts = getattr(settings, "position_slot_counts", {}) or {}

        teams, team_error = _commissioner_select_teams(league, team_id)
        if team_error is not None:
            return team_error

        current_week = getattr(league, "current_week", 0) or 0
        limitations = ["lineup_lock_status is not_modeled - game-lock/timing state is not evaluated",
                       "starting_player_out/starting_player_on_bye are warnings only, never an illegal-lineup conclusion",
                       "no fantasy-strategy quality judgment is performed"]

        if week is None or week == current_week:
            # PATH A - current lineup, zero extra network calls.
            scope, source, resolved_week = "current", "espn_current_roster", current_week
            team_rows = []
            for team in teams:
                players = _commissioner_normalize_lineup_players(team.roster, is_historical=False)
                findings = _commissioner_audit_team_lineup(players, slot_counts, source)
                team_rows.append({"team_id": team.team_id, "team_name": team.team_name, "findings": findings})
            limitations.append("starting_player_on_bye is not evaluated at current scope (no reliable existing "
                                "primitive) - use an explicit historical week for bye detection")
        else:
            # PATH B - explicit historical week, box_scores() ONLY, never load_roster_week.
            matchup_periods = getattr(settings, "matchup_periods", {}) or {}
            if not isinstance(week, int) or isinstance(week, bool) or week <= 0 or str(week) not in matchup_periods:
                return {"status": "error", "error": "invalid_week",
                        "message": f"week={week!r} is not a valid scoring week for this league.",
                        "valid_weeks": sorted((int(k) for k in matchup_periods.keys()), key=int) if matchup_periods else []}
            scope, source, resolved_week = "historical", f"espn_box_score_week_{week}", week
            try:
                boxes = _fetch_historical_lineup_boxes(league.league_id, league.year, week, league.settings)
            except Exception as e:
                return {
                    "status": "partial", "error": None,
                    "commissioner": {"configured_for_commissioner_reads": True, "alias": resolved_alias, "scope": "read_only"},
                    "league": {"league_id": resolved_league_id, "league_name": getattr(settings, "name", None), "year": resolved_year},
                    "audit": {"requested_week": week, "resolved_week": week, "scope": scope, "source": source,
                               "lineup_lock_status": "not_modeled"},
                    "data_availability": {"historical_lineup": "unavailable",
                                            "reason": "espn_box_score_lineup_data_unavailable"},
                    "summary": {"teams_evaluated": 0, "teams_with_findings": 0, "findings_total": 0},
                    "teams": [], "limitations": limitations + [
                        f"ESPN historical lineup data for week {week} was unavailable "
                        f"(commonly seen in preseason) - this is NOT evidence of a clean lineup"],
                }

            box_by_team_id = {}
            for box in boxes:
                if isinstance(box.home_team, object) and hasattr(box.home_team, "team_id"):
                    box_by_team_id[box.home_team.team_id] = box.home_lineup
                if isinstance(box.away_team, object) and hasattr(box.away_team, "team_id"):
                    box_by_team_id[box.away_team.team_id] = box.away_lineup

            team_rows = []
            for team in teams:
                lineup = box_by_team_id.get(team.team_id)
                if lineup is None:
                    team_rows.append({"team_id": team.team_id, "team_name": team.team_name, "findings": [],
                                       "data_availability": "unavailable_for_this_team"})
                    continue
                players = _commissioner_normalize_lineup_players(lineup, is_historical=True)
                findings = _commissioner_audit_team_lineup(players, slot_counts, source, week_label=week)
                team_rows.append({"team_id": team.team_id, "team_name": team.team_name, "findings": findings})

        team_rows.sort(key=lambda t: t["team_id"])
        findings_total = sum(len(t["findings"]) for t in team_rows)
        structural = sum(1 for t in team_rows for f in t["findings"] if f.get("issue_class") == "structural")
        warnings_n = sum(1 for t in team_rows for f in t["findings"] if f.get("issue_class") == "warning")

        return {
            "status": "ok",
            "commissioner": {"configured_for_commissioner_reads": True, "alias": resolved_alias, "scope": "read_only"},
            "league": {"league_id": resolved_league_id, "league_name": getattr(settings, "name", None), "year": resolved_year},
            "audit": {"requested_week": week, "resolved_week": resolved_week, "scope": scope, "source": source,
                       "lineup_lock_status": "not_modeled"},
            "summary": {"teams_evaluated": len(team_rows),
                         "teams_with_findings": sum(1 for t in team_rows if t["findings"]),
                         "findings_total": findings_total, "structural_findings": structural, "warnings": warnings_n},
            "teams": team_rows,
            "limitations": limitations,
        }
    except commissioner_config.CommissionerConfigError as e:
        return {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}
    except Exception as e:
        return _error_response("auditing commissioner lineups", e)

@mcp.tool()
async def commissioner_audit_rosters(alias: str = None, league_id: int = None, year: int = None,
                                        team_id: int = None) -> dict:
    """Audits commissioner-league CURRENT roster occupancy/compliance
    against live ESPN league settings - never historical, never calls
    load_roster_week. Read-only; makes zero FantasyPros calls. Detects:
    roster_over_capacity (roster_count exceeds the sum of ALL configured
    slot counts), slot_over_capacity / ir_over_capacity (per-slot
    occupancy exceeds configured count), duplicate_player_record (same
    non-null ESPN player ID twice - a data-integrity signal, not
    misconduct), and unknown_slot_assignment (a lineupSlot not present
    in this league's configured position_slot_counts). Under-capacity
    is factual metadata (available_spots), never a violation. Does NOT
    determine team inactivity (requires multi-week history - deferred
    to a later phase).

    Args:
        alias: Configured commissioner-league alias.
        league_id: Configured commissioner ESPN league ID.
        year: NFL season year (same _resolve_year semantics as every
              other tool).
        team_id: Optional single team ID (sparse IDs fully supported).
                 Omit for all teams.
    """
    try:
        resolved_alias, entry, guard_error = _commissioner_resolve_guard(alias, league_id)
        if guard_error is not None:
            return guard_error

        resolved_year = _resolve_year(year)
        resolved_league_id = entry["league_id"]
        commissioner_payload = _fetch_commissioner_current_payload(resolved_league_id, resolved_year)
        league = build_commissioner_snapshot(commissioner_payload, resolved_league_id, resolved_year)
        settings = league.settings
        slot_counts = getattr(settings, "position_slot_counts", {}) or {}

        teams, team_error = _commissioner_select_teams(league, team_id)
        if team_error is not None:
            return team_error

        team_rows = []
        for team in teams:
            audit = _commissioner_audit_team_roster(team, slot_counts)
            team_rows.append({
                "team_id": team.team_id, "team_name": team.team_name,
                "roster_count": audit["roster_count"], "configured_capacity": audit["configured_capacity"],
                "available_spots": audit["available_spots"], "slot_occupancy": audit["slot_occupancy"],
                "findings": audit["findings"],
            })
        team_rows.sort(key=lambda t: t["team_id"])
        findings_total = sum(len(t["findings"]) for t in team_rows)

        return {
            "status": "ok",
            "commissioner": {"configured_for_commissioner_reads": True, "alias": resolved_alias, "scope": "read_only"},
            "league": {"league_id": resolved_league_id, "league_name": getattr(settings, "name", None), "year": resolved_year},
            "audit": {"source": "espn_current_roster"},
            "summary": {"teams_evaluated": len(team_rows),
                         "teams_with_findings": sum(1 for t in team_rows if t["findings"]),
                         "findings_total": findings_total},
            "teams": team_rows,
            "limitations": ["roster inactivity/abandonment is not determined here - requires multi-week "
                             "history and transaction data (a later phase)",
                             "IR eligibility is not modeled - ESPN's eligibleSlots universally includes IR "
                             "for observed players, so it is not a reliable eligibility signal"],
        }
    except commissioner_config.CommissionerConfigError as e:
        return {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}
    except Exception as e:
        return _error_response("auditing commissioner rosters", e)

# --- COMMISSIONER READ/AUDIT - PHASE C3 (2026-08-15) ---
# One new tool: commissioner_audit_transactions. Reuses the exact same
# _commissioner_resolve_guard as C1/C2 - guard FIRST, league access
# SECOND, transaction fetch THIRD. ESPN-only (zero FantasyPros calls).
# Never calls load_roster_week, box_scores, or message_board.
#
# Installed espn_api contract (verified 2026-08-15, do not assume):
#   League.recent_activity(size=25, msg_type=None, offset=0) -> List[Activity]
#   msg_type=None resolves to msg_types=[178,180,179,239,181,244] (the
#   FULL mixed stream covering every currently-mapped activity type in
#   ONE network call) - so local filtering after one mixed fetch is
#   strictly preferred over separate per-type network calls.
#   ACTIVITY_MAP = {178:'FA ADDED', 180:'WAIVER ADDED', 179:'DROPPED',
#                    181:'DROPPED', 239:'DROPPED', 244:'TRADED', ...}
#   Activity.date = raw epoch milliseconds (int). Activity.actions is a
#   list of 4-tuples: (Team_or_None, action_string, Player, bid_amount).
#   bid_amount is ONLY ever populated (from msg['from']) when
#   action_string=='WAIVER ADDED' - it defaults to plain int 0 for every
#   other action type, which is a structural placeholder, NOT a factual
#   bid value, so it must never be surfaced for non-waiver actions.
#   Activity exposes NO transaction/event ID of any kind - the Activity
#   object itself (one HTTP "topic") is the only factual event boundary.
#   get_team_data() is a pure local lookup (team.team_id == comparison,
#   never positional) - zero extra network calls. player_info() IS a
#   real network call (get_player_card + _get_all_pro_schedule) that
#   only fires when a referenced player is not found on the resolved
#   team's CURRENT roster (common for older drops/trades) - this is why
#   the performance harness must never benchmark large historical scans.
_COMMISSIONER_ACTIVITY_TYPE_MAP = {"FA ADDED": "free_agent_add", "WAIVER ADDED": "waiver_add",
                                     "DROPPED": "drop", "TRADED": "trade"}
_COMMISSIONER_PUBLIC_ACTION_TYPES = {"free_agent_add", "waiver_add", "drop", "trade"}
_COMMISSIONER_ACTIVITY_PAGE_SIZE = 25  # matches ESPN's own internal limitPerMessageSet
_COMMISSIONER_MAX_ACTIVITY_SCAN = 200  # hard bound on total raw Activity objects scanned
_COMMISSIONER_MAX_TRANSACTION_LIMIT = 100
_COMMISSIONER_TRANSACTION_CAPABILITIES = {
    "free_agent_adds": "available", "winning_waiver_claims": "available",
    "waiver_bid_amount": "when_exposed_by_espn", "failed_waiver_claims": "unavailable",
    "other_waiver_bidders": "unavailable", "historical_waiver_priority": "unavailable",
    "trade_activity": "available", "trade_grouping": "espn_activity_object_only",
}
_COMMISSIONER_TRANSACTION_LIMITATIONS = [
    "failed/losing waiver claims and other bidders are not exposed by the installed ESPN "
    "activity feed - their absence from these results is not evidence no competition occurred",
    "trade grouping follows ESPN's own Activity object boundaries only; multiple Activity "
    "objects sharing a timestamp are never automatically merged into one trade",
    "no trade fairness, waiver fairness, or misconduct judgment is performed here",
]

def _commissioner_normalize_activity_action(action_tuple):
    """Normalizes one raw (Team_or_None, action_string, Player, bid_amount)
    tuple. bid_amount is only ever factual for WAIVER ADDED (ESPN's own
    library default of 0 for every other type is a structural
    placeholder, never a real bid) - normalized to None otherwise."""
    team, source_action, player, bid = action_tuple
    action_type = _COMMISSIONER_ACTIVITY_TYPE_MAP.get(source_action, "unknown")
    return {
        "action_type": action_type,
        "team_id": getattr(team, "team_id", None) if team else None,
        "team_name": getattr(team, "team_name", None) if team else None,
        "player_id": getattr(player, "playerId", None) if player else None,
        "player_name": getattr(player, "name", None) if player else None,
        "bid_amount": bid if action_type == "waiver_add" else None,
        "source_action": source_action,
    }

def _commissioner_derive_event_type(action_types_set):
    """Deterministic event_type derivation rule (documented, not an
    opaque classifier): trade takes precedence if any TRADED action is
    present; else waiver/free_agent if that add type is present (a
    same-event drop is allowed alongside); else drop if drop-only;
    else unknown if unknown-only; else mixed for any other combination."""
    if "trade" in action_types_set:
        return "trade"
    if "waiver_add" in action_types_set and action_types_set <= {"waiver_add", "drop"}:
        return "waiver"
    if "free_agent_add" in action_types_set and action_types_set <= {"free_agent_add", "drop"}:
        return "free_agent"
    if action_types_set == {"drop"}:
        return "drop"
    if action_types_set == {"unknown"}:
        return "unknown"
    return "mixed"

def _commissioner_normalize_activity_event(activity):
    """Normalizes ONE ESPN Activity object into one event. Action order
    within the event is preserved exactly as ESPN's own messages-list
    order (never re-sorted) since actions within a single trade may
    carry meaningful give/receive pairing order that a stable re-sort
    could obscure. paired_add_drop is only ever true for actions
    co-located in this SAME Activity object - never inferred across
    separate objects."""
    actions = [_commissioner_normalize_activity_action(t) for t in activity.actions]
    action_types_set = {a["action_type"] for a in actions}
    event_type = _commissioner_derive_event_type(action_types_set)
    has_add = ("waiver_add" in action_types_set) or ("free_agent_add" in action_types_set)
    paired_add_drop = bool(has_add and "drop" in action_types_set)
    timestamp_ms = activity.date
    timestamp_utc = datetime.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=datetime.timezone.utc).isoformat()
    return {
        "timestamp_ms": timestamp_ms, "timestamp_utc": timestamp_utc,
        "source": "espn_recent_activity", "event_type": event_type,
        "actions": actions,
        "paired_add_drop": paired_add_drop,
        "paired_add_drop_basis": "same_espn_activity_object" if paired_add_drop else None,
    }

def _commissioner_event_matches_filters(event, team_id, player_id, player_name_cf,
                                          action_types_set, start_ms, end_ms):
    """An event matches if AT LEAST ONE action satisfies team/player/
    action-type constraints (event context is always preserved whole -
    never stripped to only the matching action), AND the event
    timestamp falls within the requested inclusive time range. If both
    player_id and player_name are supplied, a single action must
    satisfy BOTH (AND semantics, never OR)."""
    ts = event["timestamp_ms"]
    if start_ms is not None and ts < start_ms:
        return False
    if end_ms is not None and ts > end_ms:
        return False
    if action_types_set is not None:
        event_action_types = {a["action_type"] for a in event["actions"]}
        if not (event_action_types & action_types_set):
            return False
    # C7 narrow extension (2026-08-15): team_id may now ALSO be a
    # set/frozenset of ints (for two-team investigation scope) in
    # addition to a plain int or None. Existing int/None behavior for
    # C3's own call site is 100% unchanged (single-int membership test
    # is identical to the prior equality test) - proven via regression.
    team_id_set = None
    if team_id is not None:
        team_id_set = team_id if isinstance(team_id, (set, frozenset)) else {team_id}
    if team_id_set is None and player_id is None and player_name_cf is None:
        return True
    for a in event["actions"]:
        if team_id_set is not None and a["team_id"] not in team_id_set:
            continue
        if player_id is not None and a["player_id"] != player_id:
            continue
        if player_name_cf is not None:
            name = (a["player_name"] or "").casefold()
            if player_name_cf not in name:
                continue
        return True
    return team_id is None and player_id is None and player_name_cf is None

def _commissioner_fetch_activity_events(league, limit, team_id, player_id, player_name_cf,
                                          action_types_set, start_ms, end_ms):
    """Bounded project-owned scan of ESPN's communication activity feed.

    Fetches one page at a time using the same 25-topic pagination/filter
    contract previously used by espn-api, parses raw topics locally, and
    stops on the same return-limit/source-exhaustion/hard-scan boundaries.
    One active-player name map is fetched per scan to avoid hidden per-player
    lookups for historical activity; unresolved historical names remain None.
    Returns (matched_events, source_events_scanned, scan_truncated).
    """
    matched = []
    scanned = 0
    offset = 0
    scan_truncated = False
    active_names = build_active_player_name_map(_fetch_activity_player_payload(league.year))
    while True:
        remaining_scan_budget = _COMMISSIONER_MAX_ACTIVITY_SCAN - scanned
        if remaining_scan_budget <= 0:
            scan_truncated = True
            break
        page_size = min(_COMMISSIONER_ACTIVITY_PAGE_SIZE, remaining_scan_budget)
        page_payload = _fetch_activity_page_payload(league.league_id, league.year, page_size, offset)
        page = build_activity_events(page_payload, league, active_names)
        scanned += len(page)
        for event in page:
            if _commissioner_event_matches_filters(event, team_id, player_id, player_name_cf,
                                                     action_types_set, start_ms, end_ms):
                matched.append(event)
                if len(matched) >= limit:
                    return matched[:limit], scanned, (scanned < _COMMISSIONER_MAX_ACTIVITY_SCAN and len(page) == page_size)
        if len(page) < page_size:
            break
        offset += page_size
    return matched, scanned, scan_truncated

def _commissioner_transaction_summary(matched_events):
    """Transparent factual counts only - never a risk/fairness score."""
    actions_total = 0
    counts = {"free_agent_add": 0, "waiver_add": 0, "drop": 0, "trade": 0, "unknown": 0}
    team_ids, player_ids = set(), set()
    for ev in matched_events:
        for a in ev["actions"]:
            actions_total += 1
            counts[a["action_type"]] = counts.get(a["action_type"], 0) + 1
            if a["team_id"] is not None:
                team_ids.add(a["team_id"])
            if a["player_id"] is not None:
                player_ids.add(a["player_id"])
    return {
        "events_returned": len(matched_events), "actions_returned": actions_total,
        "free_agent_adds": counts["free_agent_add"], "waiver_adds": counts["waiver_add"],
        "drops": counts["drop"], "trade_actions": counts["trade"],
        "teams_involved": len(team_ids), "players_involved": len(player_ids),
    }

@mcp.tool()
async def commissioner_audit_transactions(alias: str = None, league_id: int = None, year: int = None,
                                             team_id: int = None, player_id: int = None, player_name: str = None,
                                             action_types: list = None, start_timestamp_ms: int = None,
                                             end_timestamp_ms: int = None, limit: int = 50) -> dict:
    """Audits commissioner-league transaction/waiver/trade activity as
    factual EVIDENCE - who did what, to which player, when, per ESPN's
    communication activity feed. Read-only; makes zero FantasyPros
    calls; never calls load_roster_week, box_scores, or message_board.
    Combines free-agent adds, winning waiver acquisitions, drops, and
    trades because the installed ESPN library exposes all of them
    through the same activity feed.

    NOT exposed by ESPN (and therefore never fabricated here):
    unsuccessful/losing waiver claims, other bidders on a winning
    claim, or historical waiver-priority order. These are always
    reported as "unavailable" in the capabilities block - never as an
    empty list, which would incorrectly imply "checked, none occurred."

    This is an evidence/audit tool, not a strategy grader: it never
    calculates trade winners/losers, fairness, or value deltas (that is
    evaluate_trade's job, which this tool never calls). Trade grouping
    follows ESPN's own Activity object boundaries only - two Activity
    objects sharing a timestamp are never speculatively merged.

    Args:
        alias: Configured commissioner-league alias.
        league_id: Configured commissioner ESPN league ID.
        year: NFL season year (same _resolve_year semantics as every
              other tool).
        team_id: Optional team filter (sparse IDs fully supported via
                 the frozen _find_team_by_id resolver).
        player_id: Optional exact ESPN player ID filter.
        player_name: Optional case-insensitive substring player-name
                     filter (e.g. "mccaffrey" matches "Christian
                     McCaffrey"). If both player_id and player_name are
                     supplied, a matching action must satisfy BOTH.
        action_types: Optional list restricting results to any of:
                      free_agent_add, waiver_add, drop, trade. An
                      unrecognized value is rejected before any ESPN
                      fetch. Event context is always preserved whole -
                      a matching event's other, non-matching actions
                      (e.g. a paired drop) are never stripped out.
        start_timestamp_ms: Optional inclusive lower bound (epoch ms).
        end_timestamp_ms: Optional inclusive upper bound (epoch ms).
        limit: Maximum number of normalized EVENTS to return (1-100,
               default 50). Source scanning is bounded internally and
               reported transparently via source_events_scanned /
               scan_truncated.
    """
    try:
        resolved_alias, entry, guard_error = _commissioner_resolve_guard(alias, league_id)
        if guard_error is not None:
            return guard_error

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > _COMMISSIONER_MAX_TRANSACTION_LIMIT:
            return {"status": "error", "error": "invalid_transaction_limit",
                    "message": f"limit must be an integer between 1 and {_COMMISSIONER_MAX_TRANSACTION_LIMIT}."}

        action_types_set = None
        if action_types is not None:
            action_types_set = set(action_types)
            invalid = action_types_set - _COMMISSIONER_PUBLIC_ACTION_TYPES
            if invalid:
                return {"status": "error", "error": "invalid_transaction_action_type",
                        "message": f"Unrecognized action_types: {sorted(invalid)}. "
                                   f"Allowed values: {sorted(_COMMISSIONER_PUBLIC_ACTION_TYPES)}."}

        if start_timestamp_ms is not None and start_timestamp_ms < 0:
            return {"status": "error", "error": "invalid_transaction_time_range",
                    "message": "start_timestamp_ms must be >= 0."}
        if end_timestamp_ms is not None and end_timestamp_ms < 0:
            return {"status": "error", "error": "invalid_transaction_time_range",
                    "message": "end_timestamp_ms must be >= 0."}
        if start_timestamp_ms is not None and end_timestamp_ms is not None and start_timestamp_ms > end_timestamp_ms:
            return {"status": "error", "error": "invalid_transaction_time_range",
                    "message": "start_timestamp_ms must be <= end_timestamp_ms."}

        resolved_year = _resolve_year(year)
        resolved_league_id = entry["league_id"]
        commissioner_payload = _fetch_commissioner_current_payload(resolved_league_id, resolved_year)
        league = build_commissioner_snapshot(commissioner_payload, resolved_league_id, resolved_year)
        settings = league.settings

        resolved_team_id = None
        if team_id is not None:
            teams, team_error = _commissioner_select_teams(league, team_id)
            if team_error is not None:
                return team_error
            resolved_team_id = teams[0].team_id

        player_name_cf = player_name.strip().casefold() if player_name else None

        try:
            matched_events, scanned, scan_truncated = _commissioner_fetch_activity_events(
                league, limit, resolved_team_id, player_id, player_name_cf,
                action_types_set, start_timestamp_ms, end_timestamp_ms)
        except Exception as e:
            return _error_response("fetching commissioner transaction activity", e)

        def _event_sort_key(ev):
            # Tuple-based deterministic signature (no json import needed) -
            # never relies on Python object identity or dict iteration order.
            sig = tuple(sorted((a["action_type"], a["team_id"] or -1, a["player_id"] or -1,
                                 a["player_name"] or "") for a in ev["actions"]))
            return (-ev["timestamp_ms"], ev["event_type"], sig)
        matched_events.sort(key=_event_sort_key)

        limitations = list(_COMMISSIONER_TRANSACTION_LIMITATIONS)
        if scan_truncated:
            limitations.append(f"source scan stopped after {scanned} activity objects (hard bound "
                                f"{_COMMISSIONER_MAX_ACTIVITY_SCAN}) - older activity may exist beyond this scan")

        return {
            "status": "ok",
            "commissioner": {"configured_for_commissioner_reads": True, "alias": resolved_alias, "scope": "read_only"},
            "league": {"league_id": resolved_league_id, "league_name": getattr(settings, "name", None), "year": resolved_year},
            "query": {"team_id": team_id, "player_id": player_id, "player_name": player_name,
                       "action_types": sorted(action_types_set) if action_types_set else None,
                       "start_timestamp_ms": start_timestamp_ms, "end_timestamp_ms": end_timestamp_ms, "limit": limit},
            "capabilities": dict(_COMMISSIONER_TRANSACTION_CAPABILITIES),
            "summary": {**{"source_events_scanned": scanned, "scan_truncated": scan_truncated},
                         **_commissioner_transaction_summary(matched_events)},
            "events": matched_events,
            "limitations": limitations,
        }
    except commissioner_config.CommissionerConfigError as e:
        return {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}
    except Exception as e:
        return _error_response("auditing commissioner transactions", e)

# --- COMMISSIONER READ/AUDIT - PHASE C7 (2026-08-15) ---
# One new tool: commissioner_investigate. Orchestrates C1-C3 factual
# primitives into a bounded, source-attributed case file - it is an
# evidence assembler, never a judge. Reuses _commissioner_resolve_guard,
# _find_team_by_id, _commissioner_normalize_lineup_players,
# _commissioner_audit_team_lineup, _commissioner_audit_team_roster,
# _commissioner_fetch_activity_events (extended narrowly above to accept
# a team_id SET for two-team scope, proven behavior-identical for the
# existing single-int/None call site). Never calls load_roster_week,
# message_board, or FantasyPros. player_info() is never called - all
# player resolution uses already-loaded team.roster/Activity evidence.
_COMMISSIONER_INVESTIGATION_TRANSACTION_LIMIT = 25  # smaller than C3's public default of 50
_COMMISSIONER_INVESTIGATION_MAX_FACTS = 100
_COMMISSIONER_INVESTIGATION_MAX_TIMELINE = 50
_COMMISSIONER_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")

def _commissioner_investigate_parse_date(date_str, end_of_day):
    """Strict YYYY-MM-DD only. Boundaries are UTC calendar-day edges -
    never labeled local/league time. Returns epoch ms or raises ValueError."""
    if not _COMMISSIONER_DATE_RE.match(date_str or ""):
        raise ValueError(f"'{date_str}' is not in YYYY-MM-DD format")
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)

def _commissioner_investigate_resolve_player(league, player_id, player_name, search_teams):
    """Resolves a player using ONLY already-loaded team.roster evidence
    (zero player_info() network calls, ever). search_teams narrows the
    roster search when a team scope is known; otherwise searches every
    current roster in the already-cached League (still zero network -
    league.teams is already fully loaded). Returns
    (resolution_status, player_id_resolved, player_name_resolved)."""
    name_cf = player_name.strip().casefold() if player_name else None
    pool = search_teams if search_teams else league.teams

    if player_id is not None:
        found_name = None
        for team in pool:
            for p in team.roster:
                if getattr(p, "playerId", None) == player_id:
                    found_name = getattr(p, "name", None)
                    break
            if found_name:
                break
        if name_cf is not None and found_name is not None:
            fname_cf = found_name.casefold()
            if name_cf not in fname_cf and fname_cf not in name_cf:
                return "player_identity_mismatch", player_id, found_name
        return "resolved", player_id, found_name

    if name_cf is not None:
        matches = {}
        for team in pool:
            for p in team.roster:
                pname = getattr(p, "name", None)
                if pname and name_cf in pname.casefold():
                    matches[getattr(p, "playerId", None)] = pname
        if len(matches) == 1:
            pid, pname = next(iter(matches.items()))
            return "resolved", pid, pname
        if len(matches) > 1:
            return "ambiguous", None, None
        return "not_found", None, None

    return "not_required", None, None

def _commissioner_investigate_derive_case_type(has_team, has_other_team, has_player,
                                                   has_week, has_dates, action_types_set):
    if has_team and has_other_team:
        return "two_team_activity"
    if has_player and not has_team:
        return "player_activity"
    if action_types_set is not None and action_types_set == {"trade"}:
        return "trade_activity"
    if action_types_set is not None and "waiver_add" in action_types_set and action_types_set <= {"waiver_add", "drop"}:
        return "waiver_activity"
    if has_team:
        return "team_activity"
    if has_week and not has_dates and action_types_set is None:
        return "lineup_question"
    return "mixed"

@mcp.tool()
async def commissioner_investigate(alias: str = None, league_id: int = None, year: int = None,
                                      team_id: int = None, other_team_id: int = None, player_id: int = None,
                                      player_name: str = None, week: int = None, start_date: str = None,
                                      end_date: str = None, action_types: list = None,
                                      include_lineup_evidence: bool = True, include_roster_evidence: bool = True,
                                      include_transaction_evidence: bool = True) -> dict:
    """Assembles a bounded, factual, source-attributed CASE FILE by
    orchestrating the existing C1-C3 commissioner read primitives
    (context/settings, lineup audit, roster audit, transaction audit)
    plus a project-owned read-only matchup helper. This is an evidence
    assembler, NEVER a judge: it makes no fairness, collusion,
    misconduct, or motive determination, and never converts ESPN's
    "unavailable" waiver-competition data into an empty/zero result.

    Read-only; zero FantasyPros calls; never calls load_roster_week,
    message_board, or player_info() (all player resolution uses
    project-owned current-roster / Activity evidence only - zero hidden
    per-player network calls). Transaction evidence reuses C3's bounded scanning
    (hard cap unchanged) with a smaller default limit appropriate for a
    single case file.

    Args:
        alias: Configured commissioner-league alias.
        league_id: Configured commissioner ESPN league ID.
        year: NFL season year (same _resolve_year semantics as every
              other tool).
        team_id: Primary team under investigation (sparse IDs
                 supported via the frozen _find_team_by_id resolver).
        other_team_id: Optional second team for a two-team dispute or
                       trade context. Must differ from team_id.
        player_id: Optional exact ESPN player ID.
        player_name: Optional case-insensitive substring player name.
                     Resolved ONLY against already-loaded roster
                     evidence - ambiguous/not_found are honest,
                     explicit outcomes, never guessed.
        week: Optional scoring week for lineup/matchup evidence scope.
        start_date: Optional inclusive UTC calendar-day lower bound
                    (YYYY-MM-DD) for transaction evidence.
        end_date: Optional inclusive UTC calendar-day upper bound
                  (YYYY-MM-DD) for transaction evidence.
        action_types: Optional restriction to any of: free_agent_add,
                      waiver_add, drop, trade (same vocabulary as
                      commissioner_audit_transactions).
        include_lineup_evidence: Include C2 lineup-audit evidence.
        include_roster_evidence: Include C2 roster-audit evidence
                                  (always labeled espn_current_roster -
                                  never represented as historical).
        include_transaction_evidence: Include C3 transaction evidence.

    At least one meaningful scope field is required (team_id,
    other_team_id+team_id, player_id, player_name, week, a date range,
    or action_types) - an unscoped call is rejected before any ESPN
    fetch to prevent an accidental full-season/all-team scan.
    """
    try:
        resolved_alias, entry, guard_error = _commissioner_resolve_guard(alias, league_id)
        if guard_error is not None:
            return guard_error

        has_team = team_id is not None
        has_other_team = other_team_id is not None
        has_player = player_id is not None or bool(player_name)
        has_week = week is not None
        has_dates = start_date is not None or end_date is not None
        has_action_types = action_types is not None
        if not (has_team or has_player or has_week or has_dates or has_action_types):
            return {"status": "error", "error": "investigation_scope_required",
                    "message": "At least one scope field is required: team_id, other_team_id (with team_id), "
                               "player_id, player_name, week, start_date/end_date, or action_types."}

        if has_other_team and not has_team:
            return {"status": "error", "error": "invalid_investigation_scope",
                    "message": "other_team_id requires team_id to also be supplied."}
        if has_team and has_other_team and team_id == other_team_id:
            return {"status": "error", "error": "invalid_investigation_scope",
                    "message": "team_id and other_team_id must refer to different teams."}

        action_types_set = None
        if action_types is not None:
            action_types_set = set(action_types)
            invalid = action_types_set - _COMMISSIONER_PUBLIC_ACTION_TYPES
            if invalid:
                return {"status": "error", "error": "invalid_transaction_action_type",
                        "message": f"Unrecognized action_types: {sorted(invalid)}. "
                                   f"Allowed values: {sorted(_COMMISSIONER_PUBLIC_ACTION_TYPES)}."}

        start_ms = end_ms = None
        try:
            if start_date is not None:
                start_ms = _commissioner_investigate_parse_date(start_date, end_of_day=False)
            if end_date is not None:
                end_ms = _commissioner_investigate_parse_date(end_date, end_of_day=True)
        except ValueError as e:
            return {"status": "error", "error": "invalid_date_format",
                    "message": f"Dates must be strict YYYY-MM-DD: {e}"}
        if start_ms is not None and end_ms is not None and start_ms > end_ms:
            return {"status": "error", "error": "invalid_date_range",
                    "message": "start_date must be <= end_date."}

        if player_id is not None and not isinstance(player_id, int):
            return {"status": "error", "error": "invalid_investigation_scope", "message": "player_id must be an integer."}

        resolved_year = _resolve_year(year)
        resolved_league_id = entry["league_id"]
        commissioner_payload = _fetch_commissioner_current_payload(resolved_league_id, resolved_year)
        league = build_commissioner_snapshot(commissioner_payload, resolved_league_id, resolved_year)
        settings = league.settings

        resolved_team = resolved_other_team = None
        if team_id is not None:
            teams, team_error = _commissioner_select_teams(league, team_id)
            if team_error is not None:
                return team_error
            resolved_team = teams[0]
        if other_team_id is not None:
            teams2, team_error2 = _commissioner_select_teams(league, other_team_id)
            if team_error2 is not None:
                return team_error2
            resolved_other_team = teams2[0]

        search_teams = [t for t in (resolved_team, resolved_other_team) if t is not None] or None
        player_resolution, resolved_player_id, resolved_player_name = _commissioner_investigate_resolve_player(
            league, player_id, player_name, search_teams)
        if player_resolution == "player_identity_mismatch":
            return {"status": "error", "error": "player_identity_mismatch",
                    "message": f"player_id={player_id} resolves to '{resolved_player_name}', which does not "
                               f"match the supplied player_name='{player_name}'."}

        case_type = _commissioner_investigate_derive_case_type(
            has_team, has_other_team, has_player, has_week, has_dates, action_types_set)

        limitations = list(_COMMISSIONER_TRANSACTION_LIMITATIONS)
        unresolved_questions = []
        timeline = []
        observed_facts = []
        evidence = {}
        results_truncated = False

        # --- TRANSACTION EVIDENCE (reuses C3's bounded scanner exactly) ---
        if include_transaction_evidence:
            team_filter = None
            if resolved_team is not None and resolved_other_team is not None:
                team_filter = {resolved_team.team_id, resolved_other_team.team_id}
            elif resolved_team is not None:
                team_filter = resolved_team.team_id
            name_cf_for_scan = (player_name.strip().casefold() if (player_name and resolved_player_id is None) else None)
            try:
                matched_events, scanned, scan_truncated = _commissioner_fetch_activity_events(
                    league, _COMMISSIONER_INVESTIGATION_TRANSACTION_LIMIT, team_filter, resolved_player_id,
                    name_cf_for_scan, action_types_set, start_ms, end_ms)
            except Exception as e:
                return _error_response("gathering commissioner investigation transaction evidence", e)

            def _event_sort_key(ev):
                sig = tuple(sorted((a["action_type"], a["team_id"] or -1, a["player_id"] or -1, a["player_name"] or "")
                                    for a in ev["actions"]))
                return (ev["timestamp_ms"], ev["event_type"], sig)  # ascending chronology for the case timeline
            matched_events.sort(key=_event_sort_key)
            if len(matched_events) > _COMMISSIONER_INVESTIGATION_MAX_TIMELINE:
                matched_events = matched_events[:_COMMISSIONER_INVESTIGATION_MAX_TIMELINE]
                results_truncated = True

            for idx, ev in enumerate(matched_events, start=1):
                timeline.append({"event_index": idx, "timestamp_ms": ev["timestamp_ms"],
                                   "timestamp_utc": ev["timestamp_utc"], "event_type": ev["event_type"],
                                   "source": ev["source"], "actions": ev["actions"]})
                for a in ev["actions"]:
                    if len(observed_facts) >= _COMMISSIONER_INVESTIGATION_MAX_FACTS:
                        results_truncated = True
                        break
                    observed_facts.append({
                        "fact": f"{a['team_name'] or 'Unknown team'} {a['action_type']} "
                                f"{a['player_name'] or 'unknown player'} at {ev['timestamp_utc']}"
                                + (f" (paired with other actions in the same ESPN activity event)" if len(ev["actions"]) > 1 else ""),
                        "source": ev["source"], "evidence_index": idx,
                    })
            evidence["transactions"] = {"source_events_scanned": scanned, "scan_truncated": scan_truncated,
                                          "events_returned": len(matched_events),
                                          "capabilities": dict(_COMMISSIONER_TRANSACTION_CAPABILITIES)}
            if scan_truncated:
                limitations.append(f"transaction scan stopped after {scanned} activity objects - "
                                    f"older activity may exist beyond this bounded case-file scan")

        # --- LINEUP + ROSTER EVIDENCE (reuses C2 methodology exactly) ---
        evidence_teams = [t for t in (resolved_team, resolved_other_team) if t is not None]
        if not evidence_teams and player_resolution == "resolved" and resolved_player_id is not None:
            for team in league.teams:
                if any(getattr(p, "playerId", None) == resolved_player_id for p in team.roster):
                    evidence_teams = [team]
                    break

        slot_counts = getattr(settings, "position_slot_counts", {}) or {}
        lineup_evidence, roster_evidence = [], []
        current_week = getattr(league, "current_week", 0) or 0
        box_scores_cache = {}

        if include_lineup_evidence and evidence_teams:
            if week is not None and week != current_week:
                matchup_periods = getattr(settings, "matchup_periods", {}) or {}
                if not isinstance(week, int) or isinstance(week, bool) or week <= 0 or str(week) not in matchup_periods:
                    return {"status": "error", "error": "invalid_week",
                            "message": f"week={week!r} is not a valid scoring week for this league.",
                            "valid_weeks": sorted((int(k) for k in matchup_periods.keys()), key=int) if matchup_periods else []}
                if week not in box_scores_cache:
                    try:
                        box_scores_cache[week] = _fetch_historical_lineup_boxes(league.league_id, league.year, week, league.settings)
                    except Exception:
                        box_scores_cache[week] = None
                boxes = box_scores_cache[week]
                if boxes is None:
                    limitations.append(f"historical lineup data for week {week} was unavailable from ESPN "
                                        f"(commonly seen in preseason) - this is NOT evidence of a clean lineup")
                    unresolved_questions.append(f"Historical lineup data for week {week} is currently unavailable from ESPN.")
                else:
                    box_by_team_id = {}
                    for box in boxes:
                        if getattr(box, "home_team", None) is not None:
                            box_by_team_id[box.home_team.team_id] = box.home_lineup
                        if getattr(box, "away_team", None) is not None:
                            box_by_team_id[box.away_team.team_id] = box.away_lineup
                    for team in evidence_teams:
                        lineup = box_by_team_id.get(team.team_id)
                        if lineup is None:
                            continue
                        players = _commissioner_normalize_lineup_players(lineup, is_historical=True)
                        findings = _commissioner_audit_team_lineup(players, slot_counts, f"espn_box_score_week_{week}")
                        lineup_evidence.append({"team_id": team.team_id, "team_name": team.team_name,
                                                  "scope": "historical", "week": week, "findings": findings})
                        for f in findings:
                            if len(observed_facts) < _COMMISSIONER_INVESTIGATION_MAX_FACTS:
                                observed_facts.append({"fact": f"{team.team_name}: {f['basis']}",
                                                         "source": f["source"], "evidence_index": None})
            else:
                for team in evidence_teams:
                    players = _commissioner_normalize_lineup_players(team.roster, is_historical=False)
                    findings = _commissioner_audit_team_lineup(players, slot_counts, "espn_current_roster")
                    lineup_evidence.append({"team_id": team.team_id, "team_name": team.team_name,
                                              "scope": "current", "week": None, "findings": findings})
                    for f in findings:
                        if len(observed_facts) < _COMMISSIONER_INVESTIGATION_MAX_FACTS:
                            observed_facts.append({"fact": f"{team.team_name}: {f['basis']}",
                                                     "source": f["source"], "evidence_index": None})
                limitations.append("lineup evidence reflects the CURRENT ESPN lineup state, not a historical "
                                    "snapshot, unless an explicit historical week was requested and available")

        if include_roster_evidence and evidence_teams:
            for team in evidence_teams:
                audit = _commissioner_audit_team_roster(team, slot_counts)
                roster_evidence.append({"team_id": team.team_id, "team_name": team.team_name,
                                          "source": "espn_current_roster", "roster_count": audit["roster_count"],
                                          "configured_capacity": audit["configured_capacity"],
                                          "findings": audit["findings"]})
            if evidence.get("transactions"):
                unresolved_questions.append("The current roster reflects present-day composition, not roster "
                                              "composition at any historical transaction timestamp shown above.")

        evidence["lineup"] = lineup_evidence
        evidence["roster"] = roster_evidence

        # --- MATCHUP EVIDENCE (project-owned scoreboard read) ---
        matchup_evidence = None
        if week is not None and evidence_teams:
            try:
                matchup_context = _fetch_matchup_context_payload(resolved_league_id, resolved_year)
                resolved_matchup_week, matchup_period, _valid_matchup_weeks = resolve_matchup_request(
                    matchup_context, week)
                if resolved_matchup_week is not None and matchup_period is not None:
                    matchup_payload = _fetch_matchup_score_payload(
                        resolved_league_id, resolved_year, resolved_matchup_week, matchup_period)
                    matchup_evidence = build_commissioner_matchup_evidence(
                        matchup_payload, resolved_matchup_week, {t.team_id for t in evidence_teams})
            except Exception:
                matchup_evidence = None
        evidence["matchup"] = matchup_evidence

        # --- GOVERNANCE EVIDENCE (only case-relevant settings) ---
        governance = {}
        if case_type in ("trade_activity", "two_team_activity"):
            governance["veto_votes_required"] = getattr(settings, "veto_votes_required", None)
            governance["trade_deadline"] = getattr(settings, "trade_deadline", None)
        if case_type == "waiver_activity":
            governance["faab"] = getattr(settings, "faab", None)
            governance["acquisition_budget"] = getattr(settings, "acquisition_budget", None)
        if roster_evidence:
            governance["position_slot_counts"] = slot_counts
            governance["ir_slots_configured"] = slot_counts.get("IR", 0)
        evidence["governance"] = {"source": "espn_live_settings", **governance} if governance else None

        # --- OBSERVED PATTERNS (only deterministically-supportable ones) ---
        observed_patterns = []
        if include_transaction_evidence and evidence.get("transactions"):
            adds_by_player = {}
            for ev in timeline:
                for a in ev["actions"]:
                    if a["action_type"] in ("free_agent_add", "waiver_add") and a["player_id"] is not None:
                        adds_by_player.setdefault(a["player_id"], []).append(ev)
                    if a["action_type"] == "trade" and a["player_id"] is not None:
                        for add_ev in adds_by_player.get(a["player_id"], []):
                            if add_ev["timestamp_ms"] < ev["timestamp_ms"]:
                                observed_patterns.append({
                                    "type": "acquire_then_trade_sequence",
                                    "basis": f"Player ID {a['player_id']} ({a['player_name']}) was acquired at "
                                             f"{add_ev['timestamp_utc']} (event_index {add_ev['event_index']}) and "
                                             f"appeared in a later trade at {ev['timestamp_utc']} (event_index {ev['event_index']})",
                                })

        # --- INACTIVITY SIGNAL: internal-only, conservative, never a verdict ---
        if include_lineup_evidence and evidence_teams and week is None:
            unresolved_questions.append("insufficient_history: this investigation scope covers a single current "
                                         "lineup snapshot, not multiple weeks - no inactivity signal can be "
                                         "responsibly derived from this evidence alone.")

        unresolved_questions.append("ESPN's installed activity interface exposes the winning waiver transaction "
                                     "but not unsuccessful competing claims or waiver-priority order.")
        unresolved_questions.append("ESPN's activity feed does not expose a transaction/event ID; event_index "
                                     "above is a local response-ordering label only, not an ESPN identifier.")

        subject = {"team_id": resolved_team.team_id if resolved_team else None,
                    "team_name": resolved_team.team_name if resolved_team else None,
                    "other_team_id": resolved_other_team.team_id if resolved_other_team else None,
                    "other_team_name": resolved_other_team.team_name if resolved_other_team else None,
                    "player_id": resolved_player_id, "player_name": resolved_player_name,
                    "player_resolution": player_resolution}

        return {
            "status": "ok",
            "commissioner": {"configured_for_commissioner_reads": True, "alias": resolved_alias, "scope": "read_only"},
            "league": {"league_id": resolved_league_id, "league_name": getattr(settings, "name", None), "year": resolved_year},
            "case": {"case_type": case_type, "subject": subject,
                      "requested_scope": {"team_id": team_id, "other_team_id": other_team_id, "player_id": player_id,
                                            "player_name": player_name, "week": week, "start_date": start_date,
                                            "end_date": end_date, "action_types": sorted(action_types_set) if action_types_set else None},
                      "resolved_scope": {"start_timestamp_ms": start_ms, "end_timestamp_ms": end_ms}},
            "timeline": timeline,
            "evidence": evidence,
            "observed_facts": observed_facts,
            "observed_patterns": observed_patterns,
            "data_limitations": limitations,
            "unresolved_questions": unresolved_questions,
            "results_truncated": results_truncated,
        }
    except commissioner_config.CommissionerConfigError as e:
        return {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}
    except Exception as e:
        return _error_response("assembling commissioner investigation case file", e)

# --- COMMISSIONER READ/AUDIT - PHASE C8 (2026-08-15) ---
# One new tool: get_commissioner_brief. Pure ORCHESTRATION/PRIORITIZATION
# layer - adds ZERO new evidence methodology. Every fact below is
# produced by the exact same frozen C1-C7 helpers already used by
# commissioner_audit_lineups/_rosters/_transactions/commissioner_investigate:
# _commissioner_select_teams, _commissioner_normalize_lineup_players,
# _commissioner_audit_team_lineup, _commissioner_audit_team_roster,
# _commissioner_fetch_activity_events. Inactivity detection is an
# internal-only derived signal (no public inactive-team tool), computed
# from the SAME C2 lineup findings plus a small bounded set of
# historical box_scores() calls (never load_roster_week, never
# scoreboard by default, never player_info).
_COMMISSIONER_BRIEF_DO_NOW_CAP = 10
_COMMISSIONER_BRIEF_REVIEW_CAP = 15
_COMMISSIONER_BRIEF_MONITOR_CAP = 15
_COMMISSIONER_BRIEF_MIN_LOOKBACK = 1
_COMMISSIONER_BRIEF_MAX_LOOKBACK = 6

def _commissioner_brief_priority_bucket(severity, issue_class):
    """The ONE deterministic severity->bucket mapping for the whole
    brief. action_required is the ONLY path to DO_NOW - no C1-C7
    finding currently emits it, which is intentional: it proves the
    bucket exists without inventing urgency for ordinary structural
    findings (empty starter slots etc. remain REVIEW, never promoted,
    because lineup_lock_status is explicitly not_modeled)."""
    if severity == "action_required":
        return "DO_NOW"
    if severity == "review":
        return "REVIEW"
    if severity == "monitor":
        return "MONITOR"
    return None  # "info"/unrecognized -> aggregated only, never a list item

def _commissioner_brief_finding_identity(finding, team_id, week):
    """Deterministic dedupe key from safe factual fields only - never
    an invented ESPN ID. Two internal paths surfacing the identical
    underlying finding collapse to ONE brief item."""
    return (finding.get("type"), team_id, week, finding.get("slot"),
            finding.get("playerId"), finding.get("player_name"))

def _commissioner_brief_item_sort_key(item):
    bucket_rank = {"DO_NOW": 0, "REVIEW": 1, "MONITOR": 2}
    return (bucket_rank.get(item["_bucket"], 9), item.get("team_id") or 0,
            item.get("type", ""), item.get("player_id") or -1, item.get("player_name") or "")

def _commissioner_brief_lineup_items(team, slot_counts):
    """Reuses C2's exact current-lineup methodology - zero new logic."""
    players = _commissioner_normalize_lineup_players(team.roster, is_historical=False)
    return _commissioner_audit_team_lineup(players, slot_counts, "espn_current_roster")

def _commissioner_brief_roster_items(team, slot_counts):
    """Reuses C2's exact roster methodology - zero new logic."""
    return _commissioner_audit_team_roster(team, slot_counts)["findings"]

def _commissioner_brief_inactivity_signal(team, current_week, lookback_weeks, box_scores_cache,
                                             league, transaction_events, slot_counts):
    """Conservative, internal-only, derived inactivity signal. NEVER a
    public tool, NEVER a motive/status verdict (no 'inactive'/
    'abandoned'/'quit'/'not trying' language). Deterministic rule:

    insufficient_history: fewer than 2 usable historical weeks of
        lineup evidence exist (covers current_week<=0, current_week==1,
        and any preseason box_scores() unavailability).

    possible_inactivity_signal: exactly 2 usable weeks show a
        structural lineup issue (empty_required_starter_slot or
        assigned_player_ineligible_for_slot) for this team, OR
        recurring issues exist but independent supporting evidence
        (transaction activity in-window) is itself unavailable/absent
        without being decisive alone.

    strong_inactivity_signal: >=2 usable weeks show a structural
        lineup issue for this team AND zero transaction events for
        this team were observed in the scanned transaction window
        (transaction inactivity is a SUPPORTING signal here, never
        used alone per the blocking rule - it only upgrades an
        already-multi-week-evidenced structural pattern).

    no_signal: usable weeks exist and show no recurring structural
        issue for this team.

    Returns (label, basis_list, weeks_examined, weeks_with_issue)."""
    if current_week <= 1:
        return "insufficient_history", ["fewer than 2 completed scoring weeks exist yet this season"], 0, 0

    candidate_weeks = [w for w in range(max(1, current_week - lookback_weeks), current_week)]
    weeks_examined = []
    weeks_with_issue = []
    for w in candidate_weeks:
        if w not in box_scores_cache:
            try:
                box_scores_cache[w] = _fetch_historical_lineup_boxes(league.league_id, league.year, w, league.settings)
            except Exception:
                box_scores_cache[w] = None
        boxes = box_scores_cache[w]
        if boxes is None:
            continue
        box_by_team_id = {}
        for box in boxes:
            if getattr(box, "home_team", None) is not None:
                box_by_team_id[box.home_team.team_id] = box.home_lineup
            if getattr(box, "away_team", None) is not None:
                box_by_team_id[box.away_team.team_id] = box.away_lineup
        lineup = box_by_team_id.get(team.team_id)
        if lineup is None:
            continue
        weeks_examined.append(w)
        players = _commissioner_normalize_lineup_players(lineup, is_historical=True)
        findings = _commissioner_audit_team_lineup(players, slot_counts, f"espn_box_score_week_{w}")
        if any(f["type"] in ("empty_required_starter_slot", "assigned_player_ineligible_for_slot") for f in findings):
            weeks_with_issue.append(w)

    if len(weeks_examined) < 2:
        return "insufficient_history", [f"only {len(weeks_examined)} of {len(candidate_weeks)} requested "
                                          f"historical week(s) had available ESPN lineup evidence"], len(weeks_examined), len(weeks_with_issue)

    if len(weeks_with_issue) < 2:
        return "no_signal", [f"structural lineup issue found in {len(weeks_with_issue)} of "
                               f"{len(weeks_examined)} examined week(s) - below the 2-week recurrence threshold"], len(weeks_examined), len(weeks_with_issue)

    team_txn_count = sum(1 for ev in transaction_events for a in ev["actions"] if a["team_id"] == team.team_id)
    if team_txn_count == 0:
        return "strong_inactivity_signal", [
            f"structural lineup issue recurred in {len(weeks_with_issue)} of {len(weeks_examined)} examined "
            f"weeks ({weeks_with_issue})",
            "no transaction activity observed for this team in the scanned interval",
        ], len(weeks_examined), len(weeks_with_issue)

    return "possible_inactivity_signal", [
        f"structural lineup issue recurred in {len(weeks_with_issue)} of {len(weeks_examined)} examined "
        f"weeks ({weeks_with_issue}), but transaction activity was observed for this team in the scanned "
        f"interval, so independent supporting evidence for a stronger signal is incomplete",
    ], len(weeks_examined), len(weeks_with_issue)

@mcp.tool()
async def get_commissioner_brief(alias: str = None, league_id: int = None, year: int = None, week: int = None,
                                    recent_activity_limit: int = 25, inactivity_lookback_weeks: int = 3) -> dict:
    """Answers "what needs my attention as commissioner?" with a
    compact, factual, prioritized league-wide administration brief.
    This is a pure ORCHESTRATION layer over the frozen C1-C7 primitives
    - it adds ZERO new evidence methodology. Read-only; zero
    FantasyPros calls; never calls load_roster_week, message_board, or
    player_info(); scoreboard() is never called by this tool.

    Uses exactly four deterministic priority buckets (DO_NOW, REVIEW,
    MONITOR, NO_ISSUE) with no numeric/AI-generated risk score.
    Critically distinguishes evaluated_clean from unavailable /
    insufficient_history - unavailable ESPN evidence (historical
    lineups, failed waiver claims, other waiver bidders, waiver
    priority) is NEVER represented as "no issue found." Inactivity
    detection is a conservative, internal-only derived signal (no
    public inactive-team tool exists, and no motive/misconduct
    language - e.g. "abandoned"/"quit"/"not trying"/"collusion" -
    is ever produced).

    Args:
        alias: Configured commissioner-league alias.
        league_id: Configured commissioner ESPN league ID.
        year: NFL season year (same _resolve_year semantics as every
              other tool).
        week: Optional explicit week for lineup/roster evaluation.
              Omit to evaluate the current ESPN lineup state (default,
              the project-owned current commissioner snapshot).
        recent_activity_limit: Bounded transaction-scan size (reuses
              C3's exact scanner and hard cap; 1-100, default 25).
        inactivity_lookback_weeks: Bounded number of prior completed
              weeks to examine for the internal inactivity signal
              (1-6, default 3). Never fetches the same week twice, and
              never fetches week 0 or negative weeks.
    """
    try:
        resolved_alias, entry, guard_error = _commissioner_resolve_guard(alias, league_id)
        if guard_error is not None:
            return guard_error

        if (not isinstance(recent_activity_limit, int) or isinstance(recent_activity_limit, bool)
                or recent_activity_limit < 1 or recent_activity_limit > _COMMISSIONER_MAX_TRANSACTION_LIMIT):
            return {"status": "error", "error": "invalid_parameter",
                    "message": f"recent_activity_limit must be an integer between 1 and {_COMMISSIONER_MAX_TRANSACTION_LIMIT}."}
        if (not isinstance(inactivity_lookback_weeks, int) or isinstance(inactivity_lookback_weeks, bool)
                or inactivity_lookback_weeks < _COMMISSIONER_BRIEF_MIN_LOOKBACK
                or inactivity_lookback_weeks > _COMMISSIONER_BRIEF_MAX_LOOKBACK):
            return {"status": "error", "error": "invalid_parameter",
                    "message": f"inactivity_lookback_weeks must be an integer between "
                               f"{_COMMISSIONER_BRIEF_MIN_LOOKBACK} and {_COMMISSIONER_BRIEF_MAX_LOOKBACK}."}

        resolved_year = _resolve_year(year)
        resolved_league_id = entry["league_id"]
        commissioner_payload = _fetch_commissioner_current_payload(resolved_league_id, resolved_year)
        league = build_commissioner_snapshot(commissioner_payload, resolved_league_id, resolved_year)
        settings = league.settings
        slot_counts = getattr(settings, "position_slot_counts", {}) or {}
        current_week = getattr(league, "current_week", 0) or 0

        teams = sorted(league.teams, key=lambda t: t.team_id)
        limitations = list(_COMMISSIONER_TRANSACTION_LIMITATIONS)
        seen_identities = set()
        items = []

        # --- LINEUP + ROSTER EVIDENCE (current state, zero extra network calls) ---
        lineup_eval_week = week if week is not None else current_week
        any_lineup_finding = False
        any_roster_finding = False
        teams_with_lineup_findings, teams_with_roster_findings = set(), set()

        for team in teams:
            lineup_findings = _commissioner_brief_lineup_items(team, slot_counts)
            for f in lineup_findings:
                identity = _commissioner_brief_finding_identity(f, team.team_id, lineup_eval_week)
                if identity in seen_identities:
                    continue
                seen_identities.add(identity)
                bucket = _commissioner_brief_priority_bucket(f["severity"], f.get("issue_class"))
                any_lineup_finding = True
                teams_with_lineup_findings.add(team.team_id)
                if bucket:
                    items.append({
                        "type": f["type"], "_bucket": bucket, "severity": f["severity"],
                        "team_id": team.team_id, "team_name": team.team_name,
                        "summary": f["basis"], "source": f["source"], "basis": f["basis"],
                        "player_id": f.get("playerId"), "player_name": f.get("player_name"),
                        "followup": {"tool": "commissioner_audit_lineups", "team_id": team.team_id},
                    })

            roster_findings = _commissioner_brief_roster_items(team, slot_counts)
            for f in roster_findings:
                identity = _commissioner_brief_finding_identity(f, team.team_id, None)
                if identity in seen_identities:
                    continue
                seen_identities.add(identity)
                bucket = _commissioner_brief_priority_bucket(f["severity"], f.get("issue_class"))
                any_roster_finding = True
                teams_with_roster_findings.add(team.team_id)
                if bucket:
                    items.append({
                        "type": f["type"], "_bucket": bucket, "severity": f["severity"],
                        "team_id": team.team_id, "team_name": team.team_name,
                        "summary": f["basis"], "source": f["source"], "basis": f["basis"],
                        "player_id": f.get("playerId"), "player_name": f.get("player_name"),
                        "followup": {"tool": "commissioner_audit_rosters", "team_id": team.team_id},
                    })

        lineups_coverage = "evaluated_with_findings" if any_lineup_finding else "evaluated_clean"
        rosters_coverage = "evaluated_with_findings" if any_roster_finding else "evaluated_clean"

        # --- TRANSACTION EVIDENCE (reuses C3's exact bounded scanner) ---
        try:
            matched_events, scanned, scan_truncated = _commissioner_fetch_activity_events(
                league, recent_activity_limit, None, None, None, None, None, None)
        except Exception as e:
            return _error_response("gathering commissioner brief transaction evidence", e)

        txn_summary = _commissioner_transaction_summary(matched_events)
        transactions_coverage = "available"
        if scan_truncated:
            limitations.append(f"transaction scan stopped after {scanned} activity objects - "
                                f"older activity may exist beyond this bounded brief scan")

        # Observed patterns (reuses the exact acquire-then-trade rule already proven in C7).
        # C7's own implementation explicitly sorts events ascending before this pass - the
        # scanner returns ESPN's native newest-first order, and an add must be visited BEFORE
        # its later trade for this single-pass detector to find the pair. Sort locally here
        # (does not affect the transaction summary/txn_summary counts computed above, and does
        # NOT mutate matched_events used for txn_summary since that already ran).
        pattern_events_ascending = sorted(matched_events, key=lambda ev: ev["timestamp_ms"])
        adds_by_player = {}
        for ev in pattern_events_ascending:
            for a in ev["actions"]:
                if a["action_type"] in ("free_agent_add", "waiver_add") and a["player_id"] is not None:
                    adds_by_player.setdefault(a["player_id"], []).append(ev)
                if a["action_type"] == "trade" and a["player_id"] is not None:
                    for add_ev in adds_by_player.get(a["player_id"], []):
                        if add_ev["timestamp_ms"] < ev["timestamp_ms"]:
                            identity = ("acquire_then_trade_sequence", a["team_id"], None, None, a["player_id"], a["player_name"])
                            if identity in seen_identities:
                                continue
                            seen_identities.add(identity)
                            items.append({
                                "type": "acquire_then_trade_sequence", "_bucket": "MONITOR", "severity": "monitor",
                                "team_id": a["team_id"], "team_name": a["team_name"],
                                "summary": f"Player {a['player_name']} was acquired then later appeared in a trade.",
                                "source": "derived_observation",
                                "basis": f"Player ID {a['player_id']} acquired at {add_ev['timestamp_utc']}, "
                                         f"traded at {ev['timestamp_utc']}",
                                "player_id": a["player_id"], "player_name": a["player_name"],
                                "followup": {"tool": "commissioner_investigate", "player_id": a["player_id"]},
                            })
        if matched_events:
            limitations.append("failed/losing waiver claims and other bidders are not exposed by the "
                                "installed ESPN activity feed - their absence is not evidence no competition occurred")

        # --- INACTIVITY EVIDENCE (internal-only derived signal, C2 lineup + C3 activity reuse) ---
        box_scores_cache = {}
        historical_lineups_status = "not_applicable"
        possible_inactivity_count = 0
        strong_inactivity_count = 0
        if current_week <= 1:
            inactivity_coverage = "insufficient_history"
        else:
            inactivity_labels = []
            any_week_attempted = False
            for team in teams:
                label, basis, weeks_examined, weeks_with_issue = _commissioner_brief_inactivity_signal(
                    team, current_week, inactivity_lookback_weeks, box_scores_cache, league, matched_events, slot_counts)
                inactivity_labels.append(label)
                if weeks_examined > 0:
                    any_week_attempted = True
                if label in ("possible_inactivity_signal", "strong_inactivity_signal"):
                    identity = (label, team.team_id, None, None, None, None)
                    if identity not in seen_identities:
                        seen_identities.add(identity)
                        bucket = "MONITOR" if label == "possible_inactivity_signal" else "REVIEW"
                        if label == "possible_inactivity_signal":
                            possible_inactivity_count += 1
                        else:
                            strong_inactivity_count += 1
                        items.append({
                            "type": label, "_bucket": bucket,
                            "severity": "monitor" if bucket == "MONITOR" else "review",
                            "team_id": team.team_id, "team_name": team.team_name,
                            "summary": "; ".join(basis), "source": "derived_observation", "basis": "; ".join(basis),
                            "player_id": None, "player_name": None,
                            "followup": {"tool": "commissioner_investigate", "team_id": team.team_id},
                        })
            if any(l == "insufficient_history" for l in inactivity_labels) and not any_week_attempted:
                inactivity_coverage = "insufficient_history"
                historical_lineups_status = "unavailable"
            elif any_week_attempted:
                inactivity_coverage = "available"
                historical_lineups_status = "available"
            else:
                inactivity_coverage = "insufficient_history"
                historical_lineups_status = "unavailable"

        # --- DEDUPE / CAP / SORT ---
        do_now = sorted([i for i in items if i["_bucket"] == "DO_NOW"], key=_commissioner_brief_item_sort_key)
        review = sorted([i for i in items if i["_bucket"] == "REVIEW"], key=_commissioner_brief_item_sort_key)
        monitor = sorted([i for i in items if i["_bucket"] == "MONITOR"], key=_commissioner_brief_item_sort_key)

        results_truncated = False
        full_do_now, full_review, full_monitor = len(do_now), len(review), len(monitor)
        if len(do_now) > _COMMISSIONER_BRIEF_DO_NOW_CAP:
            do_now = do_now[:_COMMISSIONER_BRIEF_DO_NOW_CAP]
            results_truncated = True
        if len(review) > _COMMISSIONER_BRIEF_REVIEW_CAP:
            review = review[:_COMMISSIONER_BRIEF_REVIEW_CAP]
            results_truncated = True
        if len(monitor) > _COMMISSIONER_BRIEF_MONITOR_CAP:
            monitor = monitor[:_COMMISSIONER_BRIEF_MONITOR_CAP]
            results_truncated = True

        for bucket_list in (do_now, review, monitor):
            for i in bucket_list:
                i.pop("_bucket", None)

        no_issue = []
        if lineups_coverage == "evaluated_clean":
            no_issue.append({"area": "lineups", "status": "evaluated_clean"})
        if rosters_coverage == "evaluated_clean":
            no_issue.append({"area": "roster_compliance", "status": "evaluated_clean"})

        # --- HEADLINE (deterministic, factual, no dramatic prose) ---
        if full_do_now > 0:
            headline = f"{full_do_now} commissioner item(s) require immediate attention."
        elif full_review > 0:
            headline = f"{full_review} commissioner item(s) require review."
        elif full_monitor > 0:
            headline = f"No immediate commissioner issues detected; {full_monitor} item(s) worth monitoring."
        else:
            headline = "No commissioner issues detected in the areas evaluated."
        if historical_lineups_status == "unavailable" or inactivity_coverage == "insufficient_history":
            headline += " Historical lineup/activity evidence for inactivity review is currently limited."

        coverage = {
            "lineups": lineups_coverage, "rosters": rosters_coverage, "transactions": transactions_coverage,
            "inactivity": inactivity_coverage, "historical_lineups": historical_lineups_status,
        }

        recommended_followups = []
        if review or do_now:
            recommended_followups.append({"tool": "commissioner_audit_lineups", "reason": "Review structural lineup findings in detail."})
        if any_roster_finding:
            recommended_followups.append({"tool": "commissioner_audit_rosters", "reason": "Review roster compliance findings in detail."})
        if strong_inactivity_count > 0 or possible_inactivity_count > 0:
            recommended_followups.append({"tool": "commissioner_investigate", "reason": "Investigate flagged teams' recent chronology."})

        return {
            "status": "ok",
            "commissioner": {"configured_for_commissioner_reads": True, "alias": resolved_alias, "scope": "read_only"},
            "league": {"league_id": resolved_league_id, "league_name": getattr(settings, "name", None),
                        "year": resolved_year, "current_week": current_week},
            "brief": {"headline": headline, "DO_NOW": do_now, "REVIEW": review, "MONITOR": monitor, "NO_ISSUE": no_issue},
            "summary": {"do_now": full_do_now, "review": full_review, "monitor": full_monitor,
                         "teams_with_findings": len(teams_with_lineup_findings | teams_with_roster_findings),
                         "recent_transaction_events": txn_summary["events_returned"],
                         "possible_inactivity_signals": possible_inactivity_count,
                         "strong_inactivity_signals": strong_inactivity_count},
            "coverage": coverage,
            "data_limitations": limitations,
            "recommended_followups": recommended_followups,
            "results_truncated": results_truncated,
        }
    except commissioner_config.CommissionerConfigError as e:
        return {"status": "error", "error": "commissioner_config_invalid", "message": str(e)}
    except Exception as e:
        return _error_response("assembling commissioner brief", e)

# --- DRAFT INTELLIGENCE FOUNDATION - PHASE D1 (2026-08-15) ---
# get_draft_board is a READ-ONLY factual draft-state tool. It is NOT a
# commissioner capability (no commissioner_config guard - draft
# intelligence is available for any registered league the user
# participates in) and it does NOT implement strategy/recommendations -
# those are explicitly deferred to D2+.
#
# D0 CRITICAL FINDINGS THIS TOOL IS BUILT AROUND (all empirically
# proven live against a representative live development league -
# see D0 report):
#  1. league.draft is EMPTY pre-draft because espn-api's BaseLeague.
#     _fetch_draft() returns early when draftDetail.drafted is False,
#     even though ESPN's raw response already contains the full
#     180-slot draft skeleton (12 teams x 15 rounds for the development league).
#  2. league.refresh_draft() is UNSAFE for live polling: it calls
#     _fetch_draft() again, which APPENDS to self.draft WITHOUT
#     clearing it first - synthetically proven to duplicate picks
#     (2 picks -> refresh -> 4 entries). This tool NEVER calls
#     refresh_draft() or League._fetch_draft().
#  3. The correct fresh-state path is a STATELESS project-owned ESPN transport GET
#     requesting mDraftDetail+mRoster+mTeam+mSettings in ONE combined call.
#     This never touches third-party League draft/team/settings caches.
#  4. Draft order is NOT a standard round-2-reversal snake for this
#     league (rounds 1-4 use an IDENTICAL non-reversed order, then
#     reverses every round from round 5 onward) - order is read
#     per-round directly from the raw picks list, never computed with
#     a generic snake formula.
#  5. Keeper SLOTS (reservedForKeeper) are known before the deadline;
#     keeper IDENTITIES are not. keeperValue/keeperValueFuture on
#     roster entries are currently null league-wide. This tool never
#     fabricates keeper identity/cost.

_DRAFT_STATE_VIEWS = ("mDraftDetail", "mRoster", "mTeam", "mSettings")

def _fetch_raw_draft_state(league_id: int, year: int) -> dict:
    """Fetch one fresh live-draft snapshot through the project-owned ESPN transport.

    The response intentionally combines draft detail, rosters, teams, and settings in
    one stateless request so live draft callers never depend on cached ``espn-api``
    League state or its unsafe ``refresh_draft()`` mutation behavior.
    """
    transport = api.get_transport(SESSION_ID)
    return transport.fetch_league(league_id, year, views=_DRAFT_STATE_VIEWS)

def _dp_derive_draft_status(draft_detail: dict) -> str:
    """Deterministic status rule (documented, not guessed):
    inProgress=True checked FIRST (most specific 'happening right now'
    signal, takes precedence even in a rare transitional case where
    ESPN briefly reports both flags true) -> 'in_progress'.
    inProgress=False and drafted=True  -> 'complete'.
    inProgress=False and drafted=False -> 'pre_draft'.
    Missing fields -> 'unknown' (never guessed)."""
    if "inProgress" not in draft_detail or "drafted" not in draft_detail:
        return "unknown"
    if draft_detail["inProgress"]:
        return "in_progress"
    return "complete" if draft_detail["drafted"] else "pre_draft"

def _dp_pick_slot_status(pick: dict) -> str:
    """Four mutually-exclusive real states derived ONLY from ESPN's own
    reservedForKeeper/playerId fields - never inferred from roster
    acquisitionType (D0 proved acquisitionType has no KEEPER value, so
    current roster presence is never treated as keeper truth here)."""
    reserved = bool(pick.get("reservedForKeeper"))
    has_player = pick.get("playerId", -1) not in (None, -1)
    if reserved and not has_player:
        return "reserved_keeper_unassigned"
    if reserved and has_player:
        return "keeper_assigned"
    if not reserved and has_player:
        return "drafted_player"
    return "open_pick"

def _dp_build_draft_order(picks_raw: list) -> list:
    """Derives per-round team order DIRECTLY from the raw pick
    skeleton - NEVER a generic snake formula (D0 empirically proved
    rounds 1-4 use an identical non-reversed order for this league,
    only reversing from round 5 onward). Sorted by roundPickNumber
    within each round; rounds sorted ascending. Whichever teamId
    ESPN's raw slot already shows is used as-is (authoritative even
    if picks were traded - never recomputed from original order)."""
    by_round = {}
    for p in picks_raw:
        by_round.setdefault(p.get("roundId"), []).append(p)
    order = []
    for round_id in sorted(k for k in by_round.keys() if k is not None):
        round_picks = sorted(by_round[round_id], key=lambda p: p.get("roundPickNumber", 0))
        order.append({"round": round_id, "team_order": [p.get("teamId") for p in round_picks]})
    return order

def _dp_build_position_lookup(raw_teams: list, year: int) -> dict:
    """Builds a playerId -> {name, position, proTeam, injury_status}
    lookup from the SAME combined-view response's roster entries, by
    reusing the project-owned roster-entry parser (which preserves the
    validated eligibleSlots-to-position semantics and project-owned
    POSITION_MAP compatibility table). Zero additional network calls - this
    is pure parsing of data already present in the one combined GET
    response. Covers every player CURRENTLY on a roster in this
    response; a newly-drafted player not yet reflected in this same
    roster snapshot (untestable without a live draft in progress this
    session) will not resolve here and is reported as position=None
    rather than fabricated - see 'known_limitations' in the response."""
    lookup = {}
    for t in raw_teams:
        for entry in (t.get("roster") or {}).get("entries", []):
            try:
                player = parse_roster_entry(entry, year)
                player_id = player.get("player_id")
                if player_id is None:
                    continue
                lookup[player_id] = {
                    "name": player.get("name"), "position": player.get("position"),
                    "proTeam": player.get("proTeam"), "injury_status": player.get("injury_status"),
                }
            except Exception:
                continue
    return lookup

def _dp_state_hash(league_id: int, year: int, picks_raw: list) -> str:
    """Deterministic, non-secret fingerprint of resolved draft state
    (completed/keeper-assigned slots only, ordered by overall pick) so
    future callers can detect no_change/new_pick/multiple_new_picks.
    Not a security/authorization token - purely a change-detection id."""
    resolved = sorted(
        [(p.get("overallPickNumber"), p.get("teamId"), p.get("playerId"))
         for p in picks_raw if p.get("playerId", -1) not in (None, -1)],
        key=lambda t: (t[0] if t[0] is not None else -1)
    )
    canonical = json.dumps({"league_id": league_id, "year": year, "resolved": resolved}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

def _dp_normalize_pick(pick: dict, pos_lookup: dict) -> dict:
    player_id = pick.get("playerId")
    has_player = player_id not in (None, -1)
    identity = pos_lookup.get(player_id, {}) if has_player else {}
    return {
        "overall_pick": pick.get("overallPickNumber"),
        "round": pick.get("roundId"),
        "round_pick": pick.get("roundPickNumber"),
        "team_id": pick.get("teamId"),
        "player_id": player_id if has_player else None,
        "player_name": identity.get("name"),
        "player_position": identity.get("position"),
        "reserved_for_keeper": bool(pick.get("reservedForKeeper")),
        "bid_amount": pick.get("bidAmount") if pick.get("bidAmount") else None,
        "trade_locked": bool(pick.get("tradeLocked")),
        "slot_status": _dp_pick_slot_status(pick),
    }

def _dp_build_my_pick_context(picks_raw: list, my_team_id, pos_lookup: dict, unresolved: list) -> dict:
    """Shared helper extracted from get_draft_board's original inline
    logic (D1) so D2's prepare_draft_strategy can reuse the EXACT same
    my-pick-schedule construction without duplicating it - narrow
    extraction, byte-for-byte equivalent to the original inline code
    (proven by D1 regression test comparing get_draft_board output
    before/after this extraction). Returns
    {my_picks_all, next_my_pick, picks_until_my_pick}."""
    my_picks_all, next_my_pick, picks_until_my_pick = [], None, None
    if my_team_id is not None:
        my_slots = sorted([p for p in picks_raw if p.get("teamId") == my_team_id],
                            key=lambda p: (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9))
        my_picks_all = [_dp_normalize_pick(p, pos_lookup) for p in my_slots]
        my_unresolved = [p for p in my_slots if p.get("playerId", -1) in (None, -1)]
        if my_unresolved:
            next_my_pick_raw = my_unresolved[0]
            next_my_pick = _dp_normalize_pick(next_my_pick_raw, pos_lookup)
            my_next_overall = next_my_pick_raw.get("overallPickNumber")
            picks_until_my_pick = sum(
                1 for p in unresolved
                if (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9) < my_next_overall
            )
    return {"my_picks_all": my_picks_all, "next_my_pick": next_my_pick, "picks_until_my_pick": picks_until_my_pick}

@mcp.tool()
async def get_draft_board(alias: str = None, league_id: int = None, year: int = None,
                            top_available: int = 50) -> dict:
    """Authoritative factual live ESPN draft board (D1). Use this tool
    for current availability, completed/recent picks, team draft builds,
    and live board state - not external page or DOM parsing. Every
    invocation issues exactly ONE fresh raw ESPN draft-state GET
    (bypassing the stale/buggy league.draft cache and the unsafe
    league.refresh_draft() - see D0 findings), plus one bounded
    project-owned free-agent read for the ESPN-truth available-player universe.
    FantasyPros enrichment is CACHE-ONLY (zero HTTP calls, zero quota
    impact) and covers QB/RB/WR/TE only - K/DST are included factually
    from ESPN with no fabricated FP tier/rank. NOT a commissioner tool
    (usable for any registered league). NOT a strategy/recommendation
    tool (D2+ builds that layer on top of this).

    Args:
        alias: Registered league alias (case-insensitive).
        league_id: Registered ESPN league ID.
        year: NFL season year (defaults to current).
        top_available: Bounded 1-200, default 50. Controls how many
                        enriched available players are returned
                        (response-size control only - never limits
                        the underlying ESPN draft-state scan).
    """
    try:
        resolved_year = _resolve_year(year)
        top_n, top_err = _validate_bounded_int(top_available, "top_available", 1, 200, 50)
        if top_err:
            return {"error": "invalid_top_available", "message": top_err}

        try:
            registry = league_registry.load_registry()
        except league_registry.RegistryError as e:
            return {"error": "registry_error", "message": str(e)}

        if alias is not None and league_id is not None:
            try:
                alias_norm, alias_entry = league_registry.resolve_alias(registry, alias)
            except league_registry.RegistryError as e:
                return {"error": "alias_not_found", "message": str(e)}
            try:
                id_alias_norm, _ = league_registry.resolve_league_id(registry, league_id)
            except league_registry.RegistryError as e:
                return {"error": "league_not_registered", "message": str(e)}
            if alias_norm != id_alias_norm:
                return {"error": "conflicting_parameters",
                        "message": f"alias '{alias}' resolves to '{alias_norm}' but league_id "
                                    f"{league_id} resolves to '{id_alias_norm}' - these must match."}
            resolved_alias, entry = alias_norm, alias_entry
        elif alias is not None:
            try:
                resolved_alias, entry = league_registry.resolve_alias(registry, alias)
            except league_registry.RegistryError as e:
                return {"error": "alias_not_found", "message": str(e)}
        elif league_id is not None:
            try:
                resolved_alias, entry = league_registry.resolve_league_id(registry, league_id)
            except league_registry.RegistryError as e:
                return {"error": "league_not_registered", "message": str(e)}
        else:
            resolved_alias, entry = league_registry.get_default_league(registry)

        resolved_league_id = entry["league_id"]

        # --- THE single raw draft-state fetch (D0 finding #3) ---
        try:
            raw = _fetch_raw_draft_state(resolved_league_id, resolved_year)
        except Exception as e:
            return _error_response("fetching raw ESPN draft state", e)

        # Rebuild only the narrow compatibility shape this mature draft code consumes;
        # no third-party League object or additional ESPN request is constructed.
        league = build_commissioner_snapshot(raw, resolved_league_id, resolved_year)
        authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")
        my_team = resolve_my_team_from_payload(raw, authenticated_swid)
        my_team_id = my_team.get("team_id")

        draft_detail = raw.get("draftDetail", {})
        picks_raw = draft_detail.get("picks", [])
        if not picks_raw:
            return {"status": "error", "error": "draft_data_unavailable",
                    "message": "ESPN returned no draft skeleton for this league/year."}

        draft_status = _dp_derive_draft_status(draft_detail)
        raw_teams = raw.get("teams", [])
        raw_settings = raw.get("settings", {})
        draft_settings = raw_settings.get("draftSettings", {})
        pos_lookup = _dp_build_position_lookup(raw_teams, resolved_year)

        total_rounds = max((p.get("roundId") for p in picks_raw if p.get("roundId") is not None), default=0)
        total_picks = len(picks_raw)
        completed_picks = sum(1 for p in picks_raw if p.get("playerId", -1) not in (None, -1))

        picks_sorted = sorted(picks_raw, key=lambda p: (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9))
        unresolved = [p for p in picks_sorted if p.get("playerId", -1) in (None, -1)]
        next_slot = unresolved[0] if unresolved else None
        next_overall_pick = next_slot.get("overallPickNumber") if next_slot else None
        team_on_clock = None
        if next_slot is not None:
            t = _find_team_by_id(league, next_slot.get("teamId"))
            if t is not None:
                team_on_clock = {"team_id": t.team_id, "team_name": t.team_name}
            else:
                team_on_clock = {"team_id": next_slot.get("teamId"), "team_name": None}

        draft_order = _dp_build_draft_order(picks_raw)

        # --- My team's pick schedule (D2-shared helper - see _dp_build_my_pick_context) ---
        _my_pick_ctx = _dp_build_my_pick_context(picks_raw, my_team_id, pos_lookup, unresolved)
        my_picks_all = _my_pick_ctx["my_picks_all"]
        next_my_pick = _my_pick_ctx["next_my_pick"]
        picks_until_my_pick = _my_pick_ctx["picks_until_my_pick"]

        # --- Keeper model (honest unknowns, never fabricated) ---
        reserved_slots = [_dp_normalize_pick(p, pos_lookup) for p in picks_raw if p.get("reservedForKeeper")]
        assigned_keepers = [p for p in reserved_slots if p["slot_status"] == "keeper_assigned"]
        unassigned_keepers = [p for p in reserved_slots if p["slot_status"] == "reserved_keeper_unassigned"]
        if reserved_slots and not assigned_keepers:
            identity_state = "unknown_pre_deadline"
        elif assigned_keepers and unassigned_keepers:
            identity_state = "partial"
        elif assigned_keepers and not unassigned_keepers:
            identity_state = "known"
        else:
            identity_state = "not_applicable"
        keeper_deadline_ms = draft_settings.get("keeperDeadlineDate")
        keeper_deadline_iso = None
        if keeper_deadline_ms:
            keeper_deadline_iso = datetime.datetime.fromtimestamp(
                keeper_deadline_ms / 1000, tz=datetime.timezone.utc).isoformat()

        # --- Team drafted builds (from resolved draft slots ONLY - never
        #     from the current season roster, which would conflate
        #     draft selections with pre-existing/keeper roster state) ---
        teams_builds = []
        for t in league.teams:
            team_completed = [p for p in picks_raw if p.get("teamId") == t.team_id
                                and p.get("playerId", -1) not in (None, -1)]
            position_counts = {}
            players_out = []
            for p in team_completed:
                identity = pos_lookup.get(p.get("playerId"), {})
                pos = identity.get("position") or "UNKNOWN"
                position_counts[pos] = position_counts.get(pos, 0) + 1
                players_out.append({"player_id": p.get("playerId"), "name": identity.get("name"),
                                      "position": pos, "overall_pick": p.get("overallPickNumber")})
            teams_builds.append({
                "team_id": t.team_id, "team_name": t.team_name,
                "picks_completed": len(team_completed),
                "position_counts": position_counts,
                "players": players_out,
            })

        # --- Available player pool (ESPN truth: free_agents, bounded,
        #     single call - never FantasyPros, never player_info fanout) ---
        drafted_or_keeper_ids = {p.get("playerId") for p in picks_raw if p.get("playerId", -1) not in (None, -1)}
        available_warnings = []
        enriched_available = []
        available_by_position = {}
        try:
            fa_size = min(max(top_n * 3, 100), 400)
            fa_context = _fetch_free_agent_context_payload(resolved_league_id, resolved_year)
            fa_week = resolve_free_agent_week(fa_context, None)
            fa_payload = _fetch_free_agent_player_payload(
                resolved_league_id, resolved_year, fa_week, fa_size
            )
            fa_schedule = _fetch_pro_schedule_payload(resolved_year)
            fa_rows = build_free_agents(
                fa_payload, fa_schedule, resolved_year, fa_week, include_internal=True
            )
            fa_players = [SimpleNamespace(
                playerId=p.get("_player_id"), name=p.get("name"),
                position=p.get("position"), proTeam=p.get("proTeam"),
                injuryStatus=p.get("_injury_status"),
            ) for p in fa_rows]
        except Exception as e:
            fa_players = []
            available_warnings.append(f"free_agent pool fetch failed: {type(e).__name__}")

        scoring_rules = getattr(league.settings, "scoring_format", []) or []
        scoring_bucket = _detect_league_scoring_bucket(scoring_rules)
        cache_warnings = _check_required_fp_caches(fp_client.CORE_POSITIONS, scoring_bucket)

        candidate_pool = [p for p in fa_players if getattr(p, "playerId", None) not in drafted_or_keeper_ids]
        for p in candidate_pool:
            pos = getattr(p, "position", None) or "UNKNOWN"
            available_by_position[pos] = available_by_position.get(pos, 0) + 1

        # --- PERFORMANCE (measured live 2026-08-15): fantasypros_client's
        # match_player()->get_players_cache() re-reads and re-parses the
        # entire players.json cache file from DISK on EVERY call (no
        # in-memory layer - frozen, pre-existing behavior in
        # fantasypros_client.py, which this phase does not modify).
        # Calling build_player_intelligence() once per candidate (as an
        # earlier version of this tool did) measured 6-7s per invocation
        # with ~150-200 redundant file re-reads. Fixed by reading each
        # needed FP cache dataset EXACTLY ONCE per position (players
        # cache once total, rankings cache once per position - 5 total
        # reads) and doing O(1) in-memory dict lookups by the cache's
        # own precomputed _norm_name field for every candidate, instead
        # of re-invoking match_player() per player. Still 100%
        # cache-only, zero FantasyPros HTTP calls, zero quota impact -
        # this is a call-count/architecture fix, not a network-policy
        # change.
        players_cache_once = fp_client.get_players_cache()
        adp_by_norm_name = {}
        if players_cache_once:
            for row in players_cache_once.get("players", []):
                nm = row.get("_norm_name")
                if nm:
                    adp_by_norm_name[nm] = row.get("rank_adp_ppr") if scoring_bucket == "PPR" else row.get("rank_adp")

        rankings_by_position = {}
        for pos in fp_client.CORE_POSITIONS:
            cache = fp_client.get_rankings_cache(pos, scoring_bucket)
            by_name = {}
            if cache:
                for row in cache.get("players", []):
                    nm = row.get("_norm_name")
                    if nm:
                        by_name[nm] = row
            rankings_by_position[pos] = by_name

        def _dp_fast_fp_lookup(player_name, position):
            if position not in fp_client.CORE_POSITIONS:
                return None
            norm = fp_client.normalize_player_name(player_name)
            row = rankings_by_position.get(position, {}).get(norm)
            if row is None:
                return {"match_confidence": "none", "ecr": None, "pos_rank": None,
                        "tier": None, "adp": None, "injury_status": None}
            return {"match_confidence": "high", "ecr": row.get("rank_ecr"),
                    "pos_rank": row.get("pos_rank"), "tier": row.get("tier"),
                    "adp": adp_by_norm_name.get(norm), "injury_status": None}

        for p in candidate_pool[: top_n]:
            pos = getattr(p, "position", None)
            entry_out = {
                "player_id": getattr(p, "playerId", None), "name": getattr(p, "name", None),
                "position": pos, "proTeam": getattr(p, "proTeam", None),
                "injury_status": getattr(p, "injuryStatus", None), "available": True,
            }
            if pos in fp_client.CORE_POSITIONS:
                entry_out["fantasypros"] = _dp_fast_fp_lookup(getattr(p, "name", None), pos)
            else:
                entry_out["fantasypros"] = None
                entry_out["analysis_enrichment"] = "espn_only"
            enriched_available.append(entry_out)

        # --- Factual tier-count aggregation (QB/RB/WR/TE only) - reuses
        # the SAME rankings_by_position dicts built above, zero extra
        # cache reads or file I/O beyond the 5 total already performed. ---
        available_by_position_and_tier = {}
        for pos in fp_client.CORE_POSITIONS:
            pos_candidates = [p for p in candidate_pool if getattr(p, "position", None) == pos]
            tier_counts = {}
            for p in pos_candidates:
                fp_row = _dp_fast_fp_lookup(getattr(p, "name", None), pos)
                tier = fp_row.get("tier") if fp_row else None
                if tier is not None:
                    tier_counts[f"tier_{tier}"] = tier_counts.get(f"tier_{tier}", 0) + 1
            if tier_counts:
                available_by_position_and_tier[pos] = tier_counts

        data_freshness = fp_client.get_cache_freshness_report(fp_client.CORE_POSITIONS, scoring_bucket)
        stale_warnings = [f"{k} cache is stale" for k, v in data_freshness.items() if v.get("is_stale")]

        last_pick = None
        completed_sorted = [p for p in picks_sorted if p.get("playerId", -1) not in (None, -1)]
        if completed_sorted:
            last_pick = _dp_normalize_pick(completed_sorted[-1], pos_lookup)
        recent_picks = [_dp_normalize_pick(p, pos_lookup) for p in completed_sorted[-10:]]

        known_limitations = list(cache_warnings) + list(stale_warnings) + list(available_warnings)
        known_limitations.append(
            "Position for a newly-drafted player is derived from this same response's roster "
            "snapshot; if ESPN's live roster view lags real-time picks during an actual in-progress "
            "draft, position may briefly show null instead of being fabricated (unverified without a "
            "live draft in this session)."
        )
        if identity_state == "unknown_pre_deadline":
            known_limitations.append(
                f"Keeper slots are reserved but keeper IDENTITIES are not yet known "
                f"(deadline: {keeper_deadline_iso or 'unknown'}). No keeper identity is fabricated."
            )
        if draft_status == "pre_draft":
            known_limitations.append(
                "Draft has not started. The available-player pool reflects current ESPN free-agent "
                "status, not final draft-day availability - keeper resolution and any pre-draft "
                "roster moves before the actual draft can still change this."
            )

        return {
            "status": "ok",
            "league": {"league_id": resolved_league_id, "league_name": getattr(league.settings, "name", None),
                        "year": resolved_year, "alias": resolved_alias},
            "draft": {
                "status": draft_status, "draft_type": draft_settings.get("type"),
                "order_type": draft_settings.get("orderType"),
                "total_rounds": total_rounds, "total_picks": total_picks,
                "completed_picks": completed_picks,
                "next_overall_pick": next_overall_pick,
                "current_round": next_slot.get("roundId") if next_slot else None,
                "current_round_pick": next_slot.get("roundPickNumber") if next_slot else None,
                "team_on_clock": team_on_clock,
                "time_per_selection_seconds": draft_settings.get("timePerSelection"),
                "seconds_remaining": None, "seconds_remaining_status": "not_exposed",
                "pause_state": "not_exposed",
            },
            "my_team": my_team,
            "my_picks": my_picks_all,
            "next_my_pick": next_my_pick,
            "picks_until_my_pick": picks_until_my_pick,
            "draft_order": draft_order,
            "picks": {"completed": [_dp_normalize_pick(p, pos_lookup) for p in completed_sorted],
                      "last_pick": last_pick, "recent": recent_picks},
            "teams": teams_builds,
            "available": {
                "top_available": top_n,
                "by_position_count": available_by_position,
                "by_position_and_tier": available_by_position_and_tier,
                "players": enriched_available,
            },
            "keepers": {
                "configured_count": getattr(league.settings, "keeper_count", None),
                "deadline_utc": keeper_deadline_iso,
                "identity_state": identity_state,
                "reserved_slots": reserved_slots,
                "assigned_keepers": assigned_keepers,
            },
            "data_freshness": data_freshness,
            "state_hash": _dp_state_hash(resolved_league_id, resolved_year, picks_raw),
            "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "data_limitations": known_limitations,
        }
    except Exception as e:
        return _error_response("building draft board", e)


# --- PRE-DRAFT STRATEGY ENGINE - PHASE D2 (2026-08-15) ---
# prepare_draft_strategy builds a transparent, league-specific pre-draft
# plan on top of D1's factual draft board. It is NOT a commissioner
# tool (no commissioner_config guard) and does NOT recommend live picks
# (that is D3/analyze_draft_pick). All FantasyPros access is
# CACHE-ONLY - zero HTTP calls, zero quota impact, enforced by reading
# each FP dataset exactly once per position (same D1 perf-fix pattern:
# never call fp_client.match_player()/build_player_intelligence() in a
# per-player loop).

_DS_CORE_POSITIONS = ("QB", "RB", "WR", "TE")

# Documented, fixed reach/value bands (pick-number space). Never hidden,
# never combined into an opaque score. adp_delta = ADP - current_pick:
# negative means the player's typical draft slot is EARLIER than this
# pick (i.e. they are still here later than expected = value); positive
# means drafting them now is earlier than the market typically would
# (a reach).
_DS_MARKET_BANDS = (
    ("strong_value", lambda d: d <= -8),
    ("value", lambda d: -8 < d <= -3),
    ("fair_range", lambda d: -3 < d < 3),
    ("small_reach", lambda d: 3 <= d < 8),
    ("significant_reach", lambda d: d >= 8),
)

def _ds_market_band(adp, current_pick):
    if adp is None or current_pick is None:
        return "unknown", None
    delta = adp - current_pick
    for label, test in _DS_MARKET_BANDS:
        if test(delta):
            return label, delta
    return "unknown", delta

def _ds_build_player_universe(position: str, scoring_bucket: str) -> tuple:
    """ONE read each of rankings/projections/players/injuries cache for
    this position (never per-player) - joins by _norm_name into a flat
    list of dicts. FP native tier is used AS-IS, never overwritten.
    Returns (universe_list, warnings_list)."""
    warnings = []
    rankings = fp_client.get_rankings_cache(position, scoring_bucket)
    projections = fp_client.get_projections_cache(position, scoring_bucket, week=0)
    players_cache = fp_client.get_players_cache()
    injuries = fp_client.get_injuries_cache()
    if rankings is None:
        warnings.append(f"rankings_{position}_{scoring_bucket} cache is missing.")
    if projections is None:
        warnings.append(f"projections_{position}_{scoring_bucket}_wk0 cache is missing.")

    proj_by_name = {}
    if projections:
        for row in projections.get("players", []):
            nm = row.get("_norm_name")
            if nm:
                proj_by_name[nm] = row.get("projected_points")
    adp_by_name = {}
    if players_cache:
        for row in players_cache.get("players", []):
            nm = row.get("_norm_name")
            if nm:
                adp_by_name[nm] = row.get("rank_adp_ppr") if scoring_bucket == "PPR" else row.get("rank_adp")
    injury_by_name = {}
    if injuries:
        for row in injuries.get("injuries", []):
            nm = row.get("_norm_name")
            if nm:
                injury_by_name[nm] = {"status": row.get("status"), "comment": row.get("comment")}

    universe = []
    if rankings:
        for row in rankings.get("players", []):
            nm = row.get("_norm_name")
            universe.append({
                "fp_player_id": row.get("fp_player_id"), "name": row.get("name"),
                "team": row.get("team"), "position": position,
                "ecr": row.get("rank_ecr"), "pos_rank": row.get("pos_rank"),
                "tier": row.get("tier"), "adp": adp_by_name.get(nm),
                "projection": proj_by_name.get(nm), "bye_week": row.get("bye_week"),
                "injury_status": (injury_by_name.get(nm) or {}).get("status"),
                "_norm_name": nm,
            })
    # Sort by projection descending (unknown projections sort last, never
    # treated as zero - avoids silently promoting missing-data players
    # to "elite" or falsely demoting them to "worst").
    universe.sort(key=lambda p: (p["projection"] is None, -(p["projection"] or 0)))
    return universe, warnings

def _ds_starter_flex_allocation(slot_counts: dict, universes: dict) -> dict:
    """Deterministic league-specific starter+FLEX demand model (D2
    methodology, documented in full):
    1. Dedicated demand per position = teams-wide dedicated slot count
       (direct read from league.settings.position_slot_counts - never
       hardcoded like '2 RB, 3 WR').
    2. Remove the top `dedicated_demand[pos]` players (by projection)
       from each position's own list - these are the dedicated starters.
    3. For each configured flex-style slot (RB/WR/TE, OP/SUPERFLEX,
       etc - via the frozen _parse_flex_eligibility parser), build the
       combined remaining-eligible pool and greedily assign the highest-
       projected remaining players to fill that slot type's total
       league-wide demand (teams x count).
    4. Count how many of each position actually filled flex slots.
    5. replacement_index[pos] = dedicated_demand[pos] + flex_filled[pos]
       - the boundary player at that index in position's own sorted
         list is the replacement-level player for VOR.
    Never assigns all FLEX demand to one arbitrary position."""
    dedicated_demand = {pos: (slot_counts.get(pos) or 0) for pos in _DS_CORE_POSITIONS}
    flex_filled = {pos: 0 for pos in _DS_CORE_POSITIONS}
    assigned_names = {pos: set() for pos in _DS_CORE_POSITIONS}

    for pos in _DS_CORE_POSITIONS:
        for p in universes.get(pos, [])[: dedicated_demand[pos]]:
            if p.get("_norm_name"):
                assigned_names[pos].add(p["_norm_name"])

    flex_defs = []
    for slot_key, count in (slot_counts or {}).items():
        if not count or count <= 0:
            continue
        eligible = _parse_flex_eligibility(slot_key)
        if eligible:
            flex_defs.append((slot_key, count, [p for p in eligible if p in _DS_CORE_POSITIONS]))
    flex_defs.sort(key=lambda t: t[0])  # deterministic processing order

    flex_fill_detail = []
    for slot_key, count, eligible in flex_defs:
        total_demand = count  # already team-wide count from ESPN settings
        candidates = []
        for pos in eligible:
            for p in universes.get(pos, []):
                if p.get("_norm_name") and p["_norm_name"] not in assigned_names[pos] and p.get("projection") is not None:
                    candidates.append((pos, p))
        candidates.sort(key=lambda t: -(t[1]["projection"] or 0))
        filled_this_slot = candidates[: total_demand]
        for pos, p in filled_this_slot:
            assigned_names[pos].add(p["_norm_name"])
            flex_filled[pos] += 1
        flex_fill_detail.append({"slot": slot_key, "team_wide_demand": total_demand,
                                   "filled_by_position": {pos: sum(1 for fp, _ in filled_this_slot if fp == pos) for pos in eligible}})

    replacement_index = {pos: dedicated_demand[pos] + flex_filled[pos] for pos in _DS_CORE_POSITIONS}
    return {"dedicated_demand": dedicated_demand, "flex_filled": flex_filled,
            "replacement_index": replacement_index, "flex_slot_detail": flex_fill_detail}

def _ds_apply_replacement_and_vor(universes: dict, replacement_index: dict) -> dict:
    """Annotates each player row with 'vor' in-place and returns the
    replacement_projection per position. VOR = player_projection -
    replacement_projection, computed ONLY when both are known
    projections - never a fabricated/zero-filled substitute."""
    replacement_by_position = {}
    for pos in _DS_CORE_POSITIONS:
        universe = universes.get(pos, [])
        idx = replacement_index.get(pos, 0)
        replacement_row = universe[idx] if idx < len(universe) else None
        replacement_projection = replacement_row.get("projection") if replacement_row else None
        replacement_by_position[pos] = {
            "replacement_projection": replacement_projection,
            "replacement_player_name": replacement_row.get("name") if replacement_row else None,
            "replacement_rank": idx + 1 if replacement_row else None,
        }
        for p in universe:
            if p.get("projection") is not None and replacement_projection is not None:
                p["vor"] = round(p["projection"] - replacement_projection, 2)
            else:
                p["vor"] = None
    return replacement_by_position

def _ds_build_tier_board(universes: dict) -> dict:
    """Groups each position's universe by FP NATIVE tier (never
    derived/overwritten). Players lacking a tier are reported under
    tier=unknown, never assigned a fabricated tier. Computes per-tier
    depth and the transparent projection/ECR drop to the next tier."""
    board = {}
    for pos in _DS_CORE_POSITIONS:
        universe = universes.get(pos, [])
        by_tier = {}
        for p in universe:
            key = p.get("tier") if p.get("tier") is not None else "unknown"
            by_tier.setdefault(key, []).append(p)
        tier_keys_numeric = sorted([k for k in by_tier if k != "unknown"])
        tiers_out = []
        for i, tk in enumerate(tier_keys_numeric):
            members = by_tier[tk]
            members_with_proj = [m for m in members if m.get("projection") is not None]
            next_tier_key = tier_keys_numeric[i + 1] if i + 1 < len(tier_keys_numeric) else None
            projection_drop, ecr_gap = None, None
            if next_tier_key is not None and members_with_proj:
                next_members = [m for m in by_tier[next_tier_key] if m.get("projection") is not None]
                if next_members:
                    last_in_tier = min(members_with_proj, key=lambda m: m["projection"])
                    first_in_next = max(next_members, key=lambda m: m["projection"])
                    projection_drop = round(last_in_tier["projection"] - first_in_next["projection"], 2)
                    if last_in_tier.get("ecr") is not None and first_in_next.get("ecr") is not None:
                        ecr_gap = first_in_next["ecr"] - last_in_tier["ecr"]
            tiers_out.append({
                "tier": tk, "players_available": len(members),
                "highest_ranked": members[0].get("name") if members else None,
                "lowest_ranked": members[-1].get("name") if members else None,
                "projection_range": [
                    round(max(m["projection"] for m in members_with_proj), 2) if members_with_proj else None,
                    round(min(m["projection"] for m in members_with_proj), 2) if members_with_proj else None,
                ],
                "next_tier": next_tier_key, "projection_drop_to_next_tier": projection_drop,
                "ecr_gap_to_next_tier": ecr_gap,
            })
        board[pos] = {"tiers": tiers_out, "unknown_tier_count": len(by_tier.get("unknown", []))}
    return board

def _ds_position_guidance(pos: str, universe: list, replacement_info: dict, dedicated_demand: dict) -> dict:
    """Explainable aggressive/balanced/patient classification. Basis is
    ALWAYS visible in the response (never a canned per-position rule -
    the same function runs identically for every position; a deep QB
    pool yields 'patient' and a shallow one yields 'aggressive' purely
    from the numbers, satisfying the no-canned-QB/TE-rule requirement)."""
    with_proj = [p for p in universe if p.get("projection") is not None]
    top_vor_players = sorted([p for p in with_proj if p.get("vor") is not None], key=lambda p: -p["vor"])
    top_vor = top_vor_players[0]["vor"] if top_vor_players else None
    # Depth = how many players remain within 90% of the top VOR at this position
    deep_count = sum(1 for p in top_vor_players if top_vor and p["vor"] >= 0.9 * top_vor) if top_vor and top_vor > 0 else len(top_vor_players)
    if top_vor is None:
        guidance = "unknown"
    elif top_vor >= 40 or deep_count <= 3:
        guidance = "aggressive"
    elif deep_count >= 10:
        guidance = "patient"
    else:
        guidance = "balanced"
    return {"position": pos, "guidance": guidance,
            "basis": {"top_available_vor": top_vor, "players_within_90pct_of_top_vor": deep_count,
                        "dedicated_demand": dedicated_demand.get(pos)}}

def _ds_available_universe(universe: list, unavailable_norm_names: set) -> list:
    """Excludes any player whose normalized name matches an ESPN-known
    drafted/keeper-assigned player (name-based, ESPN is availability
    truth - documented limitation: relies on name matching, same
    approach already used throughout this codebase, e.g. D0's
    match_player). Pre-draft with zero completed picks this is a
    no-op by construction (nothing to exclude yet)."""
    return [p for p in universe if p.get("_norm_name") not in unavailable_norm_names]

def _ds_build_target_list(available: list, current_pick: int, max_targets: int = 8) -> dict:
    """Builds targets/wait_candidates/reach_candidates from the
    available (post-keeper/draft-exclusion) universe, using ONLY
    transparent ECR/ADP/tier/VOR fields - never a hidden combined
    score. Candidate inclusion favors players with known projection/
    ECR whose market band is fair_range or better (value/strong_value),
    but ALSO includes plausible small_reach candidates so users can see
    both true value AND reasonable-reach options at this pick."""
    annotated = []
    for p in available:
        if p.get("projection") is None and p.get("ecr") is None:
            continue
        band, delta = _ds_market_band(p.get("adp"), current_pick)
        annotated.append({**p, "market_band": band, "adp_delta": delta})
    annotated.sort(key=lambda p: (p.get("ecr") is None, p.get("ecr") if p.get("ecr") is not None else 9999))
    targets = [p for p in annotated if p["market_band"] in ("strong_value", "value", "fair_range", "small_reach", "unknown")][:max_targets]
    wait_candidates = [p for p in annotated if p["market_band"] in ("strong_value", "value")][:max_targets]
    reach_candidates = [p for p in annotated if p["market_band"] in ("small_reach", "significant_reach")][:max_targets]
    return {"targets": targets, "wait_candidates": wait_candidates, "reach_candidates": reach_candidates}

def _ds_build_pick_plan(open_picks: list, universes: dict, tier_board: dict,
                          replacement_by_position: dict, unavailable_norm_names: set, horizon: int) -> list:
    """Per-open-pick plan. Does NOT force a rigid position sequence -
    priority.positions is derived per-pick from CURRENT tier/VOR
    evidence at generation time (pre-draft, so identical across picks
    in horizon unless keepers/data differ - live re-ranking during an
    actual draft is D3's job, not D2's)."""
    plans = []
    for i, pick in enumerate(open_picks[:horizon]):
        overall = pick["overall_pick"]
        pos_priority = []
        for pos in _DS_CORE_POSITIONS:
            avail = _ds_available_universe(universes.get(pos, []), unavailable_norm_names)
            top = sorted([p for p in avail if p.get("vor") is not None], key=lambda p: -p["vor"])
            pos_priority.append({"position": pos, "top_available_vor": top[0]["vor"] if top else None})
        pos_priority.sort(key=lambda p: (p["top_available_vor"] is None, -(p["top_available_vor"] or -9999)))

        combined_avail = []
        for pos in _DS_CORE_POSITIONS:
            combined_avail.extend(_ds_available_universe(universes.get(pos, []), unavailable_norm_names))
        target_data = _ds_build_target_list(combined_avail, overall)

        next_open = open_picks[i + 1] if i + 1 < len(open_picks) else None
        decision_rules = []
        if len(pos_priority) >= 2 and pos_priority[0]["top_available_vor"] is not None:
            best, second = pos_priority[0], pos_priority[1]
            if second["top_available_vor"] is not None:
                decision_rules.append(
                    f"Current evidence favors {best['position']} (top available VOR {best['top_available_vor']}) "
                    f"over {second['position']} (top available VOR {second['top_available_vor']}); prefer the higher-VOR "
                    f"position, but take a falling elite player of any position over a marginal-VOR reach."
                )

        plans.append({
            "overall_pick": overall, "round": pick["round"],
            "priority": {"positions": [p["position"] for p in pos_priority],
                          "basis": pos_priority},
            "targets": target_data["targets"],
            "wait_candidates": target_data["wait_candidates"],
            "reach_candidates": target_data["reach_candidates"],
            "decision_rules": decision_rules,
            "next_turn": {"next_open_pick": next_open["overall_pick"] if next_open else None,
                            "gap": (next_open["overall_pick"] - overall) if next_open else None},
        })
    return plans

def _ds_build_contingencies(tier_board: dict, replacement_by_position: dict) -> list:
    """Structured, evidence-based contingencies. D2 does NOT run these
    live (no live pick sequence exists pre-draft) - it defines the
    deterministic TRIGGER conditions D3 will evaluate against live
    state."""
    contingencies = []
    for pos in _DS_CORE_POSITIONS:
        tiers = tier_board.get(pos, {}).get("tiers", [])
        for t in tiers:
            if t.get("projection_drop_to_next_tier") is not None and t["projection_drop_to_next_tier"] >= 15:
                contingencies.append({
                    "trigger": "tier_cliff", "position": pos,
                    "condition": {"tier": t["tier"], "players_remaining_in_tier": t["players_available"],
                                   "projection_drop_to_next_tier": t["projection_drop_to_next_tier"]},
                    "response": "value_override_candidate",
                    "basis": f"{pos} tier {t['tier']} has a {t['projection_drop_to_next_tier']} point drop to tier {t['next_tier']} "
                              f"- consider taking the last tier-{t['tier']} player over a marginal-value pick at another position.",
                })
    contingencies.append({
        "trigger": "elite_value_fall", "condition": {"any_position_top_available_vor_gain": "greater_than_15_points_over_next_best"},
        "response": "value_override_candidate",
        "basis": "If any position's top available player carries materially higher VOR than the next-best option at the "
                  "planned position, prioritize value over the pre-draft position plan.",
    })
    contingencies.append({
        "trigger": "keeper_pool_change", "condition": {"keeper_identities_finalized": True},
        "response": "contingency", "basis": "Regenerate this strategy once keeper identities are known - "
                                                "provisional targets may include a to-be-kept player.",
    })
    return contingencies

def _ds_canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)

def _ds_build_input_fingerprint(league_id: int, year: int, scoring_bucket: str, team_count: int,
                                   slot_counts: dict, draft_type: str, draft_order: list,
                                   my_picks_all: list, keeper_count: int, keeper_reserved_slots: list,
                                   keeper_identity_state: str, draft_state_hash: str,
                                   fp_dataset_meta: dict, methodology_version: int) -> str:
    """Deterministic SHA-256 fingerprint of every input the strategy
    depends on. Regenerating with IDENTICAL inputs produces an
    IDENTICAL fingerprint (proven by test); any of these changing
    (keeper assignment, draft order/traded picks, roster settings,
    scoring, or a materially-refreshed FP dataset) changes it."""
    payload = {
        "league_id": league_id, "year": year, "scoring_bucket": scoring_bucket,
        "team_count": team_count, "slot_counts": slot_counts, "draft_type": draft_type,
        "draft_order": draft_order,
        "my_pick_overall_numbers": [p["overall_pick"] for p in my_picks_all],
        "keeper_count": keeper_count,
        "keeper_reserved_slots": [(s["overall_pick"], s["team_id"], s["player_id"]) for s in keeper_reserved_slots],
        "keeper_identity_state": keeper_identity_state,
        "draft_state_hash": draft_state_hash,
        "fp_dataset_meta": fp_dataset_meta,
        "methodology_version": methodology_version,
    }
    canonical = _ds_canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _ds_build_structural_inputs(league_id: int, year: int, scoring_bucket: str, team_count: int,
                                   slot_counts: dict, draft_type: str, draft_order: list,
                                   my_picks_all: list, keeper_count: int, keeper_reserved_slots: list,
                                   keeper_identity_state: str, fp_dataset_meta: dict,
                                   methodology_version: int) -> dict:
    """PHASE D2.1: Builds the DECOMPOSABLE structural snapshot that D3
    will later compare field-by-field to distinguish expected live
    draft-board advancement (completed-pick count, current pick, team
    on clock, available pool, drafted builds - NONE of which appear
    here) from genuine structural drift (draft order, roster settings,
    scoring, keeper finalization, or a materially-refreshed FP
    analytical input - ALL of which appear here).

    CRITICAL: keeper_reserved_slots must contain ONLY the RESERVED
    keeper slots (a small, stable subset of all picks) - never the
    full picks_raw list. An ordinary open draft selection changing
    from playerId=-1 to a real player is NOT a keeper-reserved slot
    and must never appear here, so normal draft advancement cannot
    alter this structural snapshot merely by picks occurring.

    fp_analysis_inputs deliberately includes ONLY rankings/projections/
    players (the ADP source) per D2's real _ds_build_player_universe
    usage - injuries and news are explicitly EXCLUDED per explicit
    product decision: an injury/news cache refresh should affect live
    D3 context, never invalidate the base pre-draft strategy."""
    fp_analysis_inputs = {k: v for k, v in fp_dataset_meta.items() if k != "injuries"}
    return {
        "methodology_version": methodology_version,
        "scoring_bucket": scoring_bucket,
        "team_count": team_count,
        "slot_counts": dict(sorted((slot_counts or {}).items())),
        "draft_type": draft_type,
        "draft_order": draft_order,
        "my_pick_overall_numbers": [p["overall_pick"] for p in my_picks_all],
        "keeper_count": keeper_count,
        "keeper_reserved_slots": [
            {"overall_pick": s["overall_pick"], "round": s["round"], "round_pick": s["round_pick"],
             "team_id": s["team_id"], "reserved_for_keeper": s["reserved_for_keeper"], "player_id": s["player_id"]}
            for s in keeper_reserved_slots
        ],
        "keeper_identity_state": keeper_identity_state,
        "fp_analysis_inputs": dict(sorted(fp_analysis_inputs.items())),
    }

def _ds_build_structural_fingerprint(structural_inputs: dict) -> str:
    """SHA-256 of canonical JSON over structural_inputs ONLY - uses the
    exact same canonicalization (sort_keys, default=str) as D2's own
    _ds_build_input_fingerprint/_ds_canonical_json and as
    draft_strategy_store._recompute_structural_fingerprint, so the
    server and the store module can never silently diverge on hash
    computation. Explicitly excludes D1's draft_state_hash, completed
    pick count, current pick, team on clock, drafted players, current
    availability, recent picks, and any timestamp - normal board
    advancement can never change this value (proven by regression
    tests in the D2.1 certification report)."""
    canonical = json.dumps(structural_inputs, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

_DS_METHODOLOGY_VERSION = 1

@mcp.tool()
async def prepare_draft_strategy(alias: str = None, league_id: int = None, year: int = None,
                                    horizon_open_picks: int = 8, save_strategy: bool = True) -> dict:
    """Builds a transparent, league-specific PRE-DRAFT strategy on top
    of D1's factual draft board. NOT a commissioner tool (no
    commissioner_config guard). NOT a live-pick recommender (D3/
    analyze_draft_pick handles live on-the-clock analysis). FantasyPros
    access is CACHE-ONLY - zero HTTP calls, zero quota impact.

    Args:
        alias: Registered league alias (case-insensitive).
        league_id: Registered ESPN league ID. If both alias and
                   league_id are supplied they must resolve to the
                   SAME registered entry.
        year: NFL season year. Defaults via _resolve_year.
        horizon_open_picks: Number of the user's future NON-KEEPER
                             open selections to build detailed plans
                             for. Bounded 1-12, default 8. Keeper-
                             reserved slots are never counted toward
                             this horizon.
        save_strategy: If True (default), atomically persists the
                        strategy to .draft_strategy/<league_id>_<year>.json,
                        replacing any previous strategy for the same
                        league+year. If False, returns identical
                        analysis with saved=false and writes nothing.
    """
    try:
        resolved_year = _resolve_year(year)
        horizon, horizon_err = _validate_bounded_int(horizon_open_picks, "horizon_open_picks", 1, 12, 8)
        if horizon_err:
            return {"error": "invalid_horizon_open_picks", "message": horizon_err}

        try:
            registry = league_registry.load_registry()
        except league_registry.RegistryError as e:
            return {"error": "registry_error", "message": str(e)}

        if alias is not None and league_id is not None:
            try:
                alias_norm, alias_entry = league_registry.resolve_alias(registry, alias)
            except league_registry.RegistryError as e:
                return {"error": "alias_not_found", "message": str(e)}
            try:
                id_alias_norm, _ = league_registry.resolve_league_id(registry, league_id)
            except league_registry.RegistryError as e:
                return {"error": "league_not_registered", "message": str(e)}
            if alias_norm != id_alias_norm:
                return {"error": "conflicting_parameters",
                        "message": f"alias '{alias}' resolves to '{alias_norm}' but league_id "
                                    f"{league_id} resolves to '{id_alias_norm}' - these must match."}
            resolved_alias, entry = alias_norm, alias_entry
        elif alias is not None:
            try:
                resolved_alias, entry = league_registry.resolve_alias(registry, alias)
            except league_registry.RegistryError as e:
                return {"error": "alias_not_found", "message": str(e)}
        elif league_id is not None:
            try:
                resolved_alias, entry = league_registry.resolve_league_id(registry, league_id)
            except league_registry.RegistryError as e:
                return {"error": "league_not_registered", "message": str(e)}
        else:
            resolved_alias, entry = league_registry.get_default_league(registry)

        resolved_league_id = entry["league_id"]

        try:
            raw = _fetch_raw_draft_state(resolved_league_id, resolved_year)
        except Exception as e:
            return _error_response("fetching raw ESPN draft state", e)

        league = build_commissioner_snapshot(raw, resolved_league_id, resolved_year)
        authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")
        my_team = resolve_my_team_from_payload(raw, authenticated_swid)
        my_team_id = my_team.get("team_id")

        draft_detail = raw.get("draftDetail", {})
        picks_raw = draft_detail.get("picks", [])
        if not picks_raw:
            return {"status": "error", "error": "draft_data_unavailable",
                    "message": "ESPN returned no draft skeleton for this league/year."}

        draft_status = _dp_derive_draft_status(draft_detail)
        if draft_status == "complete":
            return {"status": "error", "error": "draft_already_complete",
                    "message": "This league's draft is already complete - no new pre-draft strategy will be created."}

        raw_teams = raw.get("teams", [])
        raw_settings = raw.get("settings", {})
        draft_settings = raw_settings.get("draftSettings", {})
        pos_lookup = _dp_build_position_lookup(raw_teams, resolved_year)

        picks_sorted = sorted(picks_raw, key=lambda p: (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9))
        unresolved = [p for p in picks_sorted if p.get("playerId", -1) in (None, -1)]

        draft_order = _dp_build_draft_order(picks_raw)
        _my_pick_ctx = _dp_build_my_pick_context(picks_raw, my_team_id, pos_lookup, unresolved)
        my_picks_all = _my_pick_ctx["my_picks_all"]
        if my_team_id is None or not my_picks_all:
            return {"status": "error", "error": "my_team_unresolved",
                    "message": "Could not resolve the authenticated user's team/pick schedule for this league."}

        open_picks = [p for p in my_picks_all if not p["reserved_for_keeper"]]
        keeper_reserved_slots = [p for p in my_picks_all if p["reserved_for_keeper"]]

        # --- Keeper state (league-wide, not just my picks) ---
        all_reserved = [_dp_normalize_pick(p, pos_lookup) for p in picks_raw if p.get("reservedForKeeper")]
        assigned_keepers = [p for p in all_reserved if p["slot_status"] == "keeper_assigned"]
        unassigned_keepers = [p for p in all_reserved if p["slot_status"] == "reserved_keeper_unassigned"]
        keeper_count_cfg = getattr(league.settings, "keeper_count", 0) or 0
        if not all_reserved:
            keeper_identity_state = "not_applicable"
        elif assigned_keepers and not unassigned_keepers:
            keeper_identity_state = "known"
        elif assigned_keepers and unassigned_keepers:
            keeper_identity_state = "partial"
        else:
            keeper_identity_state = "unknown_pre_deadline"
        keeper_uncertainty = keeper_identity_state in ("unknown_pre_deadline", "partial")

        # Names already assigned as keepers OR already drafted - excluded
        # from every strategic target (name-based, ESPN identity via
        # pos_lookup built from the same combined-view response as D1).
        unavailable_norm_names = set()
        for p in picks_raw:
            pid = p.get("playerId")
            if pid not in (None, -1):
                nm = (pos_lookup.get(pid) or {}).get("name")
                if nm:
                    unavailable_norm_names.add(fp_client.normalize_player_name(nm))

        scoring_rules = getattr(league.settings, "scoring_format", []) or []
        scoring_bucket = _detect_league_scoring_bucket(scoring_rules)
        slot_counts = getattr(league.settings, "position_slot_counts", {}) or {}
        team_count = getattr(league.settings, "team_count", None)

        cache_warnings = []
        universes = {}
        fp_dataset_meta = {}
        for pos in _DS_CORE_POSITIONS:
            universe, warns = _ds_build_player_universe(pos, scoring_bucket)
            universes[pos] = universe
            cache_warnings.extend(warns)
            rc = fp_client.get_rankings_cache(pos, scoring_bucket)
            pc = fp_client.get_projections_cache(pos, scoring_bucket, week=0)
            fp_dataset_meta[f"rankings_{pos}"] = (rc or {}).get("fetched_at")
            fp_dataset_meta[f"projections_{pos}"] = (pc or {}).get("fetched_at")
        players_cache_meta = fp_client.get_players_cache()
        fp_dataset_meta["players"] = (players_cache_meta or {}).get("fetched_at")
        injuries_cache_meta = fp_client.get_injuries_cache()
        fp_dataset_meta["injuries"] = (injuries_cache_meta or {}).get("fetched_at")

        allocation = _ds_starter_flex_allocation(slot_counts, universes)
        replacement_by_position = _ds_apply_replacement_and_vor(universes, allocation["replacement_index"])
        tier_board = _ds_build_tier_board(universes)
        position_guidance = [_ds_position_guidance(pos, universes[pos], replacement_by_position[pos], allocation["dedicated_demand"])
                                for pos in _DS_CORE_POSITIONS]
        pick_plan = _ds_build_pick_plan(open_picks, universes, tier_board, replacement_by_position,
                                          unavailable_norm_names, horizon)
        contingencies = _ds_build_contingencies(tier_board, replacement_by_position)

        data_freshness = fp_client.get_cache_freshness_report(list(_DS_CORE_POSITIONS), scoring_bucket)
        readiness = {
            "rankings": "fresh" if not any("rankings" in w for w in cache_warnings) and not any(
                k.startswith("rankings_") and v.get("is_stale") for k, v in data_freshness.items()) else
                        ("missing" if any("rankings" in w for w in cache_warnings) else "stale"),
            "adp": "fresh" if fp_client.get_players_cache() is not None else "missing",
            "projections": "fresh" if not any("projections" in w for w in cache_warnings) and not any(
                k.startswith("projections_") and v.get("is_stale") for k, v in data_freshness.items()) else
                        ("missing" if any("projections" in w for w in cache_warnings) else "stale"),
            "injuries": ("stale" if (data_freshness.get("injuries") or {}).get("is_stale") else
                        ("missing" if fp_client.get_injuries_cache() is None else "fresh")),
            "news": "fresh" if fp_client.get_news_cache() is not None else "missing",
            "keeper_identities": "pending" if keeper_uncertainty else ("not_applicable" if keeper_identity_state == "not_applicable" else "known"),
        }
        critical_missing = any(readiness[k] == "missing" for k in ("rankings", "projections"))
        if keeper_uncertainty:
            strategy_status = "provisional"
        elif critical_missing:
            strategy_status = "partial"
        else:
            strategy_status = "current"
        strategy_generation = "late" if draft_status == "in_progress" else "pre_draft"

        draft_state_hash = _dp_state_hash(resolved_league_id, resolved_year, picks_raw)
        input_fingerprint = _ds_build_input_fingerprint(
            resolved_league_id, resolved_year, scoring_bucket, team_count, slot_counts,
            draft_settings.get("type"), draft_order, my_picks_all, keeper_count_cfg,
            keeper_reserved_slots, keeper_identity_state, draft_state_hash, fp_dataset_meta,
            _DS_METHODOLOGY_VERSION,
        )
        strategy_id = hashlib.sha256((input_fingerprint + _ds_canonical_json(pick_plan)).encode("utf-8")).hexdigest()[:20]
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # --- PHASE D2.1: decomposable structural snapshot for future D3
        # compatibility checks (see _ds_build_structural_inputs docstring
        # for the exact board-advancement-vs-structural-drift boundary). ---
        structural_inputs = _ds_build_structural_inputs(
            resolved_league_id, resolved_year, scoring_bucket, team_count, slot_counts,
            draft_settings.get("type"), draft_order, my_picks_all, keeper_count_cfg,
            keeper_reserved_slots, keeper_identity_state, fp_dataset_meta, _DS_METHODOLOGY_VERSION,
        )
        structural_fingerprint = _ds_build_structural_fingerprint(structural_inputs)

        keeper_context = {
            "configured_count": keeper_count_cfg, "identity_state": keeper_identity_state,
            "reserved_slots_league_wide": len(all_reserved), "assigned_league_wide": len(assigned_keepers),
            "unassigned_league_wide": len(unassigned_keepers),
            "note": ("Keeper identities are not yet finalized - every player target below is "
                      "provisional_until_keepers_finalized." if keeper_uncertainty else
                      "No unresolved keeper uncertainty affects this strategy."),
        }

        for plan in pick_plan:
            for bucket in ("targets", "wait_candidates", "reach_candidates"):
                for t in plan[bucket]:
                    t["provisional_until_keepers_finalized"] = keeper_uncertainty

        warnings_out = list(cache_warnings)
        if (data_freshness.get("injuries") or {}).get("is_stale"):
            warnings_out.append("injuries cache is stale - injury_status fields may be out of date.")
        if strategy_generation == "late":
            warnings_out.append("Draft is already in_progress - this strategy reflects CURRENT remaining state, "
                                  "not an original pre-draft plan.")

        strategy_doc = {
            "schema_version": draft_strategy_store.STRATEGY_SCHEMA_VERSION,
            "league_id": resolved_league_id, "year": resolved_year,
            "strategy_id": strategy_id, "input_fingerprint": input_fingerprint,
            "created_at_utc": created_at, "strategy_status": strategy_status,
            "strategy_generation": strategy_generation,
            "structural_inputs": structural_inputs, "structural_fingerprint": structural_fingerprint,
            "methodology": {
                "vor_definition": "player_projected_points - replacement_projection, where replacement_projection "
                                    "is the projection of the player ranked immediately after this league's dedicated "
                                    "+ FLEX-filled starter demand at that position.",
                "flex_allocation_method": "Dedicated slots filled first by projection per position; remaining FLEX-"
                                            "eligible players (across all configured flex slot types, via ESPN's own "
                                            "slot eligibility) pooled and filled by highest projection - never a fixed "
                                            "per-position flex split.",
                "native_tier_source": "FantasyPros consensus rankings native 'tier' field - never derived/overwritten. "
                                        "Players lacking a tier are reported as tier=unknown.",
                "reach_value_bands": {"strong_value": "adp_delta <= -8", "value": "-8 < adp_delta <= -3",
                                        "fair_range": "-3 < adp_delta < 3", "small_reach": "3 <= adp_delta < 8",
                                        "significant_reach": "adp_delta >= 8",
                                        "adp_delta_definition": "ADP - current_pick"},
                "strategy_horizon_open_picks": horizon, "keeper_uncertainty_state": keeper_identity_state,
                "methodology_version": _DS_METHODOLOGY_VERSION,
            },
            "roster_construction": {"starter_requirements": allocation["dedicated_demand"],
                                      "flex_requirements": allocation["flex_slot_detail"],
                                      "bench_capacity": slot_counts.get("BE"),
                                      "k_dst_required": {"K": slot_counts.get("K"), "DST": slot_counts.get("D/ST")}},
            "replacement_levels": replacement_by_position,
            "position_profiles": [{
                "position": pos, "replacement_rank": replacement_by_position[pos]["replacement_rank"],
                "replacement_projection": replacement_by_position[pos]["replacement_projection"],
                "tiers": tier_board[pos]["tiers"],
                "available_top_tier_players": tier_board[pos]["tiers"][0]["players_available"] if tier_board[pos]["tiers"] else 0,
            } for pos in _DS_CORE_POSITIONS],
            "tier_board": tier_board,
            "position_guidance": position_guidance,
            "pick_plan": pick_plan,
            "contingencies": contingencies,
            "keeper_context": keeper_context,
            "k_dst_note": {"required_by_roster": {"K": slot_counts.get("K"), "DST": slot_counts.get("D/ST")},
                            "analysis_enrichment": "espn_only", "model_priority": "not_ranked"},
        }

        saved = False
        save_error = None
        if save_strategy:
            try:
                draft_strategy_store.save_strategy(resolved_league_id, resolved_year, strategy_doc)
                saved = True
            except draft_strategy_store.DraftStrategyStoreError as e:
                save_error = {"code": e.code, "message": e.message}

        return {
            "status": "ok",
            "league": {"league_id": resolved_league_id, "league_name": getattr(league.settings, "name", None),
                        "year": resolved_year, "alias": resolved_alias},
            "draft_context": {"draft_status": draft_status, "draft_type": draft_settings.get("type"),
                                "my_team": my_team, "keeper_state": keeper_identity_state,
                                "open_pick_schedule": open_picks, "keeper_reserved_picks": keeper_reserved_slots},
            "readiness": readiness,
            "strategy": strategy_doc,
            "persistence": {"saved": saved, "input_fingerprint": input_fingerprint,
                              "save_error": save_error},
            "warnings": warnings_out,
            "data_limitations": [
                "Player-universe exclusion for drafted/keeper-assigned players uses ESPN-identity name matching "
                "(same approach as this codebase's existing FantasyPros matching) - not a guaranteed-unique ID join.",
                "Pre-draft VOR/replacement is computed over the full FantasyPros-ranked player pool, not filtered by "
                "current ESPN roster status (which does not yet reflect the upcoming draft).",
            ] + (["Keeper identities are not yet finalized - see keeper_context and provisional_until_keepers_finalized flags."]
                  if keeper_uncertainty else []),
        }
    except Exception as e:
        return _error_response("preparing draft strategy", e)



# ============================================================
# PHASE D3: LIVE PICK ANALYSIS ENGINE (analyze_draft_pick)
# Reuses D1 raw-draft-state machinery and D2/D2.1 analytical
# primitives AS-IS (never modified). Read-only, cache-only FP,
# zero draft actions, zero strategy writes.
# ============================================================
_ADP_SURVIVAL_BANDS = ("very_unlikely", "at_risk", "coin_flip", "likely", "very_likely")
_ADP_RUN_ACTIVE_MIN_LAST5 = 3
_ADP_RUN_ACTIVE_MIN_LAST10 = 5
_ADP_RUN_DEVELOPING_MIN_LAST5 = 2
_ADP_RUN_DEVELOPING_MIN_LAST10 = 4
_ADP_VALUE_OVERRIDE_VOR_GAIN = 15.0
_ADP_TIER_CLIFF_DROP_THRESHOLD = 15.0
_ADP_STRONG_DEMAND_RATIO = 0.5
_ADP_MINIMAL_DEMAND_RATIO = 0.25
_ADP_DEEP_TIER_MIN_REMAINING = 4
_ADP_MAX_AUTO_POOL = 20
_ADP_MAX_EXPLICIT_CANDIDATES = 12
_ADP_MAX_PATH_CANDIDATES = 3
_ADP_MAX_PATH_OPTIONS = 5

_ADP_STRUCTURAL_DIFF_REASONS = {
    "scoring_bucket": "scoring_changed", "team_count": "team_count_changed",
    "slot_counts": "slot_counts_changed", "draft_type": "draft_type_changed",
    "draft_order": "draft_order_changed", "my_pick_overall_numbers": "my_pick_schedule_changed",
    "keeper_count": "keeper_count_changed", "keeper_reserved_slots": "keeper_state_changed",
    "keeper_identity_state": "keeper_state_changed", "methodology_version": "methodology_changed",
}

def _adp_decision_pick_context(my_picks_all: list, unresolved: list) -> dict:
    """User's NEXT OPEN (non-keeper, unresolved) selection - explicitly
    skips keeper-reserved slots (assigned or not). next_user_open_pick
    is the SECOND open slot after decision_pick (for turn_span)."""
    open_unresolved = sorted(
        [p for p in my_picks_all if not p["reserved_for_keeper"] and p["player_id"] is None],
        key=lambda p: p["overall_pick"])
    if not open_unresolved:
        return {"decision_pick": None, "user_on_clock": False, "picks_until_decision": None,
                "next_user_open_pick": None, "turn_span": None, "status": "no_remaining_open_draft_pick"}
    decision_pick = open_unresolved[0]["overall_pick"]
    next_open = open_unresolved[1] if len(open_unresolved) > 1 else None
    next_user_open_pick = next_open["overall_pick"] if next_open else None
    turn_span = (next_user_open_pick - decision_pick) if next_user_open_pick else None
    picks_until_decision = sum(1 for p in unresolved if p.get("overallPickNumber") is not None
                                 and p.get("overallPickNumber") < decision_pick)
    return {"decision_pick": decision_pick, "user_on_clock": picks_until_decision == 0,
            "picks_until_decision": picks_until_decision, "next_user_open_pick": next_user_open_pick,
            "turn_span": turn_span, "status": "ok"}

def _adp_recent_runs(completed_sorted: list, pos_lookup: dict) -> dict:
    """Deterministic run detection, COMPLETED picks only, most-recent-
    last. ACTIVE: pos>=3/last5 OR >=5/last10. DEVELOPING: >=2/last5 OR
    >=4/last10. NONE otherwise. Never manufactures a full sample early."""
    last5 = completed_sorted[-5:]
    last10 = completed_sorted[-10:]
    def pos_of(p):
        return (pos_lookup.get(p.get("playerId"), {}) or {}).get("position")
    counts5, counts10 = {}, {}
    for p in last5:
        pos = pos_of(p)
        if pos:
            counts5[pos] = counts5.get(pos, 0) + 1
    for p in last10:
        pos = pos_of(p)
        if pos:
            counts10[pos] = counts10.get(pos, 0) + 1
    labels = {}
    for pos in set(counts5) | set(counts10):
        c5, c10 = counts5.get(pos, 0), counts10.get(pos, 0)
        if c5 >= _ADP_RUN_ACTIVE_MIN_LAST5 or c10 >= _ADP_RUN_ACTIVE_MIN_LAST10:
            labels[pos] = "active"
        elif c5 >= _ADP_RUN_DEVELOPING_MIN_LAST5 or c10 >= _ADP_RUN_DEVELOPING_MIN_LAST10:
            labels[pos] = "developing"
        else:
            labels[pos] = "none"
    return {"labels": labels, "counts_last5": counts5, "counts_last10": counts10,
            "coverage": {"last5_examined": len(last5), "last10_examined": len(last10)}}

def _adp_opponent_demand(picks_raw: list, decision_pick: int, next_user_open_pick, pos_lookup: dict,
                            slot_counts: dict) -> dict:
    """Factual intervening-pick window using the raw ESPN schedule (no
    snake math). Dedicated-starter-opening counts per position only -
    never a prediction of what a team WILL draft. FLEX kept separate."""
    if next_user_open_pick is None:
        return None
    intervening = [p for p in picks_raw
                    if decision_pick < (p.get("overallPickNumber") or -1) < next_user_open_pick]
    unique_teams = sorted(set(p.get("teamId") for p in intervening))
    dedicated_positions = [pos for pos in _DS_CORE_POSITIONS if (slot_counts.get(pos) or 0) > 0]
    demand_by_position = {}
    for pos in dedicated_positions:
        required = slot_counts.get(pos) or 0
        teams_with_opening = 0
        for tid in unique_teams:
            drafted_at_pos = sum(1 for p in picks_raw
                                   if p.get("teamId") == tid and p.get("playerId", -1) not in (None, -1)
                                   and (pos_lookup.get(p.get("playerId"), {}) or {}).get("position") == pos)
            if drafted_at_pos < required:
                teams_with_opening += 1
        demand_by_position[pos] = {"teams_with_dedicated_opening": teams_with_opening,
                                     "unique_intervening_teams": len(unique_teams),
                                     "dedicated_requirement": required}
    flex_capacity = sum(v for k, v in (slot_counts or {}).items() if _parse_flex_eligibility(k))
    return {"intervening_pick_count": len(intervening), "unique_intervening_teams": unique_teams,
            "demand_by_position": demand_by_position, "flex_eligible_capacity_leaguewide": flex_capacity}

def _adp_diff_structural_inputs(saved: dict, current: dict) -> list:
    reasons = []
    for field, reason in _ADP_STRUCTURAL_DIFF_REASONS.items():
        if saved.get(field) != current.get(field) and reason not in reasons:
            reasons.append(reason)
    saved_fp = saved.get("fp_analysis_inputs", {}) or {}
    current_fp = current.get("fp_analysis_inputs", {}) or {}
    for k in set(saved_fp) | set(current_fp):
        if saved_fp.get(k) != current_fp.get(k):
            if k.startswith("rankings_") and "fp_rankings_refreshed" not in reasons:
                reasons.append("fp_rankings_refreshed")
            elif k.startswith("projections_") and "fp_projections_refreshed" not in reasons:
                reasons.append("fp_projections_refreshed")
            elif k == "players" and "fp_adp_source_refreshed" not in reasons:
                reasons.append("fp_adp_source_refreshed")
    return reasons

def _adp_load_strategy_compatibility(resolved_league_id, resolved_year, current_structural_inputs,
                                        completed_picks_count):
    """Loads saved strategy and compares CURRENT structural inputs
    field-by-field against the saved snapshot. Board advancement
    (completed_picks_count) is context ONLY - never compared against
    structural_inputs, so it can never by itself yield structurally_stale."""
    try:
        doc = draft_strategy_store.load_strategy(resolved_league_id, resolved_year)
    except draft_strategy_store.DraftStrategyStoreError as e:
        return {"status": "invalid", "strategy": None, "compatibility": None, "drift_reasons": [],
                 "strategy_alignment_available": False, "error": {"code": e.code, "message": e.message}}
    if doc is None:
        return {"status": "missing", "strategy": None, "compatibility": None, "drift_reasons": [],
                 "strategy_alignment_available": False, "error": None}
    if doc.get("schema_version", 1) < 2 or "structural_inputs" not in doc:
        return {"status": "compatibility_insufficient_structural_data", "strategy": doc, "compatibility": None,
                 "drift_reasons": [], "strategy_alignment_available": False, "error": None}
    drift_reasons = _adp_diff_structural_inputs(doc["structural_inputs"], current_structural_inputs)
    if drift_reasons:
        compatibility = "structurally_stale"
    elif completed_picks_count > 0:
        compatibility = "compatible_board_advanced"
    else:
        compatibility = "compatible"
    return {"status": "loaded", "strategy": doc, "compatibility": compatibility, "drift_reasons": drift_reasons,
             "strategy_alignment_available": (compatibility != "structurally_stale"), "error": None}

def _adp_build_fp_lookup(universes: dict) -> dict:
    """name -> FP facts from D2's exact universes (VOR/tier already
    applied via _ds_apply_replacement_and_vor) - keyed by _norm_name."""
    lookup = {}
    for pos, universe in universes.items():
        for p in universe:
            if p.get("_norm_name"):
                lookup[p["_norm_name"]] = p
    return lookup

def _adp_tier_scarcity(tier_board: dict, position: str, tier, available_pool_at_pos: list, candidate_name: str = None) -> dict:
    if tier is None:
        return {"tier": None, "available_in_tier": None, "candidate_rank_within_tier": None,
                 "next_tier": None, "projection_drop_to_next_tier": None, "ecr_gap_to_next_tier": None}
    tiers = (tier_board.get(position, {}) or {}).get("tiers", [])
    match = next((t for t in tiers if t["tier"] == tier), None)
    # available_in_tier/candidate_rank use the CURRENT available pool
    # (not the full FP pool tier_board counts) so scarcity reflects
    # what is actually still draftable right now.
    same_tier_available = sorted(
        [p for p in available_pool_at_pos if p.get("tier") == tier],
        key=lambda p: (p.get("ecr") is None, p.get("ecr") if p.get("ecr") is not None else 9999))
    candidate_rank = None
    if candidate_name is not None:
        names_in_order = [p.get("name") for p in same_tier_available]
        if candidate_name in names_in_order:
            candidate_rank = names_in_order.index(candidate_name) + 1
    return {"tier": tier, "available_in_tier": len(same_tier_available),
             "candidate_rank_within_tier": candidate_rank,
             "_same_tier_available_names": [p.get("name") for p in same_tier_available],
             "next_tier": match.get("next_tier") if match else None,
             "projection_drop_to_next_tier": match.get("projection_drop_to_next_tier") if match else None,
             "ecr_gap_to_next_tier": match.get("ecr_gap_to_next_tier") if match else None}

def _adp_tier_cliff_urgency(scarcity: dict) -> str:
    """Deterministic, based only on visible available_in_tier and
    projection_drop_to_next_tier (reuses D2.1/D2's 15pt contingency
    threshold - no new conflicting methodology)."""
    if scarcity.get("tier") is None:
        return "unknown"
    remaining = scarcity.get("available_in_tier") or 0
    drop = scarcity.get("projection_drop_to_next_tier")
    if remaining <= 1 and drop is not None and drop >= _ADP_TIER_CLIFF_DROP_THRESHOLD:
        return "high"
    if remaining <= 2 or (drop is not None and drop >= _ADP_TIER_CLIFF_DROP_THRESHOLD):
        return "moderate"
    return "none"

def _adp_survival_band(adp, decision_pick: int, next_user_open_pick, demand_signal, run_label,
                          tier_cliff_urgency, tier_remaining) -> dict:
    """Categorical, conservative, MAX ONE net band adjustment. No fake
    probabilities. Baseline = ADP position normalized within the user's
    [decision_pick, next_user_open_pick] turn window."""
    if next_user_open_pick is None:
        return {"baseline_band": "not_applicable", "adjustments": [], "final_band": "not_applicable"}
    if adp is None:
        return {"baseline_band": "unknown", "adjustments": [], "final_band": "unknown"}
    turn_span = max(1, next_user_open_pick - decision_pick)
    relative = (adp - decision_pick) / turn_span
    if relative < 0.0:
        baseline = "very_unlikely"
    elif relative < 0.5:
        baseline = "at_risk"
    elif relative < 1.0:
        baseline = "coin_flip"
    elif relative < 1.5:
        baseline = "likely"
    else:
        baseline = "very_likely"
    risk_factors, positive_factors = [], []
    if demand_signal is not None:
        ratio = (demand_signal.get("teams_with_dedicated_opening", 0)
                  / max(1, demand_signal.get("unique_intervening_teams", 1)))
        if ratio >= _ADP_STRONG_DEMAND_RATIO:
            risk_factors.append("strong_intervening_demand")
        elif ratio <= _ADP_MINIMAL_DEMAND_RATIO:
            positive_factors.append("minimal_intervening_demand")
    if run_label == "active":
        risk_factors.append("active_positional_run")
    if tier_cliff_urgency == "high":
        risk_factors.append("last_or_near_last_in_meaningful_tier")
    if relative >= 1.5 and (tier_remaining or 0) >= _ADP_DEEP_TIER_MIN_REMAINING:
        positive_factors.append("adp_beyond_next_pick_with_deep_tier")
    net = (1 if positive_factors else 0) - (1 if risk_factors else 0)
    net = max(-1, min(1, net))
    idx = _ADP_SURVIVAL_BANDS.index(baseline)
    final_idx = max(0, min(len(_ADP_SURVIVAL_BANDS) - 1, idx + net))
    return {"baseline_band": baseline, "adjustments": {"risk_factors": risk_factors,
             "positive_factors": positive_factors, "net_band_shift": net},
             "final_band": _ADP_SURVIVAL_BANDS[final_idx]}

def _adp_strategy_alignment(candidate_pos, candidate_name, candidate_vor, saved_strategy,
                               strategy_alignment_available: bool, decision_pick: int) -> str:
    """Deterministic label vs saved D2 pick_plan/position_guidance.
    Reuses D2's own 15pt VOR-gain / tier-drop thresholds - no new
    conflicting methodology invented."""
    if not strategy_alignment_available or saved_strategy is None:
        return "unavailable"
    pick_plan = saved_strategy.get("pick_plan", [])
    plan_for_pick = min(pick_plan, key=lambda pl: abs(pl.get("overall_pick", 0) - decision_pick)) \
        if pick_plan else None
    if plan_for_pick is None:
        return "off_plan"
    priority_positions = (plan_for_pick.get("priority", {}) or {}).get("positions", [])
    target_names = {t.get("name") for t in (plan_for_pick.get("targets") or [])}
    if candidate_name in target_names:
        return "aligned"
    contingencies = saved_strategy.get("contingencies", [])
    for c in contingencies:
        if c.get("trigger") == "elite_value_fall" and candidate_vor is not None and candidate_vor >= _ADP_VALUE_OVERRIDE_VOR_GAIN:
            return "value_override_candidate"
        if c.get("trigger") == "tier_cliff_defense" and c.get("condition", {}).get("position") == candidate_pos:
            return "contingency_triggered"
    if priority_positions and candidate_pos == priority_positions[0]:
        return "aligned"
    if priority_positions and candidate_pos != priority_positions[0]:
        return "scarcity_override_candidate" if candidate_vor is not None and candidate_vor > 0 else "off_plan"
    return "off_plan"

_ADP_ALIGNMENT_RANK = {"aligned": 0, "roster_feasibility_override": 0, "value_override_candidate": 1,
                          "scarcity_override_candidate": 1, "contingency_triggered": 1, "off_plan": 2, "unavailable": 3}
_ADP_FIT_RANK = {"fills_open_dedicated_starter": 0, "mandatory_required_slot": 0, "flex_eligible": 1,
                    "bench_depth": 2, "position_already_filled": 3}
_ADP_SURVIVAL_RISK_RANK = {"very_unlikely": 0, "at_risk": 0, "coin_flip": 1, "likely": 2,
                              "very_likely": 2, "unknown": 1, "not_applicable": 1}

def _adp_wait_risk_rank(tier_cliff_urgency: str, survival_final_band: str) -> int:
    """Combines tier-cliff urgency and survival risk into ONE
    'major tier cliff / wait risk' decision-order dimension (item 2 of
    the documented 7-item precedence). Urgent=0 (most wait-risk,
    ranks first), then moderate=1, then none/unknown=2."""
    cliff_rank = {"high": 0, "moderate": 1, "none": 2, "unknown": 2}.get(tier_cliff_urgency, 2)
    surv_rank = _ADP_SURVIVAL_RISK_RANK.get(survival_final_band, 1)
    # urgent if EITHER signal is urgent (0); this also makes the
    # dominance rule hold: no-worse-survival + no-worse-tier => no
    # worse combined wait-risk rank.
    return min(cliff_rank, surv_rank if survival_final_band in ("very_unlikely", "at_risk") else 2 if cliff_rank == 2 else cliff_rank)

def _adp_decision_sort_key(c: dict) -> tuple:
    """Documented deterministic lexicographic order (methodology.decision_order):
    1. wait_risk (tier cliff / survival risk, urgent first)
    2. -VOR (higher VOR first)
    3. strategy alignment rank (aligned first)
    4. structural roster fit rank (fills dedicated starter first)
    5. ECR (lower/better first)
    6. ADP (market/tie context ONLY - never player-quality)
    ADP is deliberately LAST so it can never outrank a dominating
    candidate on tier/VOR/ECR/fit/survival (dominance rule)."""
    wait_risk = _adp_wait_risk_rank(c["tier_cliff_urgency"], c["survival"]["final_band"])
    neg_vor = -(c["vor"]) if c["vor"] is not None else 0.0
    align_rank = _ADP_ALIGNMENT_RANK.get(c["strategy_alignment"], 3)
    fit_rank = _ADP_FIT_RANK.get(c["roster_fit"]["label"], 3)
    ecr = c["ecr"] if c["ecr"] is not None else 9999
    adp = c["adp"] if c["adp"] is not None else 9999
    return (wait_risk, neg_vor, align_rank, fit_rank, ecr, adp)

def _adp_roster_fit(position: str, my_drafted_counts: dict, slot_counts: dict) -> dict:
    required = slot_counts.get(position) or 0
    drafted = my_drafted_counts.get(position, 0)
    structural_opening = drafted < required
    has_flex = _position_has_flex_exposure(position, slot_counts)
    if structural_opening:
        label = "fills_open_dedicated_starter"
    elif has_flex:
        label = "flex_eligible"
    elif drafted >= required:
        label = "position_already_filled"
    else:
        label = "bench_depth"
    return {"label": label, "structural_opening": structural_opening, "flex_eligible": has_flex,
             "dedicated_drafted": drafted, "dedicated_required": required}

def _adp_build_recommendation(ranked_candidates: list) -> dict:
    if not ranked_candidates:
        return None
    top = ranked_candidates[0]
    others = ranked_candidates[1:]
    reasons = []
    if top["tier_cliff_urgency"] in ("high", "moderate"):
        reasons.append(f"Tier cliff urgency is {top['tier_cliff_urgency']} at {top['position']} "
                         f"({top['tier_scarcity'].get('available_in_tier')} left in tier {top['tier_scarcity'].get('tier')}).")
    if top["vor"] is not None:
        reasons.append(f"League-specific VOR of {top['vor']} is the strongest among viable candidates.")
    if top["strategy_alignment"] == "aligned":
        reasons.append("Matches the saved pre-draft strategy's target/priority for this position.")
    elif top["strategy_alignment"] in ("value_override_candidate", "scarcity_override_candidate"):
        reasons.append(f"Strategy override justified: {top['strategy_alignment']}.")
    if not reasons:
        reasons.append("Best available candidate under the documented decision order.")
    main_tradeoff = None
    if others:
        alt = others[0]
        main_tradeoff = f"Passing on {alt['name']} ({alt['position']}), who ranks close on VOR/ECR."
    why_not_wait = None
    band = top["survival"]["final_band"]
    if band in ("at_risk", "very_unlikely"):
        why_not_wait = f"Survival to your next open pick is rated {band} - waiting risks losing this player."
    elif band in ("likely", "very_likely"):
        why_not_wait = f"Survival is rated {band}, but this candidate still leads on tier/VOR/ECR right now."
    else:
        why_not_wait = "Survival context is limited; recommendation rests on current tier/VOR/ECR evidence."
    num_independent_favor = sum([
        top["tier_cliff_urgency"] in ("high", "moderate"),
        top["strategy_alignment"] in ("aligned", "value_override_candidate", "scarcity_override_candidate"),
        band in ("at_risk", "very_unlikely"),
    ])
    if num_independent_favor >= 2:
        confidence = "high"
    elif num_independent_favor == 1 or (others and abs((top["vor"] or 0) - (others[0]["vor"] or 0)) > 5):
        confidence = "moderate"
    else:
        confidence = "low"
    return {"player_id": top["player_id"], "name": top["name"], "position": top["position"],
             "recommendation_confidence": confidence, "primary_reason": reasons[0],
             "supporting_reasons": reasons[1:], "main_tradeoff": main_tradeoff, "why_not_wait": why_not_wait}

def _adp_path_analysis(ranked_candidates: list, available_pool_by_norm_name: dict, next_user_open_pick,
                          decision_pick) -> list:
    """Bounded one-step path: candidate now -> remove from pool ->
    classify remaining top candidates by survival band. No exact future
    picks invented; max 3 candidate paths, max 5 options per bucket."""
    if next_user_open_pick is None:
        return []
    paths = []
    for cand in ranked_candidates[:_ADP_MAX_PATH_CANDIDATES]:
        remaining = [c for c in ranked_candidates if c["player_id"] != cand["player_id"]]
        likely = [c for c in remaining if c["survival"]["final_band"] in ("likely", "very_likely")][:_ADP_MAX_PATH_OPTIONS]
        at_risk = [c for c in remaining if c["survival"]["final_band"] in ("at_risk", "very_unlikely")][:_ADP_MAX_PATH_OPTIONS]
        paths.append({
            "candidate_now": {"player_id": cand["player_id"], "name": cand["name"], "position": cand["position"]},
            "next_pick": next_user_open_pick,
            "likely_next_options": [{"name": c["name"], "position": c["position"], "band": c["survival"]["final_band"]}
                                      for c in likely],
            "at_risk_options": [{"name": c["name"], "position": c["position"], "band": c["survival"]["final_band"]}
                                  for c in at_risk],
            "projected_position_shape": {
                "positions_likely_available": sorted(set(c["position"] for c in likely)),
                "positions_at_risk": sorted(set(c["position"] for c in at_risk)),
            },
        })
    return paths

def _adp_dominance_notable_omission(requested_candidates: list, all_ranked: list) -> dict:
    """Max 1 notable_omission: an available-but-unrequested player that
    dominates every requested candidate (same/better tier, higher VOR,
    better ECR, no worse fit/survival)."""
    requested_ids = {c["player_id"] for c in requested_candidates}
    for cand in all_ranked:
        if cand["player_id"] in requested_ids:
            continue
        dominates_all = True
        for req in requested_candidates:
            better_vor = (cand["vor"] or -9999) > (req["vor"] or -9999)
            no_worse_ecr = (cand["ecr"] if cand["ecr"] is not None else 9999) <= (req["ecr"] if req["ecr"] is not None else 9999)
            no_worse_fit = _ADP_FIT_RANK.get(cand["roster_fit"]["label"], 3) <= _ADP_FIT_RANK.get(req["roster_fit"]["label"], 3)
            no_worse_surv = _ADP_SURVIVAL_RISK_RANK.get(cand["survival"]["final_band"], 1) <= _ADP_SURVIVAL_RISK_RANK.get(req["survival"]["final_band"], 1)
            if not (better_vor and no_worse_ecr and no_worse_fit and no_worse_surv):
                dominates_all = False
                break
        if dominates_all and requested_candidates:
            return {"player_id": cand["player_id"], "name": cand["name"], "position": cand["position"],
                     "basis": "Not requested, but dominates all requested candidates on VOR, ECR, roster fit, and survival."}
    return None

def _adp_resolve_league_and_state(alias, league_id, year):
    """Shared registry/league/raw-draft-state resolution, mirroring
    get_draft_board/prepare_draft_strategy exactly (never duplicating
    divergent logic)."""
    resolved_year = _resolve_year(year)
    try:
        registry = league_registry.load_registry()
    except Exception as e:
        return None, {"error": "registry_error", "message": str(e)}
    if alias and league_id:
        try:
            alias_norm, alias_entry = league_registry.resolve_alias(registry, alias)
        except Exception as e:
            return None, {"error": "alias_not_found", "message": str(e)}
        try:
            id_alias_norm, _ = league_registry.resolve_league_id(registry, league_id)
        except Exception as e:
            return None, {"error": "league_not_registered", "message": str(e)}
        if alias_norm != id_alias_norm:
            return None, {"error": "conflicting_parameters",
                            "message": f"alias '{alias}' resolves to '{alias_norm}' but league_id "
                                        f"{league_id} resolves to '{id_alias_norm}' - these must match."}
        resolved_alias, entry = alias_norm, alias_entry
    elif alias:
        try:
            resolved_alias, entry = league_registry.resolve_alias(registry, alias)
        except Exception as e:
            return None, {"error": "alias_not_found", "message": str(e)}
    elif league_id:
        try:
            resolved_alias, entry = league_registry.resolve_league_id(registry, league_id)
        except Exception as e:
            return None, {"error": "league_not_registered", "message": str(e)}
    else:
        resolved_alias, entry = league_registry.get_default_league(registry)
    resolved_league_id = entry["league_id"]
    try:
        raw = _fetch_raw_draft_state(resolved_league_id, resolved_year)
    except Exception as e:
        return None, {"status": "error", "error": "draft_fetch_failed", "message": str(e)}
    league = build_commissioner_snapshot(raw, resolved_league_id, resolved_year)
    return {"league": league, "raw": raw, "resolved_alias": resolved_alias,
             "resolved_league_id": resolved_league_id, "resolved_year": resolved_year}, None

def _adp_resolve_explicit_candidates(candidate_player_ids, candidate_player_names, espn_pool_by_id,
                                        espn_pool_by_norm_name, drafted_or_keeper_ids, keeper_only_ids):
    """Conservative ID/name resolution against the CURRENT ESPN
    available/draft universe. Never guesses on ambiguity."""
    resolved, rejected = [], []
    seen_ids = set()
    for pid in (candidate_player_ids or [])[:_ADP_MAX_EXPLICIT_CANDIDATES]:
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        if pid in keeper_only_ids:
            rejected.append({"player_id": pid, "reason": "keeper_assigned"})
        elif pid in drafted_or_keeper_ids:
            rejected.append({"player_id": pid, "reason": "already_drafted"})
        elif pid in espn_pool_by_id:
            resolved.append(espn_pool_by_id[pid])
        else:
            rejected.append({"player_id": pid, "reason": "not_available"})
    for nm in (candidate_player_names or [])[:_ADP_MAX_EXPLICIT_CANDIDATES]:
        norm = fp_client.normalize_player_name(nm)
        matches = espn_pool_by_norm_name.get(norm, [])
        if len(matches) == 0:
            rejected.append({"player_name": nm, "reason": "not_found"})
        elif len(matches) > 1:
            rejected.append({"player_name": nm, "reason": "ambiguous",
                               "candidates": [m["playerId"] for m in matches]})
        else:
            m = matches[0]
            if m["playerId"] not in seen_ids:
                seen_ids.add(m["playerId"])
                resolved.append(m)
    return resolved[:_ADP_MAX_EXPLICIT_CANDIDATES], rejected

def _adp_build_candidate_fact(espn_entry, fp_lookup, tier_board, my_drafted_counts, slot_counts,
                                 decision_pick, next_user_open_pick, demand_by_position, run_labels,
                                 available_pool_by_position, saved_strategy, strategy_alignment_available):
    norm_name = fp_client.normalize_player_name(espn_entry["name"])
    fp = fp_lookup.get(norm_name)
    position = espn_entry["position"]
    ecr = fp.get("ecr") if fp else None
    pos_rank = fp.get("pos_rank") if fp else None
    adp = fp.get("adp") if fp else None
    tier = fp.get("tier") if fp else None
    projection = fp.get("projection") if fp else None
    vor = fp.get("vor") if fp else None
    available_pool_at_pos = available_pool_by_position.get(position, [])
    tier_scarcity = _adp_tier_scarcity(tier_board, position, tier, available_pool_at_pos, espn_entry["name"])
    tier_cliff_urgency = _adp_tier_cliff_urgency(tier_scarcity)
    roster_fit = _adp_roster_fit(position, my_drafted_counts, slot_counts)
    band, market_delta = _ds_market_band(adp, decision_pick)
    survival = _adp_survival_band(adp, decision_pick, next_user_open_pick,
                                     demand_by_position.get(position) if demand_by_position else None,
                                     run_labels.get(position, "none"), tier_cliff_urgency,
                                     tier_scarcity.get("available_in_tier"))
    alignment = _adp_strategy_alignment(position, espn_entry["name"], vor, saved_strategy,
                                           strategy_alignment_available, decision_pick)
    return {
        "player_id": espn_entry["playerId"], "name": espn_entry["name"], "position": position,
        "fp_match_confidence": "high" if fp else "none",
        "ecr": ecr, "pos_rank": pos_rank, "adp": adp, "native_tier": tier, "projection": projection,
        "vor": vor, "market_band": {"band": band, "adp_delta": market_delta},
        "injury_context": {"espn_status": espn_entry.get("injuryStatus"),
                             "fp_status": (fp or {}).get("injury_status")},
        "tier_scarcity": {k: v for k, v in tier_scarcity.items() if not k.startswith("_")},
        "tier_cliff_urgency": tier_cliff_urgency, "roster_fit": roster_fit,
        "strategy_alignment": alignment, "survival": survival,
    }

# ============================================================
# D3.2: MANDATORY ROSTER FEASIBILITY (authorized narrow remediation,
# 2026-08-15). Single shared implementation reused identically by BOTH
# analyze_draft_pick (D3) and _adp_core_analysis (D4 core). Does NOT
# touch D2/D2.1. Does NOT add K/DST to the strategic VOR/tier universe -
# only injects bounded ESPN-factual candidates when required to preserve
# roster legality.
# ============================================================
_ADP_REQUIRED_SLOT_EXCLUDED_KEYS = FLEX_EXCLUDED_SLOT_KEYS  # {"BE","IR",""}

def _adp_espn_season_projection(player_obj):
    """player.stats[0]['projected_points'] - the SAME ESPN season-
    projection field/precedent D2/optimize_lineup already treat as
    factual ESPN data. Zero extra network calls (already-fetched
    free_agents() Player object)."""
    try:
        return (player_obj.stats or {}).get(0, {}).get("projected_points")
    except Exception:
        return None

def _adp_required_slot_instances(slot_counts: dict) -> list:
    """Flat list of required starting-slot instance keys - excludes
    BE/IR/empty-key/zero-count. Includes every actually-configured
    direct AND flex-style slot (FLEX/SUPERFLEX/OP/etc.) - never invents
    a slot ESPN doesn't expose."""
    instances = []
    for slot_key, count in (slot_counts or {}).items():
        if slot_key in _ADP_REQUIRED_SLOT_EXCLUDED_KEYS:
            continue
        c = count or 0
        instances.extend([slot_key] * c)
    return instances

def _adp_player_slot_eligible(position, slot_key: str) -> bool:
    if position is None:
        return False
    flex_positions = _parse_flex_eligibility(slot_key)
    if flex_positions is not None:
        return position in flex_positions
    return position == slot_key

def _adp_max_required_slot_matching(players: list, slot_instances: list) -> int:
    """Classic Kuhn augmenting-path maximum bipartite matching between
    drafted players and required-slot instances - each player fills at
    most one slot instance, each slot instance filled by at most one
    player. Deterministic given a deterministically-sorted players list
    (callers sort by player_id)."""
    n = len(slot_instances)
    slot_owner = [None] * n

    def try_kuhn(p_idx, visited):
        player = players[p_idx]
        for s_idx, slot_key in enumerate(slot_instances):
            if s_idx in visited:
                continue
            if not _adp_player_slot_eligible(player.get("position"), slot_key):
                continue
            visited.add(s_idx)
            if slot_owner[s_idx] is None or try_kuhn(slot_owner[s_idx], visited):
                slot_owner[s_idx] = p_idx
                return True
        return False

    matched = 0
    for p_idx in range(len(players)):
        if try_kuhn(p_idx, set()):
            matched += 1
    return matched

def _adp_roster_completion_feasibility(drafted_players: list, slot_counts: dict) -> dict:
    """drafted_players: [{'player_id':...,'position':...}, ...] for
    every player CURRENTLY on the user's roster (completed picks +
    assigned keepers - caller's responsibility to supply, this helper
    fetches nothing). Returns required_slot_count, matched_slots, and
    minimum_required_future_picks = max(0, required - matched)."""
    slot_instances = _adp_required_slot_instances(slot_counts)
    players_sorted = sorted(drafted_players, key=lambda p: p.get("player_id") if p.get("player_id") is not None else -1)
    matched = _adp_max_required_slot_matching(players_sorted, slot_instances)
    required_slot_count = len(slot_instances)
    return {"required_slot_count": required_slot_count, "matched_slots": matched,
             "minimum_required_future_picks": max(0, required_slot_count - matched)}

def _adp_candidate_preserves_feasibility(candidate_position, drafted_players: list, slot_counts: dict,
                                             remaining_open_picks_including_decision: int) -> bool:
    """Candidate-specific feasibility test (safer than simple remaining
    == missing equality - see D3.2 spec worked example). Adds candidate
    hypothetically, recomputes minimum_required_future_picks, and checks
    it against picks remaining AFTER this selection."""
    hypothetical = list(drafted_players) + [{"player_id": -999999, "position": candidate_position}]
    after = _adp_roster_completion_feasibility(hypothetical, slot_counts)
    remaining_after = remaining_open_picks_including_decision - 1
    return after["minimum_required_future_picks"] <= remaining_after

def _adp_remaining_user_open_picks(my_picks_all: list) -> int:
    """Exact count of the user's remaining OPEN (non-keeper, unresolved)
    selections - the SAME open_unresolved filter _adp_decision_pick_context
    already applies, exposed here so feasibility math reuses the exact
    semantics without duplicating a divergent filter."""
    return sum(1 for p in my_picks_all if not p["reserved_for_keeper"] and p["player_id"] is None)

def _adp_drafted_players_for_feasibility(my_completed: list, pos_lookup: dict) -> list:
    """{'player_id','position'} rows for every player CURRENTLY on the
    user's roster from completed picks (includes assigned keepers, since
    a keeper-assigned pick already has playerId set)."""
    out = []
    for p in my_completed:
        pid = p.get("playerId")
        pos = (pos_lookup.get(pid, {}) or {}).get("position")
        if pos:
            out.append({"player_id": pid, "position": pos})
    return out

def _adp_mandatory_unfilled_positions(drafted_players: list, slot_counts: dict) -> list:
    """Which DIRECT (non-flex), non-core (outside _DS_CORE_POSITIONS)
    required positions (K/DST and any other non-core direct slot) are
    not yet fully covered by drafted-player counts. Exact for K/D-ST
    since single-position slots never share flex eligibility with core
    positions. Core positions are excluded here - their feasibility is
    already handled by the normal candidate-filtering pass since they
    live in the normal analytical universe."""
    drafted_counts = {}
    for p in drafted_players:
        pos = p.get("position")
        if pos:
            drafted_counts[pos] = drafted_counts.get(pos, 0) + 1
    mandatory = []
    for slot_key, count in (slot_counts or {}).items():
        if slot_key in _ADP_REQUIRED_SLOT_EXCLUDED_KEYS or not count:
            continue
        if _parse_flex_eligibility(slot_key) is not None:
            continue
        if slot_key in _DS_CORE_POSITIONS:
            continue
        if drafted_counts.get(slot_key, 0) < count:
            mandatory.append(slot_key)
    return sorted(mandatory)

def _adp_build_mandatory_candidate_facts(mandatory_positions: list, espn_pool_by_id: dict) -> list:
    """ESPN-factual-only candidate injection for non-core mandatory
    positions (K/DST). Reuses D1's already-fetched free-agent pool - ZERO
    extra network calls. Ordering: real ESPN season projected_points
    when present (selection_basis=espn_projected_points_with_roster_
    feasibility), else deterministic player_id ordering (selection_basis
    =required_roster_feasibility_deterministic_fallback). No FP fields
    are ever populated for these candidates - ecr/adp/native_tier/vor
    all None, analysis_enrichment=espn_only."""
    out = []
    for pos in mandatory_positions:
        pool = [e for e in espn_pool_by_id.values() if e.get("position") == pos]
        def sort_key(e):
            proj = e.get("_espn_season_projected_points")
            return (proj is None, -(proj if proj is not None else 0), e.get("playerId"))
        pool_sorted = sorted(pool, key=sort_key)
        selection_basis = ("espn_projected_points_with_roster_feasibility"
                             if any(e.get("_espn_season_projected_points") is not None for e in pool_sorted)
                             else "required_roster_feasibility_deterministic_fallback")
        for e in pool_sorted[:3]:
            out.append({
                "player_id": e["playerId"], "name": e["name"], "position": pos,
                "fp_match_confidence": "not_applicable", "ecr": None, "pos_rank": None, "adp": None,
                "native_tier": None, "projection": None, "vor": None,
                "market_band": {"band": "not_applicable", "adp_delta": None},
                "injury_context": {"espn_status": e.get("injuryStatus"), "fp_status": None},
                "tier_scarcity": {"tier": None, "available_in_tier": None, "candidate_rank_within_tier": None,
                                    "next_tier": None, "projection_drop_to_next_tier": None, "ecr_gap_to_next_tier": None},
                "tier_cliff_urgency": "not_applicable",
                "roster_fit": {"label": "mandatory_required_slot", "structural_opening": True,
                                 "flex_eligible": False, "dedicated_drafted": 0, "dedicated_required": 1},
                "strategy_alignment": "roster_feasibility_override",
                "survival": {"baseline_band": "unknown", "adjustments": {}, "final_band": "unknown"},
                "analysis_enrichment": "espn_only",
                "selection_basis": selection_basis,
                "espn_season_projected_points": e.get("_espn_season_projected_points"),
                "recommendation_confidence_hint": "low",
            })
    return out

def _adp_apply_roster_feasibility(candidate_facts: list, drafted_players: list, slot_counts: dict,
                                      remaining_open_picks_including_decision: int, mandatory_positions: list,
                                      espn_pool_by_id: dict, explicit_mode: bool) -> dict:
    """PRECONDITION-ZERO feasibility gate - the SINGLE shared
    implementation called identically from analyze_draft_pick and
    _adp_core_analysis. Filters candidate_facts to only those that
    preserve the mathematical possibility of a legal final roster;
    injects bounded ESPN-factual-only K/DST candidates ONLY when no
    normal candidate remains feasible AND we are in auto (non-explicit)
    mode. Never fabricates FP data. Explicit-mode infeasible candidates
    are rejected, never auto-replaced (per D3.2 spec Test I)."""
    feasible, infeasible_rejected = [], []
    for c in candidate_facts:
        if _adp_candidate_preserves_feasibility(c["position"], drafted_players, slot_counts,
                                                    remaining_open_picks_including_decision):
            feasible.append(c)
        else:
            infeasible_rejected.append({"player_id": c["player_id"], "name": c["name"],
                                           "reason": "would_make_roster_completion_impossible"})

    mandatory_injected_ids = []
    if not explicit_mode and mandatory_positions and not feasible:
        for mf in _adp_build_mandatory_candidate_facts(mandatory_positions, espn_pool_by_id):
            if _adp_candidate_preserves_feasibility(mf["position"], drafted_players, slot_counts,
                                                        remaining_open_picks_including_decision):
                feasible.append(mf)
                mandatory_injected_ids.append(mf["player_id"])

    return {"final_candidates": feasible, "infeasible_rejected": infeasible_rejected,
             "mandatory_injected_ids": mandatory_injected_ids, "at_risk": (len(feasible) == 0)}


@mcp.tool()
async def analyze_draft_pick(alias: str = None, league_id: int = None, year: int = None,
                                candidate_player_ids: list = None, candidate_player_names: list = None,
                                top_n: int = 5) -> dict:
    """READ-ONLY live pick-analysis engine (D3). Use this during a live
    ESPN draft when analyzing specific candidate players. Performs a fresh
    live-board fetch before evaluating any candidate - do not assume a
    candidate is available based on external page or DOM text. D2
    VOR/tier/replacement reused unmodified; D2.1 structural snapshot used
    for strategy compatibility. Cache-only FantasyPros (zero HTTP).
    No draft actions, no strategy writes.

    Args:
        alias/league_id/year: league selector (registry conventions).
        candidate_player_ids: optional explicit ESPN player IDs (max 12).
        candidate_player_names: optional explicit names (max 12).
        top_n: 1-10, bounded. Ignored size beyond max returned candidates.
    """
    try:
        top_n_val, top_n_err = _validate_bounded_int(top_n, "top_n", 1, 10, 5)
        if top_n_err:
            return {"error": "invalid_parameter", "message": top_n_err}
        if candidate_player_ids and len(candidate_player_ids) > _ADP_MAX_EXPLICIT_CANDIDATES:
            return {"error": "invalid_parameter",
                     "message": f"candidate_player_ids exceeds max {_ADP_MAX_EXPLICIT_CANDIDATES}."}
        if candidate_player_names and len(candidate_player_names) > _ADP_MAX_EXPLICIT_CANDIDATES:
            return {"error": "invalid_parameter",
                     "message": f"candidate_player_names exceeds max {_ADP_MAX_EXPLICIT_CANDIDATES}."}

        ctx, err = _adp_resolve_league_and_state(alias, league_id, year)
        if err:
            return err
        league, raw = ctx["league"], ctx["raw"]
        resolved_league_id, resolved_year, resolved_alias = ctx["resolved_league_id"], ctx["resolved_year"], ctx["resolved_alias"]

        draft_detail = raw.get("draftDetail", {})
        picks_raw = draft_detail.get("picks", [])
        if not picks_raw:
            return {"status": "error", "error": "draft_data_unavailable",
                     "message": "ESPN returned no draft slot data for this league/year."}
        draft_status = _dp_derive_draft_status(draft_detail)
        if draft_status == "complete":
            return {"status": "ok", "draft_context": {"draft_status": "complete"},
                     "recommendation": None, "message": "draft_already_complete"}

        raw_teams = raw.get("teams", [])
        raw_settings = raw.get("settings", {})
        draft_settings = raw_settings.get("draftSettings", {})
        pos_lookup = _dp_build_position_lookup(raw_teams, resolved_year)
        picks_sorted = sorted(picks_raw, key=lambda p: (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9))
        unresolved = [p for p in picks_sorted if p.get("playerId", -1) in (None, -1)]
        completed_sorted = [p for p in picks_sorted if p.get("playerId", -1) not in (None, -1)]
        current_overall_pick = unresolved[0].get("overallPickNumber") if unresolved else None
        team_on_clock = None
        if unresolved:
            t = _find_team_by_id(league, unresolved[0].get("teamId"))
            team_on_clock = {"team_id": t.team_id, "team_name": t.team_name} if t else {"team_id": unresolved[0].get("teamId"), "team_name": None}

        draft_order = _dp_build_draft_order(picks_raw)
        authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")
        my_team = _resolve_my_team(league, authenticated_swid)
        my_team_id = my_team.get("team_id")
        if my_team_id is None:
            return {"status": "error", "error": "my_team_unresolved", "message": my_team.get("status")}

        _my_pick_ctx = _dp_build_my_pick_context(picks_raw, my_team_id, pos_lookup, unresolved)
        my_picks_all = _my_pick_ctx["my_picks_all"]
        decision_ctx = _adp_decision_pick_context(my_picks_all, unresolved)
        if decision_ctx["status"] != "ok":
            return {"status": "ok", "draft_context": {"draft_status": draft_status}, "recommendation": None,
                     "message": decision_ctx["status"]}
        decision_pick = decision_ctx["decision_pick"]
        next_user_open_pick = decision_ctx["next_user_open_pick"]

        settings = league.settings
        slot_counts = getattr(settings, "position_slot_counts", {}) or {}
        scoring_rules = getattr(settings, "scoring_format", []) or []
        scoring_bucket = _detect_league_scoring_bucket(scoring_rules)
        team_count = getattr(settings, "team_count", None)

        universes, fp_dataset_meta = {}, {}
        for pos in _DS_CORE_POSITIONS:
            universe, _warns = _ds_build_player_universe(pos, scoring_bucket)
            universes[pos] = universe
            rc = fp_client.get_rankings_cache(pos, scoring_bucket)
            pc = fp_client.get_projections_cache(pos, scoring_bucket, week=0)
            fp_dataset_meta[f"rankings_{pos}"] = (rc or {}).get("fetched_at")
            fp_dataset_meta[f"projections_{pos}"] = (pc or {}).get("fetched_at")
        players_cache_meta = fp_client.get_players_cache()
        fp_dataset_meta["players"] = (players_cache_meta or {}).get("fetched_at")
        injuries_cache_meta = fp_client.get_injuries_cache()
        fp_dataset_meta["injuries"] = (injuries_cache_meta or {}).get("fetched_at")

        allocation = _ds_starter_flex_allocation(slot_counts, universes)
        replacement_by_position = _ds_apply_replacement_and_vor(universes, allocation["replacement_index"])
        tier_board = _ds_build_tier_board(universes)
        position_guidance = {pos: _ds_position_guidance(pos, universes[pos], replacement_by_position[pos], allocation["dedicated_demand"])
                                for pos in _DS_CORE_POSITIONS}
        fp_lookup = _adp_build_fp_lookup(universes)

        all_reserved = [_dp_normalize_pick(p, pos_lookup) for p in picks_raw if p.get("reservedForKeeper")]
        keeper_reserved_slots = [p for p in my_picks_all if p["reserved_for_keeper"]]
        keeper_only_ids = {r["player_id"] for r in all_reserved if r["player_id"] is not None}
        assigned_keepers = [p for p in all_reserved if p["slot_status"] == "keeper_assigned"]
        unassigned_keepers = [p for p in all_reserved if p["slot_status"] == "reserved_keeper_unassigned"]
        keeper_count_cfg = getattr(league.settings, "keeper_count", 0) or 0
        if keeper_count_cfg == 0:
            keeper_identity_state = "not_applicable"
        elif len(unassigned_keepers) == 0 and len(all_reserved) > 0:
            keeper_identity_state = "known"
        elif len(assigned_keepers) > 0:
            keeper_identity_state = "partial"
        else:
            keeper_identity_state = "unknown_pre_deadline"

        current_structural_inputs = _ds_build_structural_inputs(
            resolved_league_id, resolved_year, scoring_bucket, team_count, slot_counts,
            draft_settings.get("type"), draft_order, my_picks_all, keeper_count_cfg,
            keeper_reserved_slots, keeper_identity_state, fp_dataset_meta, _DS_METHODOLOGY_VERSION,
        )
        strategy_compat = _adp_load_strategy_compatibility(resolved_league_id, resolved_year,
                                                               current_structural_inputs, len(completed_sorted))
        saved_strategy = strategy_compat["strategy"]
        strategy_alignment_available = strategy_compat["strategy_alignment_available"]

        drafted_or_keeper_ids = {p.get("playerId") for p in picks_raw if p.get("playerId", -1) not in (None, -1)}
        try:
            fa_size = min(max(top_n_val * 20, 200), 400)
            fa_players = league.free_agents(size=fa_size)
        except Exception as e:
            fa_players = []
        espn_pool = [p for p in fa_players if getattr(p, "playerId", None) not in drafted_or_keeper_ids]
        espn_pool_by_id, espn_pool_by_norm_name = {}, {}
        for p in espn_pool:
            entry = {"playerId": p.playerId, "name": p.name, "position": getattr(p, "position", None),
                      "injuryStatus": getattr(p, "injuryStatus", None),
                      "_espn_season_projected_points": _adp_espn_season_projection(p)}
            espn_pool_by_id[p.playerId] = entry
            espn_pool_by_norm_name.setdefault(fp_client.normalize_player_name(p.name), []).append(entry)

        recent_runs = _adp_recent_runs(completed_sorted, pos_lookup)
        demand_by_position = _adp_opponent_demand(picks_raw, decision_pick, next_user_open_pick, pos_lookup, slot_counts)

        my_completed = [p for p in picks_raw if p.get("teamId") == my_team_id and p.get("playerId", -1) not in (None, -1)]
        my_drafted_counts = {}
        for p in my_completed:
            pos = (pos_lookup.get(p.get("playerId"), {}) or {}).get("position")
            if pos:
                my_drafted_counts[pos] = my_drafted_counts.get(pos, 0) + 1

        available_pool_by_position = {}
        for pos in _DS_CORE_POSITIONS:
            names_at_pos = {fp_client.normalize_player_name(e["name"]) for e in espn_pool_by_id.values() if e.get("position") == pos}
            available_pool_by_position[pos] = [p for p in universes[pos] if p.get("_norm_name") in names_at_pos]

        # --- D3.2: mandatory roster feasibility context (additive) ---
        drafted_players_for_feasibility = _adp_drafted_players_for_feasibility(my_completed, pos_lookup)
        remaining_open_picks_including_decision = _adp_remaining_user_open_picks(my_picks_all)
        feasibility_before = _adp_roster_completion_feasibility(drafted_players_for_feasibility, slot_counts)
        slack_picks = remaining_open_picks_including_decision - feasibility_before["minimum_required_future_picks"]
        binding = slack_picks <= 0
        mandatory_positions = _adp_mandatory_unfilled_positions(drafted_players_for_feasibility, slot_counts)
        roster_feasibility_ctx = {
            "remaining_open_picks": remaining_open_picks_including_decision,
            "minimum_required_future_picks": feasibility_before["minimum_required_future_picks"],
            "slack_picks": slack_picks, "binding": binding,
            "mandatory_unfilled_slots": mandatory_positions, "candidate_restriction_active": binding,
        }

        explicit_mode = bool(candidate_player_ids or candidate_player_names)
        kdst_requested_ids, kdst_requested_names = set(), set()
        if explicit_mode:
            for pid in (candidate_player_ids or []):
                ent = espn_pool_by_id.get(pid)
                if ent and ent.get("position") in ("K", "D/ST"):
                    kdst_requested_ids.add(pid)
            for nm in (candidate_player_names or []):
                matches = espn_pool_by_norm_name.get(fp_client.normalize_player_name(nm), [])
                if len(matches) == 1 and matches[0].get("position") in ("K", "D/ST"):
                    kdst_requested_names.add(nm)

        rejected_candidates = []
        if explicit_mode:
            resolved_entries, rejected_candidates = _adp_resolve_explicit_candidates(
                candidate_player_ids, candidate_player_names, espn_pool_by_id, espn_pool_by_norm_name,
                drafted_or_keeper_ids, keeper_only_ids)
            target_ids = [e["playerId"] for e in resolved_entries]
        else:
            lightweight = []
            for pos in _DS_CORE_POSITIONS:
                for p in available_pool_by_position[pos]:
                    espn_match = espn_pool_by_norm_name.get(p["_norm_name"])
                    if espn_match:
                        lightweight.append((espn_match[0]["playerId"], p))
            by_ecr = sorted([lw for lw in lightweight if lw[1].get("ecr") is not None], key=lambda t: t[1]["ecr"])[:6]
            by_vor = sorted([lw for lw in lightweight if lw[1].get("vor") is not None], key=lambda t: -t[1]["vor"])[:6]
            near_last_tier = []
            for pos in _DS_CORE_POSITIONS:
                pool = available_pool_by_position[pos]
                for p in pool:
                    tier = p.get("tier")
                    if tier is None:
                        continue
                    scarcity = _adp_tier_scarcity(tier_board, pos, tier, pool)
                    if _adp_tier_cliff_urgency(scarcity) in ("high", "moderate"):
                        match = espn_pool_by_norm_name.get(p["_norm_name"])
                        if match:
                            near_last_tier.append((match[0]["playerId"], p))
                        break
            strategy_targets = []
            if strategy_alignment_available and saved_strategy and saved_strategy.get("pick_plan"):
                plan = min(saved_strategy["pick_plan"], key=lambda pl: abs(pl.get("overall_pick", 0) - decision_pick))
                for t in (plan.get("targets") or [])[:4]:
                    norm = fp_client.normalize_player_name(t.get("name", ""))
                    match = espn_pool_by_norm_name.get(norm)
                    if match:
                        strategy_targets.append((match[0]["playerId"], None))
            binding_widen = []
            if binding:
                for pos in _DS_CORE_POSITIONS:
                    pool = available_pool_by_position[pos]
                    top_binding = sorted([p for p in pool if p.get("ecr") is not None], key=lambda p: p["ecr"])[:2]
                    for p in top_binding:
                        espn_match = espn_pool_by_norm_name.get(p["_norm_name"])
                        if espn_match:
                            binding_widen.append((espn_match[0]["playerId"], p))
            target_ids, seen = [], set()
            for pid, _ in (by_ecr + by_vor + near_last_tier + strategy_targets + binding_widen):
                if pid not in seen:
                    seen.add(pid)
                    target_ids.append(pid)
                if len(target_ids) >= _ADP_MAX_AUTO_POOL:
                    break

        candidate_facts = []
        for pid in target_ids:
            entry = espn_pool_by_id.get(pid)
            if not entry:
                continue
            fact = _adp_build_candidate_fact(entry, fp_lookup, tier_board, my_drafted_counts, slot_counts,
                                                decision_pick, next_user_open_pick, demand_by_position,
                                                recent_runs["labels"], available_pool_by_position,
                                                saved_strategy, strategy_alignment_available)
            candidate_facts.append(fact)

        kdst_facts = []
        for pid in kdst_requested_ids:
            entry = espn_pool_by_id.get(pid)
            if entry:
                kdst_facts.append({"player_id": pid, "name": entry["name"], "position": entry["position"],
                                      "note": "K/DST explicit request - limited ESPN-factual analysis only.",
                                      "espn_injury_status": entry.get("injuryStatus"),
                                      "ecr": None, "adp": None, "native_tier": None, "vor": None})

        # --- D3.2: mandatory roster feasibility gate (PRECONDITION ZERO) ---
        feasibility_result = _adp_apply_roster_feasibility(
            candidate_facts, drafted_players_for_feasibility, slot_counts,
            remaining_open_picks_including_decision, mandatory_positions, espn_pool_by_id, explicit_mode)
        candidate_facts = feasibility_result["final_candidates"]
        rejected_candidates = rejected_candidates + feasibility_result["infeasible_rejected"]
        roster_feasibility_ctx["mandatory_injected_ids"] = feasibility_result["mandatory_injected_ids"]
        roster_feasibility_ctx["at_risk"] = feasibility_result["at_risk"]

        candidate_facts.sort(key=_adp_decision_sort_key)
        ranked = candidate_facts[:top_n_val]
        recommendation = _adp_build_recommendation(ranked)
        path_comparison = _adp_path_analysis(candidate_facts, {}, next_user_open_pick, decision_pick)
        notable_omission = None
        if explicit_mode and candidate_facts:
            notable_omission = _adp_dominance_notable_omission(candidate_facts[:len(target_ids)], candidate_facts)

        warnings_out = []
        data_freshness = fp_client.get_cache_freshness_report(list(_DS_CORE_POSITIONS), scoring_bucket)
        for k, v in data_freshness.items():
            if v.get("is_stale"):
                warnings_out.append(f"{k} cache is stale.")
        if strategy_compat["status"] == "missing":
            warnings_out.append("prepare_draft_strategy_recommended")
        elif strategy_compat["status"] == "compatibility_insufficient_structural_data":
            warnings_out.append("Saved strategy is schema v1 (pre-D2.1) - structural compatibility cannot be verified; strategy_alignment unavailable.")
        elif strategy_compat["status"] == "invalid":
            warnings_out.append(f"Saved strategy failed validation ({strategy_compat['error']}); continuing with live-only analysis.")
        elif strategy_compat["compatibility"] == "structurally_stale":
            warnings_out.append(f"Saved strategy is structurally_stale: {strategy_compat['drift_reasons']}. "
                                  "prepare_draft_strategy_recommended.")
        if roster_feasibility_ctx["binding"] and roster_feasibility_ctx["mandatory_unfilled_slots"]:
            warnings_out.append(f"Roster feasibility is binding: {roster_feasibility_ctx['mandatory_unfilled_slots']} "
                                  "must be filled with remaining picks.")
        if roster_feasibility_ctx.get("at_risk"):
            warnings_out.append("roster_completion_at_risk: no currently available player preserves a legal "
                                  "final roster for at least one mandatory position.")

        return {
            "status": "ok",
            "roster_feasibility": roster_feasibility_ctx,
            "league": {"league_id": resolved_league_id, "alias": resolved_alias, "year": resolved_year},
            "draft_context": {"draft_status": draft_status, "current_overall_pick": current_overall_pick,
                                 "team_on_clock": team_on_clock, "decision_pick": decision_pick,
                                 "user_on_clock": decision_ctx["user_on_clock"],
                                 "picks_until_decision": decision_ctx["picks_until_decision"],
                                 "next_user_open_pick": next_user_open_pick, "turn_span": decision_ctx["turn_span"]},
            "strategy_context": {"status": strategy_compat["status"],
                                    "strategy_id": (saved_strategy or {}).get("strategy_id"),
                                    "schema_version": (saved_strategy or {}).get("schema_version"),
                                    "compatibility": strategy_compat["compatibility"],
                                    "structural_drift_reasons": strategy_compat["drift_reasons"],
                                    "board_advanced": len(completed_sorted) > 0},
            "my_build": {"drafted_counts": my_drafted_counts, "starter_requirements": allocation["dedicated_demand"]},
            "live_context": {"recent_runs": recent_runs, "opponent_demand": demand_by_position,
                                "position_guidance": position_guidance},
            "recommendation": recommendation,
            "candidates": ranked,
            "path_comparison": path_comparison,
            "rejected_candidates": rejected_candidates,
            "notable_omission": notable_omission,
            "kdst_analysis": kdst_facts if kdst_facts else None,
            "methodology": {
                "decision_order": ["wait_risk(tier_cliff/survival)", "-VOR", "strategy_alignment",
                                      "structural_roster_fit", "ECR", "ADP(market/tie-context only)"],
                "vor_definition": "Reused from D2 unmodified: projection - replacement_projection.",
                "tier_source": "FantasyPros native tier field, never derived.",
                "run_rule": "active: >=3/last5 or >=5/last10; developing: >=2/last5 or >=4/last10.",
                "survival_rule": "Baseline = ADP position normalized within [decision_pick, next_user_open_pick]; "
                                   "max ONE net band adjustment from demand/run/tier-cliff/depth signals.",
                "strategy_compatibility_rule": "D2.1 structural_inputs compared field-by-field; board "
                                                  "advancement (completed picks, current pick) is never compared.",
                "path_assumption": "One-step only; candidate removed from pool, remaining candidates classified "
                                      "by survival band - no exact future picks invented.",
            },
            "warnings": warnings_out,
            "data_limitations": [
                "Candidate FP enrichment uses ESPN-vs-FantasyPros name matching (same approach used throughout "
                "this codebase) - not a guaranteed-unique ID join.",
                "VOR/replacement/tier-board values are computed over the full FantasyPros-ranked pool per D2's "
                "frozen methodology, then filtered to ESPN-available players for scarcity/candidate purposes.",
            ],
        }
    except Exception as e:
        return _error_response("analyzing draft pick", e)


# ============================================================
# PHASE D4: LIVE DRAFT WAR ROOM (get_live_draft_brief)
# _adp_core_analysis is a NEW parallel orchestration helper - it does
# NOT modify or extract from analyze_draft_pick (which remains 100%
# byte-identical/untouched). It reuses every existing _adp_*/_ds_*
# analytical primitive exactly (same VOR/tier/survival/run/demand/
# dominance/recommendation/path functions D3 already uses) so there is
# zero new methodology - only a second, richer orchestration wrapper
# that exposes internals (saved_strategy doc, keeper_identity_state,
# untrimmed candidate_facts, tier_board, position_guidance) that D3's
# PUBLIC response intentionally does not expose, so D4 can build its
# presentation layer without a second board fetch or strategy read.
# ============================================================
async def _adp_core_analysis(alias, league_id, year, top_n_val):
    """Mirrors analyze_draft_pick's exact orchestration flow (same
    helper calls, same order, same one fresh raw draft fetch, same one
    strategy load, same one FP cache-read set) but returns a RICHER
    internal dict for D4's presentation layer. No candidate_player_ids/
    candidate_player_names path - D4 has no explicit-candidate parameter
    per spec (explicit comparisons remain analyze_draft_pick's job)."""
    ctx, err = _adp_resolve_league_and_state(alias, league_id, year)
    if err:
        return {"early_exit": True, "payload": err}
    league, raw = ctx["league"], ctx["raw"]
    resolved_league_id, resolved_year, resolved_alias = ctx["resolved_league_id"], ctx["resolved_year"], ctx["resolved_alias"]

    draft_detail = raw.get("draftDetail", {})
    picks_raw = draft_detail.get("picks", [])
    if not picks_raw:
        return {"early_exit": True, "payload": {"status": "error", "error": "draft_data_unavailable",
                 "message": "ESPN returned no draft slot data for this league/year."}}
    draft_status = _dp_derive_draft_status(draft_detail)
    if draft_status == "complete":
        return {"early_exit": True, "payload": {"status": "ok", "draft_status": "complete",
                 "message": "draft_already_complete"}, "resolved_league_id": resolved_league_id,
                 "resolved_year": resolved_year, "resolved_alias": resolved_alias, "league": league,
                 "picks_raw": picks_raw}

    raw_teams = raw.get("teams", [])
    raw_settings = raw.get("settings", {})
    draft_settings = raw_settings.get("draftSettings", {})
    pos_lookup = _dp_build_position_lookup(raw_teams, resolved_year)
    picks_sorted = sorted(picks_raw, key=lambda p: (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9))
    unresolved = [p for p in picks_sorted if p.get("playerId", -1) in (None, -1)]
    completed_sorted = [p for p in picks_sorted if p.get("playerId", -1) not in (None, -1)]
    current_overall_pick = unresolved[0].get("overallPickNumber") if unresolved else None
    team_on_clock = None
    if unresolved:
        t = _find_team_by_id(league, unresolved[0].get("teamId"))
        team_on_clock = {"team_id": t.team_id, "team_name": t.team_name} if t else {"team_id": unresolved[0].get("teamId"), "team_name": None}

    draft_order = _dp_build_draft_order(picks_raw)
    authenticated_swid = api.credentials.get(SESSION_ID, {}).get("swid")
    my_team = _resolve_my_team(league, authenticated_swid)
    my_team_id = my_team.get("team_id")
    if my_team_id is None:
        return {"early_exit": True, "payload": {"status": "error", "error": "my_team_unresolved",
                 "message": my_team.get("status")}}

    _my_pick_ctx = _dp_build_my_pick_context(picks_raw, my_team_id, pos_lookup, unresolved)
    my_picks_all = _my_pick_ctx["my_picks_all"]
    decision_ctx = _adp_decision_pick_context(my_picks_all, unresolved)
    if decision_ctx["status"] != "ok":
        return {"early_exit": True, "payload": {"status": "ok", "draft_status": draft_status,
                 "message": decision_ctx["status"]}, "resolved_league_id": resolved_league_id,
                 "resolved_year": resolved_year, "resolved_alias": resolved_alias, "league": league,
                 "my_team": my_team, "picks_raw": picks_raw}
    decision_pick = decision_ctx["decision_pick"]
    next_user_open_pick = decision_ctx["next_user_open_pick"]

    settings = league.settings
    slot_counts = getattr(settings, "position_slot_counts", {}) or {}
    scoring_rules = getattr(settings, "scoring_format", []) or []
    scoring_bucket = _detect_league_scoring_bucket(scoring_rules)
    team_count = getattr(settings, "team_count", None)

    universes, fp_dataset_meta = {}, {}
    for pos in _DS_CORE_POSITIONS:
        universe, _warns = _ds_build_player_universe(pos, scoring_bucket)
        universes[pos] = universe
        rc = fp_client.get_rankings_cache(pos, scoring_bucket)
        pc = fp_client.get_projections_cache(pos, scoring_bucket, week=0)
        fp_dataset_meta[f"rankings_{pos}"] = (rc or {}).get("fetched_at")
        fp_dataset_meta[f"projections_{pos}"] = (pc or {}).get("fetched_at")
    players_cache_meta = fp_client.get_players_cache()
    fp_dataset_meta["players"] = (players_cache_meta or {}).get("fetched_at")
    injuries_cache_meta = fp_client.get_injuries_cache()
    fp_dataset_meta["injuries"] = (injuries_cache_meta or {}).get("fetched_at")

    allocation = _ds_starter_flex_allocation(slot_counts, universes)
    replacement_by_position = _ds_apply_replacement_and_vor(universes, allocation["replacement_index"])
    tier_board = _ds_build_tier_board(universes)
    position_guidance = {pos: _ds_position_guidance(pos, universes[pos], replacement_by_position[pos], allocation["dedicated_demand"])
                            for pos in _DS_CORE_POSITIONS}
    fp_lookup = _adp_build_fp_lookup(universes)

    all_reserved = [_dp_normalize_pick(p, pos_lookup) for p in picks_raw if p.get("reservedForKeeper")]
    keeper_reserved_slots = [p for p in my_picks_all if p["reserved_for_keeper"]]
    assigned_keepers = [p for p in all_reserved if p["slot_status"] == "keeper_assigned"]
    unassigned_keepers = [p for p in all_reserved if p["slot_status"] == "reserved_keeper_unassigned"]
    keeper_count_cfg = getattr(league.settings, "keeper_count", 0) or 0
    if keeper_count_cfg == 0:
        keeper_identity_state = "not_applicable"
    elif len(unassigned_keepers) == 0 and len(all_reserved) > 0:
        keeper_identity_state = "known"
    elif len(assigned_keepers) > 0:
        keeper_identity_state = "partial"
    else:
        keeper_identity_state = "unknown_pre_deadline"

    current_structural_inputs = _ds_build_structural_inputs(
        resolved_league_id, resolved_year, scoring_bucket, team_count, slot_counts,
        draft_settings.get("type"), draft_order, my_picks_all, keeper_count_cfg,
        keeper_reserved_slots, keeper_identity_state, fp_dataset_meta, _DS_METHODOLOGY_VERSION,
    )
    strategy_compat = _adp_load_strategy_compatibility(resolved_league_id, resolved_year,
                                                           current_structural_inputs, len(completed_sorted))
    saved_strategy = strategy_compat["strategy"]
    strategy_alignment_available = strategy_compat["strategy_alignment_available"]

    # D11C: initial draft state hash (before free-agent fetch)
    initial_state_hash = _dp_state_hash(resolved_league_id, resolved_year, picks_raw)

    try:
        fa_size = min(max(top_n_val * 20, 200), 400)
        fa_players = league.free_agents(size=fa_size)
    except Exception:
        fa_players = []

    # D11C: second fresh draft-state GET after free-agent request
    try:
        raw2 = _fetch_raw_draft_state(league.league_id, league.year)
    except Exception as _d11c_err:
        return {"early_exit": True, "payload": {
            "status": "error", "error": "draft_revalidation_failed",
            "message": ("Final draft-state revalidation failed after free-agent request. "
                         "No recommendation can be issued because board state could not be confirmed. "
                         "Call this tool again to retry."),
            "detail": str(_d11c_err) if not _is_private_league_error(_d11c_err) else "private_league_auth_error"
        }}

    # D11C: rederive all draft-state inputs from final state
    draft_detail_2 = raw2.get("draftDetail", {})
    picks_raw_2 = draft_detail_2.get("picks", []) or picks_raw
    raw_teams_2 = raw2.get("teams", []) or raw_teams
    pos_lookup_2 = _dp_build_position_lookup(raw_teams_2, resolved_year)

    picks_sorted_2 = sorted(picks_raw_2, key=lambda p: (p.get("overallPickNumber") if p.get("overallPickNumber") is not None else 10**9))
    unresolved_2 = [p for p in picks_sorted_2 if p.get("playerId", -1) in (None, -1)]
    completed_sorted_2 = [p for p in picks_sorted_2 if p.get("playerId", -1) not in (None, -1)]

    final_state_hash = _dp_state_hash(resolved_league_id, resolved_year, picks_raw_2)
    board_advanced_during_call = (initial_state_hash != final_state_hash)

    # Rebind authoritative names to state_2
    picks_raw = picks_raw_2
    pos_lookup = pos_lookup_2
    picks_sorted = picks_sorted_2
    unresolved = unresolved_2
    completed_sorted = completed_sorted_2

    # Rebuild current_overall_pick and team_on_clock from state_2
    current_overall_pick = unresolved[0].get("overallPickNumber") if unresolved else None
    team_on_clock = None
    if unresolved:
        t2 = _find_team_by_id(league, unresolved[0].get("teamId"))
        team_on_clock = {"team_id": t2.team_id, "team_name": t2.team_name} if t2 else {"team_id": unresolved[0].get("teamId"), "team_name": None}

    # D11C-R1: rebuild turn-window context from state_2 after rebind
    _my_pick_ctx_2 = _dp_build_my_pick_context(picks_raw, my_team_id, pos_lookup, unresolved)
    my_picks_all = _my_pick_ctx_2['my_picks_all']
    decision_ctx = _adp_decision_pick_context(my_picks_all, unresolved)
    if decision_ctx['status'] != 'ok':
            return {'early_exit': True, 'payload': {'status': 'ok', 'draft_status': draft_status, 'message': decision_ctx['status']}, 'resolved_league_id': resolved_league_id, 'resolved_year': resolved_year, 'resolved_alias': resolved_alias, 'league': league, 'my_team': my_team, 'picks_raw': picks_raw}
    decision_pick = decision_ctx['decision_pick']
    next_user_open_pick = decision_ctx['next_user_open_pick']
    draft_order = _dp_build_draft_order(picks_raw)

    # Final availability filter against state_2 drafted IDs
    drafted_or_keeper_ids = {p.get("playerId") for p in picks_raw if p.get("playerId", -1) not in (None, -1)}
    espn_pool = [p for p in fa_players if getattr(p, "playerId", None) not in drafted_or_keeper_ids]
    espn_pool_by_id, espn_pool_by_norm_name = {}, {}
    for p in espn_pool:
        entry = {"playerId": p.playerId, "name": p.name, "position": getattr(p, "position", None),
                  "injuryStatus": getattr(p, "injuryStatus", None),
                  "_espn_season_projected_points": _adp_espn_season_projection(p)}
        espn_pool_by_id[p.playerId] = entry
        espn_pool_by_norm_name.setdefault(fp_client.normalize_player_name(p.name), []).append(entry)

    # Rebuild state_2-derived analytics
    recent_runs = _adp_recent_runs(completed_sorted, pos_lookup)
    demand_by_position = _adp_opponent_demand(picks_raw, decision_pick, next_user_open_pick, pos_lookup, slot_counts)

    my_completed = [p for p in picks_raw if p.get("teamId") == my_team_id and p.get("playerId", -1) not in (None, -1)]
    my_drafted_counts = {}
    for p in my_completed:
        pos = (pos_lookup.get(p.get("playerId"), {}) or {}).get("position")
        if pos:
            my_drafted_counts[pos] = my_drafted_counts.get(pos, 0) + 1

    available_pool_by_position = {}
    for pos in _DS_CORE_POSITIONS:
        names_at_pos = {fp_client.normalize_player_name(e["name"]) for e in espn_pool_by_id.values() if e.get("position") == pos}
        available_pool_by_position[pos] = [p for p in universes[pos] if p.get("_norm_name") in names_at_pos]

    # --- D3.2: mandatory roster feasibility context (additive, shared with analyze_draft_pick) ---
    drafted_players_for_feasibility = _adp_drafted_players_for_feasibility(my_completed, pos_lookup)
    remaining_open_picks_including_decision = _adp_remaining_user_open_picks(my_picks_all)
    feasibility_before = _adp_roster_completion_feasibility(drafted_players_for_feasibility, slot_counts)
    slack_picks = remaining_open_picks_including_decision - feasibility_before["minimum_required_future_picks"]
    binding = slack_picks <= 0
    mandatory_positions = _adp_mandatory_unfilled_positions(drafted_players_for_feasibility, slot_counts)
    roster_feasibility_ctx = {
        "remaining_open_picks": remaining_open_picks_including_decision,
        "minimum_required_future_picks": feasibility_before["minimum_required_future_picks"],
        "slack_picks": slack_picks, "binding": binding,
        "mandatory_unfilled_slots": mandatory_positions, "candidate_restriction_active": binding,
    }

    # Auto-candidate pool (same union methodology as analyze_draft_pick's
    # no-explicit-candidates path - top ECR, top VOR, near-last-tier,
    # still-available D2 targets - never ECR-only).
    lightweight = []
    for pos in _DS_CORE_POSITIONS:
        for p in available_pool_by_position[pos]:
            espn_match = espn_pool_by_norm_name.get(p["_norm_name"])
            if espn_match:
                lightweight.append((espn_match[0]["playerId"], p))
    by_ecr = sorted([lw for lw in lightweight if lw[1].get("ecr") is not None], key=lambda t: t[1]["ecr"])[:6]
    by_vor = sorted([lw for lw in lightweight if lw[1].get("vor") is not None], key=lambda t: -t[1]["vor"])[:6]
    near_last_tier = []
    for pos in _DS_CORE_POSITIONS:
        pool = available_pool_by_position[pos]
        for p in pool:
            tier = p.get("tier")
            if tier is None:
                continue
            scarcity = _adp_tier_scarcity(tier_board, pos, tier, pool)
            if _adp_tier_cliff_urgency(scarcity) in ("high", "moderate"):
                match = espn_pool_by_norm_name.get(p["_norm_name"])
                if match:
                    near_last_tier.append((match[0]["playerId"], p))
                break
    strategy_targets = []
    if strategy_alignment_available and saved_strategy and saved_strategy.get("pick_plan"):
        plan = min(saved_strategy["pick_plan"], key=lambda pl: abs(pl.get("overall_pick", 0) - decision_pick))
        for t in (plan.get("targets") or [])[:4]:
            norm = fp_client.normalize_player_name(t.get("name", ""))
            match = espn_pool_by_norm_name.get(norm)
            if match:
                strategy_targets.append((match[0]["playerId"], None))
    binding_widen = []
    if binding:
        for pos in _DS_CORE_POSITIONS:
            pool = available_pool_by_position[pos]
            top_binding = sorted([p for p in pool if p.get("ecr") is not None], key=lambda p: p["ecr"])[:2]
            for p in top_binding:
                espn_match = espn_pool_by_norm_name.get(p["_norm_name"])
                if espn_match:
                    binding_widen.append((espn_match[0]["playerId"], p))
    target_ids, seen = [], set()
    for pid, _ in (by_ecr + by_vor + near_last_tier + strategy_targets + binding_widen):
        if pid not in seen:
            seen.add(pid)
            target_ids.append(pid)
        if len(target_ids) >= _ADP_MAX_AUTO_POOL:
            break

    candidate_facts = []
    for pid in target_ids:
        entry = espn_pool_by_id.get(pid)
        if not entry:
            continue
        fact = _adp_build_candidate_fact(entry, fp_lookup, tier_board, my_drafted_counts, slot_counts,
                                            decision_pick, next_user_open_pick, demand_by_position,
                                            recent_runs["labels"], available_pool_by_position,
                                            saved_strategy, strategy_alignment_available)
        candidate_facts.append(fact)

    # --- D3.2: mandatory roster feasibility gate (PRECONDITION ZERO) - SAME shared helper as analyze_draft_pick ---
    feasibility_result = _adp_apply_roster_feasibility(
        candidate_facts, drafted_players_for_feasibility, slot_counts,
        remaining_open_picks_including_decision, mandatory_positions, espn_pool_by_id, False)
    candidate_facts = feasibility_result["final_candidates"]
    roster_feasibility_ctx["mandatory_injected_ids"] = feasibility_result["mandatory_injected_ids"]
    roster_feasibility_ctx["at_risk"] = feasibility_result["at_risk"]
    roster_feasibility_ctx["infeasible_rejected"] = feasibility_result["infeasible_rejected"]

    candidate_facts.sort(key=_adp_decision_sort_key)
    ranked = candidate_facts[:top_n_val]
    recommendation = _adp_build_recommendation(ranked)
    path_comparison = _adp_path_analysis(candidate_facts, {}, next_user_open_pick, decision_pick)

    warnings_out = []
    data_freshness = fp_client.get_cache_freshness_report(list(_DS_CORE_POSITIONS), scoring_bucket)
    for k, v in data_freshness.items():
        if v.get("is_stale"):
            warnings_out.append(f"{k} cache is stale.")
    if strategy_compat["status"] == "missing":
        warnings_out.append("prepare_draft_strategy_recommended")
    elif strategy_compat["status"] == "compatibility_insufficient_structural_data":
        warnings_out.append("Saved strategy is schema v1 (pre-D2.1) - structural compatibility cannot be verified; strategy_alignment unavailable.")
    elif strategy_compat["status"] == "invalid":
        warnings_out.append(f"Saved strategy failed validation ({strategy_compat['error']}); continuing with live-only analysis.")
    elif strategy_compat["compatibility"] == "structurally_stale":
        warnings_out.append(f"Saved strategy is structurally_stale: {strategy_compat['drift_reasons']}. prepare_draft_strategy_recommended.")
    if keeper_identity_state in ("unknown_pre_deadline", "partial"):
        warnings_out.append("Keeper identities are not yet finalized - strategy/targets remain provisional.")
    if roster_feasibility_ctx["binding"] and roster_feasibility_ctx["mandatory_unfilled_slots"]:
        warnings_out.append(f"Roster feasibility is binding: {roster_feasibility_ctx['mandatory_unfilled_slots']} "
                              "must be filled with remaining picks.")
    if roster_feasibility_ctx.get("at_risk"):
        warnings_out.append("roster_completion_at_risk: no currently available player preserves a legal "
                              "final roster for at least one mandatory position.")

    return {
        "early_exit": False,
        "roster_feasibility": roster_feasibility_ctx,
        "resolved_league_id": resolved_league_id, "resolved_year": resolved_year, "resolved_alias": resolved_alias,
        "league": league, "draft_status": draft_status, "current_overall_pick": current_overall_pick,
        "team_on_clock": team_on_clock, "decision_pick": decision_pick, "user_on_clock": decision_ctx["user_on_clock"],
        "picks_until_decision": decision_ctx["picks_until_decision"], "next_user_open_pick": next_user_open_pick,
        "turn_span": decision_ctx["turn_span"], "strategy_compat": strategy_compat, "saved_strategy": saved_strategy,
        "keeper_identity_state": keeper_identity_state, "my_drafted_counts": my_drafted_counts,
        "starter_requirements": allocation["dedicated_demand"], "slot_counts": slot_counts,
        "recent_runs": recent_runs, "demand_by_position": demand_by_position, "position_guidance": position_guidance,
        "recommendation": recommendation, "ranked_candidates": ranked, "all_candidate_facts": candidate_facts,
        "path_comparison": path_comparison, "warnings": warnings_out, "data_freshness": data_freshness,
        "board_state_hash": final_state_hash,
        "completed_pick_count": len(completed_sorted),
        "board_advanced_during_call": board_advanced_during_call,
    }

_GLDB_MAX_ALTERNATIVES = 7
_GLDB_MAX_TIER_CLIFFS = 4
_GLDB_MAX_WARNINGS = 5
_GLDB_MAX_LIMITATIONS = 5
_GLDB_MAX_PATHS = 3
_GLDB_MAX_SURVIVAL_GROUP = 5
_GLDB_INTERNAL_TOP_N = 10  # always request D3's max width internally for
# the broadest honest position coverage in board_pressure/tier_cliffs;
# the PUBLIC top_n only bounds the visible alternatives list length -
# this is a pure orchestration choice, not new methodology (candidate
# ORDER from D3 is unchanged; we only vary how much of D3's own already-
# ordered list we surface).

def _gldb_position_pressure(pos: str, candidates_at_pos: list, run_label: str, demand_entry) -> dict:
    """Deterministic PRESENTATION-ONLY mapping. Reuses D3's own
    constants (_ADP_STRONG_DEMAND_RATIO=0.5, _ADP_MINIMAL_DEMAND_RATIO=
    0.25) - no new thresholds. Never feeds back into recommendation.

    unknown: no demand-window evidence at all (demand_entry is None)
    high:    any present-candidate tier_cliff_urgency=="high", OR
             run=="active", OR demand ratio >= _ADP_STRONG_DEMAND_RATIO
    moderate: any tier_cliff_urgency=="moderate", OR run=="developing",
             OR demand ratio in (_ADP_MINIMAL_DEMAND_RATIO, _ADP_STRONG_DEMAND_RATIO)
    low:     otherwise
    """
    if demand_entry is None:
        return {"pressure": "unknown", "run": run_label, "tier_remaining": None,
                 "intervening_dedicated_openings": None}
    ratio = (demand_entry.get("teams_with_dedicated_opening", 0)
              / max(1, demand_entry.get("unique_intervening_teams", 1)))
    urgencies = [c["tier_cliff_urgency"] for c in candidates_at_pos]
    tier_remaining = next((c["tier_scarcity"].get("available_in_tier") for c in candidates_at_pos
                             if c["tier_scarcity"].get("available_in_tier") is not None), None)
    if "high" in urgencies or run_label == "active" or ratio >= _ADP_STRONG_DEMAND_RATIO:
        pressure = "high"
    elif "moderate" in urgencies or run_label == "developing" or (_ADP_MINIMAL_DEMAND_RATIO < ratio < _ADP_STRONG_DEMAND_RATIO):
        pressure = "moderate"
    else:
        pressure = "low"
    return {"pressure": pressure, "run": run_label, "tier_remaining": tier_remaining,
             "intervening_dedicated_openings": demand_entry.get("teams_with_dedicated_opening")}

def _gldb_build_board_pressure(all_candidate_facts: list, recent_runs: dict, demand_result: dict) -> dict:
    """demand_result is the FULL _adp_opponent_demand() return (has a
    nested 'demand_by_position' key mapping position -> per-position
    demand dict) - not itself keyed by position. Unwrap it once here."""
    by_pos = {}
    for c in all_candidate_facts:
        by_pos.setdefault(c["position"], []).append(c)
    per_position_demand = (demand_result or {}).get("demand_by_position") or {}
    out = {}
    for pos in _DS_CORE_POSITIONS:
        out[pos] = _gldb_position_pressure(pos, by_pos.get(pos, []), recent_runs["labels"].get(pos, "none"),
                                               per_position_demand.get(pos))
    return out

def _gldb_select_tier_cliffs(all_candidate_facts: list, primary_position) -> list:
    """Max 4, deterministic priority: primary's position first, then
    candidates with tier_cliff_urgency=='high', then 'moderate', then
    candidate decision-order rank. One entry per position (most urgent
    candidate at that position). Pure selection over already-computed
    D3 tier_scarcity - no new tier logic."""
    by_pos_best = {}
    for idx, c in enumerate(all_candidate_facts):
        if c["tier_scarcity"].get("tier") is None:
            continue
        pos = c["position"]
        urgency_rank = {"high": 0, "moderate": 1, "none": 2, "unknown": 2}.get(c["tier_cliff_urgency"], 2)
        key = (urgency_rank, idx)
        if pos not in by_pos_best or key < by_pos_best[pos][0]:
            by_pos_best[pos] = (key, c)
    entries = list(by_pos_best.items())
    def sort_key(item):
        pos, (key, c) = item
        is_primary = 0 if pos == primary_position else 1
        return (is_primary, key[0], key[1])
    entries.sort(key=sort_key)
    out = []
    for pos, (key, c) in entries[:_GLDB_MAX_TIER_CLIFFS]:
        ts = c["tier_scarcity"]
        out.append({"position": pos, "tier": ts.get("tier"), "remaining": ts.get("available_in_tier"),
                     "next_tier_projection_drop": ts.get("projection_drop_to_next_tier"),
                     "urgency": c["tier_cliff_urgency"]})
    return out

def _gldb_build_alternatives(ranked_candidates: list, primary_player_id) -> list:
    """Straight slice of D3's own decision-ordered list minus primary -
    NEVER reranked. Labels are descriptive tags over already-computed
    D3 fields, not a new scoring system."""
    alts = [c for c in ranked_candidates if c["player_id"] != primary_player_id]
    out = []
    for c in alts[:_GLDB_MAX_ALTERNATIVES]:
        label = None
        if c["strategy_alignment"] == "aligned":
            label = "best_strategy_fit"
        elif c["strategy_alignment"] in ("value_override_candidate",):
            label = "best_pure_value"
        elif c["strategy_alignment"] in ("scarcity_override_candidate", "contingency_triggered"):
            label = "best_positional_scarcity"
        elif c["tier_cliff_urgency"] in ("high", "moderate"):
            label = "best_tier_value"
        elif c["roster_fit"]["label"] == "fills_open_dedicated_starter":
            label = "best_roster_fit"
        why = c.get("strategy_alignment") if label == "best_strategy_fit" else \
              f"VOR {c['vor']}" if c.get("vor") is not None else f"ECR {c.get('ecr')}"
        out.append({"player_id": c["player_id"], "name": c["name"], "position": c["position"],
                     "label": label, "why": str(why),
                     "tradeoff_vs_primary": f"survival={c['survival']['final_band']}, tier={c['native_tier']}"})
    return out

def _gldb_next_turn_groups(all_candidate_facts: list) -> dict:
    likely, at_risk, unlikely = [], [], []
    for c in all_candidate_facts:
        band = c["survival"]["final_band"]
        entry = {"name": c["name"], "position": c["position"], "band": band}
        if band in ("likely", "very_likely") and len(likely) < _GLDB_MAX_SURVIVAL_GROUP:
            likely.append(entry)
        elif band == "at_risk" and len(at_risk) < _GLDB_MAX_SURVIVAL_GROUP:
            at_risk.append(entry)
        elif band == "very_unlikely" and len(unlikely) < _GLDB_MAX_SURVIVAL_GROUP:
            unlikely.append(entry)
    return {"likely_to_survive": likely, "at_risk": at_risk, "unlikely_to_survive": unlikely}

def _gldb_position_outlook(all_candidate_facts: list) -> dict:
    by_pos = {}
    for c in all_candidate_facts:
        by_pos.setdefault(c["position"], []).append(c["survival"]["final_band"])
    outlook = {}
    for pos in _DS_CORE_POSITIONS:
        bands = by_pos.get(pos)
        if not bands:
            outlook[pos] = "unknown"
            continue
        safe = sum(1 for b in bands if b in ("likely", "very_likely"))
        risky = sum(1 for b in bands if b in ("at_risk", "very_unlikely"))
        if safe > risky:
            outlook[pos] = "deep"
        elif risky > safe:
            outlook[pos] = "thin"
        else:
            outlook[pos] = "moderate"
    return outlook

def _gldb_why_now(recommendation: dict, primary_candidate: dict) -> list:
    """Compress D3 evidence into max 3 reasons - each maps to an actual
    structured field already computed (tier_scarcity, demand, survival).
    No new inference."""
    reasons = []
    if primary_candidate.get("strategy_alignment") == "roster_feasibility_override":
        reasons.append("Selecting another optional position would make the final roster structurally impossible.")
    ts = primary_candidate["tier_scarcity"]
    if primary_candidate["tier_cliff_urgency"] in ("high", "moderate") and ts.get("tier") is not None:
        reasons.append(f"Only {ts.get('available_in_tier')} player(s) remain in {primary_candidate['position']} "
                         f"tier {ts.get('tier')} (next tier drops {ts.get('projection_drop_to_next_tier')} pts).")
    band = primary_candidate["survival"]["final_band"]
    if band in ("at_risk", "very_unlikely"):
        reasons.append(f"Survival to your next open pick is rated {band}.")
    if recommendation.get("main_tradeoff"):
        reasons.append(recommendation["main_tradeoff"])
    return reasons[:3]

def _gldb_strategy_summary(strategy_compat: dict, primary_candidate) -> dict:
    alignment = primary_candidate["strategy_alignment"] if primary_candidate else "unavailable"
    message = None
    if alignment == "aligned":
        message = "Recommendation matches the saved pre-draft strategy's priority for this position."
    elif alignment == "value_override_candidate":
        message = "Deviating from the saved plan: this player's value fell well beyond expected range."
    elif alignment == "scarcity_override_candidate":
        message = "Deviating from the saved plan: this is the last strong player in a thinning tier."
    elif alignment == "contingency_triggered":
        message = "A pre-planned contingency for this scenario has been triggered."
    elif alignment == "off_plan":
        message = "This pick is off the original plan; no override condition was met."
    elif alignment == "roster_feasibility_override":
        message = "This selection is required to preserve the mathematical possibility of a legal final roster."
    elif alignment == "unavailable":
        message = "Strategy alignment unavailable (missing, invalid, compatibility, or structurally stale)."
    return {"base_strategy_status": strategy_compat.get("status"), "compatibility": strategy_compat.get("compatibility"),
             "alignment": alignment, "structural_drift_reasons": strategy_compat.get("drift_reasons", []),
             "message": message}

def _gldb_build_summary_sentence(headline: dict, recommendation, why_now: list, alternatives: list) -> str:
    """Generated ONLY from already-structured fields in this same brief -
    no independent reasoning."""
    if recommendation is None:
        return "No recommendation is available for this decision context."
    clock_phrase = "on the clock" if headline["user_on_clock"] else f"picking in {headline['picks_until_decision']} selection(s)"
    parts = [f"You're {clock_phrase} at pick {headline['decision_pick']}. "
              f"{recommendation['name']} ({recommendation['position']}) is the recommendation"]
    if why_now:
        parts[0] += f" because {why_now[0].rstrip('.').lower()}."
    else:
        parts[0] += "."
    if alternatives:
        parts.append(f"{alternatives[0]['name']} is the top alternative.")
    return " ".join(parts)

@mcp.tool()
async def get_live_draft_brief(alias: str = None, league_id: int = None, year: int = None,
                                   top_n: int = 5) -> dict:
    """PRIMARY LIVE ESPN DRAFT RECOMMENDATION TOOL (D4). During an ESPN
    live draft, call this before every on-clock player recommendation.
    Its fresh ESPN board state is the authoritative availability source
    for the current draft - do not treat availability shown in any
    external page or DOM view as authoritative when this tool is reachable.
    If this tool returns an error, surface that error explicitly before
    using any less-reliable fallback.

    Orchestrates D1 fresh board + D2 saved strategy + D3 recommendation
    methodology into one compact response. Introduces NO new analytical
    methodology - the primary recommendation is IDENTICAL to what
    analyze_draft_pick would return for the same board/strategy state.
    Fresh ESPN draft-state fetch plus final revalidation fetch, cache-only FantasyPros, zero
    strategy writes, zero draft actions.

    Args:
        alias/league_id/year: league selector (registry conventions).
        top_n: bounds the visible alternatives list; 3-8, default 5.
    """
    try:
        top_n_val, top_n_err = _validate_bounded_int(top_n, "top_n", 3, 8, 5)
        if top_n_err:
            return {"error": "invalid_parameter", "message": top_n_err}

        core = await _adp_core_analysis(alias, league_id, year, _GLDB_INTERNAL_TOP_N)
        if core["early_exit"]:
            payload = core["payload"]
            if payload.get("error") in ("draft_data_unavailable", "my_team_unresolved"):
                return payload
            if payload.get("draft_status") == "complete" or payload.get("message") == "draft_already_complete":
                return {"status": "ok", "headline": {"draft_status": "complete"}, "recommendation": None,
                         "message": "draft_already_complete",
                         "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            if payload.get("message") == "no_remaining_open_draft_pick":
                return {"status": "ok", "headline": {"draft_status": payload.get("draft_status")},
                         "recommendation": None, "message": "no_remaining_open_draft_pick",
                         "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            return payload

        recommendation = core["recommendation"]
        ranked = core["ranked_candidates"]
        all_facts = core["all_candidate_facts"]
        primary_candidate = next((c for c in all_facts if recommendation and c["player_id"] == recommendation["player_id"]), None)

        headline = {
            "draft_status": core["draft_status"], "current_overall_pick": core["current_overall_pick"],
            "decision_pick": core["decision_pick"], "user_on_clock": core["user_on_clock"],
            "picks_until_decision": core["picks_until_decision"],
            "next_user_open_pick": core["next_user_open_pick"], "turn_span": core["turn_span"],
        }

        rec_out = None
        why_now = []
        if recommendation and primary_candidate is not None:
            why_now = _gldb_why_now(recommendation, primary_candidate)
            strategy_effect = primary_candidate["strategy_alignment"]
            rec_out = {"player_id": recommendation["player_id"], "name": recommendation["name"],
                        "position": recommendation["position"], "confidence": recommendation["recommendation_confidence"],
                        "primary_reason": recommendation["primary_reason"], "why_now": why_now,
                        "main_tradeoff": recommendation.get("main_tradeoff"), "strategy_effect": strategy_effect}

        alternatives = _gldb_build_alternatives(ranked, recommendation["player_id"] if recommendation else None)
        alternatives = alternatives[:max(0, top_n_val - 1)]

        board_pressure = _gldb_build_board_pressure(all_facts, core["recent_runs"], core["demand_by_position"])
        primary_position = recommendation["position"] if recommendation else None
        tier_cliffs = _gldb_select_tier_cliffs(all_facts, primary_position)

        my_build = {"counts": core["my_drafted_counts"], "open_starters": {
            pos: max(0, (core["starter_requirements"].get(pos, 0)) - core["my_drafted_counts"].get(pos, 0))
            for pos in _DS_CORE_POSITIONS}}

        next_turn = {"next_user_open_pick": core["next_user_open_pick"], "turn_span": core["turn_span"],
                      **_gldb_next_turn_groups(all_facts), "position_outlook": _gldb_position_outlook(all_facts)}

        path_comparison = core["path_comparison"][:_GLDB_MAX_PATHS]

        strategy_out = _gldb_strategy_summary(core["strategy_compat"], primary_candidate)
        rf_ctx = core.get("roster_feasibility") or {}
        roster_feasibility_out = {"binding": rf_ctx.get("binding"), "remaining_open_picks": rf_ctx.get("remaining_open_picks"),
                                     "mandatory_unfilled_slots": rf_ctx.get("mandatory_unfilled_slots")}

        warnings_full = core["warnings"]
        warnings_out = warnings_full[:_GLDB_MAX_WARNINGS]
        warnings_truncated = len(warnings_full) > _GLDB_MAX_WARNINGS

        limitations_full = [
            "ESPN's remaining pick-clock timer is not exposed and is not estimated here.",
            "Survival estimates are categorical (very_likely..very_unlikely), not calibrated probabilities.",
            "K/DST are excluded from the default recommendation pool - no equivalent FantasyPros analysis exists.",
        ]
        limitations_out = limitations_full[:_GLDB_MAX_LIMITATIONS]

        summary = _gldb_build_summary_sentence(headline, rec_out, why_now, alternatives)

        return {
            "status": "ok",
            "summary": summary,
            "headline": headline,
            "recommendation": rec_out,
            "alternatives": alternatives,
            "board_pressure": board_pressure,
            "tier_cliffs": tier_cliffs,
            "next_turn": next_turn,
            "my_build": my_build,
            "path_comparison": path_comparison,
            "strategy": strategy_out,
            "roster_feasibility": roster_feasibility_out,
            "warnings": warnings_out,
            "warnings_truncated": warnings_truncated,
            "data_limitations": limitations_out,
            "methodology": {"recommendation_source": "D3 analyze_draft_pick methodology (identical, unmodified)",
                              "board_source": "fresh ESPN raw draft state", "strategy_source": "saved D2/D2.1 strategy",
                              "fantasypros": "cache_only"},
            "drill_down": {"compare_players": "analyze_draft_pick", "full_board": "get_draft_board",
                             "rebuild_strategy": "prepare_draft_strategy"},
            "league": {"league_id": core["resolved_league_id"], "alias": core["resolved_alias"], "year": core["resolved_year"]},
            "board_state_hash": core.get("board_state_hash"),
            "completed_pick_count": core.get("completed_pick_count"),
            "board_advanced_during_call": core.get("board_advanced_during_call"),
            "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    except Exception as e:
        return _error_response("building live draft brief", e)

def main() -> None:
    """Run the project-owned FastMCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
