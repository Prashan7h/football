"""
Pull Premier League fixtures, results, and standings from football-data.org.

Caches raw responses to data/raw/. Emits:
  - api/fixtures.json     (all matches in the current season)
  - api/match/<id>.json   (one stub per match, meta only — preview/review filled in
                           by ingest_understat.py and model_dixon_coles.py)

Auth: requires FOOTBALL_DATA_KEY in the environment.
Rate: free tier is 10 req/min; this script makes ~3 calls per run, so no throttling.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
API = ROOT / "api"
ASSETS = ROOT / "assets"

API_BASE = "https://api.football-data.org/v4"
PL_CODE = "PL"

# Crests are mirrored to assets/crests/<slug>.png and served from our Pages site
# so the app does not depend on football-data.org's CDN at runtime.
CRESTS_BASE = "https://prashan7h.github.io/football/assets/crests"

STATUS_MAP = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "POSTPONED": "scheduled",
    "SUSPENDED": "scheduled",
    "IN_PLAY": "live",
    "PAUSED": "live",
    "LIVE": "live",
    "FINISHED": "finished",
    "AWARDED": "finished",
    "CANCELLED": "finished",
}

# Slugs that need to be shorter than the API's verbose names.
SLUG_ALIASES = {
    "brighton-and-hove-albion": "brighton",
    "tottenham-hotspur": "tottenham",
    "newcastle-united": "newcastle",
    "west-ham-united": "west-ham",
    "leicester-city": "leicester",
    "leeds-united": "leeds",
    "norwich-city": "norwich",
    "ipswich-town": "ipswich",
    "luton-town": "luton",
    "afc-bournemouth": "bournemouth",
}


def slug_from_name(name: str) -> str:
    s = name.lower().strip()
    for suffix in (" fc", " afc", " f.c."):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.replace(" & ", " and ").replace(".", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return SLUG_ALIASES.get(s, s)


def get(session: requests.Session, path: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    r = session.get(url, params=params or {}, timeout=30)
    if r.status_code == 429:
        # Free tier: 10 req/min. Back off and retry once.
        time.sleep(60)
        r = session.get(url, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def cache_raw(name: str, payload: dict) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RAW / f"{name}_{stamp}.json").write_text(json.dumps(payload, indent=2))
    # Also write a "latest" pointer for easy local inspection.
    (RAW / f"{name}_latest.json").write_text(json.dumps(payload, indent=2))


def load_clubs() -> dict:
    clubs = json.loads((ASSETS / "clubs.json").read_text())
    default = clubs.pop("_default")
    clubs.pop("_comment", None)
    return {"clubs": clubs, "default": default}


def team_block(team: dict, clubs: dict) -> dict:
    slug = slug_from_name(team["name"])
    palette = clubs["clubs"].get(slug, clubs["default"])
    return {
        "slug": slug,
        "name": palette.get("name") or team.get("shortName") or team["name"],
        "primary": palette["primary"],
        "crest": f"{CRESTS_BASE}/{slug}.png",
    }


def match_id(kickoff_utc: str, home_slug: str, away_slug: str) -> str:
    date = kickoff_utc[:10]  # YYYY-MM-DD from ISO string
    return f"{date}-{home_slug}-{away_slug}"


def stable_updated_at(path: Path, new_content_no_ts: dict) -> str:
    """Reuse the previous `updated_at` when content is byte-identical, so a
    no-op ingest run produces no git diff and no commit."""
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = None
        if existing is not None:
            existing_no_ts = {k: v for k, v in existing.items() if k != "updated_at"}
            prior = existing.get("updated_at")
            if existing_no_ts == new_content_no_ts and prior:
                return prior
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_match(m: dict, clubs: dict) -> dict:
    home = team_block(m["homeTeam"], clubs)
    away = team_block(m["awayTeam"], clubs)
    kickoff = m["utcDate"]  # already ISO 8601 Z
    mid = match_id(kickoff, home["slug"], away["slug"])

    full = (m.get("score") or {}).get("fullTime") or {}
    has_score = full.get("home") is not None and full.get("away") is not None

    return {
        "id": mid,
        "kickoff_utc": kickoff,
        "matchday": m.get("matchday"),
        "venue": m.get("venue"),
        "status": STATUS_MAP.get(m["status"], "scheduled"),
        "home": home,
        "away": away,
        "score": {"home": full["home"], "away": full["away"]} if has_score else None,
    }


def main() -> int:
    key = os.environ.get("FOOTBALL_DATA_KEY")
    if not key:
        print("FOOTBALL_DATA_KEY is not set. Register at https://www.football-data.org/client/register", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers["X-Auth-Token"] = key

    competition = get(session, f"/competitions/{PL_CODE}")
    cache_raw("competition", competition)

    season_start_year = competition["currentSeason"]["startDate"][:4]
    current_matchday = competition["currentSeason"]["currentMatchday"]

    matches_raw = get(session, f"/competitions/{PL_CODE}/matches", {"season": season_start_year})
    cache_raw("matches", matches_raw)

    standings = get(session, f"/competitions/{PL_CODE}/standings", {"season": season_start_year})
    cache_raw("standings", standings)

    clubs = load_clubs()

    matches = [normalise_match(m, clubs) for m in matches_raw["matches"]]
    matches.sort(key=lambda x: x["kickoff_utc"])

    fixtures_no_ts = {
        "season": int(season_start_year),
        "current_matchday": current_matchday,
        "matches": matches,
    }
    fixtures_path = API / "fixtures.json"
    fixtures = {
        "season": fixtures_no_ts["season"],
        "current_matchday": fixtures_no_ts["current_matchday"],
        "updated_at": stable_updated_at(fixtures_path, fixtures_no_ts),
        "matches": fixtures_no_ts["matches"],
    }

    API.mkdir(parents=True, exist_ok=True)
    fixtures_path.write_text(json.dumps(fixtures, indent=2))

    match_dir = API / "match"
    match_dir.mkdir(parents=True, exist_ok=True)
    for m in matches:
        per_match = {"meta": m, "preview": None, "review": None}
        (match_dir / f"{m['id']}.json").write_text(json.dumps(per_match, indent=2))

    print(f"Wrote {len(matches)} matches. Current matchday: {current_matchday}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
