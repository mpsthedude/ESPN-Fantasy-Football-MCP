# Provider Credentials Setup

This guide explains how to obtain and configure the credentials used by ESPN Fantasy Football MCP.

The project uses a **bring-your-own-credentials** model. No ESPN cookies or third-party API keys are bundled with the repository, and provider secrets should never be committed to Git.

## What You Need

| Provider | Required? | Credential | Configuration name |
|---|---:|---|---|
| ESPN Fantasy Football | Required for private leagues | `espn_s2` browser cookie | `ESPN_S2` |
| ESPN Fantasy Football | Required for private leagues | `SWID` browser cookie | `ESPN_SWID` (preferred) or `SWID` |
| FantasyPros | Optional | FantasyPros API key | `FANTASYPROS_API_KEY` |
| SportsGameOdds | Optional | SportsGameOdds API key | `SPORTSGAMEODDS_API_KEY` |

Public ESPN leagues may be readable without ESPN cookies. FantasyPros and SportsGameOdds are supplemental providers; the ESPN fantasy-league features do not require either optional API.

## ESPN Private-League Credentials

ESPN does not provide a separate public developer-token flow for the unofficial Fantasy interfaces used by this project. Private-league access uses two cookies from **your own logged-in ESPN browser session**:

- `espn_s2` — the long ESPN session cookie
- `SWID` — the ESPN account identifier cookie, normally formatted with surrounding braces such as `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`

Treat both values as account credentials. In particular, `espn_s2` should be handled like a password/session token.

### Chrome / Edge

