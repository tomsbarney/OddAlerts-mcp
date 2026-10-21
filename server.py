"""OddAlerts MCP server.

Gives Claude tools to rank upcoming football fixtures for goals, corners and
cards using the OddAlerts Football Data API (https://data.oddalerts.com/api).

Run locally (Claude Desktop / Claude Code):   python server.py
Run as a hosted connector (Claude app):       python server.py --http

Only dependency: mcp (pip install -r requirements.txt). HTTP calls use the
standard library so there is nothing else to install.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = os.environ.get("ODDALERTS_BASE_URL", "https://data.oddalerts.com/api").rstrip("/")
ALL_INCLUDES = "probability,odds,stats,timings,correctScores,referee,h2h"
MAX_TEXT = 40_000  # keep raw responses small enough for Claude to read

# Words that identify each market in OddAlerts field names.
MARKET_WORDS: dict[str, tuple[str, ...]] = {
    "goals": ("goal", "btts"),
    "corners": ("corner",),
    "cards": ("card", "booking"),
}

mcp = MCPServer(
    name="oddalerts",
    instructions=(
        "Tools for OddAlerts football data. To find the best fixtures for a market, "
        "call find_top_fixtures with market='goals', 'corners' or 'cards' and an optional "
        "line (e.g. 2.5 goals, 9.5 corners, 3.5 cards). If the ranking field looks wrong, "
        "call discover_fields once to see the exact field names OddAlerts returns, then pass "
        "sort_field explicitly. Probabilities are OddAlerts model outputs, not certainties."
    ),
)


# ---------------------------------------------------------------- HTTP layer

class OddAlertsError(ToolError):
    """Raised with a plain-English message that Claude sees and can relay."""


def _token() -> str:
    token = os.environ.get("ODDALERTS_API_TOKEN", "").strip()
    if not token:
        raise OddAlertsError(
            "ODDALERTS_API_TOKEN is not set. Add your token from oddalerts.com/football-data-api "
            "to the server's environment (.env file or connector config)."
        )
    return token


def _get_sync(path: str, params: dict[str, Any]) -> Any:
    query = {k: v for k, v in params.items() if v not in (None, "", False)}
    query["api_token"] = _token()
    url = f"{BASE_URL}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "oddalerts-mcp/1.0"})

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:  # OddAlerts allows ~100 requests/minute
                retry = e.headers.get("Retry-After")
                time.sleep(float(retry) if retry and retry.isdigit() else 5 * (attempt + 1))
                continue
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code in (401, 403):
                raise OddAlertsError(f"OddAlerts rejected the API token (HTTP {e.code}). Check your plan includes API access. {body}")
            raise OddAlertsError(f"OddAlerts returned HTTP {e.code} for /{path}: {body}")
        except urllib.error.URLError as e:
            raise OddAlertsError(f"Could not reach OddAlerts: {e.reason}")
        except json.JSONDecodeError:
            raise OddAlertsError(f"OddAlerts returned a non-JSON response for /{path}")
    raise OddAlertsError("OddAlerts rate limit hit repeatedly; try again in a minute.")


async def _get(path: str, params: dict[str, Any]) -> Any:
    return await asyncio.to_thread(_get_sync, path, params)


def _data(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else [data]
    return payload if isinstance(payload, list) else []


async def _upcoming(
    *, include: str | None, competitions: str | None, seasons: str | None, teams: str | None,
    has_odds: bool, days_ahead: float, max_pages: int,
) -> list[dict[str, Any]]:
    """Page through /fixtures/upcoming until fixtures fall outside the window."""
    cutoff = time.time() + days_ahead * 86400
    out: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = await _get("fixtures/upcoming", {
            "include": include, "competitions": competitions, "seasons": seasons,
            "teams": teams, "has_odds": 1 if has_odds else None, "page": page,
        })
        rows = _data(payload)
        out.extend(r for r in rows if (r.get("unix") or 0) <= cutoff)
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        last_unix = max((r.get("unix") or 0 for r in rows), default=0)
        if not rows or not info.get("next_page_url") or last_unix > cutoff:
            break
    return out


# ------------------------------------------------------------ field helpers

def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """{'probability': {'o95_corners': 61}} -> {'probability.o95_corners': 61}."""
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:25]):
            flat.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        flat[prefix] = obj
    return flat


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _line_tokens(line: float) -> list[str]:
    s = f"{line:g}"            # 2.5 -> "2.5", 10.5 -> "10.5"
    compact = s.replace(".", "")  # "25", "105"
    return [f"o{compact}", f"over{compact}", f"over_{compact}", f"over{s}", f"over_{s}", f"o_{compact}", s, s.replace(".", "_")]


def _market_fields(flat: dict[str, Any], market: str, line: float | None) -> dict[str, float]:
    words = MARKET_WORDS[market]
    skip_roots = ("home_goals", "away_goals", "corners", "home_played", "away_played")  # result fields, not predictions
    found: dict[str, float] = {}
    for path, val in flat.items():
        low = path.lower()
        if low in skip_roots or not any(w in low for w in words):
            continue
        n = _num(val)
        if n is None:
            continue
        if line is not None:
            leaf = low.rsplit(".", 1)[-1]
            if not any(t in leaf or t in low for t in _line_tokens(line)):
                continue
            if re.search(rf"(^|[._])u(nder)?_?{f'{line:g}'.replace('.', '')}", leaf):
                continue  # skip the under side
        found[path] = n
    return found


def _ko(f: dict[str, Any]) -> str:
    return f.get("ko_human") or f.get("date") or ""


# ------------------------------------------------------------------- tools

@mcp.tool()
async def find_top_fixtures(
    market: Literal["goals", "corners", "cards"],
    line: float | None = None,
    days_ahead: float = 2,
    competitions: str | None = None,
    seasons: str | None = None,
    teams: str | None = None,
    has_odds: bool = True,
    limit: int = 15,
    sort_field: str | None = None,
    max_pages: int = 4,
) -> dict[str, Any]:
    """Rank upcoming fixtures by OddAlerts' probability for a goals, corners or cards market.

    Args:
        market: "goals", "corners" or "cards".
        line: Over line to rank by, e.g. 2.5 (goals), 9.5 (corners), 3.5 (cards). Omit to see every field for the market.
        days_ahead: How far ahead to look (OddAlerts serves up to 7 days).
        competitions: Comma-separated OddAlerts competition IDs (max 25), e.g. to limit to European top flights.
        seasons: Comma-separated OddAlerts season IDs (max 25).
        teams: Comma-separated OddAlerts team IDs (max 25).
        has_odds: Only fixtures with bookmaker odds (filters out obscure games). Default true.
        limit: How many fixtures to return.
        sort_field: Exact flattened field to rank by, e.g. "probability.o95_corners". Use discover_fields to find it.
        max_pages: Pages of 250 fixtures to scan. Each page is one API call.
    """
    fixtures = await _upcoming(
        include="probability,odds,referee", competitions=competitions, seasons=seasons,
        teams=teams, has_odds=has_odds, days_ahead=days_ahead, max_pages=max_pages,
    )
    if not fixtures:
        return {"message": "No upcoming fixtures matched those filters.", "fixtures": []}

    per_fixture = []
    field_counts: Counter[str] = Counter()
    for f in fixtures:
        flat = _flatten(f)
        fields = {sort_field: _num(flat.get(sort_field))} if sort_field else _market_fields(flat, market, line)
        fields = {k: v for k, v in fields.items() if v is not None}
        field_counts.update(k for k in fields if k.startswith("probability"))
        per_fixture.append((f, flat, fields))

    # Pick the ranking field: explicit, else the most common probability field for this market/line.
    rank_by = sort_field or (field_counts.most_common(1)[0][0] if field_counts else None)
    if not rank_by:
        sample = sorted({k for _, flat, _ in per_fixture[:20] for k in flat if k.startswith("probability")})[:60]
        return {
            "message": (
                f"No probability fields matched market '{market}'"
                + (f" line {line:g}" if line is not None else "")
                + ". Call discover_fields to see what OddAlerts returns, then retry with sort_field."
            ),
            "fixtures_scanned": len(fixtures),
            "probability_fields_seen": sample,
        }

    ranked = sorted(
        (x for x in per_fixture if _num(x[1].get(rank_by)) is not None),
        key=lambda x: _num(x[1].get(rank_by)) or 0, reverse=True,
    )[:limit]

    results = []
    for f, flat, fields in ranked:
        odds = {k: v for k, v in flat.items() if k.startswith("odds") and any(w in k.lower() for w in MARKET_WORDS[market])}
        results.append({
            "fixture_id": f.get("id"),
            "match": f"{f.get('home_name')} vs {f.get('away_name')}",
            "kickoff": _ko(f),
            "competition_id": f.get("competition_id"),
            "positions": f"{f.get('home_position')} v {f.get('away_position')}",
            "score": _num(flat.get(rank_by)),
            "market_fields": dict(list(fields.items())[:12]),
            "odds": dict(list(odds.items())[:12]) or None,
            "referee": flat.get("referee.name") or flat.get("referee.common_name"),
        })

    return {
        "market": market,
        "line": line,
        "ranked_by": rank_by,
        "fixtures_scanned": len(fixtures),
        "other_fields_for_market": [k for k, _ in field_counts.most_common(15) if k != rank_by],
        "fixtures": results,
    }


@mcp.tool()
async def discover_fields(fixture_id: int | None = None, keyword: str | None = None) -> dict[str, Any]:
    """Show the exact field names and sample values OddAlerts returns for a fixture with every include.

    Use this once to learn the names of the corners/cards/goals probability fields.

    Args:
        fixture_id: A fixture to inspect. Omit to use the first upcoming fixture that has odds.
        keyword: Only show fields containing this text, e.g. "corner" or "card".
    """
    if fixture_id is None:
        rows = _data(await _get("fixtures/upcoming", {"has_odds": 1}))
        if not rows:
            return {"message": "No upcoming fixtures with odds found."}
        fixture_id = rows[0]["id"]
    rows = _data(await _get(f"fixtures/{fixture_id}", {"include": ALL_INCLUDES}))
    if not rows:
        return {"message": f"Fixture {fixture_id} not found."}
    flat = _flatten(rows[0])
    if keyword:
        flat = {k: v for k, v in flat.items() if keyword.lower() in k.lower()}
    return {
        "fixture_id": fixture_id,
        "match": f"{rows[0].get('home_name')} vs {rows[0].get('away_name')}",
        "field_count": len(flat),
        "fields": dict(list(flat.items())[:400]),
    }


@mcp.tool()
async def get_fixture(fixture_id: int, include: str = "probability,odds,stats,referee,h2h") -> str:
    """Get full OddAlerts data for one fixture (probabilities, odds, stats, referee, head-to-head).

    Args:
        fixture_id: OddAlerts fixture ID (from find_top_fixtures or list_fixtures).
        include: Comma-separated includes: probability, odds, odds.live, stats, stats.periods,
                 timings, correctScores, h2h, links, referee.
    """
    payload = await _get(f"fixtures/{fixture_id}", {"include": include})
    text = json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " …[truncated; request fewer includes]"


@mcp.tool()
async def list_fixtures(
    days_ahead: float = 1,
    competitions: str | None = None,
    teams: str | None = None,
    has_odds: bool = True,
    max_pages: int = 2,
) -> dict[str, Any]:
    """List upcoming fixtures with their IDs, kickoff and competition/team IDs (no probabilities).

    Useful for finding competition_id and team IDs to filter other tools.
    """
    rows = await _upcoming(include=None, competitions=competitions, seasons=None, teams=teams,
                           has_odds=has_odds, days_ahead=days_ahead, max_pages=max_pages)
    return {
        "count": len(rows),
        "fixtures": [
            {"fixture_id": r.get("id"), "match": f"{r.get('home_name')} vs {r.get('away_name')}",
             "kickoff": _ko(r), "competition_id": r.get("competition_id"), "season_id": r.get("season_id"),
             "home_id": r.get("home_id"), "away_id": r.get("away_id")}
            for r in rows[:300]
        ],
    }


@mcp.tool()
async def simulate_fixtures(fixture_ids: list[int]) -> dict[str, Any]:
    """Run OddAlerts' 5,000-match simulation for fixtures: win/draw/loss %, BTTS %, over 2.5 %,
    expected goals and likeliest scorelines. Goals markets only (no corners/cards).

    Args:
        fixture_ids: Up to 30 fixture IDs (sent in batches of 10).
    """
    ids = list(dict.fromkeys(fixture_ids))[:30]
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        payload = await _get("predictions/generate/multiple", {"ids": ",".join(map(str, batch))})
        out.extend(_data(payload))
    return {"simulations": out}


# -------------------------------------------------------------------- main

def main() -> None:
    p = argparse.ArgumentParser(description="OddAlerts MCP server")
    p.add_argument("--http", action="store_true", help="Serve over streamable HTTP (for a hosted connector)")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = p.parse_args()

    _load_dotenv()
    if not args.http:
        mcp.run("stdio")
        return

    # A secret path stops strangers who find the URL from spending your API quota.
    secret = os.environ.get("MCP_PATH_SECRET", "").strip("/")
    path = f"/{secret}/mcp" if secret else "/mcp"
    print(f"OddAlerts MCP listening on http://{args.host}:{args.port}{path}", flush=True)
    mcp.run("streamable-http", host=args.host, port=args.port, streamable_http_path=path, stateless_http=True)


def _load_dotenv() -> None:
    """Read KEY=value lines from a .env file next to this script (no extra dependency)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()
