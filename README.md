# ESPN Fantasy Football MCP

**Package:** `fantasy-football-mcp`  
**Current release:** `0.4.1`  
**Current `main` tool surface:** **52 MCP tools**

ESPN Fantasy Football MCP is a local-first Model Context Protocol server for analyzing and managing **ESPN Fantasy Football** leagues. ESPN is the only supported fantasy-league platform. A single stdio MCP connection exposes ESPN fantasy-league data, optional FantasyPros intelligence, and optional read-only SportsGameOdds market context.

The current production implementation is project-owned. ESPN transport, authentication/session state, payload parsing, state handling, provider integrations, packaging, and MCP registration no longer depend on the historical `espn-api` runtime or the original ESPN MCP server implementation. See [PROVENANCE.md](PROVENANCE.md).

## Capabilities

- ESPN league, settings, standings, rosters, matchups, players, waivers, trades, draft, and commissioner analysis
- Server-side ESPN authentication with optional explicit in-memory override
- ESPN account league discovery and preview-first registry synchronization
- FantasyPros rankings, ADP, player intelligence, projections, news, injuries, and ESPN free-agent enrichment
- SportsGameOdds slates, player props, cross-book market comparison/disagreement discovery, sportsbook usage, team resolution, and fantasy market signals
- Cross-provider NFL player-prop context that combines live sportsbook disagreement with cache-only FantasyPros evidence and optional one-read ESPN league context
- Persistent 24-hour SportsGameOdds team-metadata cache with live fallback and explicit cursor pagination
- Local application state under one configurable application-home directory
- Linux and Windows CI, Python 3.12/3.13 coverage, fresh dependency resolution, wheel auditing/install, and console-entry smoke tests

ESPN is the supported fantasy-league platform. FantasyPros and SportsGameOdds are supplemental providers. SportsGameOdds functionality is read-only and cannot place, modify, or cancel wagers. Cross-provider market context is descriptive evidence only; it does not calculate expected value, fair odds, win probability, or a wager recommendation.

This project is not affiliated with or endorsed by ESPN, FantasyPros, SportsGameOdds, or the maintainers/licensors of third-party dependencies. ESPN and related marks are the property of their respective owners; the use of “ESPN” here is descriptive of the fantasy platform this independent project interoperates with.

## Requirements

- Python 3.12+
- `uv`
- MCP SDK `>=1.7,<2`

Runtime dependencies are intentionally small: the MCP SDK and `requests`. The project does **not** require the `espn-api` Python package.

## Quick Start

```bash
git clone https://github.com/mpsthedude/ESPN-Fantasy-Football-MCP.git
cd ESPN-Fantasy-Football-MCP
uv sync --locked
uv run fantasy-football-mcp
```

The production server communicates over stdio. In normal use, an MCP host launches the command for you.

## MCP Host Configuration

Generic command:

```bash
uv --directory /absolute/path/to/ESPN-Fantasy-Football-MCP run fantasy-football-mcp
```

Example host configuration:

```json
{
  "mcpServers": {
    "fantasy-football": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/ESPN-Fantasy-Football-MCP",
        "run",
        "fantasy-football-mcp"
      ],
      "env": {
        "ESPN_S2": "<secret>",
        "ESPN_SWID": "<secret>",
        "FANTASYPROS_API_KEY": "<secret>",
        "SPORTSGAMEODDS_API_KEY": "<secret>"
      }
    }
  }
}
```

`SWID` is accepted as a compatibility alias when `ESPN_SWID` is not supplied. Prefer the canonical `ESPN_SWID` name when you control the host configuration.

## Credential Model

Normal production use keeps credentials on the server side. Individual tool calls should not need provider secrets. For step-by-step instructions to obtain your own ESPN `espn_s2` / `SWID` cookies and the optional FantasyPros and SportsGameOdds API keys, see [Provider Credentials Setup](docs/PROVIDER_CREDENTIALS.md).

Supported environment variables:

```text
ESPN_S2
ESPN_SWID
SWID
FANTASYPROS_API_KEY
SPORTSGAMEODDS_API_KEY
FANTASY_FOOTBALL_MCP_HOME
```

The shared credentials file is:

```text
~/.fantasy-football-mcp/credentials.json
```

Example:

```json
{
  "version": 1,
  "espn": {
    "espn_s2": "YOUR_ESPN_S2",
    "swid": "YOUR_SWID"
  },
  "fantasypros": {
    "api_key": "YOUR_FANTASYPROS_API_KEY"
  },
  "sportsgameodds": {
    "api_key": "YOUR_SPORTSGAMEODDS_API_KEY"
  }
}
```

Environment values take precedence over the credentials file. ESPN credential pairs must come from one complete source; partial pairs are rejected rather than mixed. Literal placeholder values such as `ENV` are rejected by the active ESPN session boundary.

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full configuration model.

## Application Home

Default:

```text
~/.fantasy-football-mcp/
```

Override:

```text
FANTASY_FOOTBALL_MCP_HOME=/custom/path
```

Current project-owned state:

```text
~/.fantasy-football-mcp/
├── credentials.json          # secret
├── league_registry.json      # non-secret
├── commissioner_config.json  # non-secret
├── draft_strategy/           # generated strategy state
├── fp_cache/                 # FantasyPros cache/quota state
└── sgo_cache/                # SportsGameOdds team metadata cache
```

Provider credentials must never be stored in the non-secret registry, commissioner configuration, draft state, or metadata caches.

## Tool Surface

The unified production server exposes **52 tools**:

