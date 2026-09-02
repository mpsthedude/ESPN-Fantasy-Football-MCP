#!/usr/bin/env python3
"""
Performance Regression Protection Suite - ESPN/FantasyPros core registry.

Detects future regressions (latency, FantasyPros disk-read collapse loss,
ContextVar cache leakage, quota drift, tool-count/schema drift, Tool #23
search-budget drift, ESPN network drift, memory/handle leaks) WITHOUT
modifying production source. All instrumentation is external monkeypatching,
always restored.

NEVER modifies espn_fantasy_server.py or fantasypros_client.py.
Only writes performance_baseline.json, and only in `baseline --accept`.

This harness intentionally imports espn_fantasy_server directly and protects
the 37-tool ESPN/FantasyPros core registry. The unified production wrapper
adds ESPN discovery and SportsGameOdds tools and is protected separately by
tests/test_unified_mcp.py and packaging CI.

Usage:
    uv run python performance_regression.py smoke
    uv run python performance_regression.py full [--json-report PATH]
    uv run python performance_regression.py soak
    uv run python performance_regression.py baseline [--accept]

    Optional overrides on any subcommand:
        --league-id INT   (default 123456789)
        --team-id INT     (default 1)
        --year INT        (default 2026)

Exit codes:
    0 = PASS
    1 = PASS_WITH_WARNINGS
    2 = FAIL (a real regression or safety invariant was violated)
    3 = TEST HARNESS / ENVIRONMENT ERROR (import failure, source file
        modified mid-run, instrumentation could not attach, etc.)
"""
import sys, os, asyncio, time, json, gc, threading, statistics, hashlib, argparse, datetime, functools

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(REPO_DIR, "espn_fantasy_server.py")
FPCLIENT_PATH = os.path.join(REPO_DIR, "fantasypros_client.py")
BASELINE_PATH = os.path.join(REPO_DIR, "performance_baseline.json")

sys.path.insert(0, REPO_DIR)

DEFAULT_LEAGUE_ID = 123456789
DEFAULT_TEAM_ID = 1
DEFAULT_YEAR = 2026

FP_READ_EXPECTED = 13
FP_READ_WARNING = 15
FP_READ_FAIL = 20
STAGE_B_BUDGET_EXPECTED = 60


class HarnessError(Exception):
    """Raised for environment/harness-level failures (exit code 3): import
    failure, production source mutated mid-run, instrumentation attach
    failure. NEVER raised for a genuine product performance regression -
    those are FAIL results (exit code 2) reported in the normal flow."""
    pass


# ---------------------------------------------------------------------
# Windows memory/handle instrumentation - stdlib ctypes only, no new deps
# ---------------------------------------------------------------------
def _make_proc_stats():
    if os.name != "nt":
        return lambda: {"rss_mb": None, "handles": None, "threads": threading.active_count(),
                          "gc_objects": len(gc.get_objects())}
    import ctypes
    import ctypes.wintypes as wt
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel32.GetProcessHandleCount.restype = wt.BOOL

    class PMC(ctypes.Structure):
        _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    proc_handle = kernel32.GetCurrentProcess()

    def _stats():
        c = PMC(); c.cb = ctypes.sizeof(PMC)
        psapi.GetProcessMemoryInfo(proc_handle, ctypes.byref(c), c.cb)
        cnt = wt.DWORD()
        kernel32.GetProcessHandleCount(proc_handle, ctypes.byref(cnt))
        return {"rss_mb": round(c.WorkingSetSize / 1024 / 1024, 2), "handles": cnt.value,
                "threads": threading.active_count(), "gc_objects": len(gc.get_objects())}
    return _stats

proc_stats = _make_proc_stats()


