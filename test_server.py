"""Offline test: fakes the OddAlerts API and runs every tool through a real MCP client.

Run with:  python test_server.py
Field names match the live OddAlerts API (probability.o25, probability.o9_corners,
odds.total_goals.over_25, odds.total_corners.over_95 ...).
"""

import asyncio
import json
import os
import time

os.environ["ODDALERTS_API_TOKEN"] = "test-token"
import server  # noqa: E402

NOW = int(time.time())


def fixture(i, home, away, hours, prob):
    return {"id": i, "home_name": home, "away_name": away, "competition_id": 8, "season_id": 1,
            "home_id": i * 10, "away_id": i * 10 + 1, "home_position": 3, "away_position": 12,
            "status": "NS", "unix": NOW + hours * 3600, "ko_human": f"+{hours}h",
            "home_goals": None, "away_goals": None, "corners": None,
            "probability": prob,
            "odds": {"total_corners": {"over_95": 1.9, "under_95": 1.9},
                     "total_goals": {"over_25": 1.5 if i == 2 else 2.2, "under_25": 2.5},
                     "btts": {"yes": 1.8, "no": 2.0}},
            "referee": {"name": "Ref " + home}}


FIXTURES = [
    fixture(1, "Brentford", "Fulham", 5, {"o25": 58, "btts": 61, "o9_corners": 71}),
    fixture(2, "Leeds", "Burnley", 20, {"o25": 49, "btts": 50, "o9_corners": 55}),
    fixture(3, "Bayern", "Leipzig", 30, {"o25": 77, "btts": 66, "o9_corners": 62}),
    fixture(4, "Far Future", "FC", 24 * 6, {"o25": 99, "btts": 99, "o9_corners": 99}),
    fixture(5, "Inter U19", "Ajax U19", 6, {"o25": 95, "btts": 95, "o9_corners": 95}),
]


# Past matches per team: (yellow cards for, against, corners for, against). Team ids: home = i*10, away = i*10+1.
CARD_LEVEL = {10: 1, 11: 1, 20: 2, 21: 2, 30: 3, 31: 3}


def past_matches(team_id):
    lvl = CARD_LEVEL.get(team_id, 2)
    rows = []
    for k in range(12):
        home = k % 2 == 0
        us = {"yellow_cards": lvl, "red_cards": 1 if k == 0 else 0, "corners": 6}
        them = {"yellow_cards": lvl, "red_cards": 0, "corners": 4}
        h, a = (us, them) if home else (them, us)
        rows.append({
            "id": 1000 + k, "home_id": team_id if home else 999, "away_id": 999 if home else team_id,
            "home_name": f"T{team_id}" if home else "Opp", "away_name": "Opp" if home else f"T{team_id}",
            "home_goals": 1, "away_goals": 1, "unix": NOW - (k + 1) * 86400 * 7, "is_friendly": k == 1,
            "stats": {f"home_{s}": v for s, v in h.items()} | {f"away_{s}": v for s, v in a.items()},
        })
    # a future fixture and one without stats, both must be ignored
    rows.insert(0, {"id": 2000, "home_id": team_id, "away_id": 999, "home_goals": None, "unix": NOW + 86400, "stats": None})
    rows.append({"id": 2001, "home_id": team_id, "away_id": 999, "home_goals": 0, "away_goals": 0,
                 "unix": NOW - 99 * 86400 * 7, "stats": {}})
    return rows


def fake_get(path, params):
    if path.startswith("fixtures/team/"):
        assert params["include"] == "stats" and params["order"] == "desc"
        return {"info": {"next_page_url": None}, "data": past_matches(int(path.rsplit("/", 1)[1]))}
    if path == "fixtures/upcoming":
        return {"info": {"page": 1, "per_page": 250, "count": len(FIXTURES), "next_page_url": None}, "data": FIXTURES}
    if path.startswith("fixtures/"):
        fid = int(path.split("/")[1])
        return {"info": {"count": 1}, "data": [f for f in FIXTURES if f["id"] == fid]}
    if path == "predictions/generate/multiple":
        ids = params["ids"].split(",")
        return {"info": {"results": len(ids)}, "data": [{"fixture_id": int(x), "o25_goals_percentage": 60} for x in ids]}
    raise AssertionError(path)


