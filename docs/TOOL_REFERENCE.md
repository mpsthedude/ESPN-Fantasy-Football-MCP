# MCP Tool Reference

The unified `fantasy-football-mcp` production server exposes **52 tools** through one FastMCP registry.

This catalog describes the current `main` contract. Provider APIs are read-oriented; the only intentional local mutation in the discovery surface is preview-first league-registry synchronization when explicitly confirmed.

## Tool Count Contract

| Area | Count |
|---|---:|
| Auth / Session | 2 |
| League Navigation & Discovery | 8 |
| Rosters / Teams | 4 |
| Players | 3 |
| FantasyPros Data | 5 |
| Team Management | 3 |
| Waivers | 1 |
| Trades | 2 |
| Draft | 5 |
| Commissioner | 6 |
| SportsGameOdds | 12 |
| Cross-provider Market Context | 1 |
| **Total** | **52** |

The test suite protects both the 37-tool ESPN/FantasyPros core registry and the 52-tool unified registry.

## Auth / Session — 2

| Tool | Purpose | Provider behavior |
|---|---|---|
| `authenticate` | Use/report configured ESPN auth with no arguments, or explicitly replace the in-memory ESPN credential pair. | No provider read required merely to store/report configuration; partial pairs and `ENV` placeholders are rejected. |
| `logout` | Clear active in-memory ESPN credentials for the running process. | Local session-state mutation only. |

### Authentication contract

Normal MCP-host configuration is server-side. `authenticate()` is optional when `ESPN_S2` plus `ESPN_SWID`/`SWID` are already configured.

Explicit override requires both `espn_s2` and `swid` together.

## League Navigation & Discovery — 8

| Tool | Purpose |
|---|---|
| `list_my_leagues` | List enabled leagues from the local registry and resolve current access/team context where possible. |
| `get_league_context` | Resolve one configured league's identity, settings, and user's team context. |
| `get_league_info` | Return basic ESPN league identity/season information. |
| `get_league_settings` | Return ESPN roster-slot requirements and scoring rules. |
| `get_league_standings` | Return ESPN standings information using project-owned payload parsing. |
| `get_league_snapshot` | Return a compact current settings/standings/rosters snapshot. |
| `discover_my_espn_leagues` | Discover candidate leagues for the authenticated ESPN account and verify them against the Fantasy league endpoint. |
| `sync_my_espn_leagues` | Preview or explicitly confirm synchronization of verified discovered leagues into the local registry. |

### Discovery/sync contract

- discovery is read-only,
- fan-profile candidate discovery is treated as undocumented/drift-prone,
- candidates are verified before use,
- sync defaults to preview behavior,
- registry writes require explicit confirmation.

## Rosters / Teams — 4

| Tool | Purpose |
|---|---|
| `get_team_roster` | Return the current ESPN roster for a team ID. |
| `get_team_info` | Return team record, points, and transaction context. |
| `get_all_rosters` | Return all team rosters for a league. |
| `get_matchup_info` | Return matchup scores/context for a valid scoring week. |

These paths use project-owned ESPN transport and parser modules rather than a cached third-party league object graph.

## Players — 3

| Tool | Purpose |
|---|---|
| `get_player_stats` | Return ESPN roster statistics for a player-name match. |
| `get_free_agents` | Return available ESPN free agents/waiver players, optionally filtered. |
| `compare_players` | Compare 2–4 players using cached FantasyPros intelligence and project analysis. |

## FantasyPros Data — 5

| Tool | Purpose |
|---|---|
| `get_player_intelligence` | Return combined cached FantasyPros intelligence for one player. |
| `get_consensus_rankings` | Return cached consensus rankings for a position. |
| `get_adp` | Return cached average draft position data. |
| `refresh_fantasypros_cache` | Refresh supported FantasyPros player/ranking/projection/injury/news datasets. |
| `enrich_espn_free_agents` | Join current ESPN availability with cached FantasyPros market/intelligence data. |

FantasyPros credentials are resolved server-side. These tools never require an API key argument.

## Team Management — 3

| Tool | Purpose |
|---|---|
| `analyze_my_team` | Produce league-relative roster analysis for the user's team. |
| `optimize_lineup` | Recommend a best starting lineup from available league/player evaluation data. |
| `get_fantasy_brief` | Produce a compact current-team action brief. |

## Waivers — 1

| Tool | Purpose |
|---|---|
| `rank_waiver_targets` | Rank available waiver/free-agent targets for a team using current ESPN availability plus available intelligence. |

## Trades — 2

| Tool | Purpose |
|---|---|
| `evaluate_trade` | Evaluate a proposed roster-to-roster trade against roster construction and available player evidence. |
| `find_trade_targets` | Identify realistic target players/packages on actual opposing rosters. |

## Draft — 5

