# Phase 1: Feasibility and Research Report

Date: 2026-11-09
Status: Phase 1 is complete. Phase 2 (architecture) is waiting on the decisions in section 9.

Labels used in this report:
- **[VERIFIED]**: confirmed in official docs found during research.
- **[REPORTED]**: from several third-party sources, not confirmed in official docs.
- **[ESTIMATE]**: my own calculation, to be measured in Phase 4.
- **[ASSUMPTION]**: unverified. Needs checking.

---

## 0. Starting state

- `F:\Tools\Youtube Earning` is empty. No code, no `youtube_automation.db`, no deployment. The second instruction block describes a deployed system, but none exists yet.
- Local machine: Python 3.13.12 and git are installed. FFmpeg, Node.js and the GitHub CLI are **not** installed. They aren't needed locally if we deploy to the cloud.

---

## 1. Executive summary

| Question | Answer |
|---|---|
| Can it run with the PC off for $0/month? | **Yes, with conditions.** It can run on GitHub Actions scheduled workflows, which are free and unlimited on public repos. |
| Can a PayPak card pay for anything? | Not verified. Overseas merchants: no evidence it works. Pakistani VPS providers: none was confirmed to accept PayPak specifically. **The design needs no card at all.** |
| Can uploads be fully automatic from day one? | **No.** YouTube locks API uploads from unaudited projects to Private. Until Google approves an API audit, a human has to publish each video. |
| Can it be 100% hands-off and still get monetized? | **Not safely at the start.** YouTube's "inauthentic content" policy targets mass-produced AI videos. For the first 10–20 videos I recommend autonomous production plus one-click human approval. |
| n8n? | **Not recommended** for the core. It needs an always-on server, and that is the part we can't pay for. GitHub Actions cron does the same job for free. |
| Edge-TTS? | **Not recommended** for a monetized channel. Commercial use outside a paid Azure subscription may break Microsoft's terms. Replace it with Kokoro-82M (Apache-2.0). |
| Niche? | "Economic History & Tech Collapses" holds up as a reasonable choice, with caveats (section 6). This is a qualitative judgment. Data-driven scoring is built into Agent 1. |

---

## 2. Hosting and infrastructure

### 2.1 Options compared

| Option | Monthly cost | Specs | Runs with PC off | Payment needed | Verdict |
|---|---|---|---|---|---|
| **GitHub Actions, public repo** | $0 [VERIFIED] | 4 vCPU, 16 GB RAM, 14 GB SSD per job (`ubuntu-latest`) [VERIFIED] | Yes (cron schedule) | None | **Recommended primary** |
| GitHub Actions, private repo | $0 up to 2,000 min/month on GitHub Free [VERIFIED] | Smaller runner for private repos [ASSUMPTION: 2 vCPU] | Yes | None, but extra minutes need a card | Fallback if code must stay private |
| Hugging Face Spaces (CPU Basic) | $0 | 2 vCPU, 16 GB RAM, 50 GB non-persistent disk [VERIFIED] | Sleeps after 48 h with no visitors [VERIFIED] | None | **Not suitable** as a scheduler. 2026 forum reports also describe quota errors. |
| Oracle Cloud Always Free | $0 | Generous | Yes | Card verification at signup | **Excluded.** It needs an internationally usable card. |
| Pakistani VPS | ~PKR 1,800–6,000/month for entry plans (~USD 6–21) [REPORTED, inconsistent] | e.g. 2 cores / 4 GB RAM at PKR 2,999 [REPORTED] | Yes | Mostly bank transfer / JazzCash / Easypaisa [REPORTED] | **Optional later.** No provider confirmed PayPak. Needs your approval and a manual check. |

### 2.2 GitHub Actions constraints and mitigations

