# ESPN Fantasy Integration Audit — 2026

**Document type:** historical audit and closure record  
**Original audit date:** 2026-09-01  
**Current closure date:** 2026-09-02  
**Original baseline:** pre-migration `main` at `1fb284ee19b8e14d982972d483e8bd17a8ca3afd`  
**Current architecture:** see [ARCHITECTURE.md](ARCHITECTURE.md)

## Status

The original September 1 audit described a materially different ESPN architecture than the one now on `main`.

At the time of the audit, the project still depended on the third-party `espn-api` package and cached wrapper `League` objects for significant portions of runtime behavior. The audit recommended progressively moving transport, authentication, parsing, and freshness-critical reads behind project-owned boundaries.

That recommendation has now been implemented.

This file is retained to document **what was found, why the migration happened, and how each major finding was closed**. It must not be used as the current architecture reference.

## Executive Closure Summary

Current production ESPN behavior now uses:

```text
ESPNSessionManager
    -> ESPNTransport
        -> project-owned espn_*_read payload parsers
            -> MCP / fantasy analysis
```

The current runtime:

- does not depend on `espn-api`,
- does not cache third-party `League` object graphs as the ESPN source of truth,
- does not ship the copied `espn_constants.py` table,
- uses project-owned authentication/session state,
- uses project-owned ESPN HTTP transport,
- uses project-owned payload readers for the migrated league/roster/matchup/free-agent/draft/activity/snapshot paths,
- preserves the unofficial/undocumented nature of ESPN's Fantasy interfaces as an explicit operational risk.

The clean-provenance workstream subsequently rebuilt the remaining historical server scaffolding and was merged in PR #43.

## What the Original Audit Established

The audit's enduring factual conclusion remains important:

> The ESPN Fantasy v3-style interfaces used by this project should be treated as unofficial/undocumented provider contracts rather than a stable public developer API.

That means the project should continue to:

- isolate provider transport,
- use explicit timeouts/error translation,
- test request/response shapes,
- preserve bounded filters/views,
- keep freshness behavior explicit,
- fail safely when provider schemas drift.

The migration changed **who owns the implementation**, not whether ESPN guarantees the interface.

## Original Findings and Closure

### P0 — Wrapper access errors could expose ESPN cookies

**Original risk:** the then-locked `espn-api` version could include raw `espn_s2` / `SWID` values in access-denied exception messages, while some legacy handlers stringified exceptions.

**Closure:** resolved architecturally.

- `espn-api` is no longer a runtime dependency.
- ESPN transport/session/error behavior is project-owned.
- runtime redaction remains in place as defense in depth.
- security documentation now treats ESPN error serialization as a permanent review concern.

### P0 — Process-lifetime cached `League` objects could serve stale current state

**Original risk:** long-running MCP processes reused wrapper league objects indefinitely, making roster/standings/lineup/record state stale.

**Closure:** resolved by migration away from third-party league-object caching.

Current tools use project-owned ESPN transport plus payload readers for the migrated live/current-state paths. Freshness is now defined per data class rather than inherited from a wrapper object's lifetime.

### P1 — Standings were recreated with simplified local sorting

**Original risk:** sorting by wins/points-for could diverge from ESPN's league-specific standings behavior.

**Closure:** project-owned league payload parsing now treats ESPN-provided standings context as the provider evidence rather than relying on the old simplified wrapper-era reconstruction.

### P1 — Wrapper HTTP calls lacked an explicit project-owned transport contract

**Original risk:** timeout/retry/error behavior belonged to the third-party wrapper.

**Closure:** `espn_transport.py` is now the project request boundary. Transport-specific behavior belongs there and can be tested independently from business logic.

### P1 — Locked and fresh CI resolved materially different `espn-api` versions

**Original risk:** normal and fresh environments validated different wrapper releases with different behavior/security characteristics.

**Closure:** `espn-api` was retired entirely. Current fresh dependency CI instead protects the MCP SDK major-version range and the project's own unit/package behavior.

Packaging CI explicitly asserts that `espn_api` is absent from the installed environment.

### P1 — Raw secrets were duplicated into wrapper cache identifiers

**Original risk:** cached league keys included raw ESPN credential values.

**Closure:** the wrapper league-object cache architecture was removed. `ESPNSessionManager` stores active credentials as session state without constructing secret-bearing league cache identifiers.

### P1 — Matchup logic hardcoded a fixed scoring-week range

**Original risk:** the legacy path assumed NFL-style fixed weeks rather than resolving valid periods from league/provider context.

**Closure:** project-owned matchup readers resolve matchup/scoring context from ESPN payload/settings rather than relying on the original hardcoded wrapper-era guard.

### P1 — Wrapper `League.refresh()` was unsafe as a generic freshness fix

**Original risk:** wrapper refresh behavior did not preserve all football-specific settings semantics needed by the project.

**Closure:** the project did not adopt `League.refresh()`. Freshness-critical paths moved to project-owned stateless/bounded reads instead.

### P2 — Wrapper scoring parser mutated shared module-level constants

**Original risk:** league scoring rules could contaminate other league instances through shared mutable compatibility data.

**Closure:** the runtime no longer depends on the wrapper parser/constants path. The copied compatibility table was later removed as part of clean provenance and replaced by `espn_reference.py` for the smaller factual identifier set required by current parsers.

