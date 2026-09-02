# Contributing to ESPN Fantasy Football MCP

Thanks for contributing to ESPN Fantasy Football MCP.

This project intentionally supports **ESPN Fantasy Football as its only fantasy-league platform**. FantasyPros and SportsGameOdds are optional supplemental providers.

## Before You Start

Please review:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DEVELOPMENT.md`
- `docs/TOOL_REFERENCE.md`
- `SECURITY.md`
- `PROVENANCE.md`

For security-sensitive reports, follow `SECURITY.md` rather than opening a public issue.

## Development Setup

Requirements:

- Python 3.12+
- `uv`

Install and test:

```bash
uv sync --locked
uv run python -m unittest discover
```

Build a wheel when packaging behavior changes:

```bash
uv build --wheel --out-dir dist
```

Tests must remain offline and use synthetic credentials and mocked provider requests.

## Project Boundaries

- ESPN Fantasy Football is the only supported fantasy-league platform.
- ESPN transport/session/payload parsing remains project-owned.
- `espn-api` is retired and must not be reintroduced.
- FantasyPros is optional.
- SportsGameOdds is optional and read-only.
- No tool may place, modify, or cancel a wager.
- Sportsbook disagreement/context must remain descriptive and must not invent EV, fair odds, win probability, or betting-edge percentages.
- Provider pagination and quota-consuming behavior should remain explicit and bounded.
- Secrets must never appear in source, tests, logs, errors, screenshots, issues, or pull requests.

## Tool Surface Contract

The current unified MCP surface contains 52 tools:

```text
37 ESPN/FantasyPros core
+ 2 ESPN discovery/sync
+ 12 SportsGameOdds
+ 1 cross-provider market context
= 52
```

The current annotation contract is 47 Read / 5 Write.

When adding, removing, or renaming a tool, update the implementation, tests, tool reference, README counts, maintainer documentation, and changelog together.

Do not mechanically change `performance_baseline.json` to match the unified tool count. The performance harness intentionally covers the 37-tool ESPN/FantasyPros core.

## Pull Requests

Keep pull requests focused and independently verifiable. Explain the user-visible change, provider/runtime boundaries, focused tests, full validation, documentation impact, and any quota/authentication/security implications.

Normal changes should go through a pull request and pass CI before squash merge.

## Credentials and Test Data

Never commit real ESPN cookies, SWIDs, FantasyPros API keys, SportsGameOdds API keys, `.env`, `credentials.json`, personal league registries/configuration, or generated user-specific provider data.

Use obvious synthetic placeholders in tests and examples.

## License

Contributions accepted into this repository are distributed under the repository's MIT License.

## Reporting Bugs

Use the Bug Report issue form. Include project version/commit, OS, Python version, MCP host, affected provider, reproducible steps, and sanitized logs. Never include credentials.

## Feature Requests

Feature requests should describe the problem first, then the proposed behavior. Requests for Sleeper, Yahoo, NFL.com, or other fantasy-league platforms are outside this repository's intended scope.

## Security Issues

Do not disclose suspected credential leakage, authentication bypasses, secret exposure, or other sensitive vulnerabilities in a public issue. Follow `SECURITY.md`.