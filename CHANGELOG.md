# Changelog

All notable shared releases and unreleased `main` changes for `fantasy-football-mcp` are documented here.

## Unreleased

## 0.4.1 — 2026-09-02

Public-launch patch release. Runtime behavior and the 52-tool MCP surface are unchanged from `0.4.0`.

### Changed

- Adopted the MIT License for current project-authored source and added explicit package/repository licensing metadata.
- Corrected the GitHub release title convention to **ESPN Fantasy Football MCP vX.Y.Z**.
- Generalized MCP read/write annotation documentation so the public project describes standard MCP-host behavior rather than a specific host product.
- Preserved the 47 Read / 5 Write ToolAnnotations contract and all existing provider/runtime boundaries.

### Security / Public Readiness

- Public repository history remains intentionally separate from the historical private development repository.
- Provider credentials remain bring-your-own and are not bundled with the repository or release artifacts.
- The public release retains the ESPN non-affiliation/trademark disclaimer and provider-specific credential guidance.

## 0.4.0 — 2026-09-02

Release `0.4.0` exposes **52 MCP tools** and completes the sportsbook comparison/disagreement/context work added after `0.3.0`, while preparing the current clean source tree for a separate public ESPN-specific repository.

### Added

- `compare_sportsbook_market`, a read-only exact-event comparison tool for moneyline, spread, total, and player-prop offers across selected SportsGameOdds bookmakers.
- `sportsgameodds_comparison.py`, a pure comparison layer that keeps posted line and American price semantics separate, groups identical lines before price ranking, and exposes line/implied-probability disagreement without placing or recommending wagers.
- `find_sportsbook_market_disagreements`, a one-slate-page game-market disagreement detector with explicit cursor continuation and no hidden pagination.
- `find_sportsbook_player_prop_disagreements`, an exact-event/player prop disagreement detector that ranks within each bet type and reuses the bounded prop provider path.
- `sportsgameodds_disagreement.py` plus `sportsgameodds_disagreement_tools.py` for pure disagreement ranking and MCP registration.
- `get_player_prop_market_context`, a read-only NFL cross-provider tool that combines exact-event SportsGameOdds player-prop disagreement with cache-only FantasyPros evidence and optional one-read ESPN league player context.
- `player_market_context.py` and `player_market_context_tools.py` for pure evidence composition and bounded MCP orchestration, including FantasyPros cache freshness/data-quality reporting.

### Changed

- The unified MCP surface increases from 48 to 52 tools; MCP host annotations remain truthful at 47 Read / 5 Write.
- SportsGameOdds remains a 12-tool provider surface; the new player market-context tool is classified separately because it combines SportsGameOdds, FantasyPros, and optional ESPN evidence.
- SportsGameOdds game-market comparison is bounded to one targeted `/events` request; player-prop comparison reuses the existing bounded exact-event prop path. Game disagreement discovery ranks one explicit slate page, while player-prop disagreement requires exact event/player scope and never silently paginates.
- Cross-provider player market context validates NFL/scoring/ESPN scope before spending sportsbook quota, makes zero live FantasyPros requests, optionally makes at most one ESPN roster read, and degrades enrichment safely without discarding valid sportsbook evidence.
- SportsGameOdds provider normalization now tolerates list or ID-keyed data collections and malformed optional nested fields, with explicit contracts for auth/rate-limit/server/network/JSON failures and no hidden pagination.
- Public-facing project identity is clarified as **ESPN Fantasy Football MCP** while the compatible Python distribution and console command remain `fantasy-football-mcp`.
- README now explicitly states that ESPN Fantasy Football is the only supported fantasy-league platform and points new users to the clean `ESPN-Fantasy-Football-MCP` repository location.
- Security/provenance documentation is prepared for a public repository without changing the documented clean-provenance or provider-credential boundaries.
- Generated `sgo_cache/` state is explicitly gitignored alongside FantasyPros and draft state.

### Security / Public Readiness

- Preserved bring-your-own-provider-credential behavior; no ESPN cookies or FantasyPros/SportsGameOdds keys are bundled with the project.
- Preserved the explicit non-affiliation/endorsement disclaimer and added descriptive-use wording for the ESPN name.
- Kept the historical development repository private; the intended public repository is a clean source-tree publication rather than an exposure of historical commits and migration artifacts.
- Licensing remained an explicit owner decision in this release; the subsequent `0.4.1` public-launch patch adopts MIT.

## 0.3.0 — 2026-09-02

Release `0.3.0` exposes **48 MCP tools** and establishes the current project-owned ESPN, FantasyPros, SportsGameOdds, provenance, packaging, and MCP annotation baseline.

### Added

