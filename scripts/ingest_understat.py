"""
Augment per-match JSON with Understat shot-level data (free, no auth).

For every finished match in api/fixtures.json, look up Understat's match id
from the league overview page, then scrape the match page's `shotsData` JSON
into api/match/<our_id>.json under the `understat` key.

Idempotent: matches already augmented are skipped. Polite (1s sleep between
match-page fetches).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "understat"
API = ROOT / "api"

SEASON = "2025"  # 2025-26
LEAGUE_URL = f"https://understat.com/league/EPL/{SEASON}"
MATCH_URL = "https://understat.com/match/{id}"
HEADERS = {"User-Agent": "football-bot/1.0 (+https://github.com/Prashan7h/football)"}

# Understat display name -> our slug.
NAME_TO_SLUG = {
    "Arsenal":                   "arsenal",
    "Aston Villa":               "aston-villa",
    "Bournemouth":               "bournemouth",
    "Brentford":                 "brentford",
    "Brighton":                  "brighton",
    "Burnley":                   "burnley",
    "Chelsea":                   "chelsea",
    "Crystal Palace":            "crystal-palace",
    "Everton":                   "everton",
    "Fulham":                    "fulham",
    "Leeds":                     "leeds",
    "Liverpool":                 "liverpool",
    "Manchester City":           "manchester-city",
    "Manchester United":         "manchester-united",
    "Newcastle United":          "newcastle",
    "Nottingham Forest":         "nottingham-forest",
    "Sunderland":                "sunderland",
    "Tottenham":                 "tottenham",
    "West Ham":                  "west-ham",
    "Wolverhampton Wanderers":   "wolverhampton-wanderers",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def extract_var(html: str, name: str):
    """Pull `var <name> = JSON.parse('...')` payload out of the page."""
    pat = rf"var\s+{re.escape(name)}\s*=\s*JSON\.parse\('([^']*)'\)"
    m = re.search(pat, html)
    if not m:
        raise ValueError(f"{name} not found in page")
    raw = m.group(1)
    decoded = bytes(raw, "utf-8").decode("unicode_escape")
    return json.loads(decoded)


def cache_raw(name: str, payload) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{name}.json").write_text(json.dumps(payload, indent=2))


def normalize_shot(s: dict) -> dict:
    return {
        "side":      "home" if s.get("h_a") == "h" else "away",
        "minute":    int(s.get("minute") or 0),
        "x":         float(s.get("X") or 0),
        "y":         float(s.get("Y") or 0),
        "xg":        round(float(s.get("xG") or 0), 4),
        "result":    s.get("result"),
        "player":    s.get("player"),
        "situation": s.get("situation"),
        "body":      s.get("shotType"),
    }


def main() -> int:
    fixtures = json.loads((API / "fixtures.json").read_text())

    # Only process finished matches lacking Understat data.
    to_process = [
        m for m in fixtures["matches"]
        if m["status"] == "finished" and not (m.get("understat") or {}).get("match_id")
    ]
    if not to_process:
        print("No newly-finished matches to process.")
        return 0
    print(f"Found {len(to_process)} newly-finished matches needing Understat data.")

    print("Fetching Understat league overview …")
    league_html = fetch(LEAGUE_URL)
    dates_data = extract_var(league_html, "datesData")
    cache_raw("dates", dates_data)

    by_key: dict[tuple[str, str, str], str] = {}
    for m in dates_data:
        if not m.get("isResult"):
            continue
        date = (m.get("datetime") or "")[:10]
        h = NAME_TO_SLUG.get((m.get("h") or {}).get("title"))
        a = NAME_TO_SLUG.get((m.get("a") or {}).get("title"))
        if not (date and h and a):
            continue
        by_key[(date, h, a)] = str(m["id"])
    print(f"Indexed {len(by_key)} finished Understat matches.")

    augmented = 0
    skipped_no_match = 0
    failed = 0

    for our_match in to_process:
        date = our_match["kickoff_utc"][:10]
        key = (date, our_match["home"]["slug"], our_match["away"]["slug"])
        understat_id = by_key.get(key)
        if not understat_id:
            skipped_no_match += 1
            continue

        per_path = API / "match" / f"{our_match['id']}.json"
        per = json.loads(per_path.read_text()) if per_path.exists() else {"meta": our_match}

        try:
            html = fetch(MATCH_URL.format(id=understat_id))
            shots_raw = extract_var(html, "shotsData")
        except Exception as e:
            print(f"  FAIL {our_match['id']}: {e}", file=sys.stderr)
            failed += 1
            continue

        shots = []
        for side in ("h", "a"):
            for s in (shots_raw.get(side) or []):
                shots.append(normalize_shot(s))
        shots.sort(key=lambda s: s["minute"])

        per["understat"] = {"match_id": int(understat_id), "shots": shots}
        per_path.write_text(json.dumps(per, indent=2))
        augmented += 1
        if augmented % 25 == 0:
            print(f"  …{augmented} augmented")
        time.sleep(1.0)

    print(
        f"\nDone. augmented={augmented} skipped_no_match={skipped_no_match} failed={failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
