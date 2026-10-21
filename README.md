# OddAlerts for Claude

An MCP server that lets Claude rank upcoming football fixtures for **goals, corners and BTTS**, and check value against bookmaker prices, using your OddAlerts API token.

## What Claude can do with it

| Tool | What it does |
|---|---|
| `find_top_fixtures` | Ranks upcoming fixtures for over goals (0.5–4.5), over corners (4.5–11.5) or BTTS, by OddAlerts' probability or by value (edge) against the bookmaker price. Filters out youth, women's and reserve games by default. |
| `find_cards_and_corners` | Ranks upcoming games for corners **and** cards together: OddAlerts' corners probability for the top games, combined with both teams' recent card record (all matches, not just head-to-head). |
| `team_form` | Any team's average cards and corners (for, against and match total) over its last N matches, plus how often its games went over a line. |
| `get_fixture` | Full detail for one game: probabilities, odds, stats, referee, head-to-head. |
| `simulate_fixtures` | OddAlerts' 5,000-run simulation: win/draw/loss, BTTS, over 2.5, expected goals, likeliest scores. |
| `list_fixtures` | Upcoming games with IDs, so you can find competition and team IDs to filter by. |
| `discover_fields` | Shows the exact field names OddAlerts returns. Claude uses it if a ranking comes back empty. |

Example prompts:
- "Best 10 games for over 9.5 corners in the next 2 days"
- "Best value over 2.5 goals bets this weekend, odds of at least 1.6"
- "Top over 2.5 goals games tomorrow, then run simulations on the top 5"
- "Games today most likely to have over 9.5 corners and over 3.5 cards"

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
- **Field names** (confirmed on the live API): goals `probability.o25` etc., corners `probability.o9_corners` = over 9.5 (10+), BTTS `probability.btts`. Matching prices come from `odds.total_goals.over_25`, `odds.total_corners.over_95` and `odds.btts.yes`.
- **Cards.** OddAlerts' API has no card probabilities or card odds. Instead, `team_form` and `find_cards_and_corners` read each team's recent finished matches (`/fixtures/team/{id}` with `include=stats`) and count yellow + red cards. Friendlies are skipped. `find_cards_and_corners` scores each game as corners probability × cards hit rate, which treats the two as independent: use it to rank games, not as an exact probability. It doesn't account for the referee.
- **API calls.** `find_cards_and_corners` checks 20 games by default: up to 4 calls for the corners scan plus 2 per game (one per team), so about 44 calls.
- **Edge.** `edge_pct` = OddAlerts' probability minus the bookmaker's implied probability (with the margin removed when both over and under prices exist). Positive means the model rates the bet higher than the price does.
- **Rate limits.** OddAlerts allows roughly 100 requests a minute. A ranking scans up to 4 pages of 250 fixtures (4 requests). The server retries automatically if it's throttled.
- **Narrowing to European leagues.** Use `list_fixtures` to find the `competition_id` values for the leagues you follow, then ask Claude to filter by them. Up to 25 IDs per filter.
- **Probabilities are model outputs, not guarantees.** Compare them with the bookmaker odds before staking anything.

## Testing
`python test_server.py` runs every tool against a mocked API, no token needed.