| Tool | Purpose |
|---|---|
| `get_draft_results` | Return ESPN draft results by pick/round/player/team. |
| `get_draft_board` | Return fresh factual ESPN draft-board state. |
| `prepare_draft_strategy` | Build and persist transparent league-specific strategy/methodology. |
| `analyze_draft_pick` | Analyze the next pick against fresh board state and saved strategy, with final-state revalidation. |
| `get_live_draft_brief` | Return the primary draft-night board/strategy/recommendation brief. |

### Draft-state contract

`get_draft_board` is the factual source. Persisted strategy is advisory methodology. Live recommendation tools re-read/revalidate authoritative state rather than treating the saved strategy artifact as the board.

## Commissioner — 6

| Tool | Purpose |
|---|---|
| `get_commissioner_context` | Resolve commissioner-eligible league context from local configuration and ESPN data. |
| `get_commissioner_brief` | Return a compact commissioner attention brief. |
| `commissioner_audit_lineups` | Audit objective lineup issues. |
| `commissioner_audit_rosters` | Audit roster occupancy/compliance conditions. |
| `commissioner_audit_transactions` | Audit bounded transaction/waiver/trade activity evidence. |
| `commissioner_investigate` | Assemble a bounded factual case file for a specific commissioner question. |

Commissioner configuration is a local read/audit allowlist. It does not grant ESPN permissions or enable provider writes.

## SportsGameOdds — 12

| Tool | Purpose | Important contract |
|---|---|---|
| `get_sportsbook_usage` | Return plan/rate-limit usage without account identifiers. | Read-only. |
| `find_sportsbook_team` | Resolve a human team name/abbreviation to a provider `teamID`. | Requires `team_name` + `league`; cache-first; live miss fetches one team page; returns `nextCursor` when continuation is available. |
| `get_sportsbook_slate` | Return one page of game moneyline/spread/total markets. | Provide exactly one of `league` or `sport`; optional `team_id`, date window, bookmakers, cursor, and limit. |
| `get_sportsbook_player_props` | Return compact full-game player props for a supported league. | Requires provider `team_id`; optional exact `event_id`, stat/bookmaker filters, alt-line behavior. |
| `compare_sportsbook_market` | Compare one exact-event moneyline, spread, total, or player prop across selected sportsbooks. | Requires `event_id` + `league` + `market`; player props additionally require player/team/stat scope. Prices are ranked only within identical posted lines. |
| `find_sportsbook_market_disagreements` | Rank cross-book disagreement for one game market across one explicit slate page. | Requires `market`; exactly one of `league`/`sport` at runtime. No hidden pagination; spread/total ranking keeps line and same-line price disagreement separate. |
| `find_sportsbook_player_prop_disagreements` | Rank one player's exact-event prop disagreements across books. | Requires exact event/player/league/team scope; optional stat/bet-type filters; results rank only within the same bet type. |
| `get_nfl_sportsbook_slate` | Return NFL-focused sportsbook slate data. | NFL compatibility convenience surface. |
| `get_nfl_player_props` | Return compact fantasy-relevant NFL props. | NFL/team abbreviation compatibility behavior. |
| `get_fantasy_market_signal` | Return position-aware NFL sportsbook evidence for fantasy analysis. | Analysis/read-only. |
| `get_supported_sportsbook_leagues` | Return common provider league IDs/aliases known by the integration. | Informational/local. |
| `get_supported_sportsbooks` | Return default supported bookmaker identifiers. | Informational/local. |

### Team resolution

`find_sportsbook_team` supports human inputs such as team nickname, abbreviation, or full name. Team identity metadata is cached for 24 hours under `sgo_cache/`.

A confident cache hit makes no provider request. On a miss, one provider team page is fetched and merged into the league cache. If the target is not confidently found and `nextCursor` is present, call again with the exact cursor.

### Slate scope

`get_sportsbook_slate` requires exactly one scope:

```text
league="NFL"
```

or:

```text
sport="FOOTBALL"
```

Use `team_id` when a team has already been resolved. This allows provider-side event targeting rather than retrieving a broad league slate and filtering locally.

### Date windows

`starts_after` and `starts_before` accept provider-compatible ISO-8601 date/time bounds. Use offset-aware values when converting a user's local calendar day into a provider window.

### Cursor pagination

SportsGameOdds pagination is deliberately explicit:

1. omit `cursor` for page one,
2. inspect `nextCursor`,
3. pass that opaque value back unchanged,
4. preserve all other query arguments between pages.

The MCP does not silently fetch every page.

### Exact-event market comparison

`compare_sportsbook_market` requires an exact event ID. Game markets make one targeted provider event request filtered to the requested main oddIDs and selected books. Player-prop comparison reuses the exact-event prop path. Different spread/total/prop lines are kept in separate line groups; price ranking is only meaningful within the same posted line.

### Disagreement discovery

`find_sportsbook_market_disagreements` ranks exactly one current slate page and returns `nextCursor` unchanged. Moneylines rank by implied-probability spread; spreads/totals rank by posted-line range first and same-line price spread second.

`find_sportsbook_player_prop_disagreements` requires an exact event/player/team scope and reuses the bounded prop provider path. Results are separated by `betTypeID` before ranking so unlike propositions are not collapsed into one score. Neither tool estimates expected value or recommends a wager.