server._get_sync = fake_get


async def main():
    from mcp.client.session import ClientSession  # noqa: F401
    from mcp.client import Client

    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools:", names)
        assert names == ["discover_fields", "find_cards_and_corners", "find_top_fixtures", "get_fixture",
                         "list_fixtures", "simulate_fixtures", "team_form"]

        async def call(name, args):
            res = await client.call_tool(name, args)
            assert not res.is_error, res
            return json.loads(res.content[0].text)

        r = await call("find_top_fixtures", {"market": "corners", "line": 9.5})
        print("corners:", r["probability_field"], [(f["match"], f["probability_pct"], f["edge_pct"]) for f in r["fixtures"]])
        assert r["probability_field"] == "probability.o9_corners"
        assert [f["fixture_id"] for f in r["fixtures"]] == [1, 3, 2]  # U19 + 6-day fixture excluded
        assert r["fixtures"][0]["edge_pct"] == 21.0  # 71 vs 50% implied

        r = await call("find_top_fixtures", {"market": "goals", "rank_by": "edge"})
        print("goals edge:", [(f["match"], f["edge_pct"]) for f in r["fixtures"]])
        assert r["probability_field"] == "probability.o25" and r["fixtures"][0]["fixture_id"] == 3

        r = await call("find_top_fixtures", {"market": "goals", "min_odds": 2.0, "senior_men_only": False})
        assert 2 not in [f["fixture_id"] for f in r["fixtures"]] and r["fixtures"][0]["fixture_id"] == 5

        r = await call("find_top_fixtures", {"market": "cards"})
        assert "no card probabilities" in r["message"]

        r = await call("find_top_fixtures", {"market": "corners", "line": 13.5})
        assert "11.5" in r["message"]

        r = await call("find_top_fixtures", {"market": "btts", "days_ahead": 7})
        assert r["fixtures"][0]["fixture_id"] == 4

        r = await call("discover_fields", {"keyword": "corner"})
        print("discover:", r["fields"])
        assert "probability.o9_corners" in r["fields"]

        r = await call("simulate_fixtures", {"fixture_ids": list(range(1, 25))})
        assert len(r["simulations"]) == 24

        r = await call("list_fixtures", {"days_ahead": 2})
        assert r["count"] == 4

        # team_form: 10 matches used (friendly, future and no-stats rows skipped)
        r = await call("team_form", {"team_ids": [30], "include_matches": True})
        t = r["teams"][0]
        print("team_form:", {k: v for k, v in t.items() if k != "matches"})
        assert t["team"] == "T30" and t["matches_used"] == 10
        assert t["avg_cards_in_match"] == 6.1  # 3 + 3 each game, plus one red in the latest
        assert t["avg_cards_for"] == 3.1 and t["avg_cards_against"] == 3.0
        assert t["avg_corners_for"] == 6.0 and t["avg_corners_against"] == 4.0
        assert t["pct_matches_over_3.5_cards"] == 100.0
        assert t["avg_corners_in_match"] == 10.0 and t["pct_matches_over_9.5_corners"] == 100.0
        assert len(t["matches"]) == 10 and t["matches"][0]["cards_for"] == 4

        # find_cards_and_corners: Bayern v Leipzig (high cards) beats Brentford v Fulham (higher corners, few cards)
        r = await call("find_cards_and_corners", {"days_ahead": 2})
        print("cards+corners:", [(f["match"], f["over_9.5_corners_pct"], f["expected_cards"],
                                  f["cards_over_3.5_hit_rate_pct"], f["combined_score_pct"]) for f in r["fixtures"]])
        assert [f["fixture_id"] for f in r["fixtures"]] == [3, 2, 1]
        assert r["fixtures"][0]["combined_score_pct"] == 62.0
        assert r["fixtures"][2]["cards_over_3.5_hit_rate_pct"] == 0.0  # Brentford/Fulham: 2-3 card games

        r = await call("find_top_fixtures", {"market": "corners"})
        assert r["fixtures"][0]["home_id"] == 10
    print("ALL TESTS PASSED")


asyncio.run(main())