def file_stats(path):
    st = os.stat(path)
    with open(path, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    return {"size": st.st_size, "mtime": st.st_mtime, "sha256": h}


def load_credentials():
    # Keep development/performance tooling on the same project-owned
    # credential resolver used by production instead of maintaining a
    # separate ~/.orcha secret-file convention.
    import app_config

    try:
        resolved = app_config.resolve_espn_credentials()
    except app_config.ConfigError as exc:
        raise HarnessError(
            f"ESPN credentials unavailable via project resolver: {exc}"
        ) from exc
    if resolved is None:
        raise HarnessError(
            "ESPN credentials not found: configure ESPN_S2 + ESPN_SWID "
            "or app-home credentials.json"
        )
    espn_s2, swid, _source = resolved
    return espn_s2, swid


# ---------------------------------------------------------------------
# Pure classification functions - independently unit-testable, no I/O
# ---------------------------------------------------------------------
def classify_latency(measured_p50, baseline_p50, warning_ceiling, hard_ceiling):
    """Uses BOTH a relative baseline comparison AND absolute ceilings, per
    spec, so a 0.003s->0.007s micro-tool blip never fails on ratio alone."""
    if hard_ceiling is not None and measured_p50 > hard_ceiling:
        return "FAIL", f"{measured_p50:.3f}s exceeds hard ceiling {hard_ceiling}s"
    if baseline_p50 and baseline_p50 > 0:
        ratio = measured_p50 / baseline_p50
        abs_increase = measured_p50 - baseline_p50
        if ratio > 2.0 and hard_ceiling is not None and measured_p50 > hard_ceiling and abs_increase > 0.05:
            return "FAIL", f"{measured_p50:.3f}s is {ratio:.1f}x baseline {baseline_p50:.3f}s and exceeds hard ceiling"
        if ratio > 1.5 and abs_increase > 0.05:
            return "WARNING", f"{measured_p50:.3f}s is {ratio:.1f}x baseline {baseline_p50:.3f}s"
    if warning_ceiling is not None and measured_p50 > warning_ceiling:
        return "WARNING", f"{measured_p50:.3f}s exceeds warning ceiling {warning_ceiling}s"
    return "PASS", "within expected range"


def classify_fp_reads(measured, expected=FP_READ_EXPECTED, warning=FP_READ_WARNING, fail=FP_READ_FAIL):
    if measured > fail:
        return "FAIL", f"{measured} original FP reads exceeds FAIL threshold {fail} (expected ~{expected})"
    if measured > warning:
        return "WARNING", f"{measured} original FP reads exceeds WARNING threshold {warning} (expected ~{expected})"
    return "PASS", f"{measured} original FP reads (expected ~{expected})"


def check_contextvar_clean(ctx_value):
    return ctx_value is None


def check_quota_delta(before, after):
    return after == before


def check_tool_count(count, expected=35):
    return count == expected


def check_stage_b_budget(evaluation_budget, expected=STAGE_B_BUDGET_EXPECTED):
    if not evaluation_budget:
        return "FAIL", "evaluation_budget missing from search_summary"
    actual = evaluation_budget.get("max_full_evaluations")
    if actual != expected:
        return "FAIL", f"max_full_evaluations={actual}, expected {expected} - methodology drift"
    return "PASS", f"max_full_evaluations={actual}"


def classify_response_size(measured_bytes, baseline_bytes):
    if baseline_bytes is None or baseline_bytes == 0 or measured_bytes is None:
        return "PASS", "no baseline size for comparison"
    ratio = measured_bytes / baseline_bytes
    if ratio > 2.0:
        return "WARNING", f"{measured_bytes}B is {ratio:.1f}x baseline {baseline_bytes}B"
    return "PASS", f"{measured_bytes}B ({ratio:.1f}x baseline)"


# ---------------------------------------------------------------------
# Production module import (done here, deliberately, so any import
# failure is caught and reported as a HarnessError, not a crash).
# ---------------------------------------------------------------------
try:
    import espn_fantasy_server as srv
    import fantasypros_client as fp_client
except Exception as e:
    raise HarnessError(f"production import failure: {e}")


def quota_now():
    return fp_client.get_usage_summary()["requests_used_today"]


class Instrumentation:
    """External monkeypatch-based instrumentation. Wraps
    srv._GFB_ORIGINAL_FP_READ_CACHE (the TRUE frozen disk reader, captured
    once by Tool #25's own Phase-1 code), fp_client.build_player_intelligence,
    srv.api.get_league, and League.free_agents/scoreboard/box_scores if
    present. Always restores the exact previous callable on uninstall(),
    even under exception (caller must use try/finally or the context-
    manager form below)."""

    def __init__(self):
        self.original_reads = []
        self.bpi_calls = []
        self.get_league_calls = 0
        self.free_agents_calls = 0
        self.scoreboard_calls = 0
        self.box_scores_calls = 0
        self._installed = False

    def install(self):
        self._real_original = srv._GFB_ORIGINAL_FP_READ_CACHE
        def counting_original(dataset_key):
            self.original_reads.append(dataset_key)
            return self._real_original(dataset_key)
        srv._GFB_ORIGINAL_FP_READ_CACHE = counting_original

        self._real_bpi = fp_client.build_player_intelligence
        def counting_bpi(name, team=None, position=None, scoring=fp_client.DEFAULT_SCORING):
            self.bpi_calls.append((name, team, position, scoring))
            return self._real_bpi(name, team, position, scoring)
        fp_client.build_player_intelligence = counting_bpi
        srv.fp_client.build_player_intelligence = counting_bpi

        self._real_get_league = srv.api.get_league
        def counting_gl(*a, **kw):
            self.get_league_calls += 1
            return self._real_get_league(*a, **kw)
        srv.api.get_league = counting_gl

        self._real_free_agents = srv.League.free_agents
        def counting_fa(self_, *a, **kw):
            self.free_agents_calls += 1
            return self._real_free_agents(self_, *a, **kw)
        srv.League.free_agents = counting_fa

        self._real_scoreboard = getattr(srv.League, "scoreboard", None)
        if self._real_scoreboard:
            def counting_sb(self_, *a, **kw):
                self.scoreboard_calls += 1
                return self._real_scoreboard(self_, *a, **kw)
            srv.League.scoreboard = counting_sb

        self._real_box_scores = getattr(srv.League, "box_scores", None)
        if self._real_box_scores:
            def counting_bs(self_, *a, **kw):
                self.box_scores_calls += 1
                return self._real_box_scores(self_, *a, **kw)
            srv.League.box_scores = counting_bs

        self._installed = True

    def uninstall(self):
        if not self._installed:
            return
        srv._GFB_ORIGINAL_FP_READ_CACHE = self._real_original
        fp_client.build_player_intelligence = self._real_bpi
        srv.fp_client.build_player_intelligence = self._real_bpi
        srv.api.get_league = self._real_get_league
        srv.League.free_agents = self._real_free_agents
        if self._real_scoreboard:
            srv.League.scoreboard = self._real_scoreboard
        if self._real_box_scores:
            srv.League.box_scores = self._real_box_scores
        self._installed = False

    def snapshot(self):
        return {
            "original_reads": len(self.original_reads),
            "distinct_fp_keys": len(set(self.original_reads)),
            "bpi_calls": len(self.bpi_calls),
            "unique_bpi_players": len(set(self.bpi_calls)),
            "get_league_calls": self.get_league_calls,
            "free_agents_calls": self.free_agents_calls,
            "scoreboard_calls": self.scoreboard_calls,
            "box_scores_calls": self.box_scores_calls,
        }

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


# ---------------------------------------------------------------------
# Tool classes and per-tool absolute ceilings
# ---------------------------------------------------------------------
TOOL_CLASSES = {
    "get_league_info": "A", "get_team_roster": "A", "get_team_info": "A",
    "get_player_stats": "A", "get_league_standings": "A", "get_matchup_info": "A",
    "get_league_settings": "A", "get_draft_results": "A", "get_all_rosters": "A",
    "authenticate": "A", "logout": "A",
    "get_free_agents": "B", "get_league_snapshot": "B", "enrich_espn_free_agents": "B",
    "get_player_intelligence": "C", "get_consensus_rankings": "C", "get_adp": "C",
    "compare_players": "C",
    "rank_waiver_targets": "D", "analyze_my_team": "D", "evaluate_trade": "D",
    "find_trade_targets": "D", "optimize_lineup": "D",
    "get_fantasy_brief": "E",
    "refresh_fantasypros_cache": "SKIP",
    # Added 2026-08-15 for the 27-tool multi-league foundation surface.
    # Classified empirically (not guessed): get_league_context warm p50
    # ~2.6ms, single-league registry+ESPN lookup, matches the existing "A"
    # class (get_league_info/get_team_info) exactly. list_my_leagues warm
    # p50 ~3.4ms but COLD (all enabled registry leagues uncached) measured
    # 2.79s for 3 leagues - genuinely network-sensitive like the existing
    # "B" class (get_free_agents/get_league_snapshot/enrich_espn_free_agents),
    # and will scale with however many leagues are enabled in the registry.
    # Neither tool touches FantasyPros data (confirmed: quota delta 0 in
    # calibration) - no FP_READ_SENSITIVE_TOOLS entry needed, matching the
    # existing pattern for other zero-FP-read tools like get_team_roster.
    "list_my_leagues": "B",
    "get_league_context": "A",
    # Added 2026-08-15 for Commissioner Phase C1. Classified empirically:
    # warm p50 measured at 2.2ms (config guard + cached League lookup +
    # settings normalization, zero FantasyPros involvement) - identical
    # range to get_league_context/get_team_info/get_league_info, so it
    # matches the existing "A" class exactly, no new class introduced.
    "get_commissioner_context": "A",
    # Added 2026-08-15 for Commissioner Phase C2. Both empirically measured
    # (current-lineup/current-roster default path only - historical
    # box_scores is a separate, preseason-nondeterministic network path,
    # deliberately never used as the performance fixture): warm p50 3.1ms /
    # 2.7ms, identical range to get_commissioner_context/get_league_context.
    "commissioner_audit_lineups": "A",
    "commissioner_audit_rosters": "A",
    # Added 2026-08-15 for Commissioner Phase C3. Empirically measured
    # (default bounded limit=25 call only - never a large historical scan,
    # per explicit design decision, since unresolved players inside
    # recent_activity() can trigger hidden extra ESPN network calls via
    # player_info()): warm p50 100ms, matching class B (real network read),
    # not class A like get_commissioner_context/commissioner_audit_lineups
    # which reuse the already-cached League with zero extra network calls.
    "commissioner_audit_transactions": "B",
    # Added 2026-08-15 for Commissioner Phase C7. Empirically measured
    # (default team_id-only investigation, no historical week - avoids
    # forcing an expensive box_scores fetch as the performance fixture):
    # warm p50 130ms, one recent_activity call, zero box_scores/scoreboard/
    # player_info calls - matches class B (real network read).
    "commissioner_investigate": "B",
    # Added 2026-08-15 for Commissioner Phase C8. Empirically measured
    # (production defaults - inactivity_lookback_weeks NOT disabled, per
    # explicit design decision to represent real default UX): warm p50
    # 100ms, one recent_activity call, zero box_scores/scoreboard/
    # player_info calls in the current preseason state (current_week<=1
    # short-circuits historical lookback before any box_scores fetch, per
    # the documented insufficient_history rule) - matches class B.
    "get_commissioner_brief": "B",
    # Added 2026-08-15 for Draft Intelligence Phase D1. Empirically measured
    # AFTER a required performance fix (see get_draft_board's own inline
    # comment): warm p50 ~1.0s across 5 consecutive live calls. This is
    # driven by real ESPN network cost - ONE combined mDraftDetail+mRoster+
    # mTeam+mSettings GET (D0/D1's core fresh-draft-state contract) PLUS
    # league.free_agents(), which is itself a frozen espn-api method that
    # internally issues 3 further ESPN calls (kona_player_info, pro_schedule,
    # positional_ratings) - inherited, pre-existing behavior already used by
    # 3 other frozen tools (rank_waiver_targets, analyze_my_team,
    # find_trade_targets), not something D1 introduces. 4 total ESPN GETs
    # at ~1.0s matches class B exactly (get_free_agents/commissioner_audit_
    # transactions/get_commissioner_brief all class B for the same reason -
    # real, bounded, non-fanout network reads). Zero FantasyPros HTTP calls
    # (cache-only enrichment, quota delta 0 - confirmed empirically).
    "get_draft_board": "B",
    # Added 2026-08-15 for Draft Intelligence Phase D2. Empirically measured:
    # warm p50 ~1.9s with save_strategy=False (matches harness default fixture
    # to avoid repeated local file writes during smoke/full/soak). Cost is
    # dominated by ONE raw combined ESPN draft-state GET (same D1 contract)
    # plus reading 4 core-position FantasyPros rankings/projections caches
    # exactly once each (never per-player - same D1 perf-fix discipline).
    # Zero FantasyPros HTTP calls, zero quota impact. This is heavier than
    # get_draft_board's class B but still bounded and non-fanout - matches
    # get_fantasy_brief's class E (multi-domain aggregation over cached
    # data), not a new class.
    "prepare_draft_strategy": "E",
    # Draft Intelligence Phase D3 addition (2026-08-15). One fresh raw draft
    # fetch + D2 universe/VOR/tier build (cache-only FP) + candidate/survival/
    # path analysis. Measured warm p50 ~1.5-1.8s - class B (get_draft_board's
    # own bucket) with a slightly wider ceiling reflected in CRITICAL_TOOL_CEILINGS
    # is not needed since B's existing 2.0/4.0s ceiling already covers it.
    "analyze_draft_pick": "B",
    # Draft Intelligence Phase D4 addition (2026-08-15). Orchestrates one
    # fresh raw draft fetch + D3's own analytical primitives (same class B
    # bucket as get_draft_board/analyze_draft_pick) into a compact brief -
    # no new expensive work, response is materially SMALLER than D3's own.
    "get_live_draft_brief": "B",
}

CLASS_CEILINGS = {
    "A": {"warning": 1.0, "hard": 1.0},
    "B": {"warning": 2.0, "hard": 4.0},
    "C": {"warning": 1.0, "hard": 2.0},
    "D": {"warning": None, "hard": None},
    "E": {"warning": 5.0, "hard": 8.0},
}

CRITICAL_TOOL_CEILINGS = {
    "enrich_espn_free_agents": {"warning": 3.5, "hard": 5.0},
    "rank_waiver_targets": {"warning": 2.5, "hard": 4.0},
    "analyze_my_team": {"warning": 1.5, "hard": 3.0},
    "evaluate_trade": {"warning": 1.5, "hard": 3.0},
    "find_trade_targets": {"warning": 2.5, "hard": 4.0},
    "optimize_lineup": {"warning": 2.5, "hard": 4.0},
    "get_fantasy_brief": {"warning": 5.0, "hard": 8.0},
}

FP_READ_SENSITIVE_TOOLS = ("rank_waiver_targets", "analyze_my_team", "evaluate_trade", "find_trade_targets")

def get_ceiling(tool_name):
    if tool_name in CRITICAL_TOOL_CEILINGS:
        return CRITICAL_TOOL_CEILINGS[tool_name]
    cls = TOOL_CLASSES.get(tool_name, "D")
    return CLASS_CEILINGS.get(cls, {"warning": None, "hard": None})


# ---------------------------------------------------------------------
# Live fixture discovery (evaluate_trade needs a currently-valid package;
# NEVER hardcode ownership-sensitive player names in the baseline/harness).
# ---------------------------------------------------------------------
def discover_sample_player(league_id, team_id, year):
    """Uses the STRUCTURED get_all_rosters dict (never the stringified-dict
    text of get_team_roster) to find one currently-real player name."""
    rosters = asyncio.run(srv.get_all_rosters(league_id=league_id, year=year, detailed=True))
    if rosters.get("error"):
        return None
    for t in rosters.get("teams", []):
        if t.get("team_id") == team_id:
            for p in (t.get("roster") or t.get("players") or []):
                name = p.get("name") if isinstance(p, dict) else None
                if name:
                    return name
    return None


def discover_trade_fixture(league_id, team_id, year):
    result = asyncio.run(srv.find_trade_targets(league_id=league_id, team_id=team_id, limit=5,
                                                    max_package_size=2, year=year))
    if result.get("error"):
        return None, None, result.get("error")
    targets = result.get("trade_targets", [])
    if not targets:
        return None, None, "no live trade targets currently available"
    pt = targets[0].get("proposed_trade", {})
    players_out = pt.get("players_out")
    players_in = pt.get("players_in")
    if not players_out or not players_in:
        return None, None, "discovered target missing proposed_trade fields"
    return players_out, players_in, None


# ---------------------------------------------------------------------
# Core timed-and-instrumented single-tool runner
# ---------------------------------------------------------------------
def run_timed(coro_factory, reps=1, instrument=True):
    """Runs coro_factory() `reps` times (each a fresh asyncio.run). Returns
    dict with per-rep timings, per-rep original-FP-read counts (proves
    freshness across repeats), the LAST rep's full instrumentation
    snapshot (representative), quota before/after across the WHOLE run,
    and the ContextVar state immediately after the final rep."""
    timings = []
    last_result = None
    per_rep_reads = []
    per_rep_bpi = []
    snap = None
    quota_before = quota_now()
    for i in range(reps):
        inst = Instrumentation() if instrument else None
        if inst:
            inst.install()
        t0 = time.perf_counter()
        try:
            result = asyncio.run(coro_factory())
        except Exception as e:
            result = {"error": f"EXCEPTION: {type(e).__name__}: {e}"}
        elapsed = time.perf_counter() - t0
        if inst:
            snap = inst.snapshot()
            per_rep_reads.append(snap["original_reads"])
            per_rep_bpi.append(snap["bpi_calls"])
            inst.uninstall()
        timings.append(elapsed)
        last_result = result
    quota_after = quota_now()
    try:
        out_size = len(json.dumps(last_result, default=str)) if isinstance(last_result, (dict, list)) else \
                    (len(str(last_result)) if last_result is not None else None)
    except Exception:
        out_size = None
    last_error = last_result.get("error") if isinstance(last_result, dict) else None
    ctx_after = srv._GFB_FP_PARSED_CACHE_CONTEXT.get()
    return {
        "timings": timings, "error": last_error, "output_bytes": out_size,
        "fp_snapshot": snap, "per_rep_original_reads": per_rep_reads,
        "per_rep_bpi_calls": per_rep_bpi, "quota_before": quota_before, "quota_after": quota_after,
        "ctx_after": ctx_after, "last_result": last_result,
    }


def tool_stats(timings):
    if not timings:
        return {}
    s = sorted(timings)
    n = len(s)
    d = {"min": min(s), "max": max(s), "mean": statistics.mean(s), "p50": statistics.median(s), "n": n}
    d["p95"] = s[int(0.95 * (n - 1))] if n > 1 else s[0]
    return d


# ---------------------------------------------------------------------
# Factory builder - one safe, representative invocation per tool
# ---------------------------------------------------------------------
def build_factories(league_id, team_id, year, players_out=None, players_in=None):
    f = {
        "get_league_info": lambda: srv.get_league_info(league_id=league_id, year=year),
        "get_team_roster": lambda: srv.get_team_roster(league_id=league_id, team_id=team_id, year=year),
        "get_team_info": lambda: srv.get_team_info(league_id=league_id, team_id=team_id, year=year),
        "get_league_standings": lambda: srv.get_league_standings(league_id=league_id, year=year),
        "get_matchup_info": lambda: srv.get_matchup_info(league_id=league_id, week=None, year=year),
        "get_league_settings": lambda: srv.get_league_settings(league_id=league_id, year=year),
        "get_free_agents": lambda: srv.get_free_agents(league_id=league_id, week=None, position=None, size=50, year=year),
        "get_draft_results": lambda: srv.get_draft_results(league_id=league_id, year=year),
        "get_all_rosters": lambda: srv.get_all_rosters(league_id=league_id, year=year, detailed=True),
        "get_league_snapshot": lambda: srv.get_league_snapshot(league_id=league_id, year=year, free_agent_limit=25),
        "get_consensus_rankings": lambda: srv.get_consensus_rankings(position="QB", scoring="PPR", limit=20),
        "get_adp": lambda: srv.get_adp(position="RB", limit=20),
        "enrich_espn_free_agents": lambda: srv.enrich_espn_free_agents(league_id=league_id, position=None, limit=25, year=year),
        "rank_waiver_targets": lambda: srv.rank_waiver_targets(league_id=league_id, team_id=team_id, limit=10, year=year),
        "analyze_my_team": lambda: srv.analyze_my_team(league_id=league_id, team_id=team_id, year=year),
        "find_trade_targets": lambda: srv.find_trade_targets(league_id=league_id, team_id=team_id, limit=10, max_package_size=2, year=year),
        "optimize_lineup": lambda: srv.optimize_lineup(league_id=league_id, team_id=team_id, year=year),
        "get_fantasy_brief": lambda: srv.get_fantasy_brief(league_id=league_id, team_id=team_id, year=year),
        # 27-tool surface additions (2026-08-15) - both read-only, registry-
        # driven, make zero FantasyPros calls. get_league_context uses the
        # canonical fixture's league_id explicitly (deterministic, not the
        # registry default, so the measurement is stable regardless of
        # which alias is configured as default_league).
        "list_my_leagues": lambda: srv.list_my_leagues(year=year),
        "get_league_context": lambda: srv.get_league_context(league_id=league_id, year=year),
        # Commissioner Phase C1 addition (2026-08-15). Uses the canonical
        # fixture's league_id explicitly (this also matches the one configured
        # commissioner league) so this measurement is deterministic.
        "get_commissioner_context": lambda: srv.get_commissioner_context(league_id=league_id, year=year),
        # Commissioner Phase C2 additions (2026-08-15). Deliberately use the
        # DEFAULT current-lineup/current-roster path (week omitted) so
        # ordinary smoke/full/soak runs never depend on live historical
        # ESPN box-score availability, which is known to be nondeterministic
        # in preseason. Historical-lineup behavior is tested functionally,
        # separately from this performance harness.
        "commissioner_audit_lineups": lambda: srv.commissioner_audit_lineups(league_id=league_id, year=year),
        "commissioner_audit_rosters": lambda: srv.commissioner_audit_rosters(league_id=league_id, year=year),
        # Commissioner Phase C3 addition (2026-08-15). Deliberately uses a
        # SMALL bounded limit (25, matching ESPN's own internal page size)
        # so ordinary smoke/full/soak runs never perform a large historical
        # scan or trigger the hidden per-unresolved-player network calls
        # that a big scan could amplify. This is the intended lightweight
        # performance fixture, not a scan-coverage test.
        "commissioner_audit_transactions": lambda: srv.commissioner_audit_transactions(league_id=league_id, year=year, limit=25),
        # Commissioner Phase C7 addition (2026-08-15). Deliberately omits
        # `week` so the performance fixture never forces an expensive
        # historical box_scores() fetch (which itself can trigger further
        # hidden player_info() network calls for unresolved players) -
        # matches the same "small bounded default" principle as C3.
        "commissioner_investigate": lambda: srv.commissioner_investigate(league_id=league_id, year=year, team_id=team_id),
        # Commissioner Phase C8 addition (2026-08-15). Uses production
        # defaults (week=None, recent_activity_limit=25,
        # inactivity_lookback_weeks=3) - the brief's own internal
        # preseason/insufficient-history short-circuit keeps this bounded
        # without artificially disabling any real default behavior.
        "get_commissioner_brief": lambda: srv.get_commissioner_brief(league_id=league_id, year=year),
        # Draft Intelligence Phase D1 addition (2026-08-15). Uses the
        # canonical fixture's league_id explicitly and the default
        # top_available=50, matching production defaults exactly - no
        # artificial narrowing, since D1's own measured cost is dominated
        # by real ESPN network calls (see TOOL_CLASSES comment), not by
        # top_available's bounded enrichment loop.
        "get_draft_board": lambda: srv.get_draft_board(league_id=league_id, year=year, top_available=50),
        # Draft Intelligence Phase D2 addition (2026-08-15). save_strategy=False
        # is DELIBERATE for the harness fixture - avoids repeated local
        # .draft_strategy/ file writes during ordinary smoke/full/soak runs.
        # Persistence itself is tested functionally, separately from this
        # performance harness.
        "prepare_draft_strategy": lambda: srv.prepare_draft_strategy(league_id=league_id, year=year, save_strategy=False),
        "analyze_draft_pick": lambda: srv.analyze_draft_pick(league_id=league_id, year=year, top_n=5),
        "get_live_draft_brief": lambda: srv.get_live_draft_brief(league_id=league_id, year=year, top_n=5),
    }
    # Tools needing a discovered player name are filled in by the caller
    # after get_team_roster / find_trade_targets have run once, via the
    # keyword hooks below.
    def _get_player_intelligence(name):
        return lambda: srv.get_player_intelligence(player_name=name)
    def _get_player_stats(name):
        return lambda: srv.get_player_stats(league_id=league_id, player_name=name, year=year)
    def _compare_players(names):
        return lambda: srv.compare_players(players=names)
    def _authenticate(espn_s2, swid):
        return lambda: srv.authenticate(espn_s2=espn_s2, swid=swid)
    def _logout():
        return lambda: srv.logout()
    def _evaluate_trade(p_out, p_in):
        return lambda: srv.evaluate_trade(league_id=league_id, team_id=team_id, players_out=p_out, players_in=p_in, year=year)
    f["_get_player_intelligence"] = _get_player_intelligence
    f["_get_player_stats"] = _get_player_stats
    f["_compare_players"] = _compare_players
    f["_authenticate"] = _authenticate
    f["_logout"] = _logout
    f["_evaluate_trade"] = _evaluate_trade
    if players_out and players_in:
        f["evaluate_trade"] = _evaluate_trade(players_out, players_in)
    return f


# ---------------------------------------------------------------------
# Tool #25 per-domain FP-cache attribution (proves the nested-scope
# invariant: exactly ONE constituent domain pays the ~13 initial misses,
# the rest pay ZERO additional misses because they join the same outer
# per-brief cache).
# ---------------------------------------------------------------------
def run_brief_with_domain_attribution(league_id, team_id, year):
    real_analyze = srv.analyze_my_team
    real_optimize = srv.optimize_lineup
    real_waiver = srv.rank_waiver_targets
    real_trade = srv.find_trade_targets
    real_original = srv._GFB_ORIGINAL_FP_READ_CACHE

    current_domain = {"name": "gfb_output"}
    domain_times = {}
    domain_misses = {}
    first_load_domain = {}

    def tagged_original(dataset_key):
        d = current_domain["name"]
        if dataset_key not in first_load_domain:
            first_load_domain[dataset_key] = d
            domain_misses.setdefault(d, []).append(dataset_key)
        return real_original(dataset_key)

    async def d_analyze(*a, **kw):
        current_domain["name"] = "analyze_my_team"
        t0 = time.perf_counter(); r = await real_analyze(*a, **kw); domain_times["analyze_my_team"] = time.perf_counter() - t0; return r
    async def d_optimize(*a, **kw):
        current_domain["name"] = "optimize_lineup"
        t0 = time.perf_counter(); r = await real_optimize(*a, **kw); domain_times["optimize_lineup"] = time.perf_counter() - t0; return r
    async def d_waiver(*a, **kw):
        current_domain["name"] = "rank_waiver_targets"
        t0 = time.perf_counter(); r = await real_waiver(*a, **kw); domain_times["rank_waiver_targets"] = time.perf_counter() - t0; return r
    async def d_trade(*a, **kw):
        current_domain["name"] = "find_trade_targets"
        t0 = time.perf_counter(); r = await real_trade(*a, **kw); domain_times["find_trade_targets"] = time.perf_counter() - t0; return r

    srv.analyze_my_team = d_analyze
    srv.optimize_lineup = d_optimize
    srv.rank_waiver_targets = d_waiver
    srv.find_trade_targets = d_trade
    srv._GFB_ORIGINAL_FP_READ_CACHE = tagged_original

    t0 = time.perf_counter()
    try:
        brief_result = asyncio.run(srv.get_fantasy_brief(league_id=league_id, team_id=team_id, year=year))
    finally:
        srv.analyze_my_team = real_analyze
        srv.optimize_lineup = real_optimize
        srv.rank_waiver_targets = real_waiver
        srv.find_trade_targets = real_trade
        srv._GFB_ORIGINAL_FP_READ_CACHE = real_original
    total_elapsed = time.perf_counter() - t0

    total_misses = sum(len(v) for v in domain_misses.values())
    return {
        "total_elapsed": total_elapsed, "domain_times": domain_times,
        "domain_misses_count": {k: len(v) for k, v in domain_misses.items()},
        "total_original_misses": total_misses, "error": brief_result.get("error"),
        "ctx_after": srv._GFB_FP_PARSED_CACHE_CONTEXT.get(),
        "brief_status": brief_result.get("brief_status"),
    }


# ---------------------------------------------------------------------
# Baseline load / propose / save
# ---------------------------------------------------------------------
def load_baseline():
    if not os.path.exists(BASELINE_PATH):
        return None
    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def baseline_p50_for(baseline, tool_name):
    if not baseline:
        return None
    entry = baseline.get("tools", {}).get(tool_name)
    return entry.get("baseline_p50_seconds") if entry else None


def build_proposed_baseline(league_id, team_id, year, tool_count, per_tool_measurements):
    tools = {}
    for name, m in per_tool_measurements.items():
        cls = TOOL_CLASSES.get(name, "D")
        ceiling = get_ceiling(name)
        stats = m.get("stats", {})
        tools[name] = {
            "class": cls,
            "baseline_p50_seconds": round(stats.get("p50", 0.0), 4),
            "warning_p50_seconds": ceiling["warning"],
            "hard_p50_seconds": ceiling["hard"],
            "max_original_fp_reads": (m.get("fp_snapshot") or {}).get("original_reads"),
            "response_bytes": m.get("output_bytes"),
            "quota_delta": m.get("quota_after", 0) - m.get("quota_before", 0),
        }
    return {
        "version": 1,
        "tool_count": tool_count,
        "league_id": league_id, "team_id": team_id, "year": year,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "environment": {"python": sys.version.split()[0], "platform": sys.platform},
        "tools": tools,
    }


def save_baseline(baseline_dict):
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline_dict, f, indent=2)