| Constraint | Source | Mitigation |
|---|---|---|
| In public repos, scheduled workflows are disabled after 60 days with no repository activity | [VERIFIED] GitHub docs | The pipeline commits its state (DB snapshot, dashboard JSON) on every run. [ASSUMPTION] Bot commits count as activity. A watchdog workflow and a weekly email check confirm it. |
| Maximum job length | 6 h per job on GitHub-hosted runners [ASSUMPTION: from GitHub docs, not re-checked this session] | One video per job. Rendering is estimated well under 1 h (2.4). |
| No persistent disk between runs | [VERIFIED] (ephemeral VMs) | SQLite DB is committed to a `state` branch. Large media is never kept long term: render, upload, delete. |
| Public repo means public code | — | Credentials live in **GitHub Actions Secrets** (encrypted, never printed in logs). The DB holds no secrets. Decision needed (section 9). |
| Cron timing is best-effort | [REPORTED] | Publishing uses YouTube's own `publishAt` schedule, so delays don't matter. |

### 2.3 Where n8n fits

n8n Community Edition is free software, but it needs a server that is always on. Without a paid VPS there is nowhere for it to live while your PC is off. **Recommendation:** use GitHub Actions workflows as the orchestrator. They are YAML, versioned, free and observable. I can still ship n8n workflow JSON as an optional layer for a future VPS, but the system won't depend on it.

### 2.4 Rendering estimate on the free runner [ESTIMATE, to be measured in Phase 4]

| Step (10-minute long-form video) | Estimated time on 4 vCPU |
|---|---|
| Research + script (LLM API calls) | 3–8 min (mostly waiting on rate limits) |
| Kokoro TTS, 10 min of audio, CPU | 3–8 min |
| Visuals: charts (matplotlib), maps, public-domain images, title cards | 2–5 min |
| FFmpeg 1080p30 render with Ken Burns, captions, music bed | 10–25 min |
| Upload | 1–3 min |
| **Total per long video** | **~20–50 min** |

Monthly load: 8 long videos + 12 Shorts ≈ 8×50 + 12×10 = **~520 runner-minutes**. That's unlimited on a public repo and fits within 2,000 on a private one.

---

## 3. Payments: PayPak findings

- PayPak supports **domestic** e-commerce through 1LINK, with gateways such as PayFast/APPS and HBL's IPG. It uses 3-D Secure and an OTP for every transaction. [REPORTED: 2021 press releases from HBL, MCB and ProPakistani]
- PayPak markets "co-badged" cards (with MasterCard/UPI/JCB) for international use. **Yours is mono-badge, so that doesn't apply.** I found no source showing a mono-badge PayPak card working at a foreign online merchant.
- Because every payment needs an OTP, **automatic recurring billing is unlikely to work** even at domestic providers. [ASSUMPTION, consistent with OTP-per-transaction]
- **Conclusion:** the system is designed to need **no card at all**. Default spend stays **USD 0**. If we ever need a Pakistani VPS, I'll pick a specific provider, you check its payment page and invoice terms yourself, and I'll ask for your approval first.

---

## 4. Software stack: free tools checked

