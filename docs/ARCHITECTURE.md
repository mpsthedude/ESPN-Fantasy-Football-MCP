# Architecture

This document describes the current production architecture of `fantasy-football-mcp` after the 2026 ESPN ownership and clean-provenance workstreams.

## Design Goals

The server is designed around a few durable constraints:

1. **One MCP connection.** ESPN, FantasyPros, SportsGameOdds, and cross-provider analysis tools are registered onto one FastMCP server.
2. **Project-owned ESPN boundary.** ESPN HTTP transport, credential/session lifecycle, payload parsing, and domain translation are owned by this repository.
3. **Local-first state.** Secrets, registries, strategy artifacts, and caches live on the user's machine.
4. **Read-only provider integrations by default.** SportsGameOdds is strictly read-only; commissioner tooling audits ESPN data rather than granting provider write authority.
5. **Fresh data where freshness matters.** Current ESPN state and sportsbook markets are fetched through bounded provider reads instead of process-lifetime third-party object graphs.
6. **Explicit provider cost.** Pagination and broad market reads are intentionally bounded rather than silently draining provider pages.
7. **Secret-safe diagnostics.** Credential values should never be serialized into normal tool responses or logs.
8. **Evidence stays distinguishable.** Cross-provider analysis keeps sportsbook disagreement, cached fantasy intelligence, optional league evidence, and data quality separate instead of collapsing them into an unsupported edge score.

## Production Entry Point

`fantasy_football_server.py` is the packaged production entry point.

It performs small orchestration responsibilities:

1. Accept `SWID` as a host-compatibility alias when `ESPN_SWID` is absent.
2. Import the shared FastMCP registry and active ESPN session state from `espn_fantasy_server.py`.
3. Prime the project-owned ESPN session from server-side configuration.
4. Register SportsGameOdds, cross-provider market-context, and ESPN account-discovery tools onto the shared MCP registry.

The console command is defined in `pyproject.toml`:

```text
fantasy-football-mcp = fantasy_football_server:main
```

The unified server exposes 52 tools. The underlying ESPN/FantasyPros core registry contains 37 tools; ESPN discovery/sync adds 2, SportsGameOdds adds 12, and cross-provider player market context adds 1.

## High-Level Data Flow

```text
MCP host
  |
  v
fantasy_football_server.py
  |
  +--> espn_fantasy_server.py ---------------------------+
  |      |                                               |
  |      +--> espn_session.py --> espn_transport.py --> ESPN
  |      |
  |      +--> espn_*_read.py --> project domain/results
  |      |
  |      +--> fantasypros_client.py ------------------> FantasyPros
  |      |
  |      +--> league_registry.py / commissioner_config.py
  |      +--> draft_strategy_store.py
  |
  +--> espn_league_discovery.py --> ESPN discovery + verification
  |
  +--> sportsgameodds_tools.py
  |       |
  |       +--> sportsgameodds_client.py -------------> SportsGameOdds
  |       +--> sportsgameodds_analysis.py
  |       +--> sportsgameodds_comparison.py
  |
  +--> sportsgameodds_disagreement_tools.py
  |       +--> sportsgameodds_disagreement.py
  |
  +--> player_market_context_tools.py
          +--> exact-event SGO disagreement path
          +--> fantasypros_client.py (cache only)
          +--> optional ESPN roster read
          +--> player_market_context.py
```

## ESPN Boundary

### Session state — `espn_session.py`

`ESPNSessionManager` owns the active in-memory ESPN credential context.

It:

- resolves configured credentials once for the logical production session,
- stores explicit in-memory overrides when intentionally supplied,
- rejects literal `ENV` placeholder credentials,
- clears active credentials on logout,
- constructs project-owned `ESPNTransport` objects.

It does **not** construct or cache third-party league objects.

### HTTP transport — `espn_transport.py`

`ESPNTransport` is the provider request boundary for ESPN Fantasy reads. Higher-level modules ask it for bounded league, season, player, or communication payloads and receive raw JSON-like data.

Transport responsibilities belong here rather than in business logic:

- endpoint construction,
- authenticated cookies,
- request headers/views/filters,
- timeout/error handling,
- safe access-error translation.

### Payload readers — `espn_*_read.py`

Project-owned read modules translate ESPN payloads into the data needed by MCP tools:

