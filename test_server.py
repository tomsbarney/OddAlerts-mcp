"""Offline test: fakes the OddAlerts API and runs every tool through a real MCP client.

Run with:  python test_server.py
The probability field names below are placeholders; the server matches fields by
keyword ("corner", "card", "goal"/"btts") and line ("o95", "over_3_5" ...), so it
adapts to whatever names OddAlerts actually uses.
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
            "odds": {"o95_corners": 1.9, "o35_cards": 2.1, "o25_goals": 1.8},
            "referee": {"name": "Ref " + home}}


FIXTURES = [
    fixture(1, "Brentford", "Fulham", 5, {"o25_goals": 58, "btts": 61, "o95_corners": 71, "o35_cards": 44, "u25_goals": 42}),
    fixture(2, "Leeds", "Burnley", 20, {"o25_goals": 49, "btts": 50, "o95_corners": 55, "o35_cards": 68, "u25_goals": 51}),
    fixture(3, "Bayern", "Leipzig", 30, {"o25_goals": 77, "btts": 66, "o95_corners": 62, "o35_cards": 51, "u25_goals": 23}),
    fixture(4, "Far Future", "FC", 24 * 6, {"o25_goals": 99, "btts": 99, "o95_corners": 99, "o35_cards": 99}),
]


def fake_get(path, params):
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
        assert names == ["discover_fields", "find_top_fixtures", "get_fixture", "list_fixtures", "simulate_fixtures"]

        async def call(name, args):
            res = await client.call_tool(name, args)
            assert not res.is_error, res
            return json.loads(res.content[0].text)

        r = await call("find_top_fixtures", {"market": "corners", "line": 9.5})
        print("corners:", r["ranked_by"], [(f["match"], f["score"]) for f in r["fixtures"]])
        assert r["ranked_by"] == "probability.o95_corners"
        assert [f["fixture_id"] for f in r["fixtures"]] == [1, 3, 2]  # 6-day-away fixture excluded

        r = await call("find_top_fixtures", {"market": "cards", "line": 3.5})
        print("cards:", r["ranked_by"], [f["match"] for f in r["fixtures"]])
        assert r["fixtures"][0]["fixture_id"] == 2

        r = await call("find_top_fixtures", {"market": "goals", "line": 2.5})
        print("goals:", r["ranked_by"], [f["match"] for f in r["fixtures"]])
        assert r["ranked_by"] == "probability.o25_goals" and r["fixtures"][0]["fixture_id"] == 3

        r = await call("find_top_fixtures", {"market": "corners", "line": 11.5})
        print("missing line ->", r["message"][:70])
        assert "discover_fields" in r["message"]

        r = await call("find_top_fixtures", {"market": "goals", "sort_field": "probability.btts", "days_ahead": 7})
        assert r["fixtures"][0]["fixture_id"] == 4

        r = await call("discover_fields", {"keyword": "card"})
        print("discover:", r["fields"])
        assert "probability.o35_cards" in r["fields"]

        r = await call("simulate_fixtures", {"fixture_ids": list(range(1, 25))})
        assert len(r["simulations"]) == 24

        r = await call("list_fixtures", {"days_ahead": 2})
        assert r["count"] == 3
    print("ALL TESTS PASSED")


asyncio.run(main())
