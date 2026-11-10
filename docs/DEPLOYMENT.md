# Phase 5: Deployment guide (your steps)

Everything below is free. **No step asks for a card.** If any page asks for card details or "billing", stop: you're on the wrong page or a paid upgrade. Close it and follow these steps again.

Time needed: about 60–90 minutes, once. After that your PC can stay off.

Why these steps need you: they create accounts, grant access to your YouTube channel and handle secret keys. Those are owner-only actions, so I prepared everything except the clicks only you can make.

---

## Step 1: GitHub account and repository (15 min)

1. Create a free account at <https://github.com/signup>, or use your existing one. Turn on two-factor authentication (Settings → Password and authentication).
2. Create a new repository at <https://github.com/new>:
   - **Name:** anything, e.g. `channel-engine`.
   - **Visibility: Public.** Public repos get unlimited free Actions minutes and free Pages. The code, content plans and dashboard become public. Secrets never do (see [SECURITY.md](SECURITY.md)).
   - Don't add a README, .gitignore or licence.
3. Upload the project from this PC (PowerShell, in `F:\Tools\Youtube Earning`). Use the repository URL GitHub shows you.

```bash
git remote add origin https://github.com/YOUR-USERNAME/channel-engine.git
```

```bash
git push -u origin main
```

   A browser window asks you to sign in to GitHub the first time.

4. In the repository on GitHub:
   - **Settings → Pages → Build and deployment → Source: "GitHub Actions"**
   - **Settings → Actions → General → Workflow permissions: "Read and write permissions"**, then Save.
   - **Settings → General → Features:** make sure **Issues** is ticked. Approvals and alerts arrive as issues, and GitHub emails you about them.

## Step 2: Free AI key: Gemini (10 min)

1. Go to <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** and choose to create it in a new project. Since 28 May 2026, AI Studio creates **auth keys** that start with `AQ.` (older standard keys started with `AIza`). Both work; the engine sends the key in the `x-goog-api-key` header, as Google's documentation shows.
3. **Don't set up billing** or "upgrade". The free tier needs no card. With no billing account, paid usage is impossible.
4. If AI Studio says your region isn't supported, skip this step. The system automatically falls back to **GitHub Models** (free, using the repository's built-in token; nothing to set up). Tell me if this happens, because quality and limits differ.
5. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**:
   - Name `GEMINI_API_KEY`, value = the key. **Paste it only here.** Don't paste it in chat, issues or files.

## Step 3: The YouTube channel (10 min)

1. At <https://www.youtube.com/account>, click **Add or manage your channel(s)** and create a channel. A brand-account channel is recommended, so the channel isn't tied to your personal name.
2. Choose the final channel name. "Collapse Ledger" is only a placeholder; search YouTube first to make sure the name isn't taken. Then change `channel.name_working` in `config/channel.yaml` and the eyebrow text on the sample's closing card.
3. Verify the channel at <https://www.youtube.com/verify> (phone number). This allows custom thumbnails and videos over 15 minutes.

## Step 4: Google Cloud project for the YouTube API (20 min)

1. Open <https://console.cloud.google.com/> with the **same Google account that owns the channel**. Create a new project (e.g. `channel-engine`). **Skip any billing prompt.** The YouTube APIs don't need billing.
2. **APIs & Services → Library:** enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **APIs & Services → OAuth consent screen** (also called "Google Auth Platform"):
   - User type **External**. App name e.g. "Channel Engine". Use your email for support and developer contact.
   - **Scopes:** you can skip this. The script requests them itself: `youtube.upload`, `youtube.force-ssl`, `yt-analytics.readonly`.
   - **Test users:** add your own Google account.
   - Then click **Publish app → In production**. Leaving it in "Testing" makes Google expire your login every **7 days**, which would stop the automation. Google will show an "unverified app" warning; that's expected for a private tool you're the only user of.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: Desktop app.** Download the JSON and save it as **`client_secret.json`** in `F:\Tools\Youtube Earning\`. It's git-ignored and won't be uploaded.
5. Run the one-time sign-in on your PC:

```bash
.venv\Scripts\python scripts\oauth_bootstrap.py
```

   Your browser opens. Sign in with the channel owner account. On the "Google hasn't verified this app" screen, click **Advanced → Go to Channel Engine (unsafe)**; it's your own app. Approve all three permissions. The script saves three values into the local `.env` file.
6. In GitHub, add three more secrets with the values from `.env`: `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.
7. Delete `client_secret.json` from the folder (you can always download it again).

## Step 5: Apply for the YouTube API audit (15 min, then wait)

Until Google approves this audit, API uploads are locked to **Private**, so the system runs in **package mode**: it makes the finished video, and you upload it in YouTube Studio (about 2 minutes). Everything else is automatic.

1. Open the audit and quota form linked from Google's YouTube API documentation. Search for "YouTube API Services audit and quota extension form". [REPORTED location: support.google.com/youtube/contact/yt_api_form. Check that the link comes from Google's own docs.]
2. Describe it honestly: a single-channel, owner-only tool that uploads the owner's own original documentaries and reads that channel's analytics. No third-party users.
3. **Privacy policy URL:** `https://rizwanpyg-dotcom.github.io/youtube/privacy.html`. It's published by the first pipeline run (Step 6).
4. Approval isn't guaranteed and its timing is unknown. When it's approved, comment `/mode review_window` on the `[control]` issue.

## Step 6: First run and checks (15 min)

1. In GitHub: **Actions** → enable workflows if asked → **pipeline → Run workflow**.
2. That first run creates the `[control] Owner commands` issue, researches topics, fills the calendar and starts the first video. Research and script take ~5–10 min. Then a **Script approval** issue appears and GitHub emails you.
3. Read the script and claims table in the issue, then comment `/approve`, `/changes <what to fix>` or `/reject <reason>`.
4. The next scheduled run (every 4 hours, or run it manually) renders the video. The run takes up to ~60 min [ESTIMATE; measure the first one]. A **Ready to upload** issue links to the download.
5. Upload in YouTube Studio by following `UPLOAD_CHECKLIST.md` in the package. Then comment `/published <video id>` on that issue. The video id is the 11 characters after `youtu.be/`.
6. Open the dashboard at `https://rizwanpyg-dotcom.github.io/youtube/`.

## Step 7: Prove it works with the PC off

1. Shut your PC down for 24 hours.
2. Then, from your phone, check that new runs appear under **Actions** (every 4 h, plus maintenance every 6 h). Check that the dashboard's "Updated" time is recent and that any new approval issue arrived by email.
3. If anything failed, a `[watchdog]` issue explains it. See [SECURITY.md → Recovery runbook](SECURITY.md#recovery-runbook).

---

## What runs where after deployment

| Work | Runs on | Your PC needed? |
|---|---|---|
| Research, fact-check, script, narration, rendering, thumbnails, SEO, QC | GitHub Actions (free) | No |
| Scheduling, retries, health checks, backups, dashboard | GitHub Actions + Pages (free) | No |
| Approvals during the supervised launch | Your phone or PC (GitHub issue comments) | Any device |
| Uploading, **until the API audit is approved** | YouTube Studio (2 min per video) | Any device that can upload (phone works) |
| Uploading after audit approval | GitHub Actions | No |

## Where the secrets are

| Secret | Where it lives | Never put it in |
|---|---|---|
| `GEMINI_API_KEY` | GitHub Actions secret (and optionally local `.env`) | chat, issues, code, dashboard |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | GitHub Actions secrets (and local `.env`) | same |
| Card number, CVV, PIN, OTP | **Nowhere.** The system never needs them. | anywhere |
