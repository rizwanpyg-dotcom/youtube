"""Structured logging with secret redaction.

Every record passes through RedactingFilter before it reaches any handler, so a
secret can't end up in Actions logs, the events table or the dashboard export.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time

from . import config

# Token-shaped strings redacted even if they're not a registered secret.
_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),                 # Google standard API keys
    re.compile(r"AQ\.[0-9A-Za-z\-_\.]{20,}"),               # Google auth keys (AI Studio, from May 2026)
    re.compile(r"ya29\.[0-9A-Za-z\-_\.]+"),                 # Google OAuth access tokens
    re.compile(r"1//[0-9A-Za-z\-_]{20,}"),                  # Google refresh tokens
    re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),              # GitHub tokens
    re.compile(r"github_pat_[0-9A-Za-z_]{30,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)([?&](key|access_token|token)=)[^&\s\"']+"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for value in config.Config.secret_values():
        text = text.replace(value, "[REDACTED]")
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "component": record.name.removeprefix("ytengine."),
            "msg": record.getMessage(),
        }
        for key in ("video_id", "stage", "run_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps(payload, ensure_ascii=False)


class DbEventHandler(logging.Handler):
    """Mirrors WARNING+ records into the events table (best effort)."""

    def __init__(self, conn_factory):
        super().__init__(level=logging.WARNING)
        self._conn_factory = conn_factory

    def emit(self, record: logging.LogRecord) -> None:
        try:
            conn = self._conn_factory()
            conn.execute(
                "INSERT INTO events(level, component, video_id, run_id, message) VALUES (?,?,?,?,?)",
                (record.levelname, record.name.removeprefix("ytengine."),
                 getattr(record, "video_id", None), os.environ.get("GITHUB_RUN_ID"),
                 record.getMessage()[:2000]),
            )
            conn.commit()
        except Exception:  # logging must never crash the pipeline
            pass


_configured = False


def setup(level: str | None = None, json_output: bool | None = None) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("ytengine")
    root.setLevel(level or os.environ.get("YT_LOG_LEVEL", "INFO"))
    handler = logging.StreamHandler(sys.stdout)
    use_json = json_output if json_output is not None else bool(os.environ.get("GITHUB_ACTIONS"))
    handler.setFormatter(JsonFormatter() if use_json else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def attach_db(conn_factory) -> None:
    handler = DbEventHandler(conn_factory)
    handler.addFilter(RedactingFilter())
    logging.getLogger("ytengine").addHandler(handler)


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"ytengine.{name}")