| Function | Choice | Cost | License / terms | Status |
|---|---|---|---|---|
| Language | Python 3.11+ | $0 | PSF | — |
| Orchestration | GitHub Actions cron + `workflow_dispatch` | $0 (public) | GitHub ToS | [VERIFIED] |
| Rendering | FFmpeg (preinstalled on `ubuntu-latest`) [ASSUMPTION; installed via apt if not] | $0 | LGPL/GPL | — |
| Database | SQLite, committed to a `state` branch | $0 | Public domain | — |
| LLM (research, script, SEO) | **Google Gemini API, free tier** (primary), **GitHub Models** (fallback) | $0, no card for the free tier [REPORTED] | Free-tier data may be used by Google to improve its products [ASSUMPTION; check terms] | Pakistan's availability is **not verified**. Check it during API setup. |
| LLM limits | Gemini Flash ~10 RPM / 250 RPD; Flash-Lite ~15 RPM / 1,000 RPD [REPORTED Feb 2026, cut back in Dec 2025]. GitHub Models: 50–150 requests/day, 8k input / 4k output tokens per request [REPORTED] | — | — | Enough for ~1 video/day. The code tracks quota and backs off. |
| Local LLM | Not recommended on CPU runners (too slow for long scripts) | — | — | Possible for small tasks only |
| TTS | **Kokoro-82M** (Apache-2.0 weights) [REPORTED via HF model card]; Piper as fallback | $0 | Each voice's license must be checked in VOICES.md before launch | **Action item** |
| TTS (rejected) | Edge-TTS | $0 | Commercial use without paid Azure "could be a violation" (Microsoft Q&A); no SLA | **Rejected for monetized use** |
| Visuals | Data charts from public datasets (e.g. FRED, World Bank) made with matplotlib; Wikimedia Commons public-domain/CC images, licenses stored per asset; Library of Congress public-domain images; original title cards | $0 | Per-asset license records | — |
| Music | YouTube Audio Library (downloaded once, licenses recorded) or CC0 tracks | $0 | Licenses recorded per track | Needs your manual one-time download |
| Publishing | YouTube Data API v3 | $0 within 10,000 units/day [VERIFIED default] | `videos.insert` ≈ 100 units [REPORTED Dec 2025 change; older docs say 1,600] | Even at 1,600 units, 6 uploads/day fit |
| Analytics | YouTube Analytics API + Data API | $0 | — | — |
| Dashboard | Static HTML/CSS/JS on GitHub Pages, reading JSON exported each run | $0 for public repos | — | Controls work through GitHub Issues/labels plus `workflow_dispatch` buttons |

---

## 5. YouTube compliance blockers (critical path)

### 5.1 API upload audit: biggest blocker [REPORTED, many sources quote the videos.insert reference]
- Videos uploaded through Google Cloud projects created after 28 July 2020 that **haven't passed the YouTube API Services audit** are **locked to Private**, whatever `privacyStatus` you send.
- Passing the audit lifts the restriction for **new** uploads only.
- **Plan:**
  1. Phase 3 builds a compliant single-channel tool with a privacy policy page on GitHub Pages.
  2. You submit the audit form. That's an account action, so it's yours to do.
  3. Until it's approved, the system runs in **"package mode."** It renders the video, thumbnail and metadata and posts them to the dashboard. You upload in YouTube Studio, about 2 minutes per video. Approval is **not guaranteed**, and how long it takes is unknown.

### 5.2 OAuth refresh-token expiry [ASSUMPTION: Google OAuth docs, not re-checked this session]
- If the OAuth consent screen is left in "Testing" status, refresh tokens expire after **7 days**, and autonomy breaks every week.
- The app must be set to "In production." For a single-user internal tool, sensitive scopes may show an "unverified app" warning that you click through once.

### 5.3 Inauthentic content policy
- Since 15 July 2025, YouTube's "repetitious content" policy has been called "inauthentic content." It targets mass-produced, repetitive or template videos. YouTube calls this a clarification, and AI content that adds real value can still be monetized. [REPORTED: BetaNews, Windows Central, Gulf News]
- One report says monetization is barred for "AI personas discussing health or finance." [REPORTED, date unclear] **Our channel uses no persona and gives no financial advice. It is historical narrative.**
- **Built-in safeguards:**
  - Each video is a distinct story with its own data charts.
  - No fixed template script.
  - Quality-control rejection rules.
  - A cap of 2 long-form videos and 2–3 Shorts per week.
  - Human approval for the first 10–20 videos.

### 5.4 AI disclosure
- Disclosure is required for **realistic** synthetic content that could be mistaken for a real person, place or event. A generic synthetic narration voice that imitates no one is likely **not** covered. A cloned real voice is. [REPORTED; official wording not confirmed]
- **Policy for this system:**
  - Never clone a real person's voice.
  - Never generate photorealistic fake "archive footage" of real events.
  - Every description states that narration is AI-generated and lists sources.
  - The "altered or synthetic" flag is set whenever a video includes realistic generated imagery.

