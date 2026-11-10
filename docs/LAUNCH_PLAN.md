# Phase 6: Controlled launch plan

The goal of the first two months is **trust in quality**, not volume. Growth on YouTube is slow and uncertain. Nothing here promises views, subscribers or earnings.

## Stage A: supervised (from the first run until at least 15 published videos)
**Settings:** `publish_mode=package`, script approval ON, 2 long-form + 2 Shorts per week.

For **every** video, before `/approve`:
1. **Claims table.** Every claim the script uses should be `verified`. Spot-check two sources by opening the links.
2. **Script.** Does the opening make you want to keep watching? Is anything vague, repetitive or "AI-sounding"? If so, `/changes <specific notes>`.
3. **After rendering, watch the full video once**, listening for mispronounced names. Add fixes to `config/lexicon.yaml`. Check that charts match the narration.
4. **Title and thumbnail** must not promise more than the video delivers.
5. **YouTube Studio:** answer "Altered content" honestly. "No" for a generic AI narration voice with charts and cards (reported guidance); "Yes" if a video ever contains realistic generated imagery.

Track these in the dashboard weekly:
- average view percentage;
- the first 30 seconds of retention, in YouTube Studio (the API doesn't provide curves);
- click-through rate, in Studio;
- comments mentioning errors. Correct real errors in a pinned comment.

## Stage B: review window (after the API audit is approved AND Stage A is stable)
**Move when all are true:**
- the audit is approved;
- the last 10 videos needed no `/changes` for factual reasons;
- no copyright or Content ID claims;
- no policy warnings.

**Change:** comment `/mode review_window` on the `[control]` issue. Videos then upload as **private** and auto-publish at their slot. You get the issue in advance and can `/reject` before the slot.

## Stage C: autonomous (optional)
**Move when:**
- 15 or more videos have gone through Stage B without intervention;
- the watchdog has been quiet for 30 days.

**Change:** comment `/mode auto` and `/approval off` on the `[control]` issue. Both are recorded as owner decisions.

**You still keep:**
- any spending decision;
- AdSense and identity verification;
- copyright disputes;
- niche changes;
- raising the cadence.

These are never automated.

## Cadence rules
- Start at 2 long-form + 2 Shorts per week.
- Raise Shorts to 3 per week only after 8 long-form videos average ≥ 35% viewed (design threshold; revisit with real data).
- Never raise the long-form cadence while any quality problem is open. Quantity is not the goal, and repetitive mass production risks demonetisation under YouTube's inauthentic-content policy.

## The learning loop (automatic, but slow)
- From day 7 after publishing, each video's views and average view percentage, ranked against your other videos, feed the strategy arms (topic category, thumbnail style, title pattern).
- An option needs ≥ 4 videos before the system treats it as a finding. Expect meaningful insights after ~2–3 months.
- The **Performance** tab shows what the system has learned, with the evidence.

## Monetisation milestones (when they happen; no promises)
- **YPP thresholds** (reported for Pakistan): 1,000 subscribers + 4,000 public watch hours in 12 months, or 1,000 subscribers + 10M Shorts views in 90 days. Check YouTube's official page before applying.
- **Applying, AdSense, identity verification and payout setup** (wire transfer to a Pakistani IBAN, reportedly) are **your actions**. The system only tracks progress.

## Things that would make me recommend pausing
- A Content ID or copyright claim. The design avoids third-party media, so investigate any claim.
- A community guidelines warning.
- Repeated factual corrections from viewers.
- Average view percentage under ~20% across 5+ videos. That points to a format problem, not a volume problem.
