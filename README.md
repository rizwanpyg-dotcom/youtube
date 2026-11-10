# Channel Engine

An autonomous, source-verified documentary pipeline for a faceless YouTube channel on **economic history and tech collapses**. It runs on free GitHub infrastructure (**$0/month, no card**) while your PC is off.

```
topics (Wikipedia demand + source availability)
  → research (Wikipedia + its cited press + FRED data)
  → fact-check (quotes must be in the fetched page; numbers must be in the evidence or the data)
  → script (only verified claims; every number cited)
  → narration (Kokoro-82M) → animated charts and cards (FFmpeg) → thumbnail → SEO
  → QC gates → upload package (or API upload after Google's audit)
  → analytics → learning loop
```

## Status
| Phase | State | Document |
|---|---|---|
| 1 Feasibility | done | [docs/PHASE1_FEASIBILITY.md](docs/PHASE1_FEASIBILITY.md) |
| 2 Architecture | done | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [db/schema.sql](db/schema.sql) |
| 3 Development | done | [docs/PHASE3_PROGRESS.md](docs/PHASE3_PROGRESS.md) |
| 4 Testing | done locally (cloud runner measured after deployment) | [docs/TESTING_REPORT.md](docs/TESTING_REPORT.md) |
| 5 Deployment | **your steps**, ~60–90 min | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| 6 Controlled launch | plan ready | [docs/LAUNCH_PLAN.md](docs/LAUNCH_PLAN.md) |

Also: [docs/SECURITY.md](docs/SECURITY.md) (secrets, backups, recovery runbook) · [docs/COSTS.md](docs/COSTS.md)

## Quick start (local)
```bash
python -m venv .venv
```
```bash
.venv\Scripts\python -m pip install -r requirements.txt
```
```bash
.venv\Scripts\python -m pytest -q tests
```
```bash
.venv\Scripts\python -m ytengine sample
```
The sample needs the Kokoro model files in `models/`. The cloud setup step downloads them automatically; locally, see `.github/actions/setup/action.yml` for the two URLs.

## Owner commands (comment on GitHub issues; only the repo owner is obeyed)
`/approve` · `/changes <notes>` · `/reject <reason>` · `/published <youtube-id>` · `/retry` · `/pause` · `/resume` · `/mode package|review_window|auto` · `/approval on|off` · `/status`

## Layout
```
ytengine/            Python package (agents/, render/, youtube/, pipeline, cycle, control, ops, dashboard)
dashboard/           static dashboard + privacy policy (deployed to GitHub Pages)
.github/workflows/   pipeline (4-hourly), control (issue commands), maintenance (watchdog/analytics/backups/market), ci
db/schema.sql        database (25 tables)
config/              channel settings, banned phrases, pronunciation lexicon
scripts/             state-branch sync, one-time YouTube OAuth
n8n/                 optional workflow for a future VPS (not used by the $0 setup)
tests/               unit tests + fixtures + integration test
```