# ---------------------------------------------------------------------
# Shared run-context object accumulates checks across a mode's execution
# ---------------------------------------------------------------------
class RunContext:
    def __init__(self):
        self.checks = []      # (name, status, detail)
        self.abort = False
        self.abort_reason = None
        self.per_tool = {}    # name -> measurement dict

    def check(self, name, status, detail=""):
        self.checks.append({"name": name, "status": status, "detail": detail})
        return status

    def abort_now(self, reason):
        self.abort = True
        self.abort_reason = reason

    def overall_status(self):
        if any(c["status"] == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c["status"] == "WARNING" for c in self.checks):
            return "PASS_WITH_WARNINGS"
        return "PASS"

    def exit_code(self):
        s = self.overall_status()
        return {"PASS": 0, "PASS_WITH_WARNINGS": 1, "FAIL": 2}[s]

    def counts(self):
        return {
            "pass": sum(1 for c in self.checks if c["status"] == "PASS"),
            "warning": sum(1 for c in self.checks if c["status"] == "WARNING"),
            "fail": sum(1 for c in self.checks if c["status"] == "FAIL"),
        }


def source_integrity_guard(before):
    """Call at the end of a run; raises HarnessError (exit 3) if either
    production file changed DURING this run - a harness/environment
    integrity violation, never treated as a normal regression."""
    after_server = file_stats(SERVER_PATH)
    after_fp = file_stats(FPCLIENT_PATH)
    if after_server != before["server"]:
        raise HarnessError(f"espn_fantasy_server.py changed during the run! before={before['server']} after={after_server}")
    if after_fp != before["fp_client"]:
        raise HarnessError(f"fantasypros_client.py changed during the run! before={before['fp_client']} after={after_fp}")


