"""OAuth credentials from environment secrets (refresh-token flow, no browser in CI)."""
from __future__ import annotations

from .. import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",      # captions, thumbnails, video updates
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class NotConfigured(RuntimeError):
    pass


def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    cid, secret, refresh = (config.Config.secret(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and secret and refresh):
        raise NotConfigured("YouTube OAuth secrets are not set (YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN)")
    creds = Credentials(None, refresh_token=refresh, client_id=cid, client_secret=secret,
                        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
    creds.refresh(Request())
    return creds


def service(name: str = "youtube", version: str = "v3"):
    from googleapiclient.discovery import build
    return build(name, version, credentials=credentials(), cache_discovery=False)