### 5.5 Monetization (no earnings promised)
- Standard YPP: **1,000 subscribers + 4,000 public watch hours (12 months)**, or 1,000 subscribers + 10M Shorts views in 90 days. [REPORTED by several guides; the official page content was not retrieved]
- Pakistan: reported eligible for YPP. Reported **not** eligible for the lower 500-subscriber tier. [REPORTED, one source]
- Payout: through AdSense for YouTube by **wire transfer to a Pakistani bank account (IBAN/SWIFT)**, $100 threshold. Intermediary banks may take ~$20–30 per wire. [REPORTED] **No card is needed to receive money.**
- Reaching YPP thresholds typically takes months to years. It may never happen. The dashboard tracks progress honestly.

---

## 6. Niche evaluation

### Candidates scored (qualitative, 1–5)

| Niche | Demand | Competition (5 = low) | RPM potential | Automation fit | Copyright/accuracy risk (5 = low) | Total |
|---|---|---|---|---|---|---|
| **Economic history & tech/business collapses** | 4 | 3 | 4–5 | 5 | 3 | **19–20** |
| Personal finance tips | 5 | 1 | 5 | 4 | 2 (advice liability, "AI finance persona" risk) | 17 |
| General history documentaries | 4 | 2 | 3 | 4 | 3 | 16 |
| Tech news | 4 | 1 | 4 | 3 (speed matters) | 2 | 14 |
| Science explainers | 4 | 2 | 3 | 3 (visuals harder) | 3 | 15 |

### Why economic history and collapses
- **Evergreen:** a 2008 or 1720 collapse doesn't go stale, so videos keep earning views for years. [REPORTED pattern]
- **Advertiser-friendly topic adjacent to finance:** third-party RPM claims range from ~$6–9 (history) to ~$15–22 (finance). [REPORTED, self-reported, unreliable]
- **Gap:** stock-tip channels are crowded, while economic history and macro explainers are "less common but highly searchable." [REPORTED] Big competitors (MagnatesMedia, ColdFusion, Business Casual) concentrate on famous companies. Long-tail topics are open: forgotten bank panics, currency crises, failed tech standards, regional economic collapses.
- **Automation advantage:** public datasets give us **original, legally clean visuals** (charts, timelines, maps). That avoids the biggest copyright risk in this genre, which is reused news footage.

### Risks
- Factual accuracy (mitigated by Agent 3: every claim needs 2+ credible sources or gets cut).
- Company logos and trademarks (show them only in an informational, editorial way and never in a misleading thumbnail).
- No archive footage (by design).

**Status:** recommended, pending validation by Agent 1. In Phase 3, Agent 1 scores ~50 candidate topics using YouTube search results (view counts against channel sizes, upload recency). Search calls cost 100 units each [REPORTED], so that's about 25 searches per day max inside the quota budget.

---

## 7. Monthly cost breakdown

| Item | Cost | Basis |
|---|---|---|
| GitHub Actions (public repo) | $0.00 | [VERIFIED] |
| GitHub Pages dashboard | $0.00 | [VERIFIED for public repos] |
| Gemini / GitHub Models free tiers | $0.00 | [REPORTED]; hard-capped in code so no paid usage happens |
| YouTube Data / Analytics API | $0.00 | [VERIFIED default quota] |
| Kokoro TTS, FFmpeg, Python libraries | $0.00 | Open source |
| Music/images (PD, CC0, YouTube Audio Library) | $0.00 | License records kept |
| **Total** | **$0.00** | |
| Optional later: Pakistani VPS for n8n and a private dashboard | ~PKR 1,800–6,000 (~USD 6–21) | [REPORTED]; **not authorized**; needs a payment check and your approval |

Safeguard: no Google Cloud billing account is ever linked. Without one, paid Gemini usage and quota overages **can't** happen.

---

## 8. Technical limitations (stated plainly)

