# SportsGameOdds Integration

SportsGameOdds is the optional read-only sportsbook provider inside the unified `fantasy-football-mcp` server. Its tools share the same MCP connection as ESPN and FantasyPros.

The integration does **not** place, modify, or cancel wagers.

## Architecture

```text
MCP tool
  |
  +--> sportsgameodds_tools.py
  |
  +--> sportsgameodds_disagreement_tools.py
  |
  +--> sportsgameodds_client.py ----> SportsGameOdds API
  |
  +--> sportsgameodds_analysis.py --> compact/fantasy market interpretation
  |
  +--> sportsgameodds_comparison.py --> cross-book line/price comparison
  |
  +--> sportsgameodds_disagreement.py --> bounded disagreement ranking
```

`SportsGameOddsClient` is the single provider request/normalization boundary. The MCP layer should not duplicate raw provider request logic.

## Authentication

Preferred MCP-host environment variable:

```text
SPORTSGAMEODDS_API_KEY
```

Alternative file-backed configuration:

```text
~/.fantasy-football-mcp/credentials.json
```

```json
{
  "sportsgameodds": {
    "api_key": "YOUR_KEY"
  }
}
```

If `FANTASY_FOOTBALL_MCP_HOME` is set, the credentials file and SportsGameOdds cache live under that application home.

The API key is resolved server-side and sent in the provider request header. It is never a normal MCP tool argument and is never persisted in the team metadata cache.

## SportsGameOdds Tools — 12

The provider surface remains 12 tools. The separate cross-provider `get_player_prop_market_context` tool described later consumes SportsGameOdds evidence but is not counted as a SportsGameOdds-only tool because it also uses FantasyPros cache data and optional ESPN context.

### Generic / multi-sport

- `get_sportsbook_usage`
- `find_sportsbook_team`
- `get_sportsbook_slate`
- `get_sportsbook_player_props`
- `compare_sportsbook_market`
- `find_sportsbook_market_disagreements`
- `find_sportsbook_player_prop_disagreements`
- `get_supported_sportsbook_leagues`
- `get_supported_sportsbooks`

### NFL fantasy compatibility

- `get_nfl_sportsbook_slate`
- `get_nfl_player_props`
- `get_fantasy_market_signal`

## Common League IDs and Aliases

The generic tools accept provider league IDs directly. The integration also recognizes common aliases:

| League | Provider ID | Common aliases |
|---|---|---|
| NFL | `NFL` | `NFL` |
| College Football | `NCAAF` | `NCAAF`, `CFB`, `college football` |
| NBA | `NBA` | `NBA` |
| College Basketball | `NCAAB` | `NCAAB`, `CBB`, `college basketball` |
| NHL | `NHL` | `NHL` |
| MLB | `MLB` | `MLB` |
| WNBA | `WNBA` | `WNBA` |
| MLS | `MLS` | `MLS` |
| Premier League | `EPL` | `EPL` |
| PGA Men | `PGA_MEN` | `PGA`, `PGA_MEN` |

SportsGameOdds may support additional league IDs. Provider/plan coverage and actual posted markets vary.

## Team Resolution

Use `find_sportsbook_team` when you know a human name/abbreviation but not the provider `teamID`.

Examples:

```text
team_name="Broncos", league="NFL"
team_name="DEN", league="NFL"
team_name="Denver Broncos", league="NFL"
team_name="Lakers", league="NBA"
```

### Required inputs

`find_sportsbook_team` requires:

- `team_name`
- `league`

Optional:

- `cursor`
- `limit` (default 100)

### Cache behavior

Team identity metadata is cached locally for **24 hours**.

Default path:

```text
~/.fantasy-football-mcp/sgo_cache/
```

Cache contents are non-secret team metadata only.

A confident cache hit makes **no provider request**.

On a cache miss, one MCP call:

1. requests at most one provider `/teams` page,
2. merges returned team metadata into the league cache,
3. attempts a confident match,
4. returns the provider `nextCursor` when more pages are available.

If the team was not confidently found and `nextCursor` is present, call `find_sportsbook_team` again with that exact cursor. This makes pagination/API usage explicit rather than silently draining every team page.

The repository does not bundle a generated team cache. Each user's cache is local application state.

## Game Slate

`get_sportsbook_slate` returns one provider page of current game markets, including compact moneyline/spread/total information.

### Choose exactly one scope

League-specific:

```text
league="NCAAF"
```

Sport-wide:

```text
sport="FOOTBALL"
```