| Area | Count | Examples |
|---|---:|---|
| Auth / session | 2 | `authenticate`, `logout` |
| League navigation / discovery | 8 | `get_league_context`, `discover_my_espn_leagues`, `sync_my_espn_leagues` |
| Rosters / teams | 4 | `get_team_roster`, `get_all_rosters`, `get_matchup_info` |
| Players | 3 | `get_player_stats`, `get_free_agents`, `compare_players` |
| FantasyPros data | 5 | `get_player_intelligence`, `get_consensus_rankings`, `refresh_fantasypros_cache` |
| Team management | 3 | `analyze_my_team`, `optimize_lineup`, `get_fantasy_brief` |
| Waivers | 1 | `rank_waiver_targets` |
| Trades | 2 | `evaluate_trade`, `find_trade_targets` |
| Draft | 5 | `get_draft_board`, `prepare_draft_strategy`, `get_live_draft_brief` |
| Commissioner | 6 | `commissioner_audit_lineups`, `commissioner_investigate` |
| SportsGameOdds | 12 | `get_sportsbook_slate`, `compare_sportsbook_market`, `find_sportsbook_market_disagreements`, `find_sportsbook_player_prop_disagreements` |
| Cross-provider market context | 1 | `get_player_prop_market_context` |

### MCP read/write classification

The server publishes MCP `ToolAnnotations` for all 52 tools. **47 tools are advertised as Read** with `readOnlyHint: true`. Five tools remain Write-classified because their purpose includes changing active session or persisted local application state: `authenticate`, `logout`, `sync_my_espn_leagues`, `refresh_fantasypros_cache`, and `prepare_draft_strategy`.

This classification reduces unnecessary approval prompts in MCP hosts while keeping the annotations truthful. `find_sportsbook_team` remains Read-classified even though a cache miss can refresh non-secret team metadata in `sgo_cache`; that cache is an internal retrieval optimization and does not modify provider or fantasy-domain state. `get_player_prop_market_context` is also Read-classified: it performs bounded provider reads and cache reads without changing fantasy or sportsbook state.

See [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md) for the complete 52-tool catalog and important input/behavior contracts.

## Provider Notes

### ESPN

ESPN integration is implemented directly through project-owned HTTP transport and payload parsers against unofficial ESPN interfaces. Private leagues use the user's own `espn_s2` and `SWID` browser-session cookies. The project does not depend on `espn-api`.

League discovery uses an undocumented fan-profile interface only for candidate discovery; candidates are then verified against the fantasy-football league endpoint before they are returned or persisted.

### FantasyPros

FantasyPros is optional. The API key is resolved server-side. Cached rankings, projections, news, injuries, and player data live under `fp_cache/`.

### SportsGameOdds

SportsGameOdds is optional and read-only. Team identity metadata can be cached for 24 hours under `sgo_cache/`; events, odds, and player props remain live. Cursor pagination remains explicit so provider usage is visible and bounded.

### Cross-provider player market context

`get_player_prop_market_context` currently supports NFL player props. It reuses the exact-event SportsGameOdds disagreement path, reads FantasyPros intelligence from cache only, and optionally performs one ESPN fantasy-league roster read when `espn_league_id` is supplied. It reports evidence, cache freshness, and provider-cost bounds without asserting causality or betting edge.

See [SPORTSGAMEODDS.md](SPORTSGAMEODDS.md).

## Documentation

- [Provider Credentials Setup](docs/PROVIDER_CREDENTIALS.md) — how to obtain ESPN cookies and optional FantasyPros/SportsGameOdds API keys safely
- [Architecture](docs/ARCHITECTURE.md) — runtime boundaries, provider ownership, data flow, state, and freshness
- [Configuration](docs/CONFIGURATION.md) — environment variables, credentials, registry, commissioner config, caches, and app home
- [Tool Reference](docs/TOOL_REFERENCE.md) — all 52 MCP tools
- [Development](docs/DEVELOPMENT.md) — setup, testing, CI, packaging, release flow, and contribution rules
- [SportsGameOdds](SPORTSGAMEODDS.md) — sportsbook-specific behavior and examples
- [Security](SECURITY.md) — threat model, credential handling, and reporting
- [Provenance](PROVENANCE.md) — historical origin and the clean-provenance boundary
- [ESPN 2026 Audit Record](docs/ESPN_INTEGRATION_AUDIT_2026.md) — historical audit findings and closure status
- [Changelog](CHANGELOG.md) — release and unreleased changes
- [MIT License](LICENSE) — project license

## Development

```bash
uv sync --locked
uv run python -m unittest discover
uv build --wheel --out-dir <temp-dir>
```

Normal CI validates Ubuntu/Python 3.12, Ubuntu/Python 3.13, Windows/Python 3.12, fresh dependency resolution under the `mcp<2` contract, wheel contents, clean wheel installation/imports, absence of the retired `espn-api` dependency, and the unified console entry point.

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Limitations

- **ESPN Fantasy Football is the only supported fantasy-league platform.** This server does not provide adapters for Sleeper, Yahoo, NFL.com, or other fantasy platforms.
- ESPN's Fantasy interfaces used here are unofficial/undocumented and may change.
- ESPN account discovery depends on an undocumented fan-profile endpoint and is deliberately isolated/verified.
- FantasyPros and SportsGameOdds coverage depends on the user's provider access and plan.
- Cross-provider player market context is currently NFL-only because the FantasyPros enrichment layer is NFL-specific.
- One active ESPN identity is intended per running MCP process.
- The server is local stdio MCP, not a hosted multi-user service.

## Licensing

Project-authored source code is licensed under the [MIT License](LICENSE). You may use, copy, modify, publish, distribute, sublicense, and sell copies of the software subject to the MIT License terms, including preservation of the copyright and license notice.

Third-party dependencies remain governed by their own licenses. Provider data, APIs, trademarks, and service access remain subject to the applicable provider terms and are not relicensed by this project's MIT License.

See [PROVENANCE.md](PROVENANCE.md) for the historical and licensing boundary.
