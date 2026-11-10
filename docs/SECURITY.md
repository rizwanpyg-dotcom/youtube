# Security, backups and recovery

## 1. Credentials

| Control | Implementation |
|---|---|
| Secrets storage | GitHub Actions encrypted secrets only. Locally: `.env` (git-ignored). Nothing secret in code, config, DB or dashboard. |
| Secrets in logs | GitHub masks secret values in logs. `ytengine/log.py` also redacts registered secret values and token-shaped strings (Google API keys, OAuth tokens, GitHub tokens, `key=`/`Bearer` patterns) before any log line, DB event or dashboard export. Unit-tested (`test_redaction`, `test_dashboard_export_is_sanitised`). |
| Least privilege | Workflow `permissions:` are set per workflow: `contents: write` only where state is saved, `issues: write` for notifications, `pages`/`id-token` only in the deploy job, `contents: read` for CI. OAuth scopes are limited to upload, captions/thumbnails and analytics read. |
| Payment data | None exists. No billing account is linked anywhere. The `expenses` table refuses any amount > 0 without `approved_by` (DB constraint, tested). |

## 2. Public-repository risks and mitigations

| Risk | Mitigation |
|---|---|
| A stranger comments `/approve` or `/pause` | `control.yml` runs only when `author_association == 'OWNER'`, and `control.py` checks it again. Non-owner comments are ignored and logged (tested). |
| A malicious pull request steals secrets | No `pull_request_target` workflows. CI on pull requests has `contents: read` and no secrets. The control workflow checks out the default branch, never PR code. |
| Compromised third-party Action | Every Action is **pinned to a commit SHA** (looked up from the official repos on 2026-11-09), not a moving tag. |
| Comment text used in shell commands | Comment and title are passed as environment variables, never interpolated into `run:` scripts. |
| Public data | The code, content calendar, scripts, claims and channel stats are public. Nothing private about you is stored. If you later want this private, see §6. |

## 3. Reliability features

- **Idempotent task queue.** Every stage has a unique key, so re-runs never duplicate a topic, script, render or upload (tested). Uploads carry a hidden marker tag, so a crash after upload is detected rather than uploaded twice.
- **Retries.**
  - HTTP calls: 3 tries with backoff, honouring `Retry-After`.
  - Uploads: resumable, up to 6 retries.
  - Tasks: 3 attempts with 10 min × 3ⁿ backoff, then marked `dead`, which opens a GitHub issue.
- **Leases.** A crashed runner's tasks are reclaimed after 90 min by the watchdog.
- **Quota guards.** Per-provider daily caps sit below the free limits (YouTube: 8,000 of 10,000 units). The system stops instead of going over.
- **Watchdog** (every 6 h) checks:
  - the last successful pipeline run (< 8 h);
  - dead tasks;
  - DB integrity;
  - YouTube quota headroom;
  - disk space;
  - DB size;
  - OAuth token validity.

  It opens or updates a `[watchdog]` issue (GitHub emails you) and closes it when everything recovers. It also writes a heartbeat to keep the repository "active".
- **60-day rule.** GitHub disables scheduled workflows in public repos after 60 days without repository activity [VERIFIED]. The state branch is pushed on every run, which should count as activity [ASSUMPTION: confirm in the first weeks; if workflows ever stop, re-enable them in the Actions tab].

## 4. Backups (three layers)

| Layer | Frequency | Retention | Where |
|---|---|---|---|
| Live state | every run | latest only | `state` branch (single commit; integrity-checked before every push; a corrupt DB is never pushed, tested) |
| Daily snapshot | daily 06:05 UTC | 7 (one per weekday, overwritten) | Release `db-daily` |
| Weekly snapshot | Sundays 03:25 UTC | 12 weeks | Releases `db-backup-YYYY-Www` |

Snapshots use SQLite `VACUUM INTO` (consistent while live), are gzip-compressed and integrity-checked.

## 5. Recovery runbook

| Symptom | Fix |
|---|---|
| `[watchdog]` issue: **pipeline_recent** | Open Actions → pipeline → latest run → read the failing step. Re-run it. If GitHub disabled the schedule, click **Enable workflow**. |
| **youtube_oauth** failing | The refresh token was revoked or expired. Re-run `scripts/oauth_bootstrap.py` on your PC and update `YT_REFRESH_TOKEN` in GitHub Secrets. Check that the consent screen is "In production". |
| **dead_tasks** | Read the linked `Task failed` issue. Fix the cause, then comment `/retry` on it. |
| A video is stuck | Comment `/retry` on its issue, or `/reject <reason>` to drop it. |
| Bad or corrupt database | Download a snapshot from Releases (`db-daily` or `db-backup-…`). Locally: `python -m ytengine restore FILE.db.gz` with `YT_DB_PATH` pointing at a copy, check it, then push it with `scripts/state.sh push` from a clone. |
| LLM quota exhausted or region blocked | Nothing to do. The router falls back to the next free provider, and work resumes next cycle. Persistent? Change the provider order in `config/channel.yaml`. |
| Need to stop everything now | Comment `/pause` on the `[control]` issue, or disable the workflows in the Actions tab. |
| Credentials leaked | Revoke at once: Google Cloud → Credentials (delete the OAuth client) and <https://myaccount.google.com/permissions>; AI Studio → delete the key; then create new ones (DEPLOYMENT steps 2 and 4). |

## 6. If you later want a private repository
A private repo hides the code and dashboard, but it costs Actions minutes (2,000 free per month on GitHub Free [VERIFIED]), and Pages for private repos needs a paid plan. The engine works the same. Only the dashboard would need another host. That would be a cost decision, so ask first.
