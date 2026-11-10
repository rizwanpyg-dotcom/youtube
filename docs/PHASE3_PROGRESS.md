# Phase 3: Development progress

> **Update (same day, part 2 complete):** control plane (`control.py`), notifications (`notify.py`), watchdog, backups and run records (`ops.py`), Agent 1 (`market.py`), Agent 2 (`topics.py`, Wikipedia pageviews demand plus calendar), Agent 9 (`analytics.py`, learning loop and YPP tracker), Shorts (`shorts.py`), the scheduled `cycle`, the dashboard (`dashboard.py` + `dashboard/`), GitHub workflows, state-branch sync, OAuth bootstrap and optional n8n JSON are all built. Test results are in [TESTING_REPORT.md](TESTING_REPORT.md). The section below is the part 1 record.

Date: 2026-11-09
Milestone reached: **one complete sample video produced end to end on this PC**, from a real topic, real sources and real data.

---

## 1. What was built (≈3,500 lines of Python in `ytengine/`)

| Module | Status | Notes |
|---|---|---|
| `config.py`, `log.py` (secret redaction), `db.py` (migrations, task queue, leases, retries), `quota.py` (hard caps) | ✅ built + unit-tested | |
| `llm.py` (Gemini → GitHub Models fallback, JSON validation, fixture replay) | ✅ built | **Live providers not yet called.** They need your Gemini key (Phase 5). |
| `sources.py` (robots.txt-aware fetch, cache, Wikipedia + its cited references, FRED) | ✅ live-tested | 14 documents from 10 publishers fetched for the sample |
| Agent 3 `fact_check.py` | ✅ live-tested | Deterministic verification (see §3) |
| Agent 4 `scriptwriter.py` | ✅ tested with fixture LLM | |
| Agent 5 `voice.py` (Kokoro-82M, per-sentence, loudness to −14 LUFS) | ✅ live-tested | |
| Agent 6 `render/` (animated cards, self-drawing data charts, Ken Burns stills, music, mix, captions) | ✅ live-tested (cards, charts) | Still-image scenes (Wikimedia Commons) are coded but not yet exercised |
| Agent 7 `thumbnail.py` (3 styles, bandit choice) | ✅ live-tested | |
| Agent 8 `seo.py`, `publisher.py` (package mode), `youtube/upload.py` | ✅ package mode tested | API upload is coded but **not run**: it needs OAuth and an audited API project |
| Agent 10 `qc.py` (research, script, audio, video, thumbnail, metadata, final gates) | ✅ live-tested + unit-tested | |
| `pipeline.py` (idempotent tasks, approval gates, QC retry loops), CLI | ✅ live-tested | |
| `strategy.py` (Thompson-sampling bandit) | ✅ built | Learns once analytics exist |

**Not built yet (Phase 3 part 2):**
- GitHub Actions workflows, plus control commands through Issues.
- Watchdog, backups, dashboard.
- Agent 1 (market research), Agent 2 (topic research and calendar), Agent 9 (analytics and learning loop).
- Shorts cutting.
- Optional n8n JSON.

---

## 2. Sample run: "The Dot-com Bubble"

Command: `python -m ytengine sample`. Output: `output/0001-dot-com-bubble-2000/`

| Item | Result |
|---|---|
| Video | 2:54.8, 1920×1080, 30 fps, H.264 High, AAC 192k stereo, 12 MB |
| Package | `video.mp4`, `thumbnail.png`, `captions.srt`, `description.txt` (chapters, 7 numbered sources, credits, AI and education disclosure), `tags.txt`, `metadata.json`, `UPLOAD_CHECKLIST.md` |
| QC gates | research 1.00 · script 1.00 · audio 1.00 · video 1.00 · thumbnail 1.00 · metadata 1.00 · final 1.00 |
| Narration | −14.4 LUFS integrated, −1.3 dBTP peak, longest pause 0.61 s, 0 failed sentences |

**What was live and what was a fixture:** source fetching, FRED data, claim verification, TTS, rendering, QC and packaging were all real. The **LLM steps** (claim extraction, outline, script, SEO text) used a **recorded fixture** that I wrote, because no Gemini key exists yet. The fixture's claims still had to pass the same deterministic checks against the real fetched pages and data. The first real LLM run happens once you add the key.

**This sample is a pipeline test, not a launch video.** It's 3 minutes, not 9–13. The channel name is a placeholder, and the script approval was recorded as `local-demo`, not by you. Don't upload it as-is.

