"""
Augment per-match JSON with API-Football event data (goals, cards, subs, lineups).

For every finished match in api/fixtures.json that lacks event data, fetch:
  - /fixtures/events?fixture={id}   → goals, bookings, substitutions
  - /fixtures/lineups?fixture={id}  → formations + starting XI + bench

Saves normalised data under api/match/<our_id>.json as:
  {
    "meta": {...},
    "events": [...],
    "lineups": [...],
    "preview": null, "review": null
  }

Idempotent: matches already augmented (events key present) are skipped.
Rate: free tier = 100 req/day. 2 calls per match → ~50 matches/day max.
Auth: API_FOOTBALL_KEY env var.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API  = ROOT / "api"

BASE_URL = "https://v3.football.api-sports.io"
PL_LEAGUE_ID = 39
SEASON = 2025

# API-Football team name → our slug
NAME_TO_SLUG: dict[str, str] = {
    "Arsenal":                      "arsenal",
    "Aston Villa":                  "aston-villa",
    "Bournemouth":                  "bournemouth",
    "Brentford":                    "brentford",
    "Brighton":                     "brighton",
    "Brighton & Hove Albion":       "brighton",
    "Burnley":                      "burnley",
    "Chelsea":                      "chelsea",
    "Crystal Palace":               "crystal-palace",
    "Everton":                      "everton",
    "Fulham":                       "fulham",
    "Leeds":                        "leeds",
    "Leeds United":                 "leeds",
    "Liverpool":                    "liverpool",
    "Manchester City":              "manchester-city",
    "Manchester United":            "manchester-united",
    "Newcastle":                    "newcastle",
    "Newcastle United":             "newcastle",
    "Nottingham Forest":            "nottingham-forest",
    "Nottingham Forrest":           "nottingham-forest",
    "Sunderland":                   "sunderland",
    "AFC Sunderland":               "sunderland",
    "Tottenham":                    "tottenham",
    "Tottenham Hotspur":            "tottenham",
    "West Ham":                     "west-ham",
    "West Ham United":              "west-ham",
    "Wolverhampton":                "wolverhampton-wanderers",
    "Wolverhampton Wanderers":      "wolverhampton-wanderers",
    "Wolves":                       "wolverhampton-wanderers",
}


def api_get(key: str, path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={
        "x-apisports-key": key,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
    errors = body.get("errors")
    if errors and errors not in ({}, []):
        raise ValueError(f"API error: {errors}")
    return body


def lookup_fixture_id(key: str, date: str, home_slug: str, away_slug: str) -> int | None:
    """Fetch fixtures for a specific date+league to find the API-Football fixture id.
    Uses date-based lookup to avoid the season restriction on free plans."""
    body = api_get(key, "/fixtures", {"date": date, "league": PL_LEAGUE_ID})
    for f in body.get("response", []):
        h = NAME_TO_SLUG.get(f["teams"]["home"]["name"])
        a = NAME_TO_SLUG.get(f["teams"]["away"]["name"])
        if h == home_slug and a == away_slug:
            return f["fixture"]["id"]
    return None


def normalise_events(raw: list[dict], home_slug: str, away_slug: str) -> list[dict]:
    out = []
    for e in raw:
        e_type = (e.get("type") or "").lower()
        detail  = (e.get("detail") or "").lower()
        elapsed = (e.get("time") or {}).get("elapsed")
        extra   = (e.get("time") or {}).get("extra")

        team_name = (e.get("team") or {}).get("name", "")
        team_slug = NAME_TO_SLUG.get(team_name, team_name.lower().replace(" ", "-"))

        player = (e.get("player") or {}).get("name")
        assist = (e.get("assist") or {}).get("name")

        if e_type == "goal":
            norm_type = "goal"
            if "own" in detail:
                norm_detail = "own_goal"
            elif "penalty" in detail:
                norm_detail = "penalty"
            else:
                norm_detail = "normal"
        elif e_type == "card":
            norm_type = "card"
            if "yellow red" in detail or "red card" in detail and "yellow" in detail:
                norm_detail = "yellow_red"
            elif "red" in detail:
                norm_detail = "red"
            else:
                norm_detail = "yellow"
        elif e_type == "subst":
            norm_type = "subst"
            norm_detail = None
            # For subs, player is coming off, assist is coming on
        elif e_type == "var":
            norm_type = "var"
            norm_detail = detail
        else:
            continue  # skip unknown types

        out.append({
            "elapsed":   elapsed,
            "extra":     extra,
            "type":      norm_type,
            "detail":    norm_detail,
            "team_slug": team_slug,
            "player":    player,
            "assist":    assist,
        })
    return out


def normalise_lineups(raw: list[dict]) -> list[dict]:
    out = []
    for team in raw:
        name = (team.get("team") or {}).get("name", "")
        slug = NAME_TO_SLUG.get(name, name.lower().replace(" ", "-"))
        formation = team.get("formation")
        start_xi = [p["player"]["name"] for p in (team.get("startXI") or []) if p.get("player")]
        subs     = [p["player"]["name"] for p in (team.get("substitutes") or []) if p.get("player")]
        out.append({
            "team_slug": slug,
            "formation": formation,
            "start_xi":  start_xi,
            "subs":      subs,
        })
    return out


def main() -> int:
    key = os.environ.get("API_FOOTBALL_KEY", "")
    if not key:
        print("API_FOOTBALL_KEY not set.", file=sys.stderr)
        return 2

    fixtures = json.loads((API / "fixtures.json").read_text())

    to_process = [
        m for m in fixtures["matches"]
        if m["status"] == "finished"
        and m.get("external_id")   # needs football-data id (for our own id; we match by date+slug)
        and not _already_done(m["id"])
    ]
    if not to_process:
        print("No finished matches need API-Football augmentation.")
        return 0
    print(f"Found {len(to_process)} matches to augment.")

    augmented = skipped = failed = 0
    # Cache date → list of fixtures so we only call /fixtures?date= once per date
    date_cache: dict[str, dict[tuple[str, str], int]] = {}

    for our_match in to_process:
        date = our_match["kickoff_utc"][:10]
        home_slug = our_match["home"]["slug"]
        away_slug = our_match["away"]["slug"]

        # Build per-date cache on first encounter
        if date not in date_cache:
            try:
                body = api_get(key, "/fixtures", {"date": date, "league": PL_LEAGUE_ID})
                date_cache[date] = {}
                for f in body.get("response", []):
                    h = NAME_TO_SLUG.get(f["teams"]["home"]["name"])
                    a = NAME_TO_SLUG.get(f["teams"]["away"]["name"])
                    if h and a:
                        date_cache[date][(h, a)] = f["fixture"]["id"]
                time.sleep(0.3)
            except Exception as e:
                print(f"  FAIL fetching fixtures for {date}: {e}", file=sys.stderr)
                date_cache[date] = {}

        af_id = date_cache[date].get((home_slug, away_slug))
        if not af_id:
            print(f"  SKIP {our_match['id']}: no API-Football fixture for {date} {home_slug} vs {away_slug}")
            skipped += 1
            continue

        try:
            events_body  = api_get(key, "/fixtures/events",  {"fixture": af_id})
            time.sleep(0.5)
            lineups_body = api_get(key, "/fixtures/lineups", {"fixture": af_id})
            time.sleep(0.5)
        except Exception as e:
            print(f"  FAIL {our_match['id']}: {e}", file=sys.stderr)
            failed += 1
            continue

        events  = normalise_events(events_body.get("response", []),
                                   our_match["home"]["slug"], our_match["away"]["slug"])
        lineups = normalise_lineups(lineups_body.get("response", []))

        per_path = API / "match" / f"{our_match['id']}.json"
        per = json.loads(per_path.read_text()) if per_path.exists() else {"meta": our_match}
        per["events"]  = events
        per["lineups"] = lineups
        per_path.write_text(json.dumps(per, indent=2))
        augmented += 1
        print(f"  OK  {our_match['id']}: {len(events)} events, {len(lineups)} lineups")

    print(f"\nDone. augmented={augmented} skipped={skipped} failed={failed}")
    return 0


def _already_done(match_id: str) -> bool:
    path = API / "match" / f"{match_id}.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    return data.get("events") is not None


if __name__ == "__main__":
    raise SystemExit(main())
