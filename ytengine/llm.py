"""LLM router: free-tier providers in order, JSON-only outputs, validation, fallback.

Providers:
  gemini         Google AI Studio free tier (GEMINI_API_KEY; no billing account linked)
  github_models  GitHub Models free tier (GITHUB_TOKEN with models:read)
  fixture        replays recorded JSON from tests/fixtures/<set>/ (offline tests and demos)

Select with env YT_LLM: "auto" (default; config order), "gemini", "github_models",
or "fixture:<set_name>".
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any

import requests

from . import config, quota
from .log import get as get_log, redact

log = get_log("llm")


class LLMError(RuntimeError):
    pass


SYSTEM_PROMPT = (
    "You are a meticulous research assistant and documentary writer. "
    "Reply with a single JSON object only, matching the requested schema exactly. "
    "Never invent facts, numbers, quotations, names or dates. If unsure, omit."
)


# ------------------------------------------------------------------ validation
def validate(obj: Any, schema: dict, path: str = "$") -> None:
    """Small JSON-schema subset: type, required, properties, items, enum, minItems."""
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
             "boolean": bool}
    if t and not isinstance(obj, types[t]):
        raise LLMError(f"{path}: expected {t}, got {type(obj).__name__}")
    if "enum" in schema and obj not in schema["enum"]:
        raise LLMError(f"{path}: {obj!r} not in {schema['enum']}")
    if t == "object":
        for key in schema.get("required", []):
            if key not in obj:
                raise LLMError(f"{path}: missing '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in obj:
                validate(obj[key], sub, f"{path}.{key}")
    if t == "array":
        if len(obj) < schema.get("minItems", 0):
            raise LLMError(f"{path}: needs at least {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(obj):
                validate(item, schema["items"], f"{path}[{i}]")


def _parse_json(text: str) -> Any:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError("response was not JSON")
        return json.loads(m.group(0))


# ------------------------------------------------------------------ providers
class Provider:
    name = "base"

    def __init__(self, model: str | None = None, cap: int = 100):
        self.model, self.cap = model, cap

    def available(self) -> bool:
        return True

    def call(self, task: str, prompt: str, temperature: float) -> str:
        raise NotImplementedError


class Gemini(Provider):
    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def available(self) -> bool:
        return bool(config.Config.secret("GEMINI_API_KEY"))

    def call(self, task: str, prompt: str, temperature: float) -> str:
        r = requests.post(
            self.URL.format(model=self.model),
            headers={"x-goog-api-key": config.Config.secret("GEMINI_API_KEY") or "",
                     "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
            },
            timeout=180,
        )
        if r.status_code == 429:
            raise _RateLimited(r.headers.get("Retry-After"))
        if r.status_code >= 400:
            raise LLMError(f"gemini HTTP {r.status_code}: {redact(r.text[:300])}")
        data = r.json()
        try:
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError):
            raise LLMError(f"gemini: no content ({data.get('promptFeedback')})")


class GitHubModels(Provider):
    name = "github_models"
    URL = "https://models.github.ai/inference/chat/completions"

    def available(self) -> bool:
        return bool(config.Config.secret("GITHUB_TOKEN"))

    def call(self, task: str, prompt: str, temperature: float) -> str:
        r = requests.post(
            self.URL,
            headers={"Authorization": f"Bearer {config.Config.secret('GITHUB_TOKEN')}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "temperature": temperature,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": prompt}]},
            timeout=180,
        )
        if r.status_code == 429:
            raise _RateLimited(r.headers.get("Retry-After"))
        if r.status_code >= 400:
            raise LLMError(f"github_models HTTP {r.status_code}: {redact(r.text[:300])}")
        return r.json()["choices"][0]["message"]["content"]


class Fixture(Provider):
    """Replays tests/fixtures/<set>/<task>.json. Used for offline tests and demos only."""
    name = "fixture"

    def __init__(self, set_name: str):
        super().__init__(model=set_name, cap=10**6)
        self.dir = config.get().paths.fixtures / set_name

    def call(self, task: str, prompt: str, temperature: float) -> str:
        f = self.dir / f"{task}.json"
        if not f.exists():
            raise LLMError(f"fixture missing: {f}")
        return f.read_text(encoding="utf-8")


class _RateLimited(Exception):
    def __init__(self, retry_after: str | None):
        super().__init__("rate limited")
        self.retry_after = float(retry_after) if retry_after and retry_after.isdigit() else 30.0


# ------------------------------------------------------------------ router
def providers() -> list[Provider]:
    choice = os.environ.get("YT_LLM", "auto")
    if choice.startswith("fixture:"):
        return [Fixture(choice.split(":", 1)[1])]
    out: list[Provider] = []
    for p in config.get().get("llm.providers", []):
        if choice not in ("auto", p["name"]):
            continue
        cls = {"gemini": Gemini, "github_models": GitHubModels}[p["name"]]
        out.append(cls(p["model"], int(p.get("daily_request_cap", 100))))
    return out


def complete_json(conn: sqlite3.Connection | None, task: str, prompt: str, schema: dict,
                  temperature: float = 0.2, retries: int = 2) -> tuple[dict, str]:
    """Return (validated JSON object, provider label). Tries providers in order."""
    errors: list[str] = []
    for prov in providers():
        if not prov.available():
            errors.append(f"{prov.name}: not configured")
            continue
        label = f"{prov.name}:{prov.model}"
        for attempt in range(retries + 1):
            try:
                if conn is not None and prov.name != "fixture":
                    quota.consume(conn, f"{prov.name}:{prov.model}", 1, cap=prov.cap)
                text = prov.call(task, prompt, temperature)
                obj = _parse_json(text)
                validate(obj, schema)
                log.info("%s ok via %s", task, label)
                return obj, label
            except quota.QuotaExceeded as e:
                errors.append(str(e))
                break  # next provider
            except _RateLimited as e:
                wait = min(e.retry_after, 90)
                log.warning("%s rate-limited on %s; waiting %.0fs", task, label, wait)
                time.sleep(wait)
            except (LLMError, json.JSONDecodeError, requests.RequestException) as e:
                errors.append(f"{label}: {e}")
                log.warning("%s attempt %d failed on %s: %s", task, attempt + 1, label, e)
                if prov.name == "fixture":
                    break
                time.sleep(2 * (attempt + 1))
    raise LLMError(f"all providers failed for {task}: " + " | ".join(errors[-4:]))