# ---------------------------------------------------------------------
# SMOKE MODE
# ---------------------------------------------------------------------
def mode_smoke(league_id, team_id, year):
    ctx = RunContext()
    source_before = {"server": file_stats(SERVER_PATH), "fp_client": file_stats(FPCLIENT_PATH)}

    espn_s2, swid = load_credentials()
    srv.api.store_credentials(srv.SESSION_ID, espn_s2, swid)

    tm = srv.mcp._tool_manager
    tool_count = len(tm._tools)
    if not check_tool_count(tool_count, 37):
        ctx.check("tool_count", "FAIL", f"registered={tool_count}, expected 37")
        ctx.abort_now(f"tool_count mismatch: {tool_count} != 37")
        return ctx, source_before
    ctx.check("tool_count", "PASS", f"{tool_count} tools registered")

    known_names = set(TOOL_CLASSES.keys())
    registered_names = set(tm._tools.keys())
    missing = known_names - registered_names
    extra = registered_names - known_names
    if missing or extra:
        ctx.check("tool_names", "FAIL", f"missing={missing} extra={extra}")
        ctx.abort_now("tool registry drift")
        return ctx, source_before
    ctx.check("tool_names", "PASS", "all expected tool names present, no extras")

    for name in ("_with_fp_parsed_cache_scope", "_GFB_FP_PARSED_CACHE_CONTEXT", "_GFB_ORIGINAL_FP_READ_CACHE"):
        ctx.check(f"infra_present:{name}", "PASS" if hasattr(srv, name) else "FAIL",
                    "present" if hasattr(srv, name) else "MISSING - cache infrastructure regressed")

    # refresh_fantasypros_cache schema + dry-run safety (never executed live
    # here - this tool is marked SKIP in TOOL_CLASSES and is never timed/
    # classified; this only proves its current registered schema still has
    # the expected registry-aware parameters, and that dry_run=True causes
    # ZERO FantasyPros HTTP requests / ZERO quota delta / ZERO cache writes).
    refresh_tool = tm._tools.get("refresh_fantasypros_cache")
    if refresh_tool is None:
        ctx.check("refresh_tool:schema_present", "FAIL", "refresh_fantasypros_cache not registered")
    else:
        expected_params = {"datasets", "positions", "scoring", "force", "allow_soft_limit_override", "dry_run"}
        actual_params = set(refresh_tool.parameters.get("properties", {}).keys())
        ctx.check("refresh_tool:schema_present", "PASS" if expected_params <= actual_params else "FAIL",
                    f"expected>=  {sorted(expected_params)}, actual={sorted(actual_params)}")
        q_before_refresh = quota_now()
        dry_run_result = asyncio.run(srv.refresh_fantasypros_cache(dry_run=True))
        q_after_refresh = quota_now()
        ctx.check("refresh_tool:dry_run_no_error", "PASS" if dry_run_result.get("status") == "dry_run" else "FAIL",
                    str(dry_run_result.get("status")))
        ctx.check("refresh_tool:dry_run_zero_quota", "PASS" if check_quota_delta(q_before_refresh, q_after_refresh) else "FAIL",
                    f"{q_before_refresh} -> {q_after_refresh}")

    for name in FP_READ_SENSITIVE_TOOLS:
        fn = tm._tools[name].fn
        has_wrap = hasattr(fn, "__wrapped__")
        ctx.check(f"decorator_present:{name}", "PASS" if has_wrap else "FAIL",
                    "has __wrapped__" if has_wrap else "MISSING - per-call cache scope decorator regressed")

    baseline = load_baseline()
    quota_before_all = quota_now()

    players_out, players_in, fixture_err = discover_trade_fixture(league_id, team_id, year)
    if fixture_err:
        ctx.check("trade_fixture_discovery", "WARNING", fixture_err)
    factories = build_factories(league_id, team_id, year, players_out, players_in)

    smoke_tools = ["get_league_info", "list_my_leagues", "get_league_context", "get_commissioner_context",
                    "commissioner_audit_lineups", "commissioner_audit_rosters", "commissioner_audit_transactions",
                    "commissioner_investigate", "get_commissioner_brief", "get_draft_board", "prepare_draft_strategy",
                    "get_free_agents", "rank_waiver_targets", "analyze_my_team",
                    "find_trade_targets", "optimize_lineup", "get_fantasy_brief"]
    if players_out and players_in:
        smoke_tools.insert(4, "evaluate_trade")

    for name in smoke_tools:
        if ctx.abort:
            break
        m = run_timed(factories[name], reps=1, instrument=True)
        ctx.per_tool[name] = m
        if m["error"]:
            ctx.check(f"{name}:no_error", "FAIL", str(m["error"]))
            continue
        ctx.check(f"{name}:no_error", "PASS", "")
        p50 = m["timings"][0]
        ceiling = get_ceiling(name)
        base_p50 = baseline_p50_for(baseline, name)
        status, detail = classify_latency(p50, base_p50, ceiling["warning"], ceiling["hard"])
        ctx.check(f"{name}:latency", status, f"{p50:.3f}s :: {detail}")
        if not check_contextvar_clean(m["ctx_after"]):
            ctx.check(f"{name}:contextvar_clean", "FAIL", f"leaked: {m['ctx_after']}")
            ctx.abort_now(f"ContextVar leak detected after {name}")
        else:
            ctx.check(f"{name}:contextvar_clean", "PASS", "")
        if not check_quota_delta(m["quota_before"], m["quota_after"]):
            ctx.check(f"{name}:quota_unchanged", "FAIL",
                        f"{m['quota_before']} -> {m['quota_after']} (unexpected FantasyPros request)")
            ctx.abort_now(f"unexpected quota delta caused by {name}")
        else:
            ctx.check(f"{name}:quota_unchanged", "PASS", "")
        if name in FP_READ_SENSITIVE_TOOLS or name == "get_fantasy_brief":
            reads = m["fp_snapshot"]["original_reads"]
            fstatus, fdetail = classify_fp_reads(reads)
            ctx.check(f"{name}:fp_reads", fstatus, fdetail)

    if not ctx.abort and "find_trade_targets" in ctx.per_tool and not ctx.per_tool["find_trade_targets"]["error"]:
        ss = ctx.per_tool["find_trade_targets"]["last_result"].get("search_summary", {})
        status, detail = check_stage_b_budget(ss.get("evaluation_budget"))
        ctx.check("tool23:stage_b_budget", status, detail)
        if ss.get("search_truncated") is not None:
            ctx.check("tool23:search_truncated_field_present", "PASS", str(ss.get("search_truncated")))

    if not ctx.abort:
        attr = run_brief_with_domain_attribution(league_id, team_id, year)
        if attr["error"]:
            ctx.check("brief_attribution:no_error", "FAIL", str(attr["error"]))
        else:
            ctx.check("brief_attribution:no_error", "PASS", "")
            total_misses = attr["total_original_misses"]
            if total_misses > FP_READ_FAIL:
                ctx.check("tool25:nested_cache_invariant", "FAIL",
                            f"{total_misses} total original reads across all domains "
                            f"(>{FP_READ_FAIL} - constituent tools likely stopped sharing the outer cache) "
                            f"attribution={attr['domain_misses_count']}")
                ctx.abort_now("Tool #25 nested cache invariant violated")
            else:
                ctx.check("tool25:nested_cache_invariant", "PASS",
                            f"{total_misses} total reads, attribution={attr['domain_misses_count']}")
            if not check_contextvar_clean(attr["ctx_after"]):
                ctx.check("tool25:contextvar_clean", "FAIL", f"leaked: {attr['ctx_after']}")
                ctx.abort_now("ContextVar leak detected after get_fantasy_brief")
            else:
                ctx.check("tool25:contextvar_clean", "PASS", "")

    return ctx, source_before


