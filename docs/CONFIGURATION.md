# Configuration

This document is the canonical configuration reference for `fantasy-football-mcp`.

## Configuration Principles

- Provider secrets are resolved server-side.
- Environment variables take precedence over `credentials.json`.
- ESPN credentials are treated as an inseparable pair.
- Non-secret registries and caches must not contain provider credentials.
- The application home is the canonical location for persistent local state.
- Missing optional provider credentials disable/enforce limits on those provider-backed features; they are not bundled with the project.

## Application Home

Default:

```text
~/.fantasy-football-mcp/
```

Override with:

```text
FANTASY_FOOTBALL_MCP_HOME=/custom/path
```

`app_config.get_app_home()` resolves the override first, then falls back to the user's home directory. It does not create the directory automatically.

Typical layout:

```text
~/.fantasy-football-mcp/
├── credentials.json
├── league_registry.json
├── commissioner_config.json
├── draft_strategy/
├── fp_cache/
└── sgo_cache/
```

## Environment Variables

| Variable | Purpose | Secret? | Notes |
|---|---|---:|---|
| `FANTASY_FOOTBALL_MCP_HOME` | Override application-home path | No | Redirects project-owned state paths |
| `ESPN_S2` | ESPN private-league session cookie | Yes | Must be paired with `ESPN_SWID` |
| `ESPN_SWID` | ESPN private-league identity cookie | Yes | Canonical project name |
| `SWID` | ESPN SWID compatibility alias | Yes | Unified entry point mirrors to `ESPN_SWID` only when canonical value is absent |
| `FANTASYPROS_API_KEY` | FantasyPros API key | Yes | Server-side provider auth |
| `SPORTSGAMEODDS_API_KEY` | SportsGameOdds API key | Yes | Server-side provider auth |

Use the MCP host's secret/keychain facility when available.

## Shared `credentials.json`

Canonical path:

```text
~/.fantasy-football-mcp/credentials.json
```

or:

