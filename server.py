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
        "OddAlerts has no card probabilities or odds. For cards, use team_form (each team's average "
        "cards and corners across its recent matches) or find_cards_and_corners (ranks upcoming games "
        "for corners and cards together). Probabilities are OddAlerts model outputs, not certainties."
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
                "on a card market. Use find_cards_and_corners to rank games by corners probability "
                "and both teams' recent card averages, or team_form for any team's card and corner record."
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
            "home_id": f.get("home_id"),
            "away_id": f.get("away_id"),
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


# ----------------------------------------------------- team form (cards etc.)

FORM_CONCURRENCY = 5  # parallel /fixtures/team calls; OddAlerts allows ~300/min


def _side_stat(stats: dict[str, Any], side: str, *names: str) -> float | None:
    """Sum stats like home_yellow_cards + home_red_cards; None if none of them are present."""
    vals = [_num(stats.get(f"{side}_{n}")) for n in names]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _match_line(f: dict[str, Any], team_id: int) -> dict[str, Any] | None:
    """Cards and corners for/against one team in one finished fixture, or None if no stats."""
    stats = f.get("stats") or {}
    if not isinstance(stats, dict):
        return None
    is_home = f.get("home_id") == team_id
    us, them = ("home", "away") if is_home else ("away", "home")
    cards_for = _side_stat(stats, us, "yellow_cards", "red_cards")
    cards_against = _side_stat(stats, them, "yellow_cards", "red_cards")
    corners_for = _num(stats.get(f"{us}_corners"))
    corners_against = _num(stats.get(f"{them}_corners"))
    if cards_for is None and corners_for is None:
        return None
    return {
        "date": f.get("date") or f.get("ko_human"),
        "opponent": f.get("away_name") if is_home else f.get("home_name"),
        "venue": "H" if is_home else "A",
        "score": f"{f.get('home_goals')}-{f.get('away_goals')}",
        "cards_for": cards_for, "cards_against": cards_against,
        "corners_for": corners_for, "corners_against": corners_against,
    }


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _rate(values: list[float | None], line: float) -> float | None:
    vals = [v for v in values if v is not None]
    return round(100 * sum(v > line for v in vals) / len(vals), 1) if vals else None


def _total(m: dict[str, Any], kind: str) -> float | None:
    a, b = m[f"{kind}_for"], m[f"{kind}_against"]
    return a + b if a is not None and b is not None else None


async def _team_form(
    team_id: int, *, last_n: int, competitions: str | None,
    cards_line: float, corners_line: float, include_matches: bool,
) -> dict[str, Any]:
    """Average cards and corners over a team's last N finished, non-friendly matches."""
    payload = await _get(f"fixtures/team/{team_id}", {
        "include": "stats", "competitions": competitions, "order": "desc",
        "to": int(time.time()), "per_page": min(250, max(last_n * 3, 30)),
    })
    matches: list[dict[str, Any]] = []
    name = None
    for f in _data(payload):
        if name is None:
            name = f.get("home_name") if f.get("home_id") == team_id else f.get("away_name")
        if f.get("is_friendly") or f.get("home_goals") is None or (f.get("unix") or 0) > time.time():
            continue
        line = _match_line(f, team_id)
        if line:
            matches.append(line)
        if len(matches) >= last_n:
            break

    cards_total = [_total(m, "cards") for m in matches]
    corners_total = [_total(m, "corners") for m in matches]
    out: dict[str, Any] = {
        "team_id": team_id,
        "team": name,
        "matches_used": len(matches),
        "avg_cards_in_match": _avg(cards_total),
        "avg_cards_for": _avg([m["cards_for"] for m in matches]),
        "avg_cards_against": _avg([m["cards_against"] for m in matches]),
        f"pct_matches_over_{cards_line:g}_cards": _rate(cards_total, cards_line),
        "avg_corners_in_match": _avg(corners_total),
        "avg_corners_for": _avg([m["corners_for"] for m in matches]),
        "avg_corners_against": _avg([m["corners_against"] for m in matches]),
        f"pct_matches_over_{corners_line:g}_corners": _rate(corners_total, corners_line),
    }
    if include_matches:
        out["matches"] = matches
    return out


async def _forms(team_ids: list[int], **kw: Any) -> dict[int, dict[str, Any]]:
    """Fetch several teams' form in parallel (bounded), returning errors per team instead of failing."""
    sem = asyncio.Semaphore(FORM_CONCURRENCY)

    async def one(tid: int) -> tuple[int, dict[str, Any]]:
        async with sem:
            try:
                return tid, await _team_form(tid, **kw)
            except OddAlertsError as e:
                return tid, {"team_id": tid, "error": str(e), "matches_used": 0}

    return dict(await asyncio.gather(*(one(t) for t in dict.fromkeys(team_ids))))


