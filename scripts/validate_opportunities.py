#!/usr/bin/env python3
"""Validate generated Merlion Radar data before publishing."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "_data" / "opportunities.json"
HISTORY = ROOT / "_data" / "opportunities_history.json"
CONFIG = ROOT / "config" / "opportunity_radar.json"
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){8,}(?!\d)")
SECRET_VALUE_RE = re.compile(
    r"(?ix)("
    # OpenAI / Anthropic style secret keys.
    r"sk-(?:proj-)?[a-z0-9_-]{20,}"
    r"|sk-ant-[a-z0-9_-]{20,}"
    # Google AI Studio / Gemini API keys.
    r"|AIza[0-9a-z_-]{35}"
    # Hugging Face / Replicate tokens.
    r"|hf_[a-z0-9]{30,}"
    r"|r8_[a-z0-9]{30,}"
    # GitHub tokens and generic bearer/JWT values.
    r"|gh[pousr]_[a-z0-9_]{20,}"
    r"|bearer\s+[a-z0-9._~+/=-]{24,}"
    r"|eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}"
    r")"
)
ALLOWED_BADGES = {"New this refresh", "Still open", "Repeated high match", "Watchlist"}


def load(path: Path) -> object:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def fail(message: str) -> None:
    raise SystemExit(f"validate_opportunities: {message}")


def walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)


def privacy_scan(data: object) -> None:
    text = "\n".join(walk_strings(data))
    redacted_urls = re.sub(r"https?://\S+", "[URL]", text)
    redacted_dates = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?\b", "[DATE]", redacted_urls)
    if EMAIL_RE.search(redacted_dates):
        fail("generated data contains an email-looking string")
    if ("mail" + "to:") in text.lower():
        fail("generated data contains a mail-to link")
    # Avoid flagging dates/URLs; still catches phone-like public text.
    if PHONE_RE.search(redacted_dates):
        fail("generated data contains a likely phone number")
    if SECRET_VALUE_RE.search(text):
        fail("generated data contains a token/API-key-looking value")


def config_secret_scan(config: object) -> None:
    """Reject committed secret values; configs must name env vars instead."""
    if not isinstance(config, dict):
        fail("config must be an object")
    text = json.dumps(config, ensure_ascii=False)
    if SECRET_VALUE_RE.search(text):
        fail("config contains a token/API-key-looking value; use an *_env setting and GitHub Actions secrets")

    def walk(value: object, path: str = "config") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                child = f"{path}.{key}"
                if lowered in {"api_key", "apikey", "secret", "token", "password"} and item:
                    fail(f"{child} must not store a secret value; store an environment variable name such as api_key_env instead")
                walk(item, child)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, f"{path}[{idx}]")

    walk(config)


def main() -> int:
    if not DATA.exists():
        fail(f"missing {DATA}")
    data = load(DATA)
    config = load(CONFIG)
    config_secret_scan(config)
    if not isinstance(data, dict):
        fail("data must be an object")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        fail("jobs must be a list")
    max_items = int(config.get("max_items", 12)) if isinstance(config, dict) else 12
    if len(jobs) > max_items:
        fail(f"published job count {len(jobs)} exceeds max_items {max_items}")
    if data.get("stats", {}).get("published_count") != len(jobs):
        fail("stats.published_count does not match jobs length")
    if not isinstance(data.get("source_health"), list) or not data["source_health"]:
        fail("source_health must be present")

    required = [
        "title", "company", "location", "url", "source", "summary", "score",
        "why_match", "next_action", "status_badge", "skillsets_to_build",
        "certifications_to_consider", "learning_gaps",
    ]
    for idx, job in enumerate(jobs, start=1):
        if not isinstance(job, dict):
            fail(f"job {idx} is not an object")
        missing = [key for key in required if key not in job or job[key] in (None, "", [])]
        if missing:
            fail(f"job {idx} missing required fields: {', '.join(missing)}")
        if not isinstance(job.get("score"), int) or not (0 <= job["score"] <= 100):
            fail(f"job {idx} score out of range")
        if job.get("status_badge") not in ALLOWED_BADGES:
            fail(f"job {idx} has invalid status_badge {job.get('status_badge')!r}")
        for list_key in ("skillsets_to_build", "certifications_to_consider", "learning_gaps"):
            if not isinstance(job.get(list_key), list) or not all(isinstance(x, str) and x.strip() for x in job[list_key]):
                fail(f"job {idx} {list_key} must be a non-empty list of strings")

    if HISTORY.exists():
        history = load(HISTORY)
        history_rows = history.get("jobs", {}) if isinstance(history, dict) else {}
        if not isinstance(history_rows, dict):
            fail("history jobs must be an object")
        for key, row in history_rows.items():
            if "http://" in key or "https://" in key:
                fail("history key stores a raw URL")
            if isinstance(row, dict):
                for forbidden in ("url", "job_url", "apply_url"):
                    if forbidden in row:
                        fail(f"history row stores raw URL-like field {forbidden}")
    privacy_scan(data)
    print(f"validated {len(jobs)} opportunities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
