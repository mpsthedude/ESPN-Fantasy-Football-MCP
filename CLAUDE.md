# CLAUDE.md

Maintainer/agent guidance for the current `fantasy-football-mcp` repository.

## Current Production Contract

- Package version source of truth: `pyproject.toml`
- Current release: `0.4.1`
- Current unified MCP surface on `main`: **52 tools**
- Production entry point: `fantasy_football_server.py`
- Console command: `fantasy-football-mcp`
- Runtime dependencies: MCP SDK + `requests`
- License: MIT
- `espn-api`: **retired; do not reintroduce**

Read these before large changes:

- `docs/ARCHITECTURE.md`
- `docs/CONFIGURATION.md`
- `docs/TOOL_REFERENCE.md`
- `docs/DEVELOPMENT.md`
- `SECURITY.md`
- `PROVENANCE.md`
- `LICENSE`

## Run / Test

```bash
uv sync --locked
uv run fantasy-football-mcp
uv run python -m unittest discover
```

The server uses stdio. Do not print normal diagnostics to stdout in a way that can contaminate MCP protocol traffic.

## Architecture Rules

### Unified entry point

`fantasy_football_server.py` is intentionally small. It:

- supports `SWID` as a compatibility alias when `ESPN_SWID` is absent,
- imports the shared MCP registry and project-owned ESPN session,
- primes configured ESPN credentials,
- registers SportsGameOdds tools,
- registers the cross-provider player market-context tool,
- registers ESPN discovery/sync tools,
- starts FastMCP.

Do not move broad business logic into this wrapper.

### ESPN is project-owned

Current ESPN path:

```text
ESPNSessionManager -> ESPNTransport -> espn_*_read parser -> MCP/business logic
```

Key modules:

- `espn_session.py` — in-memory credential/session state
- `espn_transport.py` — ESPN HTTP boundary
- `espn_reference.py` — project-defined factual ESPN identifiers
- `espn_league_read.py`
- `espn_roster_read.py`
- `espn_matchup_read.py`
- `espn_free_agent_read.py`
- `espn_historical_lineup_read.py`
- `espn_draft_read.py`
- `espn_snapshot_read.py`
- `espn_activity_read.py`

`espn_fantasy_server.py` remains the large established core MCP/analysis module, but it now consumes these project-owned boundaries.

Do **not**:

- add an `espn-api` dependency/import,
- restore a third-party cached `League` object as source of truth,
- restore the retired copied `espn_constants.py`,
- duplicate transport/request logic across business functions when it belongs in `ESPNTransport`.

### ESPN account discovery

`espn_league_discovery.py` owns:

- `discover_my_espn_leagues`
- `sync_my_espn_leagues`

The fan-profile interface is undocumented and must remain isolated. Candidate leagues must be verified through the Fantasy league endpoint. Synchronization must remain preview-first and require explicit confirmation for writes.

### FantasyPros

`fantasypros_client.py` owns provider auth, request behavior, quota/pacing, and local cache state.

No FantasyPros auth tool is required. API keys are resolved server-side.

### SportsGameOdds

- `sportsgameodds_client.py` — single provider request/normalization/cache boundary
- `sportsgameodds_analysis.py` — compact/fantasy market analysis
- `sportsgameodds_comparison.py` — pure cross-book market comparison
- `sportsgameodds_disagreement.py` — pure disagreement ranking for game markets and player props
- `sportsgameodds_tools.py` — **10** core MCP tools
- `sportsgameodds_disagreement_tools.py` — **2** bounded disagreement-discovery MCP tools

Preserve:

- read-only provider behavior,
- explicit opaque cursor pagination,
- provider-side team targeting when `team_id` is known,
- optional exact `event_id` targeting for props,
- 24-hour cache of non-secret team identity metadata only,
- live event/odds/prop reads.

Do not silently fetch every provider page. Disagreement discovery must remain descriptive rather than claiming expected value: game-market discovery ranks one explicit slate page, and player-prop discovery requires an exact event/player scope and ranks only within the same bet type.

### Cross-provider player market context

- `player_market_context.py` — pure composition of already-fetched sportsbook disagreement, FantasyPros cache evidence, and optional ESPN player context
- `player_market_context_tools.py` — registers **1** read-only orchestration tool, `get_player_prop_market_context`

Preserve the current bounded contract:

- NFL only while FantasyPros enrichment is NFL-specific,
- validate exact scope/scoring before spending SportsGameOdds quota,
- reuse the exact-event player-prop disagreement path,
- zero live FantasyPros requests; cache only,
- optional ESPN enrichment is at most one roster read and degrades gracefully,
- expose cache freshness/data-quality state,
- do not convert disagreement, injury/news, or expert-rank dispersion into a causal claim, EV score, fair-odds estimate, win probability, or wager recommendation.

## Tool Count Contract

Current unified server:

```text
37 ESPN/FantasyPros core
+ 2 ESPN discovery/sync
+ 12 SportsGameOdds
+ 1 cross-provider market context
= 52 tools
```

`tests/test_unified_mcp.py` protects the unified contract.

When intentionally adding/removing/renaming tools, update together:

