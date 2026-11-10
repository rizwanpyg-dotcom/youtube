"""Agent 8b: publishing.

Modes (settings.publish_mode):
  package        Default until the YouTube API audit passes. Writes an upload package
                 (video, thumbnail, captions, metadata, checklist). The owner uploads it
                 in YouTube Studio. (CI also attaches it as an Actions artifact.)
  review_window  Upload as private with publishAt = slot; the owner can watch it and
                 /reject before it goes public.
  auto           As review_window, without the notification. Owner-enabled only.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from .. import config, db
from ..log import get as get_log

log = get_log("publisher")

CHECKLIST = """# Upload checklist: {title}

Package created {created}. Takes about 2 minutes in YouTube Studio.

1. Studio → Create → Upload videos → select `video.mp4`.
2. **Title:** copy from `metadata.json` → `title`.
3. **Description:** paste the full contents of `description.txt`.
4. **Thumbnail:** upload `thumbnail.png`.
5. **Audience:** "No, it's not made for kids".
6. **Show more → Tags:** paste `tags.txt`.
7. **Show more → Altered content:** "{synthetic}". (Reported guidance: a generic synthetic narration voice that imitates no real person does not require "Yes"; realistic generated imagery or a cloned real voice does. Not yet confirmed against YouTube's official help page. If unsure, choose "Yes".)
8. **Category:** Education. **Video language / caption language:** English.
9. **Subtitles:** Upload file → "With timing" → `captions.srt`.
10. **Visibility:** Schedule for **{slot}** (or Public if no slot).
11. After publishing, reply on the GitHub issue with `/published <youtube-video-id>` so analytics can track it.
"""


def package(conn: sqlite3.Connection, video_id: int, files: dict, meta: dict) -> Path:
    v = db.video(conn, video_id)
    out = config.get().paths.output / f"{video_id:04d}-{v['slug']}"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(files["video"], out / "video.mp4")
    shutil.copy2(files["thumbnail"], out / "thumbnail.png")
    shutil.copy2(files["srt"], out / "captions.srt")
    (out / "description.txt").write_text(meta["description"], encoding="utf-8")
    (out / "tags.txt").write_text(", ".join(meta["tags"]), encoding="utf-8")
    public_meta = {k: meta[k] for k in ("title", "alt_titles", "tags", "chapters", "category_id",
                                        "synthetic_media", "made_for_kids", "title_pattern")}
    public_meta["publish_slot"] = v["publish_slot"]
    (out / "metadata.json").write_text(json.dumps(public_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "UPLOAD_CHECKLIST.md").write_text(CHECKLIST.format(
        title=meta["title"], created=db.utcnow(), slot=v["publish_slot"] or "not scheduled",
        synthetic="Yes" if meta["synthetic_media"] else "No"), encoding="utf-8")
    conn.execute("UPDATE videos SET package_url=? WHERE id=?", (str(out), video_id))
    db.set_stage(conn, video_id, "package_ready", status="waiting")
    log.info("package ready: %s", out)
    return out


def upload(conn: sqlite3.Connection, video_id: int, files: dict, meta: dict) -> str:
    """Upload as private with a scheduled publish time. Requires an audited API project.

    NOT YET EXERCISED against the live API: it needs your OAuth credentials (Phase 5).
    """
    from ..youtube import upload as yt_upload
    v = db.video(conn, video_id)
    marker = f"yte-{video_id}-{v['slug'][:24]}"
    existing = yt_upload.find_by_marker(conn, marker)
    if existing:  # crash after upload but before DB write: don't upload twice
        vid = existing
    else:
        vid = yt_upload.insert(conn, files["video"], meta, marker, publish_at=v["publish_slot"])
    conn.execute("UPDATE videos SET youtube_video_id=?, privacy='private', publish_at=? WHERE id=?",
                 (vid, v["publish_slot"], video_id))
    yt_upload.set_thumbnail(conn, vid, files["thumbnail"])
    yt_upload.add_captions(conn, vid, files["srt"])
    db.set_stage(conn, video_id, "scheduled", status="waiting")
    return vid
