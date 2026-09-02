# Project Provenance

ESPN Fantasy Football MCP has a documented historical origin and a different current implementation boundary. This file records both so future maintainers do not confuse project history with present runtime, architectural, or licensing dependency.

## Historical Origin

The project began by extending Kyle Brogan's earlier open-source ESPN Fantasy MCP project. That repository was the starting point for the earliest version of this work and is acknowledged here for transparency.

During subsequent development, the project expanded substantially with multi-league navigation, FantasyPros integration, draft/commissioner workflows, local state management, unified packaging, SportsGameOdds, testing/CI, and provider-specific hardening.

The 2026 ESPN audit then identified that the remaining ESPN implementation still depended too heavily on third-party wrapper/runtime behavior and retained inherited/copy-derived compatibility surfaces that were undesirable for long-term ownership.

## 2026 ESPN Ownership Migration

The project moved ESPN behavior behind project-owned boundaries in stages rather than replacing every behavior in one unverified change.

The migration introduced or established:

- `espn_transport.py` — project-owned ESPN HTTP/request boundary,
- `espn_session.py` — project-owned credential/session lifecycle,
- `espn_league_read.py` — league/settings/standings/team payload parsing,
- `espn_roster_read.py` — roster/lineup/player parsing,
- `espn_matchup_read.py` — matchup/scoring evidence,
- `espn_free_agent_read.py` — waiver/free-agent payload handling,
- `espn_historical_lineup_read.py` — historical lineup evidence,
- `espn_draft_read.py` — draft payload handling,
- `espn_snapshot_read.py` — compact league snapshot parsing,
- `espn_activity_read.py` — bounded commissioner activity evidence,
- project-owned ESPN account discovery/verification,
- project-owned reference identifiers in `espn_reference.py`.

High-value paths were migrated incrementally with focused regressions and then the full unit/package gates.

## `espn-api` Retirement

The project previously used the `espn-api` Python package and, during an intermediate migration stage, carried a copied compatibility constants table derived from that package.

That runtime dependency has been retired.

Current production state:

- `pyproject.toml` does not declare `espn-api`,
- the production wheel does not require `espn-api`,
- CI explicitly verifies that `espn_api` is absent from the clean installed environment,
- `espn_constants.py` has been removed,
- the smaller `espn_reference.py` contains the project-defined factual identifier subset needed by current parsers.

## Original Server Clean-Provenance Rebuild

After the ESPN runtime dependency migration, the project performed a separate clean-provenance workstream for the remaining historical server scaffolding.

That work rebuilt the surviving session/authentication and compatibility-tool structure around project-owned components rather than merely deleting names or changing comments.

Validation included:

1. focused auth/wrapper-retirement regression tests,
2. the complete locked unit suite,
3. verification that the copied constants source/imports were gone,
4. a source comparison against Kyle Brogan's original `espn_fantasy_server.py`,
5. a post-rewrite gate that found no exact matching block of four or more consecutive lines in the compared server implementation,
6. full normal PR CI on Linux/Windows plus clean wheel packaging/install/startup.

The work was squash-merged to `main` in PR #43 as commit `6573834060f37ee67631b1013251ff4099011d72` in the historical private development repository.

## Current Production Boundary

As of September 2, 2026:

- ESPN credential/session state is owned by this repository.
- ESPN HTTP requests are owned by this repository.
- ESPN payload parsers/readers are owned by this repository.
- The runtime does not depend on the original server implementation.
- The runtime does not depend on `espn-api`.
- SportsGameOdds and FantasyPros are independent supplemental provider boundaries.
- The unified MCP entry point is project-owned and packages the current 52-tool surface.

Historical acknowledgement does **not** mean that the current production server is technically layered on the original project.

## Licensing Boundary

The inherited Kyle Brogan MIT `LICENSE` file that existed during the historical development phase was removed after the clean-provenance rebuild so that the repository would not imply that inherited licensing automatically governed newly rebuilt project-authored code.

After that rebuild and public-readiness review, the project owner made a new, explicit licensing decision: **the current project-authored source is distributed under the MIT License**. The repository's current [LICENSE](LICENSE) file is the controlling project license grant for project-authored source.

This new MIT grant is a licensing decision by the current project owner; it does not assert that the present implementation remains technically dependent on the historical server, nor does it erase the historical attribution recorded above.

Third-party dependencies remain governed by their own licenses. Provider APIs, data, service access, and trademarks remain subject to the applicable provider terms and are not relicensed by this project's MIT License. A dependency's license applies to that dependency and does not imply sponsorship or affiliation with that dependency's authors/licensors.

This provenance record is a technical/project-history record, not a substitute for formal legal advice on a particular distribution or use case.

## Attribution vs. Dependency

The project continues to acknowledge its historical origin because that history is factual. The acknowledgement should not be removed merely to make the project appear older or more independent than it was.

At the same time, documentation should not use historical wording such as “built on top of” the original server or `espn-api` to describe the **current** architecture.

Preferred current wording:

> ESPN Fantasy Football MCP began by extending an earlier open-source ESPN Fantasy MCP project. The current production implementation has since been rebuilt around project-owned ESPN transport, session/authentication, payload parsing, state management, provider integrations, packaging, and MCP tooling and no longer depends on the original server implementation or the `espn-api` Python package.

## Historical Audit Record

The original September 1, 2026 ESPN integration audit documented the problems and recommendations that drove much of this migration. It is retained as a historical/closure document rather than a current architecture specification.

See [docs/ESPN_INTEGRATION_AUDIT_2026.md](docs/ESPN_INTEGRATION_AUDIT_2026.md).

## Current Architecture

For the current runtime design, use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), not historical audit text.