### Exact-event props

When an event has already been identified in the slate, pass its `eventID` as `event_id` to `get_sportsbook_player_props`. This prevents ambiguous team-level searches when a team has multiple upcoming events.

See [../SPORTSGAMEODDS.md](../SPORTSGAMEODDS.md) for the provider-specific guide.

## Cross-provider Market Context — 1

| Tool | Purpose | Important contract |
|---|---|---|
| `get_player_prop_market_context` | Combine exact-event NFL player-prop disagreement with FantasyPros player intelligence and optional ESPN league player evidence. | Requires `event_id`, `player_name`, `league`, and provider `team_id`; currently NFL-only; FantasyPros is cache-only; optional `espn_league_id` adds at most one ESPN roster read; no EV/fair-odds/wager recommendation. |

### Player-prop context contract

`get_player_prop_market_context` is intentionally a cross-provider orchestration tool rather than a thirteenth SportsGameOdds tool.

Provider-cost behavior is bounded and explicit:

1. validate exact scope, NFL support, scoring, and ESPN option relationships before spending SportsGameOdds quota,
2. reuse the existing exact-event player-prop disagreement path,
3. make zero live FantasyPros calls; `build_player_intelligence` and freshness data are read from local cache,
4. when `espn_league_id` is supplied, perform at most one ESPN roster snapshot read for the requested player,
5. degrade ESPN or FantasyPros enrichment safely without discarding a valid sportsbook disagreement result.

The output keeps evidence classes separate: sportsbook line/price disagreement, FantasyPros injury/news/expert-rank dispersion, optional ESPN player/injury evidence, and cache/data-quality status. These are possible explanatory signals only. The tool does not claim causality or calculate expected value, fair odds, win probability, or a wager recommendation.

## Provider Credentials by Tool Family

| Provider | Credentials | Required by |
|---|---|---|
| ESPN | `ESPN_S2` + `ESPN_SWID`/`SWID` for private-account/private-league access | ESPN league/discovery tools and optional ESPN enrichment in cross-provider context |
| FantasyPros | `FANTASYPROS_API_KEY` | FantasyPros refresh/provider-backed intelligence features; cross-provider context itself reads existing cache only |
| SportsGameOdds | `SPORTSGAMEODDS_API_KEY` | Sportsbook provider reads and cross-provider market context |

Credentials should be configured in the MCP host environment or `credentials.json`; they are not normal tool arguments.

## Data Freshness Summary

| Data | Freshness model |
|---|---|
| Current ESPN league/roster/matchup state | Project-owned provider reads as required by tool |
| Live ESPN draft board | Fresh stateless read |
| FantasyPros intelligence | Explicitly refreshed local cache |
| SportsGameOdds team identity | 24-hour local metadata cache |
| Sportsbook events/odds/props | Live provider reads |
| Cross-provider player market context | Live exact-event sportsbook disagreement + cache-only FantasyPros + optional one-read ESPN roster context |
| League registry / commissioner config / draft strategy | Local persisted state |

## Read/Write / MCP Classification

The unified server publishes MCP `ToolAnnotations` for every tool so compatible MCP hosts do not have to use a pessimistic default classification.

**Current contract: 47 Read / 5 Write.**

The following tools publish `readOnlyHint: false` because their purpose includes changing active session state or persisted local application state:

- `authenticate` — can replace the active in-memory ESPN credential pair
- `logout` — clears active in-memory ESPN credentials
- `sync_my_espn_leagues` — can persist league-registry changes when explicitly confirmed
- `refresh_fantasypros_cache` — refreshes persisted local FantasyPros cache/quota state
- `prepare_draft_strategy` — persists league-specific draft-strategy state

All other 47 tools publish `readOnlyHint: true`. This includes ESPN/FantasyPros analysis, commissioner audits/investigations, waiver/trade recommendations, all twelve SportsGameOdds tools, and the cross-provider market-context tool.

`find_sportsbook_team` is classified Read even though a cache miss can refresh the internal 24-hour `sgo_cache` metadata. That cache is a transparent retrieval implementation detail and does not modify provider state or user-owned fantasy/domain state. `get_player_prop_market_context` is also Read because its purpose is bounded retrieval/composition and it does not mutate fantasy or sportsbook state.

The annotation table is centralized in `mcp_tool_annotations.py` and is fail-closed: adding, removing, or renaming a production tool requires an explicit classification update or unified startup/tests fail. MCP annotations are advisory client metadata, not a security boundary.

## Maintaining This Contract

When intentionally adding/removing/renaming a tool:

1. update the implementation registration,
2. update `tests/test_unified_mcp.py` expected names/count,
3. update `mcp_tool_annotations.py` with an explicit read/write classification,
4. update this document and README count/category summary,
5. update `CLAUDE.md` / development guidance if architecture changes,
6. update `CHANGELOG.md`,
7. run the full unit and packaging CI gates before merge.