- Explicit MCP tool annotations for compatible hosts: 43 genuinely read-only tools publish `readOnlyHint: true`, while five session/local-state mutators remain write-classified.
- `find_sportsbook_team` for resolving human team names/abbreviations to SportsGameOdds provider `teamID` values.
- Persistent 24-hour SportsGameOdds team metadata cache under application home (`sgo_cache/`) with cache-first resolution, one-page live fallback, and explicit cursor continuation.
- Optional provider `team_id` targeting for generic sportsbook slates.
- Explicit SportsGameOdds cursor pagination on generic slates.
- Optional `starts_after` / `starts_before` date-window filtering for generic sportsbook slates.
- Optional exact `event_id` targeting for generic player props.
- `PROVENANCE.md` documenting historical origin, the 2026 clean-provenance workstream, and the current licensing boundary.
- Dedicated current documentation for architecture, configuration, the complete 48-tool surface, and development/CI/release workflow.

### Changed

- MCP initialization metadata now reports the installed `fantasy-football-mcp` package version, so hosts see Fantasy Football MCP `0.3.0` instead of the underlying MCP SDK version.
- Raised the supported MCP Python SDK floor to `>=1.7.0,<2`, the v1 line required for FastMCP tool annotations, and regenerated `uv.lock`.
- Centralized SportsGameOdds provider requests/normalization in `SportsGameOddsClient`; generic MCP helpers and NFL compatibility behavior now route through the same provider boundary.
- The unified MCP tool count increased from 47 to 48 with the SportsGameOdds team resolver.
- ESPN production behavior was migrated off cached third-party wrapper objects to project-owned transport, session, and payload-reader modules.
- ESPN account/league, roster, matchup, free-agent, historical-lineup, draft, commissioner, and related flows now use project-owned ESPN reads/parsers.
- `espn-api` was retired as a runtime dependency.
- The copied `espn_constants.py` compatibility table was removed and replaced by the smaller project-defined `espn_reference.py` vocabulary required by current parsers.
- The unified entry point now directly uses the project-owned ESPN session/authentication surface.
- CI wheel content/import contracts were updated for `espn_reference.py` and `espn_session.py`, and explicitly verify that `espn-api` remains absent from the installed wheel environment.
- `.gitignore` was hardened for repo-local application-home, credential/config files, and current/legacy generated-state directories.
- CI explicitly blanks every supported provider credential environment name, including the `SWID` compatibility alias.
- Release publishing is gated on successful `main` CI and can attach a reproducible wheel asset to GitHub Releases.
- Same-repository branches are cleaned up automatically after their pull requests are merged.
- `performance_baseline.json` remains intentionally scoped to the 37-tool ESPN/FantasyPros core registry rather than the 48-tool unified wrapper.
- Repository documentation was rebuilt around a concise README plus dedicated Architecture, Configuration, Tool Reference, Development, Security, SportsGameOdds, Provenance, and historical ESPN audit documents.

### Security / Provenance

- Removed the inherited Kyle Brogan MIT `LICENSE` file after the remaining production implementation was rebuilt across project-owned boundaries.
- Removed the repository's own inherited MIT package metadata during the clean-provenance transition; a new owner-issued MIT grant is introduced later in `0.4.1`.
- Preserved historical attribution/provenance in `PROVENANCE.md` while separating historical origin from current runtime/architectural/licensing dependency.
- Completed a clean-provenance source-overlap validation against the historical original server; the post-rewrite gate found no exact matching block of four or more consecutive lines in the compared server implementation.
- ESPN credential/session handling now uses project-owned state and safe `ENV` placeholder rejection.
- Error/log paths continue to redact configured/runtime provider secrets.
- SportsGameOdds team-cache files persist non-secret team metadata only; credentials and live events/odds/props are excluded.

## 0.2.0 — 2026-09-01

### Added

- Unified production MCP entry point exposing 47 tools through one connection.
- SportsGameOdds integration with 8 read-only market-data tools, including multi-sport slates, player props, NFL-specific helpers, fantasy market signals, usage, sportsbook IDs, and league aliases.
- ESPN authenticated account league discovery with `discover_my_espn_leagues`.
- Preview-first ESPN league registry synchronization with `sync_my_espn_leagues(confirm=false)` and explicit confirmed writes.
- MCP-host compatibility for `SWID` environment configuration.
- Regression coverage for FantasyPros process-environment API-key resolution and secret non-disclosure.

### Changed

- ESPN credentials configured in the MCP environment are bootstrapped automatically for the shared production session.
- The production `authenticate` MCP contract is environment-aware: no arguments are required when server-side ESPN credentials are already configured; explicit complete-pair overrides remain supported.
- Literal placeholder authentication such as `ENV` is rejected instead of overwriting valid loaded credentials.
- Live draft recommendation flow includes final-state revalidation and hardened strategy compatibility behavior.
- Documentation described the then-current unified ESPN + FantasyPros + SportsGameOdds architecture and 47-tool surface.

### Security

- ESPN access/auth error handling avoids serializing credential-bearing exception strings in protected paths.
- FantasyPros and SportsGameOdds API keys are resolved server-side and are not required as MCP tool arguments.
- Non-secret league/config state remains separated from provider credentials.

## 0.1.0 — 2026-08-19

Initial private shared release.

- 37 MCP tools centered on ESPN fantasy football analysis.
- Optional FantasyPros integration.
- Local-first configuration and credential handling.
- Packaged `fantasy-football-mcp` console command.
- Linux/Windows CI coverage for the initial release.