- `espn_league_read.py` — league identity, settings, standings, team context
- `espn_roster_read.py` — rosters, lineup/team representations, player statistics, commissioner current snapshot
- `espn_matchup_read.py` — matchup resolution, scores, commissioner matchup evidence
- `espn_free_agent_read.py` — waiver/free-agent filters and parsing
- `espn_historical_lineup_read.py` — historical lineup evidence
- `espn_draft_read.py` — draft result parsing/filtering
- `espn_snapshot_read.py` — compact league snapshots
- `espn_activity_read.py` — bounded commissioner activity/transaction evidence

These modules are intentionally separated from provider transport so parser behavior can be tested with synthetic payloads.

### Reference identifiers — `espn_reference.py`

ESPN Fantasy payloads contain factual numeric identifiers for roster slots, pro teams, and statistics. `espn_reference.py` contains the project-defined subset and labels required by current parsers.

It replaced the retired copied `espn_constants.py` compatibility table. Unknown identifiers should remain visible rather than requiring a copied exhaustive third-party vocabulary.

### Core tool implementation — `espn_fantasy_server.py`

This is still the largest production module because it contains the established MCP tool surface and higher-level fantasy analysis. It is now wired to project-owned session, transport, reference, and parser modules.

It also contains FantasyPros-enhanced team, waiver, trade, draft, and commissioner workflows.

The public compatibility class name `ESPNFantasyFootballAPI` points to the project-owned `ESPNSessionManager`; there is no third-party `get_league` object cache behind it.

## ESPN Account Discovery

`espn_league_discovery.py` owns two additional tools:

- `discover_my_espn_leagues`
- `sync_my_espn_leagues`

Discovery intentionally separates **candidate discovery** from **league verification**.

1. An authenticated ESPN fan-profile interface is used to discover candidate league IDs.
2. Candidate count and parsing are bounded/defensive.
3. Every candidate is verified against the ESPN Fantasy league endpoint.
4. Registry synchronization previews changes first.
5. Writes occur only when `confirm=true` is explicitly supplied.

The fan-profile interface is undocumented and treated as a drift-prone optional discovery mechanism, not the source of truth for league data.

## FantasyPros Boundary

`fantasypros_client.py` owns FantasyPros behavior:

- server-side API-key resolution,
- provider requests,
- pacing/quota handling,
- cache population,
- rankings/ADP/projections/news/injuries/player data.

The default cache lives under:

```text
~/.fantasy-football-mcp/fp_cache/
```

Higher-level MCP tools combine FantasyPros evidence with ESPN roster/free-agent state rather than treating FantasyPros as the league system of record. `get_player_prop_market_context` deliberately uses this cache only and never triggers a live FantasyPros request.

## SportsGameOdds Boundary

### Tool registration — `sportsgameodds_tools.py`

This module registers 10 core read-only SportsGameOdds tools onto the shared MCP registry. `sportsgameodds_disagreement_tools.py` registers two additional bounded read-only disagreement-discovery tools. MCP-facing provider errors remain normalized through the existing SportsGameOdds tool boundary.

### Provider client — `sportsgameodds_client.py`

The client is the single provider request and normalization boundary for SportsGameOdds.

It owns:

- API-key resolution,
- `/teams`, event/slate, prop, and usage requests,
- cursor forwarding,
- normalized provider responses,
- 24-hour team metadata cache behavior.

### Analysis — `sportsgameodds_analysis.py`

This module converts provider-neutral market data into compact player-prop snapshots and NFL fantasy market signals. It does not own credentials or HTTP transport.

### Cross-book comparison — `sportsgameodds_comparison.py`

This pure module compares already-returned provider market data. It groups offers by identical posted line, calculates descriptive line/implied-probability ranges, and exposes provider fair/consensus fields without making provider calls or wagering recommendations. Game comparison is exact-event targeted; player-prop comparison builds on the existing compact prop representation.

### Disagreement ranking — `sportsgameodds_disagreement.py`

This pure layer ranks descriptive cross-book disagreement without provider I/O. Game-market discovery consumes one already-fetched slate page. Exact-event player-prop discovery ranks markets within each bet type so different proposition semantics are not blended into one score. It does not estimate expected value, win probability, or wagering edge.

### Team metadata cache

The project caches slowly changing team identity metadata only:

```text
~/.fantasy-football-mcp/sgo_cache/
```

A confident cache hit avoids a provider request. On a miss, `find_sportsbook_team` fetches at most one team page, merges non-secret metadata into the league cache, and returns any `nextCursor` for explicit continuation.

