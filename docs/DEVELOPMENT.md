# Development Guide

This guide covers normal development, testing, packaging, CI, releases, and documentation maintenance for `fantasy-football-mcp`.

## Supported Development Environment

- Python 3.12+
- `uv`
- MCP SDK major version 1 (`>=1.7,<2`)

Install/sync the locked environment:

```bash
uv sync --locked
```

Run the production server:

```bash
uv run fantasy-football-mcp
```

## Repository Structure

Key production modules:

```text
fantasy_football_server.py      unified production entry point
espn_fantasy_server.py          established core MCP tools / analysis
espn_session.py                 project-owned ESPN credential/session state
espn_transport.py               project-owned ESPN HTTP boundary
espn_reference.py               project-defined ESPN factual identifiers
espn_*_read.py                  project-owned ESPN payload readers
espn_league_discovery.py        account discovery + preview-first registry sync
fantasypros_client.py           FantasyPros provider/cache boundary
sportsgameodds_client.py        SportsGameOdds provider/cache boundary
sportsgameodds_analysis.py      sportsbook interpretation
sportsgameodds_comparison.py    pure cross-book market comparison
sportsgameodds_disagreement.py  pure cross-book disagreement ranking
sportsgameodds_tools.py         core SportsGameOdds MCP registration
sportsgameodds_disagreement_tools.py bounded disagreement MCP registration
player_market_context.py        pure cross-provider evidence composition
player_market_context_tools.py  bounded cross-provider MCP registration
app_config.py                   app-home + credential/path resolution
league_registry.py              non-secret league registry
commissioner_config.py          commissioner read/audit eligibility config
draft_strategy_store.py         persisted draft strategy state
```

Documentation:

```text
README.md
docs/ARCHITECTURE.md
docs/CONFIGURATION.md
docs/TOOL_REFERENCE.md
docs/DEVELOPMENT.md
SPORTSGAMEODDS.md
SECURITY.md
PROVENANCE.md
CHANGELOG.md
CLAUDE.md
docs/ESPN_INTEGRATION_AUDIT_2026.md
```

## Architecture Rules

### ESPN ownership

Do not reintroduce `espn-api` or a third-party cached `League` object as the production source of truth.

New ESPN reads should use:

```text
ESPNSessionManager -> ESPNTransport -> project-owned parser/read module -> MCP/business logic
```

Keep provider HTTP details out of analysis code when practical.

### Freshness

Do not apply one cache policy to every data class.

- current ESPN state: fresh/bounded project reads when required,
- live draft: fresh stateless board reads,
- FantasyPros: explicit local cache,
- SportsGameOdds team identity: 24-hour metadata cache,
- sportsbook events/odds/props: live,
- cross-provider player market context: live SGO disagreement + cache-only FantasyPros + optional one-read ESPN context,
- registry/config/strategy: persisted local state.

### Read-only provider posture

SportsGameOdds must remain read-only unless a future write-capability project is separately designed and reviewed.

Commissioner tools are analysis/audit tools. Local commissioner eligibility does not confer ESPN provider permissions.

Cross-provider player market context is also read-only. It must not convert descriptive disagreement/fantasy evidence into a wager-execution surface.

### Secrets

Never:

- commit provider credentials,
- print or return raw ESPN cookies/API keys,
- put provider API keys into request URLs,
- add secrets to league/commissioner/draft/cache state,
- make unit tests depend on real credentials.

## Unit Tests

Run the complete unit suite:

```bash
uv run python -m unittest discover
```

Tests are intended to run offline using synthetic credentials and mocked provider requests.

Useful focused suites include provider/client, unified MCP, ESPN transport/parser, registry, draft, commissioner, market-context, and package-contract tests under `tests/`.

## Tool Count Contract

The production contract is currently:

```text
37 ESPN/FantasyPros core tools
+ 2 ESPN discovery/sync tools
+ 12 SportsGameOdds tools
+ 1 cross-provider market-context tool
= 52 unified tools
```

`tests/test_unified_mcp.py` validates the unified count and the provider/orchestration-added tool names.

If a tool is intentionally changed:

1. update implementation/registration,
2. update expected tests,
3. update `docs/TOOL_REFERENCE.md`,
4. update README category/count summary,
5. update `CLAUDE.md` when maintainers/agents need a new rule,
6. update `CHANGELOG.md`.

## Performance Regression Harness

`performance_regression.py` deliberately protects the **37-tool ESPN/FantasyPros core registry**, not the unified 52-tool wrapper.

`performance_baseline.json` is therefore a core-registry calibration artifact. Its tool count should not be mechanically changed to 52.

The unified registration surface is covered separately by unified MCP tests and packaging CI.

Regenerate a performance baseline only from an intentional calibration run using the harness's explicit acceptance flow. Do not update a baseline merely to refresh its timestamp or to silence a regression.

## Build the Wheel

```bash
uv build --wheel --out-dir <temp-dir>
```

The wheel contents are controlled explicitly in `pyproject.toml`.

When adding a production module, update both:

- `[tool.hatch.build.targets.wheel].include`, and
- the package wheel/import audit in `.github/workflows/ci.yml`.

The packaging contract should fail when a required module is missing or source-only/test/local-state files leak into the wheel.

## CI Contract

The normal `CI` workflow validates the following layers.

### Locked unit matrix

- Ubuntu / Python 3.12
- Ubuntu / Python 3.13
- Windows / Python 3.12

Every matrix leg runs the full unit suite with provider credential environment names explicitly blanked.

### Fresh dependency resolution

