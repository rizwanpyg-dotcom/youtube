"""YouTube Data API v3 publishing: resumable upload, thumbnail, captions.

Every call reserves quota first (quota.youtube), so the daily self-cap is never exceeded.
Status: written against the documented API; not yet run against a live channel.
"""
from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path

from .. import quota
from ..log import get as get_log
from .auth import service

log = get_log("youtube")

RETRIABLE_STATUS = {500, 502, 503, 504}


def insert(conn: sqlite3.Connection, video: Path, meta: dict, marker: str, publish_at: str | None) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    yt = service()
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": bool(meta.get("made_for_kids")),
              "containsSyntheticMedia": bool(meta.get("synthetic_media"))}  # field name to confirm (Phase 4)
    if publish_at:
        status["publishAt"] = publish_at
    # The marker rides along as an extra tag (not shown on the watch page) so a crashed run
    # can find this upload again instead of uploading a duplicate.
    body = {"snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": [*meta["tags"], marker], "categoryId": meta.get("category_id", "27"),
                        "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": status}
    quota.youtube(conn, "videos.insert")
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True,
                                                        mimetype="video/mp4"))
    response, attempt = None, 0
    while response is None:
        try:
            _, response = req.next_chunk()
        except HttpError as e:
            if e.resp.status not in RETRIABLE_STATUS or attempt >= 6:
                raise
            attempt += 1
            time.sleep(min(64, 2 ** attempt) + random.random())
    vid = response["id"]
    log.info("uploaded %s as private (publishAt=%s)", vid, publish_at)
    return vid


def find_by_marker(conn: sqlite3.Connection, marker: str) -> str | None:
    """Look for our marker tag among the channel's 25 most recent uploads (3 units, no search.list)."""
    yt = service()
    quota.youtube(conn, "channels.list")
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    quota.youtube(conn, "playlistItems.list")
    items = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=25).execute()
    ids = [it["contentDetails"]["videoId"] for it in items.get("items", [])]
    if not ids:
        return None
    quota.youtube(conn, "videos.list")
    for v in yt.videos().list(part="snippet", id=",".join(ids)).execute().get("items", []):
        if marker in v["snippet"].get("tags", []):
            return v["id"]
    return None


def set_thumbnail(conn: sqlite3.Connection, video_id: str, png: Path) -> None:
    from googleapiclient.http import MediaFileUpload
    quota.youtube(conn, "thumbnails.set")
    service().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(png), mimetype="image/png")).execute()


def add_captions(conn: sqlite3.Connection, video_id: str, srt: Path) -> None:
    from googleapiclient.http import MediaFileUpload
    quota.youtube(conn, "captions.insert")
    service().captions().insert(part="snippet", body={"snippet": {"videoId": video_id, "language": "en",
                                                                  "name": "English", "isDraft": False}},
                                media_body=MediaFileUpload(str(srt), mimetype="application/octet-stream")).execute()
