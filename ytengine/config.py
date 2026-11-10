"""Configuration: committed YAML settings plus secrets from environment variables.

Secrets are read only from the environment (GitHub Actions Secrets in the cloud,
a local shell or an untracked .env file on the PC). They are never written to the
DB, to logs or to the dashboard.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

SECRET_ENV_VARS = (
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
    "YT_CLIENT_ID",
    "YT_CLIENT_SECRET",
    "YT_REFRESH_TOKEN",
)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (KEY=VALUE lines). Existing env wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Paths:
    root: Path = ROOT
    db: Path = field(default_factory=lambda: Path(os.environ.get("YT_DB_PATH", ROOT / "youtube_automation.db")))
    schema: Path = ROOT / "db" / "schema.sql"
    work: Path = field(default_factory=lambda: Path(os.environ.get("YT_WORK_DIR", ROOT / "work")))
    cache: Path = field(default_factory=lambda: Path(os.environ.get("YT_CACHE_DIR", ROOT / "work" / "cache")))
    output: Path = field(default_factory=lambda: Path(os.environ.get("YT_OUTPUT_DIR", ROOT / "output")))
    models: Path = field(default_factory=lambda: Path(os.environ.get("YT_MODELS_DIR", ROOT / "models")))
    assets: Path = ROOT / "assets"
    fixtures: Path = ROOT / "tests" / "fixtures"

    def ensure(self) -> "Paths":
        for p in (self.work, self.cache, self.output):
            p.mkdir(parents=True, exist_ok=True)
        return self


class Config:
    def __init__(self, data: dict[str, Any], paths: Paths):
        self.data = data
        self.paths = paths

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @staticmethod
    def secret(name: str) -> str | None:
        if name not in SECRET_ENV_VARS:
            raise KeyError(f"{name} is not a registered secret")
        value = os.environ.get(name)
        return value or None

    @staticmethod
    def secret_values() -> list[str]:
        """Current secret values, used by the log redactor."""
        return [v for v in (os.environ.get(n) for n in SECRET_ENV_VARS) if v and len(v) >= 6]


_CONFIG: Config | None = None


def load(path: Path | None = None) -> Config:
    global _CONFIG
    _load_dotenv(ROOT / ".env")
    cfg_path = path or Path(os.environ.get("YT_CONFIG", ROOT / "config" / "channel.yaml"))
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    _CONFIG = Config(data, Paths().ensure())
    return _CONFIG


def get() -> Config:
    return _CONFIG or load()