Do not supply both or neither.

### Optional team targeting

If you already resolved a provider `teamID`, pass:

```text
team_id="..."
```

This narrows the provider request server-side and is preferable to downloading a broad league slate and filtering locally.

Keep the same `team_id` while following slate cursor pagination.

## Date Windows

`get_sportsbook_slate` accepts optional:

- `starts_after`
- `starts_before`

Use provider-compatible ISO-8601 date/time strings. For a local calendar date, convert the requested day to explicit offset-aware bounds.

Example for Nashville during Central Daylight Time:

```text
starts_after="2026-09-05T00:00:00-05:00"
starts_before="2026-09-06T00:00:00-05:00"
```

Omit both when no date filtering is required.

## Slate Cursor Pagination

The generic slate intentionally returns one provider page per MCP call.

Page one:

```text
cursor omitted
```

Continuation:

1. read `nextCursor`,
2. pass it back unchanged as `cursor`,
3. preserve `league`/`sport`, `team_id`, bookmakers, date bounds, and limit.

Opaque provider cursors must not be parsed, modified, or regenerated by the caller.

## Player Props

`get_sportsbook_player_props` provides generic full-game player props.

Core inputs:

- `player_name`
- `league`
- provider `team_id`

Optional inputs include:

- exact `event_id`
- `stat_id`
- bookmaker selection
- alt-line inclusion behavior

### Why `team_id` is required

Human team labels are intentionally resolved separately. Use `find_sportsbook_team` or a team ID returned by the slate, then pass the provider identity to the prop tool.

This keeps team resolution explicit and lets the provider query operate on its own identifiers.

### Exact-event targeting

When the slate already identified the desired game, pass that event's provider `eventID` as:

```text
event_id="..."
```

This targets the exact matchup instead of searching across a team's currently available upcoming events.

Use the same league/team identity that produced the event.

Omit `event_id` when you intentionally want the provider client to consider currently available upcoming team events.

## Market Comparison

`compare_sportsbook_market` compares one exact event across selected sportsbooks. It is intentionally event-targeted so comparison does not silently page through a league slate.

Supported `market` values:

- `moneyline`
- `spread`
- `total`
- `player_prop`

Game-market comparisons require `event_id`, `league`, and `market`. Optional `bookmakers` can restrict the books included. The tool makes one targeted event request containing all selected bookmaker offers for the requested main market.

For `player_prop`, also provide:

- `player_name`
- provider `team_id`
- `stat_id`

Optional `bet_type` can narrow a stat when multiple market types exist, such as `ou` or `yn`. Player-prop comparison reuses the existing exact-event prop path and remains bounded; it can require the existing roster fallback when the event payload does not embed player identity.

### Line vs. price semantics

The comparison layer does not collapse different propositions into a fake single best bet.

- Moneyline/yes-no markets can expose the highest posted American price directly because there is no handicap line.
- Spread, total, and O/U prop offers are grouped by identical posted line before price ranking.
- `bestPriceOffer` is therefore the best American price **for that exact line**.
- `mostFavorablePostedLine` reports only the most favorable posted handicap/total for the selected side and deliberately does not claim that line is the best value after accounting for price.
- `fairOdds`, `fairLine`, and related fair fields are provider consensus estimates, not project predictions.

The tool also returns implied-probability ranges, line ranges, bookmaker counts, and consensus posted lines to make sportsbook disagreement visible without placing or recommending a wager.

## Market Disagreement Discovery

The disagreement tools surface where currently selected sportsbooks differ without turning that difference into a betting recommendation or expected-value estimate.

### `find_sportsbook_market_disagreements`

This tool scans exactly **one** `get_sportsbook_slate`-equivalent provider page for one requested game market (`moneyline`, `spread`, or `total`). It accepts the same league/sport, team, bookmaker, date-window, cursor, and page-limit scope as the generic slate plus `top_n` and `min_bookmakers`.

- Moneylines rank by the largest cross-book implied-probability spread.
- Spreads and totals rank first by the largest posted-line range, then by price disagreement among books offering the **same** line.
- Only markets with at least `min_bookmakers` currently available offers are eligible.
- Exact provider `nextCursor` is returned unchanged. The tool never follows pagination automatically.

This ordering is descriptive and deliberately avoids a blended/arbitrary edge score.

### `find_sportsbook_player_prop_disagreements`

This tool requires an exact `event_id`, `player_name`, `league`, and provider `team_id`. Optional `stat_id`, `bet_type`, bookmakers, `top_n`, and `min_bookmakers` can narrow the result.

