# Phase 4: Testing report

Date: 2026-11-09. Environment: Windows 11 PC, 12 logical CPUs, Python 3.13, FFmpeg 9.0.2 (bundled build). The cloud runner (Ubuntu, 4 vCPU, Python 3.11) **hasn't been exercised yet**. It needs the repository from Deployment Step 1.

## Summary

| Area | Result | How it was tested |
|---|---|---|
| Unit tests | **41 / 41 pass** | `python -m pytest -q tests` |
| End-to-end long-form video | **pass** (all 7 QC gates 1.00) | `python -m ytengine sample` |
| End-to-end Short (vertical, burned-in captions) | **pass** (all gates 1.00; 42.7 s) | integration test, cycle 2 |
| Two scheduled cycles with an approval between them | **pass** after 4 fixes | `tests/integration_cycle.py` |
| Failure recovery and duplicate prevention | **pass** | live crash during the sample run, plus unit tests |
| State branch (cloud persistence) | **pass** | round trip against a local git remote |
| Dashboard | **pass** (desktop + 375 px phone width, no console errors) | rendered from the sample DB in a browser |
| Workflow files | **structurally valid**; every third-party Action pinned to a verified commit SHA | YAML checks in Python (actionlint couldn't be run here) |
| Live Gemini / GitHub Models calls | **not tested**: needs your API key | fixture LLM used instead |
| Live YouTube upload / analytics | **not tested**: needs your OAuth and channel | code written against the documented API |
| GitHub Actions runtime, cloud resource use | **not measured yet** | first deployed run |

## 1. Unit tests (41)

| Group | Tests | What they prove |
|---|---|---|
| Evidence matching | 5 | Exact quotes match despite punctuation; **paraphrases and invented quotes are rejected**; sentence splitting; similarity |
| Claim verification | 2 | Verified, unverified, conflicting and rejected statuses for bad quotes, wrong numbers and data mismatches; weekend dates use the previous trading day |
| Script QC | 5 | Unsupported numbers, uncited facts, on-screen numbers and financial advice are all caught |
| Task queue | 3 | Idempotent enqueue; backoff → dead; stale-lease reclaim |
| Quota / cost | 2 | Hard daily cap; any expense > $0 without approval is refused |
| Security | 3 | Secret redaction in logs; sanitised dashboard export; **non-owner commands ignored** |
| Control plane | 7 | Pause/resume, approve, reject, change requests (version rounds), `/published` validation, mode changes recorded as approvals, `/approval on|off`, retry |
| Scheduling | 4 | Calendar slots follow the schedule; best topic first; no two consecutive in one category; re-fill is idempotent |
| Shorts | 2 | One Short per long-form; weekly limit; only **verified** parent claims are inherited |
| Backups | 3 | Backup/restore round trip; corrupt backup refused; health checks recorded |
| Regressions | 3 | Same Wikipedia article = duplicate topic; backoff honoured; insufficient evidence retires the topic without retries |
| Media helpers | 2 | TTS normalisation (dates, years, %, $, Roman numerals); caption chunking |

## 2. Measured performance (this PC)

| Work | Measured |
|---|---|
| Kokoro narration | RTF 0.82–0.85 (175 s of audio in 146 s; 42.7 s in 36 s) |
| Rendering, long-form | 1.25× real time (219 s for a 175 s video) |
| Rendering, Short (vertical) | ~1 min for 43 s |
| Card / chart frame cost | ~30 ms / ~70 ms (after replacing Pillow transforms with OpenCV: 68 → 6.5 ms per warp) |
| Research gathering (parallel) | 35–55 s per topic, 4–14 documents |
| One full cycle (topic research + 1 Short render) | ~8 min |

**Cloud estimate [ESTIMATE, to measure]:** a 10-minute long-form video takes ~40–60 min on the 4-vCPU runner. The pipeline job has a 150-minute limit, and the cycle stops starting new renders after 95 min.

## 3. Defects found by testing and fixed

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | review of rendered frames | Quote card paraphrased a source ("It worked…" vs "which worked…") | Word-level verbatim check for every quote in narration and on screen |
| 2 | sample run | Loudness −16.1 LUFS (normaliser silently backed off) | Explicit gain + limiter; now −14.4 LUFS, −1.3 dBTP |
| 3 | sample run | 0.46 s one-word captions | Balanced chunking; shortest cue now 1.03 s |
| 4 | sample run | `ffprobe` missing locally crashed video QC | FFmpeg-based probe fallback |
| 5 | profiling | Rendering too slow for the runner (68 ms/frame motion) | OpenCV warps; grain and fades moved into FFmpeg |
| 6 | state test | Second push failed (leftover branch); non-atomic | Rewritten with git plumbing; single-commit branch |
| 7 | unit test | Backup left DB handle open (Windows lock) | Connections always closed |
| 8 | integration | Duplicate topic accepted (different slug) | Same Wikipedia article = duplicate; title-only fingerprint |
| 9 | integration | Failed task retried immediately (backoff ignored) | `next_run_at` honoured |
| 10 | integration | Topic with no evidence retried 3× | Retired immediately; calendar slot reopened and reassigned |
| 11 | integration | Wikipedia API **HTTP 429** (rate limit) | Throttled API client with `Retry-After` |
| 12 | diagnosis | Too few usable sources (bot blocks, paywalls, dead links) | Internet Archive fallback, parallel fetching, Federal Reserve sites as tier 1, extra authoritative URLs from the topic agent |
| 13 | review | `/changes` twice would reuse a task key | Revision rounds counter |

## 4. Known limitations (not hidden)
1. **Book-heavy topics** (citations mainly to books) may still fail the strict evidence rule and be skipped. That's by design: no evidence, no video.
2. **Fixture LLM.** Every LLM output so far was a recorded fixture that I wrote. The real model may write weaker scripts or propose bad evidence. The deterministic checks catch bad facts, but not dull writing, which is why script approval stays on at launch.
3. **Pronunciation hasn't been reviewed by ear.** Listen to the first videos and add fixes to `config/lexicon.yaml`.
4. **The YouTube upload, analytics and audit paths are untested** until your OAuth exists.
5. **Charts** at ~70 ms/frame are the render bottleneck. Matplotlib blitting could roughly halve that if the runner proves slow.
6. **YPP watch hours** from the API include all content, which approximates the official public long-form figure.
7. **The 60-day inactivity rule.** Assumed handled by state pushes; confirm over the first 2 months.

## 5. Phase 4 tests still to run after deployment (checklist)
- [ ] First `pipeline` run on GitHub completes; note the runtime and the "Workflow runs" CPU/peak-MB figures on the dashboard.
- [ ] Gemini free tier works from your account (the run log shows which model answered).
- [ ] A script approval issue arrives by email; `/approve` from your phone works.
- [ ] Package artifact downloads; manual upload; `/published <id>` recorded.
- [ ] `maintenance → watchdog` run is green; `analytics` run is green once a video is public.
- [ ] Turn the PC off for 24 h; runs continue (DEPLOYMENT Step 7).
- [ ] Kill a run mid-render (Actions → Cancel); the next run resumes the video (lease reclaim).