### P2 — Wrapper live-draft state was stale/mutation-prone

**Original risk:** pre-draft state and repeated refresh behavior were unsuitable as authoritative live draft state.

**Closure:** the project's fresh/stateless live-draft pattern became the architectural model. Current draft tooling separates factual board reads from persisted recommendation strategy and performs final-state revalidation.

### P2 — ESPN league discovery used an undocumented fan-profile endpoint

**Original risk:** discovery could drift independently of the standard Fantasy league endpoint.

**Status:** intentionally retained with containment, not “fixed away.”

Current design:

1. uses the fan-profile interface only for candidate discovery,
2. parses defensively and bounds candidates,
3. verifies every candidate against the ESPN Fantasy league endpoint,
4. keeps discovery read-only,
5. keeps registry synchronization preview-first and explicitly confirmed.

This remains one of the highest-drift ESPN surfaces and should stay isolated.

### P2 — Auth guidance assumed manual credential tool calls

**Original risk:** older messages told users to call `authenticate` with cookies even after the unified host model supported server-side credentials.

**Closure:** the current session/auth contract is environment/config aware. `authenticate()` with no arguments reports/uses configured auth; complete explicit overrides remain available; partial pairs and literal `ENV` placeholders are rejected.

## Migration Workstreams Triggered by This Audit

The audit was followed by staged ESPN ownership work including:

- project-owned league context reads,
- commissioner foundation/current-state reads,
- commissioner activity parsing,
- commissioner investigation/matchup evidence,
- live draft transport migration,
- complete `espn-api` runtime retirement,
- project-owned session/authentication state,
- project-owned ESPN reference identifiers,
- clean-provenance rebuild of the surviving historical server scaffolding.

The migrations were intentionally staged and regression-gated rather than performed as one large unvalidated rewrite.

## Clean-Provenance Follow-Up

After `espn-api` retirement, a separate provenance review identified remaining historical implementation structure and inherited/copy-derived artifacts that should not remain the basis for current ownership claims.

The clean-provenance workstream:

- structurally rebuilt the surviving authentication/session and compatibility-tool scaffolding,
- removed `espn_constants.py`,
- introduced `espn_session.py` / `espn_reference.py` into the final packaged contract,
- updated README/licensing/provenance language,
- removed the inherited Kyle Brogan MIT `LICENSE` file,
- removed the repository's own MIT package metadata,
- ran focused and complete regression tests,
- ran a source-overlap gate against the historical original server,
- passed normal Linux/Windows/fresh-dependency/wheel CI,
- squash-merged as PR #43 / commit `6573834060f37ee67631b1013251ff4099011d72`.

See [../PROVENANCE.md](../PROVENANCE.md).

## Current ESPN Module Map

| Concern | Current owner |
|---|---|
| Credential/session lifecycle | `espn_session.py` |
| HTTP request boundary | `espn_transport.py` |
| Factual ESPN identifiers | `espn_reference.py` |
| League/settings/standings/team parsing | `espn_league_read.py` |
| Rosters/lineups/player parsing | `espn_roster_read.py` |
| Matchups/scoring evidence | `espn_matchup_read.py` |
| Free-agent/waiver parsing | `espn_free_agent_read.py` |
| Historical lineup evidence | `espn_historical_lineup_read.py` |
| Draft payload parsing | `espn_draft_read.py` |
| Compact snapshots | `espn_snapshot_read.py` |
| Activity/transaction evidence | `espn_activity_read.py` |
| Account discovery/verification | `espn_league_discovery.py` |
| Core MCP/business orchestration | `espn_fantasy_server.py` |
| Unified production registration | `fantasy_football_server.py` |

## Remaining ESPN Risks

The migration does not eliminate provider risk.

### Unofficial interfaces

ESPN may change endpoints, views, filters, payload schemas, cookie requirements, or error behavior without a public compatibility commitment.

### Discovery drift

The fan-profile discovery interface is especially drift-prone and should remain optional/isolated.

### Schema assumptions

Project-owned parsers are easier to test and fix, but they still depend on observed ESPN payload shapes. Synthetic regression fixtures should cover every meaningful parser contract.

### Provider availability / throttling

The project should continue to bound broad reads and avoid unnecessary request amplification. No assumption should be made that an undocumented interface has unlimited or stable rate behavior.

## Rules Going Forward

1. Do not reintroduce `espn-api` as the runtime abstraction.
2. Keep ESPN requests centralized in project-owned transport.
3. Keep payload parsing separate from network behavior.
4. Treat current/live data freshness explicitly by data class.
5. Keep fan-profile discovery isolated and verified.
6. Keep auth/error paths secret-safe.
7. Prefer bounded focused reads over broad hidden provider work.
8. Preserve offline regression tests using synthetic payloads/credentials.
9. Update [ARCHITECTURE.md](ARCHITECTURE.md) for current design changes; do not use this historical record as the live architecture document.

## Related Documents

- [Architecture](ARCHITECTURE.md)
- [Configuration](CONFIGURATION.md)
- [Development](DEVELOPMENT.md)
- [Security](../SECURITY.md)
- [Provenance](../PROVENANCE.md)
- [Changelog](../CHANGELOG.md)