# ---------------------------------------------------------------------
# FULL MODE - benchmarks all 25 registered tools, plus concurrency smoke
# ---------------------------------------------------------------------
REPS_BY_CLASS = {"A": 3, "B": 3, "C": 3, "D": 5, "E": 5}

def mode_full(league_id, team_id, year):
    ctx = RunContext()
    source_before = {"server": file_stats(SERVER_PATH), "fp_client": file_stats(FPCLIENT_PATH)}

    espn_s2, swid = load_credentials()
    srv.api.store_credentials(srv.SESSION_ID, espn_s2, swid)

    tm = srv.mcp._tool_manager
    tool_count = len(tm._tools)
    if not check_tool_count(tool_count, 37):
        ctx.check("tool_count", "FAIL", f"registered={tool_count}, expected 37")
        ctx.abort_now(f"tool_count mismatch: {tool_count} != 37")
        return ctx, source_before
    ctx.check("tool_count", "PASS", f"{tool_count} tools registered")

    baseline = load_baseline()
    players_out, players_in, fixture_err = discover_trade_fixture(league_id, team_id, year)
    if fixture_err:
        ctx.check("trade_fixture_discovery", "WARNING", fixture_err)
    factories = build_factories(league_id, team_id, year, players_out, players_in)

    # Chain a few identity-dependent tools discovered live (player names etc.)
    # get_all_rosters returns a clean structured dict - never parse
    # get_team_roster's stringified-dict text output for this purpose.
    sample_player = discover_sample_player(league_id, team_id, year)
    if sample_player:
        factories["get_player_intelligence"] = factories["_get_player_intelligence"](sample_player)
        factories["get_player_stats"] = factories["_get_player_stats"](sample_player)
    if players_out:
        factories["compare_players"] = factories["_compare_players"](players_out[:2] if len(players_out) >= 2 else players_out + (players_in or []))
    factories["authenticate"] = factories["_authenticate"](espn_s2, swid)
    factories["logout"] = factories["_logout"]()

    ordered_tools = [n for n in TOOL_CLASSES if TOOL_CLASSES[n] != "SKIP"]
    # run logout LAST (it clears credentials) - move to end, restore creds after
    if "logout" in ordered_tools:
        ordered_tools.remove("logout")
        ordered_tools.append("logout")

    for name in ordered_tools:
        if ctx.abort:
            break
        if name not in factories:
            ctx.check(f"{name}:coverage", "WARNING", "no safe factory built (missing live identity input) - skipped")
            continue
        cls = TOOL_CLASSES[name]
        reps = REPS_BY_CLASS.get(cls, 3)
        m = run_timed(factories[name], reps=reps, instrument=True)
        ctx.per_tool[name] = m
        if m["error"]:
            ctx.check(f"{name}:no_error", "FAIL", str(m["error"]))
            continue
        ctx.check(f"{name}:no_error", "PASS", "")
        stats = tool_stats(m["timings"])
        m["stats"] = stats
        ceiling = get_ceiling(name)
        base_p50 = baseline_p50_for(baseline, name)
        status, detail = classify_latency(stats["p50"], base_p50, ceiling["warning"], ceiling["hard"])
        ctx.check(f"{name}:latency", status, f"p50={stats['p50']:.3f}s min={stats['min']:.3f}s max={stats['max']:.3f}s :: {detail}")
        if not check_contextvar_clean(m["ctx_after"]):
            ctx.check(f"{name}:contextvar_clean", "FAIL", f"leaked: {m['ctx_after']}")
            ctx.abort_now(f"ContextVar leak detected after {name}")
        else:
            ctx.check(f"{name}:contextvar_clean", "PASS", "")
        if not check_quota_delta(m["quota_before"], m["quota_after"]):
            ctx.check(f"{name}:quota_unchanged", "FAIL",
                        f"{m['quota_before']} -> {m['quota_after']} (unexpected FantasyPros request)")
            ctx.abort_now(f"unexpected quota delta caused by {name}")
            continue
        ctx.check(f"{name}:quota_unchanged", "PASS", "")
        if name in FP_READ_SENSITIVE_TOOLS or name == "get_fantasy_brief":
            reads = m["fp_snapshot"]["original_reads"]
            fstatus, fdetail = classify_fp_reads(reads)
            ctx.check(f"{name}:fp_reads", fstatus, fdetail)
            per_rep = m["per_rep_original_reads"]
            if len(per_rep) > 1 and per_rep[1] == 0:
                ctx.check(f"{name}:sequential_freshness", "FAIL",
                            f"rep1 showed 0 original reads - forbidden cross-request caching detected: {per_rep}")
                ctx.abort_now(f"cross-request FP cache leak detected in {name}")
            else:
                ctx.check(f"{name}:sequential_freshness", "PASS", f"per_rep_reads={per_rep}")
        if name in ("get_all_rosters", "find_trade_targets", "get_fantasy_brief"):
            base_bytes = None
            if baseline:
                base_bytes = baseline.get("tools", {}).get(name, {}).get("response_bytes")
            rstatus, rdetail = classify_response_size(m["output_bytes"], base_bytes)
            ctx.check(f"{name}:response_size", rstatus, rdetail)

    # Re-authenticate after logout ran, so subsequent checks (attribution, concurrency) work
    srv.api.store_credentials(srv.SESSION_ID, espn_s2, swid)

    if not ctx.abort and "find_trade_targets" in ctx.per_tool and not ctx.per_tool["find_trade_targets"]["error"]:
        ss = ctx.per_tool["find_trade_targets"]["last_result"].get("search_summary", {})
        status, detail = check_stage_b_budget(ss.get("evaluation_budget"))
        ctx.check("tool23:stage_b_budget", status, detail)

    if not ctx.abort:
        attr = run_brief_with_domain_attribution(league_id, team_id, year)
        if attr["error"]:
            ctx.check("brief_attribution:no_error", "FAIL", str(attr["error"]))
        else:
            ctx.check("brief_attribution:no_error", "PASS", "")
            total_misses = attr["total_original_misses"]
            if total_misses > FP_READ_FAIL:
                ctx.check("tool25:nested_cache_invariant", "FAIL",
                            f"{total_misses} total original reads (attribution={attr['domain_misses_count']})")
                ctx.abort_now("Tool #25 nested cache invariant violated")
            else:
                ctx.check("tool25:nested_cache_invariant", "PASS",
                            f"{total_misses} total reads, attribution={attr['domain_misses_count']}")
            ctx.check("tool25:contextvar_clean", "PASS" if check_contextvar_clean(attr["ctx_after"]) else "FAIL",
                        "" if check_contextvar_clean(attr["ctx_after"]) else f"leaked: {attr['ctx_after']}")

    if not ctx.abort:
        _run_concurrency_smoke(ctx, league_id, team_id, year)

    return ctx, source_before


