# OddAlerts for Claude

An MCP server that lets Claude rank upcoming football fixtures for **goals, corners and cards** using your OddAlerts API token.

## What Claude can do with it

| Tool | What it does |
|---|---|
| `find_top_fixtures` | Ranks upcoming fixtures by OddAlerts' probability for a market and line, e.g. corners over 9.5, cards over 3.5, goals over 2.5. Includes matching odds and referee. |
| `get_fixture` | Full detail for one game: probabilities, odds, stats, referee, head-to-head. |
| `simulate_fixtures` | OddAlerts' 5,000-run simulation: win/draw/loss, BTTS, over 2.5, expected goals, likeliest scores. |
| `list_fixtures` | Upcoming games with IDs, so you can find competition and team IDs to filter by. |
| `discover_fields` | Shows the exact field names OddAlerts returns. Claude uses it if a ranking comes back empty. |

Example prompts:
- "Best 10 games for over 9.5 corners in the next 2 days"
- "Which fixtures this weekend look strongest for over 3.5 cards? Check the referees too."
- "Top over 2.5 goals games tomorrow, then run simulations on the top 5"

## Requirements
- Python 3.10 or newer
- An OddAlerts plan with API access (token from https://oddalerts.com/football-data-api)

## Option A — Claude Desktop (simplest, runs on your computer)

1. Unzip this folder somewhere permanent, e.g. `~/oddalerts-mcp`.
2. In a terminal inside the folder:
   ```
   pip install -r requirements.txt
   cp .env.example .env
   ```
   Open `.env` and paste your token after `ODDALERTS_API_TOKEN=`.
3. Open Claude Desktop's config file (`claude_desktop_config.json`, reachable from its developer settings) and add:
   ```json
   {
     "mcpServers": {
       "oddalerts": {
         "command": "python3",
         "args": ["/FULL/PATH/TO/oddalerts-mcp/server.py"]
       }
     }
   }
   ```
   On Windows use `"command": "python"` and a path like `"C:\\Users\\you\\oddalerts-mcp\\server.py"`.
4. Restart Claude Desktop. The OddAlerts tools appear under the tools icon.

For **Claude Code**: `claude mcp add oddalerts -- python3 /FULL/PATH/TO/oddalerts-mcp/server.py`

## Option B — Claude app on web and mobile (hosted)

1. Push this folder to a private GitHub repo.
2. On https://render.com: **New → Blueprint**, pick the repo. When asked, set:
   - `ODDALERTS_API_TOKEN` = your token
   - `MCP_PATH_SECRET` = a long random string (e.g. from a password generator)
3. When it's live, your server URL is `https://<your-app>.onrender.com/<MCP_PATH_SECRET>/mcp`.
4. Add that URL as a custom connector in Claude's settings, then enable it in a chat. It then works on mobile too.

Keep the URL private: anyone with it can use your OddAlerts quota. Render's free tier sleeps when idle, so the first call after a break can take ~30 seconds.

## Notes
- **Field names are discovered, not hard-coded.** OddAlerts' public docs don't list the corners and cards probability field names, so the server matches fields by keyword (`corner`, `card`, `goal`/`btts`) and line (`o95`, `over_3_5`, …). If a ranking comes back empty, ask Claude to run `discover_fields` with keyword "corner" or "card", then rank by the exact field it finds.
- **Rate limits.** OddAlerts allows roughly 100 requests a minute. A ranking scans up to 4 pages of 250 fixtures (4 requests). The server retries automatically if it's throttled.
- **Narrowing to European leagues.** Use `list_fixtures` to find the `competition_id` values for the leagues you follow, then ask Claude to filter by them. Up to 25 IDs per filter.
- **Probabilities are model outputs, not guarantees.** Compare them with the bookmaker odds before staking anything.

## Testing
`python test_server.py` runs every tool against a mocked API, no token needed.