A clean environment installs the project from `pyproject.toml` metadata rather than relying on the committed lockfile, then verifies:

- the resolved `mcp` package remains major version 1,
- the full unit suite remains green.

This is a range-contract gate, not permission to silently broaden the supported major version.

### Packaging matrix

Ubuntu and Windows packaging jobs:

1. build a wheel to runner-temp storage,
2. audit required and forbidden wheel contents,
3. install the wheel into a clean environment,
4. import every production module from `site-packages`,
5. verify the installed MCP SDK major version contract,
6. verify the retired `espn-api` dependency is absent,
7. start the installed `fantasy-football-mcp` console command outside the source checkout,
8. verify startup does not immediately crash or contaminate stdout before protocol traffic.

A source-tree-only success is not sufficient.

## Branch and PR Workflow

Use focused branches from current `main`.

For large migrations, prefer small independently verifiable commits/gates rather than one workflow that mutates many unrelated layers. If a bootstrap or migration needs temporary validation infrastructure, remove that temporary infrastructure before the final PR unless it is intentionally becoming a permanent gate.

Before merge:

- inspect the final branch diff against `main`,
- remove temporary scripts/workflows/debug files,
- run focused regression gates for the changed contract,
- open a PR,
- require normal CI to succeed on the final head,
- squash merge when the branch contains exploratory/bootstrap commit history that should not be carried onto `main`.

The repository's merged-branch cleanup workflow removes same-repository feature branches after successful merge.

## Release Flow

`pyproject.toml` is the package-version source of truth. `CHANGELOG.md` is the human release record.

Release publishing is CI-gated:

1. merge the version/changelog change to `main`,
2. normal `CI` must succeed on that exact `main` commit,
3. `Publish Release` runs from the successful CI workflow result,
4. the workflow creates the corresponding `vX.Y.Z` tag/release when needed,
5. it builds/attaches the wheel from the validated release commit/tag,
6. an already complete release is treated as a no-op.

Do not bypass normal CI by publishing an unvalidated release directly.

## Dependency Policy

Current runtime dependencies in `pyproject.toml` are intentionally minimal:

```toml
mcp[cli]>=1.7.0,<2
requests>=2.32.3
```

`espn-api` is retired and must not be reintroduced as a transitive project dependency or production import.

Normal development should use:

```bash
uv sync --locked
```

When intentionally changing dependency ranges:

- regenerate/validate the lockfile as appropriate,
- run normal locked tests,
- run the fresh-resolution contract,
- run wheel installation/import tests.

## Provider-Specific Development

### ESPN

- Put endpoint/header/cookie behavior in `espn_transport.py`.
- Put payload interpretation in the appropriate `espn_*_read.py` module.
- Keep unknown provider identifiers visible where possible rather than requiring an exhaustive copied constants table.
- Keep access/auth errors credential-safe.
- Treat undocumented discovery separately from authoritative league verification.

### FantasyPros

- Keep API-key resolution server-side.
- Preserve quota/pacing safeguards.
- Prefer cache-aware higher-level analysis to unnecessary repeated provider reads.

### SportsGameOdds

- Keep provider requests centralized in `SportsGameOddsClient`.
- Preserve explicit cursor pagination.
- Keep team cache limited to non-secret slowly changing team metadata.
- Do not persist live odds/events/props in the team metadata cache.
- Preserve exact-event targeting for player props.
- Keep generic multi-sport behavior separate from NFL compatibility filtering when their semantics differ.
- Keep disagreement discovery bounded: one explicit slate page for game markets; exact event/player scope for prop discovery; no hidden pagination or cross-bet-type blended score.

### Cross-provider market context

- Keep pure evidence composition in `player_market_context.py`; do not put provider I/O there.
- Keep orchestration in `player_market_context_tools.py` and reuse existing provider boundaries rather than duplicating HTTP logic.
- While FantasyPros is NFL-only, validate NFL scope before spending SportsGameOdds quota.
- FantasyPros context must remain cache-only in this tool; refreshing the cache is a separate explicit write-classified operation.
- Optional ESPN enrichment must remain bounded to at most one roster read per tool call and degrade safely.
- Surface FantasyPros cache freshness/missing-data state.
- Do not create a weighted edge score from sportsbook disagreement, news, injury, projection, or expert-rank dispersion without a separately validated modeling workstream.

## Documentation Maintenance

Documentation is part of the contract.

### README

Keep README concise: value proposition, setup, architecture summary, current tool count, provider overview, and links to deeper docs.

### Architecture

Update `docs/ARCHITECTURE.md` when ownership boundaries, state/freshness models, packaging boundaries, or provider flow changes.

### Configuration

Update `docs/CONFIGURATION.md` when environment variables, credential precedence, application paths, registry schemas, or cache behavior changes.

### Tool reference

Update `docs/TOOL_REFERENCE.md` whenever the MCP surface changes.

### Historical documents

Historical audit/provenance facts should remain historically accurate. When a finding is resolved, mark it as resolved/superseded rather than rewriting history to imply the original audit never found the problem.

## Security Review Checklist

Before merging provider/auth/config changes, verify:

- no secret values in code, fixtures, docs, screenshots, or workflow arguments,
- errors do not stringify credential-bearing objects/exceptions,
- request URLs do not contain API keys,
- local non-secret state rejects credential-shaped data where designed,
- tests use synthetic values,
- logs/stdout are safe for MCP host capture,
- packaged wheel does not include local config/cache artifacts.

See [../SECURITY.md](../SECURITY.md) for the security policy.