Events, odds, and player props are **not** persisted by this cache and remain live provider reads.

## Cross-provider Player Market Context

`player_market_context_tools.py` registers `get_player_prop_market_context`. It is separate from the 12 SportsGameOdds tools because it orchestrates three evidence boundaries rather than representing only one provider.

The current path is:

```text
exact event/player/team scope
  |
  +--> validate NFL/scoring/ESPN options
  |
  +--> SportsGameOdds exact-event prop disagreement
  |      (no hidden pagination; existing single player-roster fallback only)
  |
  +--> FantasyPros player intelligence + freshness
  |      (local cache only; zero live FantasyPros requests)
  |
  +--> optional ESPN roster snapshot
  |      (at most one read when espn_league_id is supplied)
  |
  +--> player_market_context.py
         (pure evidence composition)
```

`player_market_context.py` keeps explanatory evidence separated into sportsbook disagreement, FantasyPros injury/news/expert-rank dispersion, optional ESPN player/injury evidence, and data-quality/freshness status. ESPN and FantasyPros enrichment can degrade safely without discarding a valid sportsbook result.

This is not a causal or wagering model. The tool does not convert those evidence classes into expected value, fair odds, win probability, or a wager recommendation.

## Local State Architecture

`app_config.py` is the project-owned path and credential-resolution foundation.

Canonical application home:

```text
~/.fantasy-football-mcp/
```

or the value of `FANTASY_FOOTBALL_MCP_HOME`.

State classes are separated deliberately:

| State | Secret? | Owner |
|---|---|---|
| `credentials.json` | Yes | user / MCP host configuration |
| `league_registry.json` | No | `league_registry.py` / confirmed discovery sync |
| `commissioner_config.json` | No | `commissioner_config.py` |
| `draft_strategy/` | No provider credentials | `draft_strategy_store.py` |
| `fp_cache/` | No provider credentials | `fantasypros_client.py` |
| `sgo_cache/` | No provider credentials | `sportsgameodds_client.py` |

Non-secret configuration validators defensively reject secret-shaped fields where applicable.

## Draft Architecture

The live draft workflow intentionally separates facts from strategy:

1. `get_draft_board` — fresh authoritative ESPN board state
2. `prepare_draft_strategy` — persisted league-specific strategy/methodology
3. `analyze_draft_pick` — recommendation against fresh board state
4. `get_live_draft_brief` — orchestration view for draft night

Recommendation state should never become the source of truth for factual board state.

## Commissioner Architecture

Commissioner tooling is a **read/audit** layer, not a provider permission system.

`commissioner_config.json` is a local non-secret eligibility allowlist. It does not grant ESPN privileges and does not enable writes against ESPN.

Commissioner tools use project-owned current-state, matchup, historical-lineup, and activity readers to assemble bounded factual evidence for roster, lineup, transaction, and investigation workflows.

## Freshness Principles

Different data classes have intentionally different freshness strategies:

- **Current ESPN league/roster/matchup state:** project-owned ESPN reads when required by the tool.
- **Live draft state:** fresh stateless ESPN reads.
- **FantasyPros intelligence:** local cache by design, refreshed explicitly.
- **SportsGameOdds team identity:** 24-hour local metadata cache.
- **Sportsbook events/odds/props:** live requests.
- **Cross-provider player market context:** live exact-event sportsbook disagreement plus cache-only FantasyPros evidence and optional one-read ESPN league context.
- **League registry / commissioner config / draft strategy:** local persisted application state.

This distinction prevents a generic cache policy from accidentally making live sports or draft data stale.

## Packaging Boundary

`pyproject.toml` explicitly lists the production modules included in the wheel. CI audits that list, installs the wheel into a clean environment, imports every production module from `site-packages`, verifies `mcp` remains on major version 1, verifies `espn-api` is absent, and starts the packaged console command outside the source checkout.

This packaging gate is part of the architecture contract: code that works only because the source tree is on `sys.path` is not considered production-ready.

## Provenance Boundary

The project historically began by extending Kyle Brogan's ESPN Fantasy MCP project. The current production implementation was subsequently rebuilt around project-owned boundaries and no longer depends on the original server implementation or the `espn-api` package.

See [../PROVENANCE.md](../PROVENANCE.md) for the provenance record and [ESPN_INTEGRATION_AUDIT_2026.md](ESPN_INTEGRATION_AUDIT_2026.md) for the historical audit/closure record.