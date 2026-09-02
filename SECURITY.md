# Security Policy

This policy applies to the current ESPN Fantasy Football MCP source and releases. It is not a public bug-bounty program, vulnerability-response SLA, or CVE-support commitment.

## Supported Versions

Only the latest released version and current `main` receive active security fixes. There is no long-term-support branch policy.

## Reporting a Security Issue

Do not publish credentials, exploit details, or sensitive vulnerability information in public issues, pull requests, screenshots, discussions, or other public channels.

If GitHub private vulnerability reporting is enabled for the repository, prefer that channel. Otherwise contact the repository owner through an appropriate private channel.

A useful report includes:

- affected version/commit,
- reproduction steps using synthetic/redacted values,
- expected vs. actual behavior,
- likely security impact,
- suggested mitigation when known.

Never include real ESPN cookies or provider API keys.

## Security Model

`fantasy-football-mcp` is a local stdio MCP server. It is not a hosted multi-user service.

The process may connect to:

- ESPN using the user's own account/session cookies,
- FantasyPros using the user's own API key,
- SportsGameOdds using the user's own API key.

Persistent state is local to the user's application home. Provider credentials are not bundled with the repository.

## Secret Classes

Treat the following as secrets:

```text
ESPN_S2
ESPN_SWID
SWID
FANTASYPROS_API_KEY
SPORTSGAMEODDS_API_KEY
```

`espn_s2` and `SWID` should be treated like passwords/session tokens. Anyone holding a valid pair may be able to access ESPN data available to that session.

## Secret Resolution

Preferred production configuration is the MCP host's environment/keychain/secret store.

Alternative file-backed configuration:

```text
~/.fantasy-football-mcp/credentials.json
```

Environment values take precedence over file-backed provider credentials.

ESPN credentials are resolved as a complete pair. Partial pairs are rejected; the resolver does not combine one cookie from the environment with one from the file.

The unified entry point accepts `SWID` as a compatibility alias only when canonical `ESPN_SWID` is absent.

## Secret Persistence Boundaries

The following state is **not** allowed to contain provider secrets:

```text
league_registry.json
commissioner_config.json
draft_strategy/
fp_cache/
sgo_cache/
```

Where applicable, configuration validators reject secret-shaped keys such as token, password, cookie, API-key, or authorization fields.

The SportsGameOdds team cache contains non-secret team identity metadata only. It does not persist the SportsGameOdds API key, events, odds, or player-prop data.

## Logging and MCP Output

### stdout

The MCP protocol uses stdout. Production diagnostics must not contaminate stdout before/around protocol traffic.

### stderr

Errors may be written to stderr for host diagnostics, but secret values must be redacted before output.

The ESPN boundary deserves special care because provider access failures historically created risk of credential-bearing exception strings. The current project-owned transport/session implementation must keep access/auth failures safe regardless of upstream provider response content.

### Tool responses

Do not return:

- raw ESPN cookies,
- FantasyPros/SportsGameOdds API keys,
- provider request authorization headers,
- credential-bearing exception text,
- unnecessary account identifiers.

## Provider Request Rules

### ESPN

- Cookies are used only for authenticated ESPN requests where required.
- Error translation must not serialize cookie values.
- The project uses its own transport/session/parser boundary; `espn-api` is not a runtime dependency.
- Account discovery uses an undocumented fan-profile interface and therefore must remain isolated, bounded, and followed by authoritative league verification.

### FantasyPros

- API key resolution is server-side.
- Keys belong in request headers, not URLs or tool arguments.
- Quota/pacing safeguards should remain in the provider client.

### SportsGameOdds

- API key resolution is server-side.
- Keys are sent in the provider request header, not URLs.
- Market tools are read-only.
- Team cache writes must remain limited to non-secret metadata.
- Cursor pagination should remain explicit to avoid hidden/unbounded provider consumption.

## Credential Exposure Response

### ESPN cookies exposed

1. Treat the affected ESPN session as compromised.
2. Use normal ESPN account/session controls to invalidate/sign out sessions as appropriate.
3. Re-authenticate and obtain a new `espn_s2` / `SWID` pair.
4. Replace configured values in the MCP host or private credentials file.
5. Remove leaked copies from public or collaborator-visible content where possible.
6. Review logs/history for additional disclosure.

### FantasyPros key exposed

Rotate/regenerate the key through the applicable FantasyPros account/API controls, replace local configuration, and remove leaked copies.

### SportsGameOdds key exposed

Rotate/regenerate the key through the applicable SportsGameOdds account/API controls, replace local configuration, and remove leaked copies.

The project does not implement provider token-revocation APIs; use each provider's account controls.

## Local Filesystem Expectations

Default application home:

```text
~/.fantasy-football-mcp/
```

Users are responsible for normal operating-system account/file permissions on that directory.

Prefer keeping the application home outside the source checkout. If repo-local state is required for development, use a gitignored private directory such as `local_config/` and avoid real credentials in the repository tree when possible.

`.gitignore` is defense in depth, not a secret-management system.

## Registry and Configuration Safety

`league_registry.json` is deliberately non-secret and minimal. It should contain league IDs, aliases, display names, enablement, and the default alias—not authentication state, permissions, standings, or duplicated ESPN data.

`commissioner_config.json` is a local read/audit eligibility allowlist. It does not grant provider permissions.

Registry synchronization must remain preview-first and require explicit confirmation for writes.

## Draft State Safety

Draft strategy artifacts may contain league strategy/recommendation context but must not contain provider credentials.

Factual live draft state should be re-read from ESPN rather than trusted from persisted recommendation artifacts.

## Dependency and Supply-Chain Expectations

Runtime dependencies are intentionally minimal and declared in `pyproject.toml`.

Normal development uses the committed lockfile:

```bash
uv sync --locked
```

CI also performs fresh dependency resolution under the `mcp<2` contract, builds and installs the wheel in clean environments, verifies every production import comes from the installed package, and asserts the retired `espn-api` dependency is absent.

When changing dependency ranges, review both direct and transitive security implications and require the full packaging CI gate.

## Test Safety

Tests must use synthetic/redacted credentials and mocked provider requests. Do not add tests that require real ESPN cookies or provider API keys.

CI explicitly blanks supported provider credential environment variables to reduce the risk of accidental ambient-secret use.

## Security Review Checklist

For auth/provider/config/state changes, verify:

- [ ] no real secrets in code, tests, docs, workflow files, screenshots, or commits,
- [ ] no secret values in exception text or logs,
- [ ] no API keys in URLs,
- [ ] no secrets in registry/commissioner/draft/cache state,
- [ ] stdout remains protocol-clean,
- [ ] tests use synthetic values,
- [ ] provider pagination/network work is bounded,
- [ ] package artifacts exclude local config/cache/test files,
- [ ] fresh dependency and wheel-install gates pass.

## Scope Limitations

This policy does not claim that ESPN's unofficial Fantasy interfaces, FantasyPros, or SportsGameOdds are stable or officially supported for every use. Users are responsible for their provider accounts, applicable terms, and local machine security.

For configuration details see [docs/CONFIGURATION.md](docs/CONFIGURATION.md). For architecture/provenance see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [PROVENANCE.md](PROVENANCE.md).