@mcp.tool()
async def team_form(
    team_ids: list[int],
    last_n: int = 10,
    cards_line: float = 3.5,
    corners_line: float = 9.5,
    competitions: str | None = None,
    include_matches: bool = False,
) -> dict[str, Any]:
    """Each team's average cards and corners across its recent finished matches (all opponents, not just head-to-head).

    Cards = yellow + red cards shown in the match. For each team returns averages for the whole
    match (both sides), for the team, and against it, plus how often the match total went over the lines.
    Friendlies are skipped.

    Args:
        team_ids: OddAlerts team IDs (home_id / away_id from list_fixtures or get_fixture). Up to 30.
        last_n: How many recent matches to average (default 10).
        cards_line / corners_line: Lines for the "% of matches over" figures.
        competitions: Optional comma-separated competition IDs to restrict the sample (e.g. league only).
        include_matches: Also return the match-by-match numbers.
    """
    forms = await _forms(team_ids[:30], last_n=max(1, min(last_n, 40)), competitions=competitions,
                         cards_line=cards_line, corners_line=corners_line, include_matches=include_matches)
    return {"teams": list(forms.values()),
            "note": "Cards count yellow + red cards. Averages use finished, non-friendly matches with stats."}


@mcp.tool()
async def find_cards_and_corners(
    days_ahead: float = 1,
    corners_line: float = 9.5,
    cards_line: float = 3.5,
    shortlist: int = 20,
    last_n: int = 10,
    min_matches: int = 5,
    senior_men_only: bool = True,
    competitions: str | None = None,
    has_odds: bool = True,
    limit: int = 10,
    max_pages: int = 4,
) -> dict[str, Any]:
    """Rank upcoming fixtures for a high chance of BOTH corners and cards in the same game.

    Corners: OddAlerts' over-X.5 corners probability. Cards: OddAlerts has no card model, so the
    tool uses both teams' recent form (all matches, not head-to-head) — the average cards in their
    games and how often those games went over the cards line. The shortlist is the top fixtures by
    corners probability; each is then scored as corners_probability x cards_hit_rate (treating the
    two as independent, so it is an indicator, not a true joint probability).

    Args:
        days_ahead: Window to scan (1 = roughly the next 24 hours).
        corners_line: Corners line, 4.5-11.5 (default 9.5).
        cards_line: Cards line for the hit rate (default 3.5, i.e. 4+ cards).
        shortlist: How many top corners fixtures to check for cards (2 API calls each).
        last_n: Recent matches per team to average.
        min_matches: Skip fixtures where either team has fewer matches with stats than this.
        senior_men_only / competitions / has_odds / max_pages: As in find_top_fixtures.
        limit: How many fixtures to return.
    """
    corners = await find_top_fixtures(
        market="corners", line=corners_line, days_ahead=days_ahead, senior_men_only=senior_men_only,
        competitions=competitions, has_odds=has_odds, limit=max(1, min(shortlist, 40)), max_pages=max_pages,
    )
    picks = corners.get("fixtures") or []
    if not picks:
        return {"message": corners.get("message") or "No fixtures with corner probabilities in that window.", "fixtures": []}

    team_ids = [t for p in picks for t in (p.get("home_id"), p.get("away_id")) if t]
    forms = await _forms(team_ids, last_n=max(1, min(last_n, 40)), competitions=None,
                         cards_line=cards_line, corners_line=corners_line, include_matches=False)
    rate_key = f"pct_matches_over_{cards_line:g}_cards"

    rows, skipped = [], []
    for p in picks:
        h, a = forms.get(p.get("home_id"), {}), forms.get(p.get("away_id"), {})
        if min(h.get("matches_used", 0), a.get("matches_used", 0)) < min_matches:
            skipped.append(p["match"])
            continue
        rates = [x for x in (h.get(rate_key), a.get(rate_key)) if x is not None]
        exp_cards = _avg([h.get("avg_cards_in_match"), a.get("avg_cards_in_match")])
        if not rates or exp_cards is None:
            skipped.append(p["match"])
            continue
        cards_rate = round(sum(rates) / len(rates), 1)
        rows.append({
            "fixture_id": p["fixture_id"],
            "match": p["match"],
            "kickoff": p["kickoff"],
            "competition_id": p["competition_id"],
            "referee": p.get("referee"),
            f"over_{corners_line:g}_corners_pct": p["probability_pct"],
            "corners_over_odds": p.get("over_odds"),
            "corners_edge_pct": p.get("edge_pct"),
            "expected_cards": exp_cards,
            f"cards_over_{cards_line:g}_hit_rate_pct": cards_rate,
            "home_cards": {"in_match": h.get("avg_cards_in_match"), "for": h.get("avg_cards_for"), "against": h.get("avg_cards_against")},
            "away_cards": {"in_match": a.get("avg_cards_in_match"), "for": a.get("avg_cards_for"), "against": a.get("avg_cards_against")},
            "form_corners_in_match": _avg([h.get("avg_corners_in_match"), a.get("avg_corners_in_match")]),
            "combined_score_pct": round(p["probability_pct"] * cards_rate / 100, 1),
        })

    rows.sort(key=lambda r: r["combined_score_pct"], reverse=True)
    return {
        "corners_market": f"Over {corners_line:g} corners (OddAlerts model)",
        "cards_line": f"Over {cards_line:g} cards (yellow + red), from both teams' last {last_n} matches",
        "checked": len(picks),
        "skipped_thin_data": skipped,
        "note": ("combined_score_pct = corners probability x cards hit rate, assuming independence. "
                 "It ranks games; it is not a calibrated probability. Referee strictness is not included."),
        "fixtures": rows[:limit],
    }


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