def _run_concurrency_smoke(ctx, league_id, team_id, year):
    CTX = srv._GFB_FP_PARSED_CACHE_CONTEXT
    dict_ids = {}
    real_original = srv._GFB_ORIGINAL_FP_READ_CACHE
    def tagging_original(dataset_key):
        task = asyncio.current_task()
        tname = task.get_name() if task else "?"
        dict_ids.setdefault(tname, set()).add(id(CTX.get()))
        return real_original(dataset_key)
    srv._GFB_ORIGINAL_FP_READ_CACHE = tagging_original

    async def named(label, coro):
        asyncio.current_task().set_name(label)
        return await coro

    q0 = quota_now()
    async def two_briefs():
        return await asyncio.gather(
            named("brief1", srv.get_fantasy_brief(league_id=league_id, team_id=team_id, year=year)),
            named("brief2", srv.get_fantasy_brief(league_id=league_id, team_id=team_id, year=year)),
        )
    r1, r2 = asyncio.run(two_briefs())
    q1 = quota_now()
    srv._GFB_ORIGINAL_FP_READ_CACHE = real_original
    ok = (r1.get("error") is None and r2.get("error") is None and
          dict_ids.get("brief1") != dict_ids.get("brief2") and check_quota_delta(q0, q1))
    ctx.check("concurrency:two_briefs_isolated", "PASS" if ok else "FAIL",
                f"dict_ids={dict_ids} quota={q0}->{q1}")
    ctx.check("concurrency:contextvar_clean", "PASS" if check_contextvar_clean(CTX.get()) else "FAIL", "")

    dict_ids.clear()
    srv._GFB_ORIGINAL_FP_READ_CACHE = tagging_original
    q0 = quota_now()
    async def mixed_pair():
        return await asyncio.gather(
            named("analyze", srv.analyze_my_team(league_id=league_id, team_id=team_id, year=year)),
            named("trade", srv.find_trade_targets(league_id=league_id, team_id=team_id, limit=10, max_package_size=2, year=year)),
        )
    r3, r4 = asyncio.run(mixed_pair())
    q1 = quota_now()
    srv._GFB_ORIGINAL_FP_READ_CACHE = real_original
    ok2 = (r3.get("error") is None and r4.get("error") is None and
           dict_ids.get("analyze") != dict_ids.get("trade") and check_quota_delta(q0, q1))
    ctx.check("concurrency:mixed_pair_isolated", "PASS" if ok2 else "FAIL",
                f"dict_ids={dict_ids} quota={q0}->{q1}")
    ctx.check("concurrency:contextvar_clean_2", "PASS" if check_contextvar_clean(CTX.get()) else "FAIL", "")

    # Added 2026-08-15: proportional concurrency check for the two new
    # registry/context tools. These never touch the FP parsed-cache scope,
    # so the isolation proof here is League-cache integrity, not dict_id
    # tagging - concurrent calls must not corrupt the shared League cache
    # or return the wrong league's identity to either caller.
    cache_size_before = len(srv.api.leagues)
    q0 = quota_now()
    async def context_pair():
        return await asyncio.gather(
            srv.get_league_context(league_id=league_id, year=year),
            srv.list_my_leagues(year=year),
        )
    r5, r6 = asyncio.run(context_pair())
    q1 = quota_now()
    cache_size_after = len(srv.api.leagues)
    # get_league_context returns league_id at the TOP level (not nested
    # under "league" - confirmed by direct inspection), e.g.
    # {"alias":..., "league_id": 123456789, "league": {"name":...,"scoring_bucket":...}, "my_team": {...}}.
    ok3 = (r5.get("error") is None and r5.get("league_id") == league_id and
           isinstance(r6.get("leagues"), list) and cache_size_after <= cache_size_before + 1 and
           check_quota_delta(q0, q1))
    ctx.check("concurrency:context_tools_no_cache_corruption", "PASS" if ok3 else "FAIL",
                f"cache {cache_size_before}->{cache_size_after} quota={q0}->{q1} league_id_match={r5.get('league_id') == league_id}")


