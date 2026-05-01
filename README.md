# football

Premier League stats and visualisations, before and after the games.

Companion to [Prashan7h.github.io](https://prashan7h.github.io). The project site is served at `https://prashan7h.github.io/football/`.

This repo is the **data + model layer**. It pulls from free sources (football-data.org API for fixtures/results/standings, Understat for shot-level xG), fits a Dixon–Coles forecast model, and emits static JSON consumed by:

- a SwiftUI iOS app in a separate repo (`football-ios`)
- static web pages in this repo (added in a later phase)

No backend, no API server. Everything is pre-computed in GitHub Actions, committed, and served as static files by GitHub Pages.

## Status

**Phase 0 — scaffolding.** No live data yet.

## Stack

- Python 3.11+
- `requests` for the football-data.org API
- `beautifulsoup4`, `lxml` for the Understat HTML/embedded-JSON parse (the only free shot-level source)
- `pandas`, `numpy`, `scipy` for the model
- GitHub Actions for scheduled ingestion (cron, every 6h; every 30 min on matchdays)

## Secrets

| Name | Used by | How to get it |
|---|---|---|
| `FOOTBALL_DATA_KEY` | `scripts/ingest_football_data.py` | Free at https://www.football-data.org/client/register |

Set locally with `export FOOTBALL_DATA_KEY=...`. Set in CI with `gh secret set FOOTBALL_DATA_KEY`.

## Layout

```
football/
├── scripts/                 # ingestion + modelling
│   ├── ingest_fbref.py
│   ├── ingest_understat.py
│   ├── model_dixon_coles.py
│   └── build_api.py
├── data/raw/                # local cache, not committed
├── api/                     # committed JSON, served at /football/api/
│   ├── fixtures.json
│   └── match/<id>.json
├── assets/
│   └── tokens.css           # design tokens (palette mirrored in iOS app)
└── .github/workflows/
    └── ingest.yml
```

## JSON contract

All endpoints live under `/api/` and are served as static files. Both the iOS app and the (later) web pages read from the same files.

### `api/fixtures.json`

Current and upcoming gameweek.

```json
{
  "season": 2025,
  "current_matchday": 33,
  "updated_at": "2026-05-01T08:00:00Z",
  "matches": [
    {
      "id": "2026-05-03-arsenal-chelsea",
      "kickoff_utc": "2026-05-03T15:30:00Z",
      "matchday": 33,
      "venue": "Emirates Stadium",
      "status": "scheduled",
      "home": { "slug": "arsenal", "name": "Arsenal", "primary": "#EF0107", "crest": "https://..." },
      "away": { "slug": "chelsea", "name": "Chelsea", "primary": "#034694", "crest": "https://..." },
      "score": null
    }
  ]
}
```

`status` is one of `scheduled`, `live`, `finished`.

### `api/match/<id>.json`

Per-match payload. `preview` is populated for upcoming matches; `review` is populated once the match is finished. Both are present during live (later phase).

```json
{
  "meta": {
    "id": "...",
    "date": "...",
    "kickoff": "...",
    "venue": "...",
    "home": { "slug": "...", "name": "...", "primary": "#..." },
    "away": { "slug": "...", "name": "...", "primary": "#..." },
    "status": "scheduled"
  },
  "preview": {
    "probabilities": { "home": 0.52, "draw": 0.25, "away": 0.23 },
    "expected_score_grid": [[0.05, 0.12, "..."], "..."],
    "form": {
      "home": [{ "opponent": "...", "result": "W", "score": "2-1", "xg_for": 1.8, "xg_against": 0.9 }],
      "away": [{ "...": "..." }]
    },
    "h2h": [{ "date": "...", "score": "1-2", "venue": "..." }]
  },
  "review": {
    "score": { "home": 2, "away": 1 },
    "shots": [
      {
        "minute": 23,
        "team": "home",
        "player": "Saka",
        "x": 0.91,
        "y": 0.42,
        "xg": 0.34,
        "outcome": "goal"
      }
    ],
    "xg_timeline": {
      "home": [[1, 0.0], [12, 0.08], [23, 0.42]],
      "away": [[1, 0.0]]
    },
    "elo_delta": { "home": 12, "away": -12 }
  }
}
```

`shots[].x` and `shots[].y` are normalised pitch coordinates: `x ∈ [0, 1]` from defending goal-line to attacking goal-line; `y ∈ [0, 1]` from left touchline to right touchline (from the shooting team's perspective).

`xg_timeline` entries are `[minute, cumulative_xg]`.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/ingest_fbref.py
python scripts/ingest_understat.py
python scripts/model_dixon_coles.py
python scripts/build_api.py
```

Generated JSON lands in `api/` — commit it to publish.

## Sources

| Source | Use | Type | Licence |
|---|---|---|---|
| [football-data.org](https://www.football-data.org) | Fixtures, results, standings, scorers, lineups | REST API (key, free tier) | Free for personal / non-commercial; attribution required |
| [Understat](https://understat.com) | Shot-level xG with pitch coordinates | HTML scrape (embedded JSON) | Public, attribution |
| [football-data.co.uk](https://football-data.co.uk) | Historical results + closing odds (model calibration only) | CSV download | Free for non-commercial |

All raw responses are cached to `data/raw/` (gitignored) so re-runs don't hit the providers unnecessarily. football-data.org free tier is 10 req/min — the ingestion script respects this.