1. Sign in to [ESPN Fantasy Football](https://fantasy.espn.com/football/) with the ESPN account that can access your league.
2. Open the league you want to use and make sure the page loads while signed in.
3. Open Developer Tools with `F12` or **Right-click → Inspect**.
4. Open the **Application** tab.
5. In the left sidebar, expand **Storage → Cookies**.
6. Select the ESPN cookie domain. Depending on the browser/session, the cookies may appear under `https://www.espn.com`, `https://fantasy.espn.com`, or another ESPN domain shown in that list.
7. Find the cookie named `espn_s2` and copy its **Value** exactly.
8. Find the cookie named `SWID` and copy its **Value** exactly.
9. Keep the surrounding `{}` braces on the `SWID` value.
10. Do **not** URL-decode, re-encode, trim, or otherwise modify the `espn_s2` value. Copy it verbatim.

### Firefox

1. Sign in to [ESPN Fantasy Football](https://fantasy.espn.com/football/) and open your league.
2. Open Developer Tools with `F12`.
3. Open the **Storage** tab.
4. Expand **Cookies** and select the ESPN domain containing your authenticated session.
5. Copy the exact values for `espn_s2` and `SWID`.
6. Keep the braces around `SWID` and preserve `espn_s2` exactly as displayed.

### If the cookies are not visible

- Confirm you are actually signed in to ESPN in the same browser profile whose Developer Tools you opened.
- Navigate to your ESPN Fantasy Football league page and refresh it once, then re-check the cookie list.
- Check both `www.espn.com` and `fantasy.espn.com` entries if your browser lists both.
- Browser UI labels can change; look for the cookie/storage viewer rather than relying only on the exact menu wording above.

### Configure the ESPN values

Use the cookie named `espn_s2` as `ESPN_S2` and the cookie named `SWID` as `ESPN_SWID`:

```text
ESPN_S2=<exact espn_s2 cookie value>
ESPN_SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
```

`SWID` is also accepted as a compatibility environment-variable name, but `ESPN_SWID` is the canonical project setting.

Do not configure only one ESPN cookie. The project treats the pair as inseparable and rejects partial pairs rather than combining values from different sources.

### Finding your ESPN league ID

The league ID is not a secret. When you open a league in ESPN Fantasy Football, its URL normally contains a `leagueId` value, for example:

```text
https://fantasy.espn.com/football/league?leagueId=123456789
```

In that example, the league ID is `123456789`. The MCP can also discover and synchronize leagues for an authenticated ESPN account using its discovery tools.

### Verifying ESPN authentication

After starting the MCP with both ESPN values configured, call `authenticate` with **no arguments**. It should report the configured authentication state without returning the cookie values.

If a previously working private league later returns an authentication/authorization error, the ESPN browser session may have changed or expired. Sign in to ESPN again and repeat the cookie-copy steps to obtain the current values.

## FantasyPros API Key (Optional)

FantasyPros is optional. It adds rankings, projections, player intelligence, news, injuries, ADP, and related enrichment.

1. Review the official FantasyPros API options at [FantasyPros API Data](https://www.fantasypros.com/api-data/).
2. Request or activate an API key through the official [FantasyPros API Key Request](https://secure.fantasypros.com/api-keys/request/) page.
3. Choose access appropriate for your use case. FantasyPros distinguishes personal/non-commercial API access from commercial/redistribution use; follow the current terms shown on their API pages.
4. Copy the issued API key.
5. Configure it as:

```text
FANTASYPROS_API_KEY=<your FantasyPros API key>
```

FantasyPros access plans, limits, and licensing terms can change. The official FantasyPros API pages are the source of truth for current availability and permitted use.

## SportsGameOdds API Key (Optional)

SportsGameOdds is optional and powers the read-only sportsbook market features.

1. Open the official [SportsGameOdds pricing/signup page](https://sportsgameodds.com/pricing).
2. Create an account and select the plan appropriate for your use. SportsGameOdds currently documents a free tier for testing/small projects as well as paid plans.
3. SportsGameOdds sends/provides an API key after signup; copy that key from the provider's account/email flow.
4. Configure it as:

```text
SPORTSGAMEODDS_API_KEY=<your SportsGameOdds API key>
```

The provider supports API-key authentication using either a request header or query parameter. This project deliberately sends the key in a request header so it is not exposed in request URLs/logs.

Official reference: [SportsGameOdds Quickstart](https://sportsgameodds.com/docs/basics/quickstart).

## Recommended Configuration: MCP Host Environment

The preferred setup is to store secrets in your MCP host's environment/secret configuration rather than placing them in the repository.

Example:

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
        "FANTASYPROS_API_KEY": "<optional secret>",
        "SPORTSGAMEODDS_API_KEY": "<optional secret>"
      }
    }
  }
}
```

If you only need ESPN, omit the two optional provider keys.

## Alternative: Local `credentials.json`

The project can also read a local credentials file from:

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

This file should live outside the repository checkout and should be protected using normal operating-system file permissions. Environment values take precedence over file-backed credentials.

See [CONFIGURATION.md](CONFIGURATION.md) for the complete resolution, precedence, application-home, and state model.

## Security Rules

- Never commit real ESPN cookies or API keys.
- Never paste real credentials into a public issue, pull request, screenshot, support chat, or log.
- Do not put provider API keys in URLs when you can use headers.
- Do not share `espn_s2`; possession of a valid session token can provide access to ESPN data available to that session.
- Keep the complete ESPN pair together and from the same browser session/source.
- If an ESPN cookie is exposed, use ESPN account/session controls to invalidate/sign out sessions as appropriate, then sign in again and obtain fresh cookies.
- If a FantasyPros or SportsGameOdds key is exposed, rotate/regenerate it through that provider and update your local configuration.

For the full threat model and reporting/rotation guidance, see [../SECURITY.md](../SECURITY.md).

## Troubleshooting Checklist

If authentication is not working, check these in order:

1. Both `ESPN_S2` and `ESPN_SWID` are present for private ESPN leagues.
2. `ESPN_SWID` contains the full SWID value, including its surrounding braces when ESPN provides them.
3. `ESPN_S2` was copied verbatim and was not URL-decoded or modified.
4. The ESPN cookies came from the same logged-in browser session/account that can access the league.
5. The MCP process was restarted after changing host environment variables.
6. FantasyPros and SportsGameOdds keys are stored under the exact environment-variable names shown above.
7. Provider plan/quota limits have not been exhausted.
8. If ESPN still rejects the session, sign in again and recopy both cookies.

For configuration-resolution errors, file locations, and host examples, continue with [CONFIGURATION.md](CONFIGURATION.md).