```text
$FANTASY_FOOTBALL_MCP_HOME/credentials.json
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

The configuration loader is read-only. The project does not provide a generic tool that writes provider credentials to this file.

A missing credentials file is valid and resolves as no file-backed credentials. Malformed JSON or malformed provider sections fail with path/category-only errors; credential values are not included in those errors.

## ESPN Authentication

### Resolution order

Project-owned ESPN credential resolution uses:

1. complete `ESPN_S2` + `ESPN_SWID` environment pair,
2. complete `credentials.json` ESPN pair,
3. no configured private-league credentials.

The unified entry point accepts `SWID` as a host compatibility alias. If `ESPN_SWID` is absent and `SWID` is present, it mirrors `SWID` to `ESPN_SWID` before the project resolver is primed.

### Pair integrity

A partial environment pair is a configuration error. The resolver never combines one value from the environment with one value from the file.

Similarly, a file ESPN section must contain either both usable values or neither. A single usable cookie is rejected.

### `authenticate` MCP tool

The production `authenticate` tool is optional in normal server-side configuration:

- `authenticate()` — prime/report configured auth state,
- `authenticate(espn_s2, swid)` — replace the active in-memory pair explicitly,
- one argument only — rejected,
- literal `ENV` placeholder — rejected.

`logout()` clears active in-memory ESPN credentials. Restarting the MCP process re-primes configured environment/file credentials.

### Public leagues

Public ESPN leagues may be readable without private-league cookies. Provider behavior still depends on the unofficial ESPN interfaces used by the project.

## FantasyPros Authentication

Resolution normally prefers:

1. `FANTASYPROS_API_KEY` environment configuration,
2. `credentials.json` → `fantasypros.api_key`.

FantasyPros tools do not require API keys as MCP arguments.

The provider client owns pacing/quota/cache behavior. Default generated state:

```text
~/.fantasy-football-mcp/fp_cache/
```

## SportsGameOdds Authentication

Resolution order:

1. `SPORTSGAMEODDS_API_KEY` environment variable,
2. `credentials.json` → `sportsgameodds.api_key`,
3. no configured key.

SportsGameOdds tools do not accept API keys as MCP arguments.

The key is sent in the provider request header, not in request URLs. The team metadata cache never persists the API key.

## League Registry

Canonical path:

```text
~/.fantasy-football-mcp/league_registry.json
```

Purpose:

- stable league aliases,
- optional display names,
- enabled/disabled local navigation,
- one default league,
- target for confirmed ESPN account-discovery synchronization.

Example:

```json
{
  "version": 1,
  "default_league": "home",
  "leagues": {
    "home": {
      "league_id": 123456789,
      "display_name": "Home League",
      "enabled": true
    },
    "work": {
      "league_id": 987654321,
      "display_name": "Work League",
      "enabled": true
    }
  }
}
```

### Registry rules

- `version` must be `1`.
- `default_league` must reference a configured alias.
- aliases are canonical lowercase strings containing letters, digits, `_`, or `-`.
- `league_id` values are positive integers and cannot be duplicated.
- `enabled` is optional and defaults to true.
- `display_name` is optional.
- secret-shaped keys are rejected anywhere in the parsed structure.
- role, commissioner, permissions, scoring, standings, and duplicated ESPN state do not belong in the registry.

Some legacy source-relative read fallback remains in `league_registry.py` for migration compatibility. New/canonical state belongs in the application home.

### Discovery synchronization

`sync_my_espn_leagues(confirm=false)` is preview-first. It should not modify the registry until the caller explicitly uses `confirm=true` after reviewing the proposed changes.

## Commissioner Configuration

Canonical path:

```text
~/.fantasy-football-mcp/commissioner_config.json
```

This is a local **read/audit eligibility allowlist**. It does not grant ESPN permissions and does not enable write actions.

Example:

```json
{
  "version": 1,
  "leagues": {
    "home": {
      "league_id": 123456789,
      "enabled": true
    }
  }
}
```

Keep provider credentials out of this file.

## Draft Strategy State

Canonical directory:

```text
~/.fantasy-football-mcp/draft_strategy/
```

This stores project-generated league-specific draft strategy artifacts. Strategy state is methodology/recommendation context, not authoritative live ESPN board state.

Provider credentials must not be written to draft strategy artifacts.

## FantasyPros Cache

Canonical directory:

```text
~/.fantasy-football-mcp/fp_cache/
```

This is generated local provider intelligence/cache/quota state owned by `fantasypros_client.py`.

Use `refresh_fantasypros_cache` to refresh supported cached datasets through the MCP surface.

## SportsGameOdds Team Cache

Canonical directory:

```text
~/.fantasy-football-mcp/sgo_cache/
```

This cache contains slowly changing, non-secret provider team identity metadata.

Current contract:

- 24-hour freshness window,
- league-scoped cache state,
- confident cache hits avoid provider requests,
- cache miss fetches at most one team page per MCP call,
- returned `nextCursor` remains explicit for pagination,
- API keys are not persisted,
- events, odds, and player props are not persisted by this cache.

See [../SPORTSGAMEODDS.md](../SPORTSGAMEODDS.md).

## Repo-Local Development State

If you intentionally point `FANTASY_FOOTBALL_MCP_HOME` inside a source checkout, use a location such as:

```text
./local_config/
```

The repository `.gitignore` excludes the expected local credentials/config/state patterns. Do not rely on `.gitignore` as the only secret-control mechanism; avoid creating real secret files in the repository tree when possible.

## Example: Minimal ESPN-Only Host

```json
{
  "mcpServers": {
    "fantasy-football": {
      "command": "uv",
      "args": ["--directory", "/path/to/repo", "run", "fantasy-football-mcp"],
      "env": {
        "ESPN_S2": "<secret>",
        "ESPN_SWID": "<secret>"
      }
    }
  }
}
```

## Example: Full Provider Host

```json
{
  "mcpServers": {
    "fantasy-football": {
      "command": "uv",
      "args": ["--directory", "/path/to/repo", "run", "fantasy-football-mcp"],
      "env": {
        "ESPN_S2": "<secret>",
        "ESPN_SWID": "<secret>",
        "FANTASYPROS_API_KEY": "<secret>",
        "SPORTSGAMEODDS_API_KEY": "<secret>",
        "FANTASY_FOOTBALL_MCP_HOME": "/path/to/private/app-home"
      }
    }
  }
}
```

## Troubleshooting

### ESPN says authentication is inactive

Verify that both `ESPN_S2` and `ESPN_SWID` are present in the MCP child process, or that both file-backed ESPN values exist. If the host only supports `SWID`, the unified entry point accepts that alias.

### Partial ESPN configuration error

Do not mix sources. Configure both cookies in the environment or both in `credentials.json`.

### `ENV` is rejected

`ENV` is not a credential indirection token. Configure real secret values in the host environment and call `authenticate()` with no arguments if you want to inspect active configuration.

### League registry not found

Create the canonical application-home `league_registry.json`, or use `sync_my_espn_leagues` in preview mode and explicitly confirm the proposed registry write.

### Sportsbook team not found on page one

If `find_sportsbook_team` returns a `nextCursor`, call it again with the same league/name and that exact cursor. The tool intentionally does not drain all provider pages automatically.

## Security

For reporting, rotation guidance, redaction expectations, and the credential threat model, see [../SECURITY.md](../SECURITY.md).