1. Fully autonomous public publishing depends on Google approving the API audit. That isn't in our control.
2. Free LLM tiers can change or shrink without notice (they were cut in Dec 2025 [REPORTED]). The code supports switching providers, and the pipeline degrades to "draft and wait" instead of failing silently.
3. Free LLMs hallucinate. Fact-checking reduces this but can't make it zero, which is why the human review gate exists at launch.
4. A public repo exposes code and the content calendar, never credentials. A private repo limits us to 2,000 min/month and a dashboard that isn't on Pages.
5. Video quality will be "narrated documentary with charts and stills," not cinematic B-roll, because licensed stock footage costs money.
6. Search-demand data is limited. There's no free Google Trends API, and YouTube search quota is small.

---

## 9. Decisions needed before Phase 2

1. **Repo visibility:** public (unlimited minutes, free Pages dashboard; recommended) or private?
2. **Accept these stack changes:** GitHub Actions instead of n8n for the core, and Kokoro instead of Edge-TTS?
3. **Launch mode:** human approval for the first 10–20 videos (recommended), then hands-off after audit approval and quality review?
4. **Niche:** confirm "Economic History & Tech Collapses," subject to Agent 1's data check.

### Manual one-time setup that only you can do (Phase 5)
- Create a GitHub account/repo, a Google Cloud project with the YouTube Data API v3 enabled, an OAuth client, and a Gemini API key from AI Studio (no billing).
- Run the one-time OAuth authorization on your PC, then store the token as a GitHub Secret.
- Submit the YouTube API audit form.
- AdSense and identity verification, when eligible.

---

## Sources
- GitHub-hosted runner specs: https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- GitHub Actions billing (free for public repos): https://docs.github.com/en/billing/concepts/product-billing/github-actions
- Scheduled workflow 60-day disable: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows
- HF Spaces hardware and sleep: https://huggingface.co/docs/hub/spaces-overview , https://huggingface.co/docs/huggingface_hub/main/guides/manage-spaces
- Unaudited API uploads locked private: https://www.ayrshare.com/solutions/google-api-error-403-unverified-app-how-to-fix-the-audit-pipeline/ , https://github.com/porjo/youtubeuploader
- YouTube API quota costs: https://developers.google.com/youtube/v3/determine_quota_cost , https://www.getphyllo.com/post/is-the-youtube-api-free-in-2026-quota-limits-costs-when-to-pay
- Inauthentic content policy: https://www.windowscentral.com/software-apps/youtube-ramps-up-campaign-against-ai-slop-with-stricter-monetization-caveats-on-inauthentic-and-repetitive-videos , https://betanews.com/2025/07/10/youtube-is-fighting-ai-slop-with-new-monetization-guidelines/
- AI disclosure: https://blog.youtube/news-and-events/disclosing-ai-generated-content , https://air.io/en/youtube-glossary/what-is-youtubes-ai-content-disclosure-policy
- YPP: https://support.google.com/youtube/answer/72851 , https://vidiq.com/blog/post/monetize-youtube-guide/
- PayPak e-commerce: https://www.hbl.com/news-and-media/hbl-enables-e-commerce-transactions-for-all-paypak-cards , https://propakistani.pk/2021/11/04/mcb-bank-1link-partner-to-facilitate-paypak-cardholders-in-ecommerce-transactions/ , https://paypak.net.pk/about-us
- Pakistani VPS pricing/payments: https://middlehost.com/en-PK/virtual-private-servers/ , https://pakish.net/vps-hosting , https://www.whtop.com/plans/urlpk.com/131619
- Edge-TTS terms: https://learn.microsoft.com/en-us/answers/questions/2088770/are-opensource-edge-tts-free-for-commercial-use
- Kokoro-82M: https://huggingface.co/hexgrad/Kokoro-82M
- Gemini free tier: https://ai.google.dev/gemini-api/docs/rate-limits
- GitHub Models limits: https://fast.io/resources/github-models-rate-limit/
- Niche research: https://outlierkit.com/resources/faceless-business-documentary-channels/ , https://blog.autonolab.com/niches/2026-01-17-faceless-youtube-economics/
- Pakistan AdSense payouts: https://grandavehousing.calpoly.edu/blog/youtube-withdrawal-methods-in-pakistan-a-comprehensive-guide-1764797476
