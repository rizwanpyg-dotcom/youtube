"""Owner notifications through GitHub Issues (GitHub emails the owner on new issues/comments).

Outside GitHub Actions (no GITHUB_TOKEN / GITHUB_REPOSITORY) every call just logs, so local
runs never post anything.
"""
from __future__ import annotations

import os

import requests

from .log import get as get_log, redact

log = get_log("notify")

API = "https://api.github.com"


def _repo() -> str | None:
    return os.environ.get("GITHUB_REPOSITORY")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def enabled() -> bool:
    return bool(_repo() and os.environ.get("GITHUB_TOKEN") and os.environ.get("YT_NOTIFY", "1") == "1")


def run_url() -> str | None:
    if os.environ.get("GITHUB_RUN_ID") and _repo():
        return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{_repo()}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return None


def open_issue(title: str, body: str, labels: list[str]) -> int | None:
    if not enabled():
        log.info("[notify disabled] issue: %s", title)
        return None
    payload = {"title": title, "body": redact(body)[:60000], "labels": labels}
    r = requests.post(f"{API}/repos/{_repo()}/issues", headers=_headers(), json=payload, timeout=30)
    if r.status_code == 422 and labels:  # label problem: never lose the notification over a label
        payload.pop("labels")
        r = requests.post(f"{API}/repos/{_repo()}/issues", headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        log.warning("issue create failed %s: %s", r.status_code, redact(r.text[:200]))
        return None
    return r.json()["number"]


def comment(issue: int, body: str) -> None:
    if not enabled() or not issue:
        log.info("[notify disabled] comment on #%s: %s", issue, body[:120])
        return
    r = requests.post(f"{API}/repos/{_repo()}/issues/{issue}/comments", headers=_headers(),
                      json={"body": redact(body)[:60000]}, timeout=30)
    if r.status_code >= 300:
        log.warning("comment failed %s: %s", r.status_code, redact(r.text[:200]))


def close(issue: int, reason: str = "completed") -> None:
    if not enabled() or not issue:
        return
    requests.patch(f"{API}/repos/{_repo()}/issues/{issue}", headers=_headers(),
                   json={"state": "closed", "state_reason": reason}, timeout=30)


def find_open(title_prefix: str) -> int | None:
    if not enabled():
        return None
    r = requests.get(f"{API}/repos/{_repo()}/issues", headers=_headers(),
                     params={"state": "open", "per_page": 100}, timeout=30)
    if r.status_code >= 300:
        return None
    for it in r.json():
        if it["title"].startswith(title_prefix) and "pull_request" not in it:
            return it["number"]
    return None
