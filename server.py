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
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = os.environ.get("ODDALERTS_BASE_URL", "https://data.oddalerts.com/api").rstrip("/")
ALL_INCLUDES = "probability,odds,stats,timings,correctScores,referee,h2h"
MAX_TEXT = 40_000  # keep raw responses small enough for Claude to read

mcp = MCPServer(
    name="oddalerts",
    instructions=(
        "Tools for OddAlerts football data. To find the best fixtures for a market, "
        "call find_top_fixtures with market='goals', 'corners' or 'btts' and an optional line "
        "(e.g. 2.5 goals, 9.5 corners). Use rank_by='edge' to find value against bookmaker prices. "
        "OddAlerts has no card probabilities or odds; for cards, check referee and head-to-head via "
        "get_fixture. Probabilities are OddAlerts model outputs, not certainties."
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


# OddAlerts field names (confirmed against the live API, Oct 2026):
#   goals    probability.o05 … o45 (over 0.5 … 4.5), probability.btts
#            odds.total_goals.over_25 / under_25, odds.btts.yes / no
#   corners  probability.o4_corners … o11_corners  (o9 = over 9.5, i.e. 10+)
#            odds.total_corners.over_95 / under_95
#   cards    no probability or odds fields; only stats after full time
DEFAULT_LINES = {"goals": 2.5, "corners": 9.5}
NON_SENIOR = re.compile(r"(\bU\d{2}\b|\bW\b|\bII\b|\bIII\b|\bRes\.?\b|\bReserves?\b|\bYouth\b|\bAcademy\b|\bB\b$)", re.I)


def _compact(line: float) -> str:
    return f"{line:g}".replace(".", "")  # 2.5 -> "25", 9.5 -> "95"


def _prob_field(market: str, line: float) -> str:
    if market == "goals":
        return f"probability.o{_compact(line)}"
    return f"probability.o{int(line)}_corners"  # 9.5 -> o9_corners


def _odds_fields(market: str, line: float) -> tuple[str, str]:
    group = "total_goals" if market == "goals" else "total_corners"
    c = _compact(line)
    return f"odds.{group}.over_{c}", f"odds.{group}.under_{c}"


def _value(prob: float | None, over: float | None, under: float | None) -> dict[str, Any]:
    """Compare OddAlerts' probability with the bookmaker price (margin removed when both sides exist)."""
    if prob is None or not over or over <= 1:
        return {"over_odds": over, "implied_pct": None, "edge_pct": None}
    implied = 100 / over
    if under and under > 1:
        implied = implied / (100 / over + 100 / under) * 100  # normalise out the bookmaker margin
    return {"over_odds": over, "under_odds": under, "implied_pct": round(implied, 1), "edge_pct": round(prob - implied, 1)}


def _is_senior(f: dict[str, Any]) -> bool:
    return not any(NON_SENIOR.search(str(f.get(k) or "")) for k in ("home_name", "away_name"))


def _ko(f: dict[str, Any]) -> str:
    return f.get("ko_human") or f.get("date") or ""


# ------------------------------------------------------------------- tools

@mcp.tool()
async def find_top_fixtures(
    market: Literal["goals", "corners", "btts", "cards"],
    line: float | None = None,
    rank_by: Literal["probability", "edge"] = "probability",
    days_ahead: float = 2,
    senior_men_only: bool = True,
    competitions: str | None = None,
    seasons: str | None = None,
    teams: str | None = None,
    has_odds: bool = True,
    min_odds: float | None = None,
    limit: int = 15,
    sort_field: str | None = None,
    max_pages: int = 4,
) -> dict[str, Any]:
    """Rank upcoming fixtures for an over goals, over corners or BTTS bet using OddAlerts' model.

    Each result shows OddAlerts' probability, the bookmaker over/under price, the bookmaker's
    implied probability (margin removed) and the edge (model minus implied). Positive edge = value.

    Args:
        market: "goals" (over X.5), "corners" (over X.5), or "btts". "cards" is not supported by
                OddAlerts' API (no card probabilities or odds) and returns guidance instead.
        line: Goals 0.5-4.5 (default 2.5); corners 4.5-11.5 (default 9.5). Ignored for btts.
        rank_by: "probability" (most likely) or "edge" (best value against the bookmaker price).
        days_ahead: How far ahead to look (OddAlerts serves up to 7 days).
        senior_men_only: Drop U19/U21, women's, reserve and II teams. Default true.
        competitions: Comma-separated OddAlerts competition IDs (max 25). Find them with list_fixtures.
        seasons / teams: Comma-separated OddAlerts IDs (max 25 each).
        has_odds: Only fixtures with bookmaker odds. Default true.
        min_odds: Skip fixtures whose over price is below this (e.g. 1.5 to avoid very short prices).
        limit: How many fixtures to return.
        sort_field: Advanced: rank by any exact field from discover_fields instead.
        max_pages: Pages of 250 fixtures to scan (one API call each).
    """
    if market == "cards":
        return {
            "message": (
                "OddAlerts' API has no card probabilities and no card odds, so fixtures can't be ranked "
                "for cards automatically. What is available: the referee and past card counts in "
                "head-to-head meetings (get_fixture with include='referee,h2h'), and card counts "
                "in finished matches. Shortlist fixtures (e.g. derbies, relegation games) and check those."
            ),
            "fixtures": [],
        }

    if market == "btts":
        prob_field, over_f, under_f, ln = "probability.btts", "odds.btts.yes", "odds.btts.no", None
    else:
        ln = line if line is not None else DEFAULT_LINES[market]
        if market == "corners" and not (4 <= ln <= 11.5):
            return {"message": "OddAlerts provides corner probabilities for over 4.5 up to over 11.5 only.", "fixtures": []}
        if market == "goals" and not (0.5 <= ln <= 4.5):
            return {"message": "OddAlerts provides goals probabilities for over 0.5 up to over 4.5 only.", "fixtures": []}
        prob_field = _prob_field(market, ln)
        over_f, under_f = _odds_fields(market, ln)
    prob_field = sort_field or prob_field

    fixtures = await _upcoming(
        include="probability,odds,referee", competitions=competitions, seasons=seasons,
        teams=teams, has_odds=has_odds, days_ahead=days_ahead, max_pages=max_pages,
    )
    scanned = len(fixtures)
    if senior_men_only:
        fixtures = [f for f in fixtures if _is_senior(f)]

    rows = []
    for f in fixtures:
        flat = _flatten(f)
        prob = _num(flat.get(prob_field))
        if prob is None:
            continue
        val = _value(prob, _num(flat.get(over_f)), _num(flat.get(under_f)))
        if min_odds and (val["over_odds"] is None or val["over_odds"] < min_odds):
            continue
        rows.append({
            "fixture_id": f.get("id"),
            "match": f"{f.get('home_name')} vs {f.get('away_name')}",
            "kickoff": _ko(f),
            "competition_id": f.get("competition_id"),
            "league_positions": f"{f.get('home_position')} v {f.get('away_position')}",
            "probability_pct": prob,
            **val,
            "referee": flat.get("referee.name") or flat.get("referee.common_name"),
        })

    if rank_by == "edge":
        rows = [r for r in rows if r["edge_pct"] is not None]
        rows.sort(key=lambda r: r["edge_pct"], reverse=True)
    else:
        rows.sort(key=lambda r: r["probability_pct"], reverse=True)

    label = "Both teams to score" if market == "btts" else f"Over {ln:g} {market}"
    return {
        "market": label,
        "probability_field": prob_field,
        "odds_field": over_f,
        "ranked_by": rank_by,
        "fixtures_scanned": scanned,
        "fixtures_after_filters": len(rows),
        "note": "probability_pct is OddAlerts' model; implied_pct is the bookmaker's price with margin removed; edge_pct = difference.",
        "fixtures": rows[:limit],
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