The provider path makes one exact-event prop read and may use only the existing single player-roster fallback if the event payload does not embed player identity. It does not paginate or fetch every player.

Prop results are grouped/ranked **within bet type**. O/U or spread-style props use posted-line range first and same-line price disagreement second; no-line types such as yes/no rank by implied-probability spread. This prevents an O/U line move from being numerically compared to a yes/no price difference.

## Cross-provider Player Market Context

`get_player_prop_market_context` is a separate read-only cross-provider tool built on the exact-event player-prop disagreement path. It currently supports NFL only because the FantasyPros enrichment layer is NFL-specific.

Required:

- exact SportsGameOdds `event_id`
- `player_name`
- `league="NFL"`
- provider `team_id`

Optional filters/context:

- `stat_id`
- `bet_type`
- bookmakers
- `top_n`
- `min_bookmakers`
- FantasyPros scoring (`PPR`, `HALF`, or `STD`)
- `espn_league_id`
- `espn_year` when an ESPN league ID is also supplied

The cost/freshness contract is explicit:

- sportsbook scope/scoring/ESPN options are validated before spending sportsbook quota,
- the SportsGameOdds path remains exact-event and does not silently paginate,
- FantasyPros intelligence/news/injury/rank/projection evidence is read from the local cache only, with **zero live FantasyPros requests**,
- if `espn_league_id` is supplied, the tool makes at most one ESPN roster snapshot read,
- FantasyPros cache freshness/missing-data status is returned,
- ESPN or FantasyPros enrichment failure can degrade safely without discarding a valid sportsbook result.

The result can surface sportsbook disagreement, injury flags, recent news, expert-rank dispersion, and cross-source injury corroboration as separate evidence. It does **not** claim those signals caused the sportsbook difference and does not calculate expected value, fair odds, win probability, or a wager recommendation.

## Bookmakers

`get_supported_sportsbooks` returns the integration's default bookmaker identifiers.

Bookmaker availability is provider/market dependent. A supported identifier does not guarantee that every requested event/stat has a posted market from that book.

## Usage / Quota

`get_sportsbook_usage` returns plan/rate-limit usage fields without exposing provider account identifiers.

Use it when broad pagination or repeated prop work might materially consume plan quota.

## NFL Compatibility Tools

The NFL-focused tools remain because they are useful for the primary fantasy-football workflow.

### `get_nfl_sportsbook_slate`

Convenience NFL slate with football-oriented normalization.

### `get_nfl_player_props`

Compact NFL player props with fantasy-relevant filtering/compatibility behavior.

### `get_fantasy_market_signal`

Position-aware market evidence designed to complement fantasy analysis. It is evidence, not a guarantee or autonomous betting recommendation.

Generic multi-sport behavior should not automatically inherit NFL-specific filtering when the market semantics differ.

## Cache vs. Live Data

| Data | Cached? | Behavior |
|---|---:|---|
| Team identity metadata | Yes | 24-hour local cache |
| Sportsbook events | No | Live provider request |
| Moneylines/spreads/totals | No | Live provider request |
| Player props | No | Live provider request |
| FantasyPros context used by `get_player_prop_market_context` | Yes | Existing local FantasyPros cache only |
| Optional ESPN market context | No | At most one live roster snapshot read per context call |
| Provider usage | No | Live provider request |
| API key | Never in team cache | Server-side configuration only |

## Example MCP Requests

- “Find the SportsGameOdds teamID for the Denver Broncos.”
- “Show me the Broncos sportsbook slate for Sunday.”
- “Show me the college football slate for Saturday.”
- “Get NCAAF moneylines, spreads, and totals from DraftKings and FanDuel.”
- “Continue that slate using the returned nextCursor.”
- “Use this teamID to show only Denver events.”
- “Using the eventID from the slate, get player props for this player in that exact game.”
- “Compare the Broncos spread across DraftKings, FanDuel, BetMGM, and Caesars for this eventID.”
- “Compare Bo Nix passing-yards lines across books for this exact game.”
- “On this NFL slate page, show me the games where spread lines disagree most across books.”
- “For this exact game, show me Bo Nix prop markets where sportsbooks disagree most.”
- “For this exact Bo Nix game, combine the prop disagreement with FantasyPros context and my ESPN league.”
- “Show me the NBA slate.”
- “Get Bo Nix NFL player props.”
- “How much SportsGameOdds quota have I used?”