1. implementation/registration,
2. tests,
3. `docs/TOOL_REFERENCE.md`,
4. README count/category summary,
5. this file if the architecture rule changes,
6. `CHANGELOG.md`.

The performance harness deliberately protects the 37-tool core registry. Do not mechanically change `performance_baseline.json` to match the unified 52-tool count.

## Configuration / State

Canonical application home:

```text
~/.fantasy-football-mcp/
```

Override:

```text
FANTASY_FOOTBALL_MCP_HOME
```

Canonical state:

```text
credentials.json          secret
league_registry.json      non-secret
commissioner_config.json  non-secret
draft_strategy/           generated non-secret strategy state
fp_cache/                  generated FantasyPros state
sgo_cache/                 generated SportsGameOdds team metadata
```

Supported credential environment names:

```text
ESPN_S2
ESPN_SWID
SWID
FANTASYPROS_API_KEY
SPORTSGAMEODDS_API_KEY
```

### ESPN credential rules

- canonical environment pair is `ESPN_S2` + `ESPN_SWID`,
- unified wrapper accepts `SWID` as compatibility alias,
- environment pair wins over file-backed pair,
- never combine half a pair from different sources,
- partial pair is an error,
- literal `ENV` is not an indirection token and must remain rejected,
- `authenticate()` with no args uses/reports configured auth,
- explicit override requires both values,
- `logout()` clears in-memory state until restart/re-prime.

## Freshness Rules

Do not solve freshness with one generic cache.

- current ESPN league/roster/matchup state: project-owned reads as needed,
- live draft: fresh stateless board reads,
- FantasyPros: local cache refreshed explicitly,
- SportsGameOdds team identity: 24-hour local cache,
- sportsbook events/odds/props: live,
- cross-provider market context: live sportsbook disagreement + cache-only FantasyPros + optional one-read ESPN roster context,
- registry/commissioner/draft strategy: persisted local state.

## Draft Architecture

Keep factual state and recommendation methodology separate:

1. `get_draft_board` — authoritative fresh board
2. `prepare_draft_strategy` — persisted strategy
3. `analyze_draft_pick` — live analysis + revalidation
4. `get_live_draft_brief` — orchestration brief

Saved strategy must not become the authoritative draft board.

## Commissioner Rules

Commissioner tooling is read/audit oriented.

`commissioner_config.json` is only a local eligibility allowlist. It does not grant ESPN permissions and does not authorize provider writes.

Keep investigation evidence bounded and factual.

## Security Rules

Never:

- commit real ESPN cookies or API keys,
- echo secrets in exceptions/tool responses/logs,
- put API keys in URLs,
- store secrets in registry/commissioner/draft/cache state,
- add tests requiring real credentials,
- expose provider account identifiers unnecessarily,
- contaminate MCP stdout with diagnostics.

Error/log paths that touch ESPN access failures deserve explicit redaction review.

## Testing

Full suite:

```bash
uv run python -m unittest discover
```

Tests should remain offline with synthetic credentials and mocked provider requests.

Normal CI covers:

- Ubuntu Python 3.12
- Ubuntu Python 3.13
- Windows Python 3.12
- fresh dependency resolution under `mcp<2`
- full unit suite
- wheel build/content audit
- clean wheel installation
- imports from installed `site-packages`
- installed MCP major-version gate
- explicit proof `espn-api` is absent
- unified console-entry startup outside source checkout

If packaging changes, update both `pyproject.toml` include list and `.github/workflows/ci.yml` required/import module lists.

## Performance Harness

`performance_regression.py` intentionally imports the 37-tool ESPN/FantasyPros core registry. The baseline is not the unified tool-count source of truth.

Regenerate `performance_baseline.json` only after an intentional calibration run using the harness acceptance path. Do not change it just to silence a regression or update a timestamp.

## PR / Migration Discipline

For complex migrations:

- break work into independently verifiable stages,
- use focused regression gates before broad CI,
- avoid large self-mutating bootstrap loops,
- inspect state after each meaningful mutation,
- do not assume a workflow succeeded because a commit exists,
- remove temporary bootstrap/debug workflows/scripts before final PR unless they are intentionally permanent,
- compare final branch to `main`,
- require normal CI on the final head,
- squash exploratory migration history when appropriate.

## Releases

`pyproject.toml` version + `CHANGELOG.md` must agree.

Release publishing is CI-gated. Normal `main` CI must succeed on the release commit before the publish workflow creates/updates the GitHub release and wheel asset.

Do not publish an unvalidated release directly.

## Branch Hygiene

The merged-branch cleanup workflow deletes same-repository feature branches after successful PR merge while preserving `main`.

## Documentation Contract

README is the landing page, not the exhaustive implementation reference.

Update the dedicated docs when their contract changes:

- architecture → `docs/ARCHITECTURE.md`
- environment/config/state → `docs/CONFIGURATION.md`
- MCP tools → `docs/TOOL_REFERENCE.md`
- development/CI/release → `docs/DEVELOPMENT.md`
- sportsbook behavior → `SPORTSGAMEODDS.md`
- security → `SECURITY.md`
- history/licensing boundary → `PROVENANCE.md`
- release notes → `CHANGELOG.md`

Historical audit documents should be marked resolved/superseded rather than silently rewritten as if old findings never existed.