# ---------------------------------------------------------------------
# SOAK MODE - 20x get_fantasy_brief + 10x each of the 4 critical tools
# ---------------------------------------------------------------------
def mode_soak(league_id, team_id, year):
    ctx = RunContext()
    source_before = {"server": file_stats(SERVER_PATH), "fp_client": file_stats(FPCLIENT_PATH)}

    espn_s2, swid = load_credentials()
    srv.api.store_credentials(srv.SESSION_ID, espn_s2, swid)

    tm = srv.mcp._tool_manager
    tool_count = len(tm._tools)
    if not check_tool_count(tool_count, 37):
        ctx.check("tool_count", "FAIL", f"registered={tool_count}, expected 37")
        ctx.abort_now(f"tool_count mismatch: {tool_count} != 37")
        return ctx, source_before
    ctx.check("tool_count", "PASS", f"{tool_count} tools registered")

    players_out, players_in, fixture_err = discover_trade_fixture(league_id, team_id, year)
    factories = build_factories(league_id, team_id, year, players_out, players_in)

    gc.collect()
    soak_log = []
    quota_start = quota_now()

    for i in range(20):
        if ctx.abort:
            break
        t0 = time.perf_counter()
        r = asyncio.run(srv.get_fantasy_brief(league_id=league_id, team_id=team_id, year=year))
        elapsed = time.perf_counter() - t0
        entry = {"tool": "get_fantasy_brief", "iter": i, "elapsed": elapsed, "error": r.get("error"),
                  "proc": proc_stats(), "ctx_after": srv._GFB_FP_PARSED_CACHE_CONTEXT.get(),
                  "league_cache": len(srv.api.leagues), "quota": quota_now()}
        soak_log.append(entry)
        if not check_contextvar_clean(entry["ctx_after"]):
            ctx.check("soak:contextvar_leak", "FAIL", f"leaked at brief iter {i}: {entry['ctx_after']}")
            ctx.abort_now(f"ContextVar leak at brief soak iter {i}")
        if not check_quota_delta(quota_start, entry["quota"]):
            ctx.check("soak:quota_unchanged", "FAIL", f"quota moved during brief soak: {quota_start} -> {entry['quota']} at iter {i}")
            ctx.abort_now(f"quota drift during brief soak at iter {i}")

    critical_tools = ["rank_waiver_targets", "analyze_my_team", "find_trade_targets"] + \
                       (["evaluate_trade"] if (players_out and players_in) else [])
    for name in critical_tools:
        if ctx.abort:
            break
        for i in range(10):
            if ctx.abort:
                break
            t0 = time.perf_counter()
            r = asyncio.run(factories[name]())
            elapsed = time.perf_counter() - t0
            entry = {"tool": name, "iter": i, "elapsed": elapsed, "error": r.get("error"),
                      "proc": proc_stats(), "ctx_after": srv._GFB_FP_PARSED_CACHE_CONTEXT.get(),
                      "league_cache": len(srv.api.leagues), "quota": quota_now()}
            soak_log.append(entry)
            if not check_contextvar_clean(entry["ctx_after"]):
                ctx.check("soak:contextvar_leak", "FAIL", f"leaked at {name} iter {i}: {entry['ctx_after']}")
                ctx.abort_now(f"ContextVar leak at {name} soak iter {i}")
            if not check_quota_delta(quota_start, entry["quota"]):
                ctx.check("soak:quota_unchanged", "FAIL", f"quota moved during {name} soak: {quota_start} -> {entry['quota']} at iter {i}")
                ctx.abort_now(f"quota drift during {name} soak at iter {i}")

    gc.collect()
    if not any(c["name"] == "soak:contextvar_leak" for c in ctx.checks):
        ctx.check("soak:contextvar_leak", "PASS", "no leak across all iterations")
    if not any(c["name"] == "soak:quota_unchanged" for c in ctx.checks):
        ctx.check("soak:quota_unchanged", "PASS", f"quota stable at {quota_start} throughout")

    all_errors = [e for e in soak_log if e["error"]]
    ctx.check("soak:all_calls_error_free", "PASS" if not all_errors else "FAIL",
                "" if not all_errors else f"{len(all_errors)} calls errored")

    brief_series = [e for e in soak_log if e["tool"] == "get_fantasy_brief"]
    if brief_series:
        timings = [e["elapsed"] for e in brief_series]
        first5 = statistics.mean(timings[:5])
        middle = statistics.mean(timings[5:15]) if len(timings) >= 15 else None
        last5 = statistics.mean(timings[-5:])
        ctx.check("soak:brief_latency_trend", "PASS" if last5 < first5 * 2.0 + 0.5 else "WARNING",
                    f"first5={first5:.2f}s middle10={middle if middle is None else round(middle,2)}s last5={last5:.2f}s "
                    f"p50={statistics.median(timings):.2f}s min={min(timings):.2f}s max={max(timings):.2f}s")

    rss_series = [e["proc"]["rss_mb"] for e in soak_log if e["proc"]["rss_mb"] is not None]
    handle_series = [e["proc"]["handles"] for e in soak_log if e["proc"]["handles"] is not None]
    if rss_series:
        ctx.check("soak:memory_trend", "PASS" if rss_series[-1] < rss_series[0] * 2.0 + 10 else "WARNING",
                    f"rss {rss_series[0]}MB -> {rss_series[-1]}MB (min={min(rss_series)} max={max(rss_series)})")
    if handle_series:
        ctx.check("soak:handle_trend", "PASS" if handle_series[-1] <= handle_series[0] + 20 else "WARNING",
                    f"handles {handle_series[0]} -> {handle_series[-1]}")

    ctx.soak_log = soak_log
    return ctx, source_before