### Measured performance (this PC: 12 logical CPUs)
| Step | Time | Rate |
|---|---|---|
| Research (fetch 14 documents + FRED) | ~20 s | network-bound |
| Kokoro TTS | 145.6 s for 174.8 s of audio | RTF 0.83 |
| Scene rendering + mix + mux | 218.5 s | 1.25× real time |
| Cards / charts | ~30 ms / ~70 ms per frame | charts redraw the whole matplotlib figure |

**Extrapolation [ESTIMATE, to be measured on the runner in Phase 4]:** a 10-minute video takes ~8 min TTS + ~13 min render ≈ 21 min here. The GitHub runner has 4 vCPUs, so expect roughly 2–3× longer, **≈ 40–60 min**. That's inside the planned 120-minute job limit. It's above my Phase 1 estimate of 20–50 min, mainly because Kokoro on CPU is slower than I assumed.

---

## 3. What the safety checks caught during development

These were real catches, not staged ones, apart from the two deliberately planted test claims.

| Check | Caught |
|---|---|
| Evidence quote must be in the fetched page | planted fabricated claim "Pets.com spent $11.5 million on a Super Bowl ad" → **rejected** |
| Every number in a claim must be in its evidence | planted "lost 87%" next to a real "78%" quote → **rejected** |
| Contradicting source | "exactly 17 dot-com Super Bowl advertisers" (Business Insider says 14) → **conflicting**; the script may only say "counts range from 12 to 19" |
| Single-publisher claims | "$396M raised by Webvan" and "P/E over 200" → **unverified**, unusable |
| Data beats text | Wikipedia states both "+600%" and "+400%" for 1995–2000; FRED data gives **+579%**, and the script uses the data. Wikipedia's "fell 9%" (14 Apr 2000) is −9.7% in the data. |
| On-screen numbers must be cited | a card showed "2000" without citing a claim containing it → **script QC fail** (fixed in the script) |
| Quotes must be verbatim | a card showed "It worked for Amazon.com…" while the source says "which worked…" → **misquote caught** after I switched to word-level matching. My first, character-level matcher had missed it. |
| Word count | script 11 words under minimum → **fail** (fixed with content, not by lowering the bar) |
| Loudness | first mix −16.1 LUFS → normaliser rewritten (gain + limiter) → −14.4 LUFS, −1.3 dBTP |
| Captions | one-word 0.46 s cue → balanced chunking; shortest cue now 1.03 s |
| Retry/idempotency | a bug (ffprobe missing locally) failed the media task; the re-run **skipped** research and script and retried only media (attempt 2). A third run skipped everything, so nothing was duplicated. |

---

## 4. Tests

`python -m pytest -q tests` → **20 passed**. They cover:
- evidence matching, including the paraphrase test;
- claim statuses: verified, unverified, and rejected for a bad quote, a bad number or bad data;
- script QC: unsupported numbers, uncited facts, on-screen numbers, financial advice;
- the task queue: idempotency, backoff to dead, stale-lease reclaim;
- quota hard caps and the expense-approval constraint;
- secret redaction;
- TTS text normalisation;
- caption chunking.

---

## 5. Decisions and changes made during the build

- **Fact-check rule refinement:** a direct quotation can be verified by one tier-1/2 publisher (it is "X said Y, as reported by Z"), but the script must attribute it, which QC checks. All other claims still need tier-1 data or 2+ independent publishers.
- **Kokoro voice:** `am_michael` (grade C+). The model weights are Apache-2.0, and the model card states no restriction on commercial use of the English voices (checked on the HF model card and VOICES.md).
- **Music:** generated by code (original), so there's no third-party licence to track. A curated YouTube Audio Library folder can still be added later.
- **Fonts:** Archivo Black and Inter (SIL OFL 1.1), stored in `assets/fonts/` with their licence texts.
- **Performance:** OpenCV `warpAffine` replaced Pillow transforms for motion (68 ms → 6.5 ms per frame). Grain and fades moved into FFmpeg filters.

---

## 6. Next (Phase 3 part 2)
1. GitHub Actions: `pipeline.yml`, `control.yml` (owner-only `/approve`, `/reject`, `/changes`, `/pause`, `/resume`, `/retry`, `/published`), `watchdog.yml`, `backup.yml`, `pages.yml`, with the DB on the `state` branch.
2. Dashboard: static site plus a sanitised JSON export.
3. Agent 2 (topic research and calendar) and Agent 1 (market research). Agent 9 (analytics, learning loop and YPP tracker).
4. Shorts (vertical cut with burned-in captions) and Commons image scenes.
5. Faster charts (blitting) to bring the runner estimate down.
