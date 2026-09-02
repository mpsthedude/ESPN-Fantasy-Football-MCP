## Summary

Describe what this PR changes and why.

## Scope

- Affected area(s):
- User-visible behavior:
- Provider/runtime boundaries affected:

## Testing

Describe focused tests and full validation performed.

```text
uv run python -m unittest discover
```

## Contract impact

- [ ] No MCP tools are added, removed, or renamed.
- [ ] Tool-count/documentation contracts were updated if the tool surface changed.
- [ ] The 47 Read / 5 Write annotation contract remains accurate or was intentionally updated.
- [ ] ESPN remains the only supported fantasy-league platform.
- [ ] SportsGameOdds behavior remains read-only.
- [ ] No betting EV, fair-odds, win-probability, or wager-execution claim was introduced.
- [ ] Provider pagination/quota behavior remains explicit and bounded.

## Security / privacy

- [ ] No ESPN cookies, SWIDs, API keys, credentials, personal league configuration, or generated user data are included.
- [ ] Error/log paths were reviewed if authentication or provider access changed.
- [ ] Tests use synthetic credentials and mocked provider calls.

## Documentation

- [ ] README/docs/changelog were updated where public behavior changed.
- [ ] No documentation change is required.