# ---------------------------------------------------------------------
# BASELINE MODE - measures current performance, proposes a new baseline,
# NEVER writes performance_baseline.json unless --accept is passed.
# ---------------------------------------------------------------------
def mode_baseline(league_id, team_id, year, accept=False):
    ctx = RunContext()
    source_before = {"server": file_stats(SERVER_PATH), "fp_client": file_stats(FPCLIENT_PATH)}

    espn_s2, swid = load_credentials()
    srv.api.store_credentials(srv.SESSION_ID, espn_s2, swid)

    tm = srv.mcp._tool_manager
    tool_count = len(tm._tools)
    if not check_tool_count(tool_count, 37):
        ctx.check("tool_count", "FAIL", f"registered={tool_count}, expected 37")
        ctx.abort_now(f"tool_count mismatch: {tool_count} != 37")
        return ctx, source_before, None, None

    old_baseline = load_baseline()
    players_out, players_in, fixture_err = discover_trade_fixture(league_id, team_id, year)
    factories = build_factories(league_id, team_id, year, players_out, players_in)

    sample_player = discover_sample_player(league_id, team_id, year)
    if sample_player:
        factories["get_player_intelligence"] = factories["_get_player_intelligence"](sample_player)
        factories["get_player_stats"] = factories["_get_player_stats"](sample_player)
    if players_out:
        factories["compare_players"] = factories["_compare_players"](players_out[:2] if len(players_out) >= 2 else players_out + (players_in or []))

    ordered_tools = [n for n in TOOL_CLASSES if TOOL_CLASSES[n] != "SKIP" and n != "logout" and n != "authenticate"]
    per_tool = {}
    for name in ordered_tools:
        if name not in factories:
            continue
        cls = TOOL_CLASSES[name]
        reps = REPS_BY_CLASS.get(cls, 3)
        m = run_timed(factories[name], reps=reps, instrument=True)
        if m["error"]:
            ctx.check(f"{name}:no_error", "FAIL", str(m["error"]))
            continue
        m["stats"] = tool_stats(m["timings"])
        per_tool[name] = m
        ctx.check(f"{name}:measured", "PASS", f"p50={m['stats']['p50']:.3f}s")

    new_baseline = build_proposed_baseline(league_id, team_id, year, tool_count, per_tool)

    comparisons = []
    if old_baseline:
        for name in CRITICAL_TOOL_CEILINGS:
            old_p50 = old_baseline.get("tools", {}).get(name, {}).get("baseline_p50_seconds")
            new_p50 = new_baseline["tools"].get(name, {}).get("baseline_p50_seconds")
            if old_p50 is not None and new_p50 is not None and old_p50 > 0:
                pct = ((new_p50 - old_p50) / old_p50) * 100
                comparisons.append({"tool": name, "old": old_p50, "new": new_p50, "pct_change": pct})

    return ctx, source_before, new_baseline, {"old_baseline": old_baseline, "comparisons": comparisons, "accept": accept}


def print_baseline_report(new_baseline, extra):
    print("\n" + "=" * 60)
    print("PROPOSED PERFORMANCE BASELINE")
    print("=" * 60)
    if extra["old_baseline"] is None:
        print("(no existing baseline file - this will be the FIRST calibration)")
    else:
        print("\nCRITICAL BASELINE CHANGES")
        print("-" * 40)
        for c in extra["comparisons"]:
            sign = "+" if c["pct_change"] >= 0 else ""
            flag = "  <-- WARNING: >25% slower" if c["pct_change"] > 25 else ""
            print(f"{c['tool']}:")
            print(f"  old {c['old']:.3f}s")
            print(f"  new {c['new']:.3f}s")
            print(f"  {sign}{c['pct_change']:.1f}%{flag}")
    print("\nFull proposed tool table:")
    for name, entry in sorted(new_baseline["tools"].items()):
        print(f"  {name:28s} class={entry['class']}  p50={entry['baseline_p50_seconds']:.3f}s  "
              f"fp_reads<={entry['max_original_fp_reads']}  bytes={entry['response_bytes']}")
    if extra["accept"]:
        save_baseline(new_baseline)
        print(f"\n>>> BASELINE WRITTEN to {BASELINE_PATH} <<<")
    else:
        print(f"\n(--accept not supplied - {BASELINE_PATH} was NOT modified)")


# ---------------------------------------------------------------------
# Terminal report printer (smoke/full/soak)
# ---------------------------------------------------------------------
def print_report(mode_name, ctx, elapsed, quota_before_all=None, quota_after_all=None):
    status = ctx.overall_status()
    counts = ctx.counts()
    print(f"\nPERFORMANCE REGRESSION ({mode_name}): {status}")
    if ctx.abort:
        print(f"ABORTED EARLY: {ctx.abort_reason}")
    print(f"\n{len(srv.mcp._tool_manager._tools)} tools registered\n")
    if mode_name == "soak":
        print("Soak summary:")
        for c in ctx.checks:
            if c["name"].startswith("soak:"):
                print(f"  {c['status']:8s} {c['name']:30s} {c['detail']}")
        print()
    print("Critical:")
    for name in ("analyze_my_team", "rank_waiver_targets", "evaluate_trade", "find_trade_targets",
                  "optimize_lineup", "get_fantasy_brief"):
        m = ctx.per_tool.get(name)
        if not m:
            continue
        stats = m.get("stats") or tool_stats(m["timings"])
        p50 = stats.get("p50", m["timings"][0] if m["timings"] else 0)
        reads = (m.get("fp_snapshot") or {}).get("original_reads")
        lat_status = next((c["status"] for c in ctx.checks if c["name"] == f"{name}:latency"), "?")
        is_fp_checked = name in FP_READ_SENSITIVE_TOOLS or name == "get_fantasy_brief"
        read_txt = f"  FP reads {reads}/{FP_READ_FAIL}" if (is_fp_checked and reads is not None) else ""
        print(f"  {lat_status:8s} {name:22s} {p50:.2f}s{read_txt}")
    print(f"\nQuota: {quota_before_all} -> {quota_after_all}" if quota_before_all is not None else "")
    print(f"Context leaks: {sum(1 for c in ctx.checks if 'contextvar' in c['name'] and c['status'] == 'FAIL')}")
    print(f"Failures: {counts['fail']}")
    print(f"Warnings: {counts['warning']}")
    print(f"Elapsed: {elapsed:.1f}s")
    if counts["fail"] or counts["warning"]:
        print("\nNon-PASS details:")
        for c in ctx.checks:
            if c["status"] != "PASS":
                print(f"  [{c['status']}] {c['name']} :: {c['detail']}")


def build_json_report(mode_name, ctx, elapsed, source_before, league_id, team_id, year):
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "mode": mode_name,
        "environment": {"python": sys.version.split()[0], "platform": sys.platform},
        "league_id": league_id, "team_id": team_id, "year": year,
        "source_hashes": source_before,
        "tool_count": len(srv.mcp._tool_manager._tools),
        "elapsed_seconds": elapsed,
        "overall_status": ctx.overall_status(),
        "exit_code": ctx.exit_code(),
        "checks": ctx.checks,
        "per_tool": {
            name: {
                "timings": m["timings"], "stats": m.get("stats") or tool_stats(m["timings"]),
                "error": m["error"], "output_bytes": m["output_bytes"],
                "fp_snapshot": m.get("fp_snapshot"), "quota_before": m["quota_before"], "quota_after": m["quota_after"],
            } for name, m in ctx.per_tool.items()
        },
        "soak_log": getattr(ctx, "soak_log", None),
    }


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Performance Regression Protection Suite for the ESPN Fantasy Football MCP server. "
                     "Read-only against production source; only 'baseline --accept' writes performance_baseline.json.")
    parser.add_argument("mode", choices=["smoke", "full", "soak", "baseline"])
    parser.add_argument("--league-id", type=int, default=DEFAULT_LEAGUE_ID)
    parser.add_argument("--team-id", type=int, default=DEFAULT_TEAM_ID)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--json-report", type=str, default=None, help="Write full machine-readable results to PATH")
    parser.add_argument("--accept", action="store_true", help="baseline mode only: write performance_baseline.json")
    args = parser.parse_args()

    try:
        t0 = time.perf_counter()

        if args.mode == "smoke":
            ctx, source_before = mode_smoke(args.league_id, args.team_id, args.year)
        elif args.mode == "full":
            ctx, source_before = mode_full(args.league_id, args.team_id, args.year)
        elif args.mode == "soak":
            ctx, source_before = mode_soak(args.league_id, args.team_id, args.year)
        elif args.mode == "baseline":
            ctx, source_before, new_baseline, extra = mode_baseline(args.league_id, args.team_id, args.year, accept=args.accept)
            elapsed = time.perf_counter() - t0
            if ctx.abort:
                print(f"\nBASELINE ABORTED: {ctx.abort_reason}")
                source_integrity_guard(source_before)
                return 2
            print_baseline_report(new_baseline, extra)
            source_integrity_guard(source_before)
            if args.json_report:
                with open(args.json_report, "w", encoding="utf-8") as f:
                    json.dump({"mode": "baseline", "proposed_baseline": new_baseline, **extra}, f, indent=2, default=str)
                print(f"\nJSON report written to {args.json_report}")
            return 0

        elapsed = time.perf_counter() - t0
        print_report(args.mode, ctx, elapsed)
        source_integrity_guard(source_before)

        if args.json_report:
            report = build_json_report(args.mode, ctx, elapsed, source_before, args.league_id, args.team_id, args.year)
            with open(args.json_report, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, default=str)
            print(f"\nJSON report written to {args.json_report}")

        return ctx.exit_code()

    except HarnessError as e:
        print(f"\nHARNESS/ENVIRONMENT ERROR: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
