# Live Draft Setup

Use `get_live_draft_brief` immediately before each draft recommendation. It combines fresh ESPN draft state with cached FantasyPros evidence and an optional saved league-specific strategy. The tools provide snapshots when called; they do not start continuous monitoring or make draft selections.

## Configure one application home

The MCP host must launch `fantasy-football-mcp` with the application home containing the intended league registry, FantasyPros caches, and saved strategies. The default is `~/.fantasy-football-mcp`. If these files live elsewhere, set `FANTASY_FOOTBALL_MCP_HOME` in the host's server environment to that directory. A successful authentication does not imply that a league registry is configured.

Keep credentials server-side using the supported environment variables or credentials file. Do not put cookies or API keys in tool arguments or league registries. See [Configuration](CONFIGURATION.md) and [Provider Credentials](PROVIDER_CREDENTIALS.md). Reload the MCP connection after changing host configuration or server code.

## Prepare before the draft

1. Resolve the intended league and authenticated team with `get_league_context`. Pass the league ID and season explicitly during draft calls to avoid selecting another default league.
2. Call `get_draft_board` and compare the team, scoring bucket, pick order, and keeper state with ESPN's draft room.
3. Call `refresh_fantasypros_cache` for the required scoring bucket near draft time. Use `dry_run: true` to inspect the request plan and quota before refreshing. Rankings/projections have an eight-hour cache TTL and injuries three hours. Draft recommendation calls themselves make no live FantasyPros requests.
4. Run `prepare_draft_strategy` after refreshing the data and finalizing keeper assignments. Strategies using methodology version 1 should be rebuilt; version 2 accounts for league-wide replacement demand.
5. Verify successive board snapshots against actual draft-room picks during a rehearsal or the opening selections. Pre-draft response time does not establish in-progress ESPN update latency.

Example primary brief arguments, with a synthetic league ID:

```json
{"league_id": 123456, "year": 2026, "top_n": 5}
```

## Availability and retries

ESPN can retain full rosters before keeper processing. Draft tools therefore query active players independently of ordinary `FREEAGENT`/`WAIVERS` status, then exclude player IDs assigned to draft or keeper slots. The query has a 2,000-player bound and fails explicitly if it reaches that limit; it does not silently paginate or claim the truncated pool is complete.

While keeper assignments are unresolved, responses mark availability as provisional and lower recommendation confidence. Do not infer keeper identities from carried-over roster membership. A user-supplied keeper can inform discussion, but this tool contract does not add a manual keeper override to ESPN's data.

The brief rechecks the board after fetching candidates. A player taken during the call is excluded, and draft completion produces no recommendation. Incomplete final state produces an error instead of falling back to an older snapshot. Changed keeper assignments require a retry so strategy context can be rebuilt from the new state. Detailed comparisons also require a retry when their board changes during analysis.

Always retry an availability or revalidation error before choosing. Even a successful response can become outdated after it returns.

## Remaining limits

- ESPN's remaining countdown and pause state are not exposed or estimated. Use ESPN's draft room for these and for actual selections.
- FantasyPros scoring-bucket projections do not fully capture custom rules such as six-point passing touchdowns.
- Survival estimates are categorical judgments, not calibrated probabilities.
- K/DST normally have ESPN-only evidence and are outside the default ranked recommendation pool; roster-completion constraints can require them later.
- Saved strategy is advisory and may become stale as settings, keeper information, or provider datasets change. Inspect response warnings and cache freshness.

No tool names, provider credentials, or MCP annotation permissions change as part of this setup.
