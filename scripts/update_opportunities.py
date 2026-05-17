#!/usr/bin/env python3
"""Generate a configurable static opportunity radar from public job feeds.

Python standard library only. Intended for GitHub Actions + GitHub Pages.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "opportunity_radar.json"
OUT = ROOT / "_data" / "opportunities.json"
HISTORY_OUT = ROOT / "_data" / "opportunities_history.json"
USER_AGENT = "MerlionRadar/1.0 (+https://github.com/Hinotoi-agent/merlion-radar)"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RoleProfile:
    label: str
    terms: tuple[str, ...]
    skillsets: tuple[str, ...]
    certifications: tuple[str, ...]
    learning_gaps: tuple[str, ...]


@dataclass(frozen=True)
class CandidateProfile:
    enabled: bool
    skills: tuple[str, ...]
    strengths: tuple[str, ...]
    target_terms: tuple[str, ...]
    learning_priorities: tuple[str, ...]


@dataclass(frozen=True)
class Opportunity:
    title: str
    company: str
    location: str
    url: str
    source: str
    published_at: str
    summary: str
    tags: tuple[str, ...]
    score: int
    matched_profiles: tuple[str, ...]
    why_match: str
    next_action: str
    skillsets_to_build: tuple[str, ...]
    certifications_to_consider: tuple[str, ...]
    learning_gaps: tuple[str, ...]
    status_badge: str = ""
    first_seen: str = ""
    last_seen: str = ""
    status: str = "New"


def clean_text(value: object, limit: int | None = None) -> str:
    text = str(value or "")
    for _ in range(3):
        text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def load_config() -> dict[str, Any]:
    with CONFIG.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("config must be a JSON object")
    return data


def fetch_json(url: str, extra_headers: dict[str, str] | None = None) -> object:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def post_json(url: str, payload: dict[str, object], headers: dict[str, str], timeout: int) -> object:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        **headers,
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def extract_json_object(text: str) -> dict[str, object]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def compact_list(value: object, limit: int, item_limit: int = 180) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    rows: list[str] = []
    for item in value:
        cleaned = clean_text(item, item_limit)
        if cleaned:
            rows.append(cleaned)
    return tuple(dict.fromkeys(rows))[:limit]


def env_text(env_name: object) -> str:
    name = clean_text(env_name)
    return os.environ.get(name, "") if name else ""


def configured_public_profile_terms(config: dict[str, Any], profiles: list[RoleProfile]) -> tuple[str, ...]:
    """Return public-safe terms that may be derived from a private CV.

    Private CV/resume text can contain names, employers, emails, phone numbers,
    and other details that must never be copied into the generated static site.
    The parser therefore only emits terms already present in public config.
    """
    terms: list[str] = []
    terms.extend(clean_text(x) for x in config.get("title_terms", []) if clean_text(x))
    for profile in profiles:
        terms.append(profile.label)
        terms.extend(profile.terms)
    private_cfg = config.get("private_profile", {}) if isinstance(config.get("private_profile"), dict) else {}
    terms.extend(clean_text(x) for x in private_cfg.get("public_skill_terms", []) if clean_text(x))
    terms.extend(clean_text(x) for x in private_cfg.get("public_target_terms", []) if clean_text(x))
    return tuple(dict.fromkeys(t for t in terms if 2 <= len(t) <= 80))


def load_private_profile(config: dict[str, Any], profiles: list[RoleProfile]) -> CandidateProfile:
    """Parse optional private CV/profile material without publishing raw text."""
    private_cfg = config.get("private_profile", {}) if isinstance(config.get("private_profile"), dict) else {}
    if not private_cfg.get("enabled"):
        return CandidateProfile(False, (), (), (), ())

    raw_parts: list[str] = []
    json_payload: dict[str, object] = {}
    json_text = env_text(private_cfg.get("json_env"))
    if json_text:
        try:
            parsed = json.loads(json_text)
            if isinstance(parsed, dict):
                json_payload = parsed
                raw_parts.append(json.dumps(parsed, ensure_ascii=False))
        except json.JSONDecodeError:
            print("warn: private_profile.json_env is set but is not valid JSON; using text extraction only", file=sys.stderr)

    cv_text = env_text(private_cfg.get("text_env"))
    if cv_text:
        raw_parts.append(cv_text)

    path_env = clean_text(private_cfg.get("path_env"))
    path_value = os.environ.get(path_env, "") if path_env else ""
    if path_value:
        path = Path(path_value)
        if path.exists() and path.is_file():
            raw_parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        else:
            print(f"warn: private profile path from {path_env} does not exist; ignoring", file=sys.stderr)

    allowed_terms = configured_public_profile_terms(config, profiles)
    raw_text = "\n".join(raw_parts)
    matched_terms = tuple(dict.fromkeys(term for term in allowed_terms if term_matches(raw_text, term)))

    def public_list(name: str, limit: int) -> tuple[str, ...]:
        values: list[str] = []
        raw = json_payload.get(name)
        if isinstance(raw, list):
            for item in raw:
                item_s = clean_text(item, 80)
                if item_s and any(term_matches(item_s, allowed) or term_matches(allowed, item_s) for allowed in allowed_terms):
                    values.append(item_s)
        return tuple(dict.fromkeys(values))[:limit]

    max_terms = max(1, int(private_cfg.get("max_public_terms", 14) or 14))
    skills = tuple(dict.fromkeys((*public_list("skills", max_terms), *matched_terms)))[:max_terms]
    strengths = public_list("strengths", 4) or tuple(f"Private profile shows evidence of {term}." for term in skills[:3])
    target_terms = public_list("target_terms", 8) or tuple(t for t in matched_terms if any(term_matches(t, title) for title in config.get("title_terms", [])))[:8]
    priorities = public_list("learning_priorities", 4)
    return CandidateProfile(bool(raw_parts or json_payload), skills, strengths[:4], target_terms, priorities)


def llm_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    return raw if isinstance(raw, dict) else {}


def llm_enrichment_enabled(config: dict[str, Any]) -> bool:
    settings = llm_settings(config)
    if not settings.get("enabled"):
        return False
    env_name = clean_text(settings.get("api_key_env"))
    if not env_name:
        print("warn: llm.enabled is true but llm.api_key_env is empty; using deterministic guidance", file=sys.stderr)
        return False
    if not os.environ.get(env_name):
        print(f"warn: {env_name} is not set; using deterministic guidance", file=sys.stderr)
        return False
    return True


def call_llm(prompt: str, config: dict[str, Any]) -> dict[str, object]:
    settings = llm_settings(config)
    provider = clean_text(settings.get("provider")).lower() or "openai_compatible"
    model = clean_text(settings.get("model"))
    env_name = clean_text(settings.get("api_key_env"))
    credential = os.environ.get(env_name, "")
    timeout = int(settings.get("timeout_seconds", 30) or 30)
    if not credential or not model:
        return {}

    if provider in {"openai", "openai_compatible"}:
        base_url = clean_text(settings.get("base_url")) or "https://api.openai.com/v1"
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "Return only compact JSON. Never include emails, phone numbers, secrets, URLs, markdown, or code fences."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        data = post_json(base_url.rstrip("/") + "/chat/completions", payload, {"Authorization": f"Bearer {credential}"}, timeout)
        content = get_path(data, "choices.0.message.content", "")
        return extract_json_object(str(content))

    if provider == "anthropic":
        base_url = clean_text(settings.get("base_url")) or "https://api.anthropic.com/v1"
        payload = {
            "model": model,
            "max_tokens": int(settings.get("max_tokens", 700) or 700),
            "temperature": 0.2,
            "system": "Return only compact JSON. Never include emails, phone numbers, secrets, URLs, markdown, or code fences.",
            "messages": [{"role": "user", "content": prompt}],
        }
        data = post_json(base_url.rstrip("/") + "/messages", payload, {"x-api-key": credential, "anthropic-version": "2023-06-01"}, timeout)
        content = get_path(data, "content.0.text", "")
        return extract_json_object(str(content))

    if provider == "gemini":
        base_url = clean_text(settings.get("base_url")) or "https://generativelanguage.googleapis.com/v1beta"
        endpoint = f"{base_url.rstrip('/')}/models/{urllib.parse.quote(model, safe='')}:generateContent?key={urllib.parse.quote(credential)}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Return only compact JSON. Never include emails, phone numbers, secrets, URLs, markdown, or code fences.\n\n" + prompt}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        data = post_json(endpoint, payload, {}, timeout)
        content = get_path(data, "candidates.0.content.parts.0.text", "")
        return extract_json_object(str(content))

    print(f"warn: unsupported llm.provider {provider!r}; using deterministic guidance", file=sys.stderr)
    return {}


def get_path(row: object, dotted: str, default: object = "") -> object:
    cur = row
    for bit in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(bit, default)
        elif isinstance(cur, list) and bit.isdigit():
            idx = int(bit)
            cur = cur[idx] if idx < len(cur) else default
        else:
            return default
    return cur


def items_at_path(payload: object, dotted: str) -> list[object]:
    if not dotted:
        return payload if isinstance(payload, list) else []
    value = get_path(payload, dotted, [])
    return value if isinstance(value, list) else []


def term_matches(text: str, term: object) -> bool:
    lowered = clean_text(term).lower()
    if not lowered:
        return False
    haystack = text.lower()
    if len(lowered) <= 3 or re.fullmatch(r"[a-z0-9]+", lowered):
        return re.search(rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])", haystack) is not None
    return lowered in haystack


def term_hits(text: str, terms: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for term in terms:
        if term_matches(text, term):
            hits.append(term)
    return hits


def numeric_config(config: dict[str, Any], dotted: str, default: int = 0) -> int:
    value = get_path(config, dotted, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def source_boost(source: str, company: str, config: dict[str, Any]) -> int:
    boosts = config.get("source_boosts", {}) if isinstance(config.get("source_boosts"), dict) else {}
    combined = f"{source} {company}".lower()
    total = 0
    for label, value in boosts.items():
        if term_matches(combined, label):
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
    return total


def bounded(value: float, lower: int = 0, upper: int = 100) -> int:
    return max(lower, min(upper, round(value)))


def freshness_score(published_at: str) -> int:
    if not published_at:
        return 45
    try:
        published = datetime.fromisoformat(published_at[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return 45
    age_days = max(0, (datetime.now(timezone.utc) - published).days)
    if age_days <= 7:
        return 100
    if age_days <= 21:
        return 82
    if age_days <= 45:
        return 58
    if age_days <= 90:
        return 32
    return 12


def role_profiles(config: dict[str, Any]) -> list[RoleProfile]:
    profiles = []
    for row in config.get("role_profiles", []):
        if not isinstance(row, dict):
            continue
        profiles.append(RoleProfile(
            label=clean_text(row.get("label")) or "General",
            terms=tuple(clean_text(x) for x in row.get("terms", []) if clean_text(x)),
            skillsets=tuple(clean_text(x) for x in row.get("skillsets", []) if clean_text(x)),
            certifications=tuple(clean_text(x) for x in row.get("certifications", []) if clean_text(x)),
            learning_gaps=tuple(clean_text(x) for x in row.get("learning_gaps", []) if clean_text(x)),
        ))
    return profiles


def location_allowed(location: str, summary: str, config: dict[str, Any]) -> bool:
    location_cfg = config.get("location", {}) if isinstance(config.get("location"), dict) else {}
    include = location_cfg.get("include_terms", [])
    exclude = location_cfg.get("exclude_terms", [])
    combined = f"{location} {summary}"
    if any(term_matches(combined, term) for term in exclude):
        return False
    return not include or any(term_matches(combined, term) for term in include)


def score_opportunity(title: str, company: str, location: str, summary: str, published_at: str, source: str, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> tuple[int, list[str], list[RoleProfile], str, str, list[str], list[str], list[str]]:
    text = " ".join([title, company, location, summary])
    title_hits = term_hits(title, config.get("title_terms", []))
    exclude_hits = term_hits(text, config.get("exclude_terms", []))
    candidate_hits = term_hits(text, candidate.skills + candidate.target_terms) if candidate.enabled else []
    matched: list[RoleProfile] = []
    profile_hits_total = 0
    tags: list[str] = []
    for profile in profiles:
        hits = term_hits(text, profile.terms)
        if hits:
            matched.append(profile)
            profile_hits_total += min(5, len(set(hits)))
            tags.extend([profile.label, *hits[:3]])
    if candidate_hits:
        tags.extend(candidate_hits[:4])
    if exclude_hits:
        penalty = 28 + 6 * len(exclude_hits)
    else:
        penalty = 0
    location_fit = 100 if location_allowed(location, summary, config) else 0
    title_score = min(100, 25 * len(set(title_hits)))
    profile_score = min(100, 18 * profile_hits_total)
    private_fit = min(100, 22 * len(set(candidate_hits))) if candidate.enabled else 0
    freshness = freshness_score(published_at)
    boost = source_boost(source, company, config)
    score = bounded((0.39 * profile_score) + (0.20 * title_score) + (0.16 * location_fit) + (0.12 * freshness) + (0.13 * private_fit) + boost - penalty)
    matched_labels = ", ".join(p.label for p in matched[:3]) or "general configured focus"
    boost_note = f", source boost +{boost}" if boost else ""
    private_note = f", private-profile fit {private_fit}/100" if candidate.enabled else ""
    why = f"Matches {matched_labels}; title relevance {title_score}/100, profile relevance {profile_score}/100, location fit {location_fit}/100, freshness {freshness}/100{private_note}{boost_note}."
    if score >= int(config.get("alert_score", 82)):
        prefix = "Prioritize"
    elif score >= 65:
        prefix = "Shortlist"
    else:
        prefix = "Watch"
    if candidate_hits:
        safe_hits = ", ".join(dict.fromkeys(candidate_hits[:3]))
        next_action = f"{prefix}: verify the posting is still open, then tailor your application around {matched_labels} and public-safe profile signals: {safe_hits}."
    else:
        next_action = f"{prefix}: verify the posting is still open, then tailor your application around {matched_labels}."
    skills: list[str] = []
    certs: list[str] = []
    gaps: list[str] = []
    for profile in matched[:3] or profiles[:1]:
        skills.extend(profile.skillsets)
        certs.extend(profile.certifications)
        gaps.extend(profile.learning_gaps)
    if candidate.enabled and candidate_hits:
        skills.insert(0, "Emphasize concrete evidence for the matched private-profile signals without exposing private CV details: " + ", ".join(dict.fromkeys(candidate_hits[:4])) + ".")
    if candidate.learning_priorities:
        gaps = list(candidate.learning_priorities) + gaps
    if not skills:
        skills = ["Build role-specific portfolio evidence, communication clarity, and measurable outcomes aligned to the configured focus."]
    if not certs:
        certs = ["Choose one hands-on course or credential that directly supports the role profile, then publish a small proof project."]
    if not gaps:
        gaps = ["Prepare concise examples that connect your experience to the role's day-to-day responsibilities."]
    unique_tags = list(dict.fromkeys(clean_text(t).title() for t in tags if clean_text(t)))[:8]
    return score, unique_tags, matched, why, next_action, list(dict.fromkeys(skills))[:4], list(dict.fromkeys(certs))[:3], list(dict.fromkeys(gaps))[:4]


def build_opportunity(title: object, company: object, location: object, url: object, source: str, published_at: object, summary: object, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> Opportunity | None:
    title_s = clean_text(title)
    company_s = clean_text(company) or "Unknown employer"
    location_s = clean_text(location) or "Remote"
    url_s = str(url or "").strip()
    summary_s = clean_text(summary, 360)
    if not title_s or not url_s:
        return None
    if not location_allowed(location_s, f"{title_s} {summary_s}", config):
        return None
    score, tags, matched, why, action, skills, certs, gaps = score_opportunity(title_s, company_s, location_s, summary_s, clean_text(published_at)[:10], source, config, profiles, candidate)
    if score <= 0:
        return None
    return Opportunity(
        title=title_s,
        company=company_s,
        location=location_s,
        url=url_s,
        source=source,
        published_at=clean_text(published_at)[:10],
        summary=summary_s or "Matched by title, company, source, and location metadata.",
        tags=tuple(tags),
        score=score,
        matched_profiles=tuple(p.label for p in matched),
        why_match=why,
        next_action=action,
        skillsets_to_build=tuple(skills),
        certifications_to_consider=tuple(certs),
        learning_gaps=tuple(gaps),
    )


def source_result(name: str, fn) -> tuple[list[Opportunity], dict[str, object]]:
    started = time.time()
    try:
        rows = fn()
        return rows, {"source": name, "status": "ok", "count": len(rows), "seconds": round(time.time() - started, 2)}
    except Exception as exc:  # noqa: BLE001
        print(f"warn: {name}: {exc}", file=sys.stderr)
        return [], {"source": name, "status": "error", "count": 0, "error": clean_text(exc, 160), "seconds": round(time.time() - started, 2)}


def from_greenhouse(company: str, board: str, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> list[Opportunity]:
    payload = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    jobs: list[Opportunity] = []
    for item in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        location = clean_text((item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else item.get("location"))
        job = build_opportunity(item.get("title"), company, location, item.get("absolute_url") or f"https://boards.greenhouse.io/{board}", "Greenhouse", item.get("updated_at") or item.get("first_published"), item.get("content"), config, profiles, candidate)
        if job:
            jobs.append(job)
    return jobs


def from_lever(company: str, slug: str, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> list[Opportunity]:
    payload = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs: list[Opportunity] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
        location = categories.get("location") or item.get("workplaceType") or "Remote"
        created_at = item.get("createdAt")
        published = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).date().isoformat() if isinstance(created_at, int) and created_at > 0 else ""
        job = build_opportunity(item.get("text"), company, location, item.get("hostedUrl") or item.get("applyUrl") or f"https://jobs.lever.co/{slug}", "Lever", published, item.get("descriptionPlain") or item.get("description"), config, profiles, candidate)
        if job:
            jobs.append(job)
    return jobs


def from_ashby(company: str, board: str, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> list[Opportunity]:
    payload = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    jobs: list[Opportunity] = []
    for item in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        job = build_opportunity(item.get("title"), company, item.get("location") or item.get("locationName") or "Remote", item.get("jobUrl") or item.get("externalLink") or f"https://jobs.ashbyhq.com/{board}", "Ashby", item.get("publishedDate") or item.get("createdAt"), item.get("descriptionPlain") or item.get("descriptionHtml"), config, profiles, candidate)
        if job:
            jobs.append(job)
    return jobs


def from_remotive(query: str, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> list[Opportunity]:
    payload = fetch_json("https://remotive.com/api/remote-jobs?search=" + urllib.parse.quote(query))
    jobs: list[Opportunity] = []
    for item in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        job = build_opportunity(item.get("title"), item.get("company_name"), item.get("candidate_required_location"), item.get("url") or item.get("job_url"), "Remotive", item.get("publication_date"), item.get("description"), config, profiles, candidate)
        if job:
            jobs.append(job)
    return jobs


def from_remoteok(config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> list[Opportunity]:
    payload = fetch_json("https://remoteok.com/api")
    jobs: list[Opportunity] = []
    rows = payload[1:] if isinstance(payload, list) and payload else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        summary = " ".join(clean_text(t) for t in item.get("tags", []) if t) + " " + clean_text(item.get("description"), 300)
        job = build_opportunity(item.get("position"), item.get("company"), item.get("location") or "Remote", item.get("url") or "https://remoteok.com/", "RemoteOK", item.get("date"), summary, config, profiles, candidate)
        if job:
            jobs.append(job)
    return jobs


def join_named_rows(rows: object, field: str, limit: int = 5) -> str:
    """Return a compact comma-separated label list from API rows."""
    if not isinstance(rows, list):
        return ""
    labels: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            label = clean_text(row.get(field))
            if label:
                labels.append(label)
    return ", ".join(list(dict.fromkeys(labels))[:limit])


def from_mycareersfuture(query: str, config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile, limit: int = 20) -> list[Opportunity]:
    """Fetch Singapore roles from MyCareersFuture's public jobs endpoint."""
    params = urllib.parse.urlencode({"search": query, "limit": max(1, min(limit, 100)), "page": 0})
    payload = fetch_json(f"https://api.mycareersfuture.gov.sg/v2/jobs?{params}")
    jobs: list[Opportunity] = []
    for item in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        posted_company = item.get("postedCompany") if isinstance(item.get("postedCompany"), dict) else {}
        hiring_company = item.get("hiringCompany") if isinstance(item.get("hiringCompany"), dict) else {}
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        salary = item.get("salary") if isinstance(item.get("salary"), dict) else {}
        salary_type = salary.get("type") if isinstance(salary.get("type"), dict) else {}
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
        districts = address.get("districts") if isinstance(address.get("districts"), list) else []
        district = ""
        if districts and isinstance(districts[0], dict):
            district = clean_text(districts[0].get("location") or districts[0].get("region"))
        company = hiring_company.get("name") or posted_company.get("name") or "Unknown employer"
        public_url = metadata.get("jobDetailsUrl") or f"https://www.mycareersfuture.gov.sg/job/{item.get('uuid', '')}"
        skills = join_named_rows(item.get("skills"), "skill", 8)
        categories = join_named_rows(item.get("categories"), "category", 4)
        employment = join_named_rows(item.get("employmentTypes"), "employmentType", 4)
        salary_summary = ""
        if isinstance(salary.get("minimum"), (int, float)) and isinstance(salary.get("maximum"), (int, float)):
            salary_summary = f"Salary range SGD {int(salary['minimum'])}-{int(salary['maximum'])} {clean_text(salary_type.get('salaryType')).lower()}"
        summary_parts = [
            clean_text(item.get("description"), 320),
            f"Categories: {categories}" if categories else "",
            f"Employment: {employment}" if employment else "",
            f"Skills: {skills}" if skills else "",
            salary_summary,
        ]
        job = build_opportunity(
            item.get("title"),
            company,
            district or "Singapore",
            public_url,
            "MyCareersFuture",
            metadata.get("newPostingDate") or metadata.get("originalPostingDate") or metadata.get("createdAt"),
            " ".join(part for part in summary_parts if part),
            config,
            profiles,
            candidate,
        )
        if job:
            jobs.append(job)
    return jobs


def load_custom_payload(feed: dict[str, Any]) -> object:
    if isinstance(feed.get("items"), list):
        return feed.get("items")
    if feed.get("path"):
        path = Path(str(feed.get("path")))
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists() and feed.get("optional", False):
            return []
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    if feed.get("url"):
        return fetch_json(str(feed.get("url")))
    return []


def field_value(item: dict[str, Any], fields: dict[str, Any], name: str, default_path: str = "") -> object:
    raw = fields.get(name, default_path or name)
    if isinstance(raw, list):
        return " ".join(clean_text(get_path(item, str(path))) for path in raw if clean_text(get_path(item, str(path))))
    return get_path(item, str(raw))


def from_custom(feed: dict[str, Any], config: dict[str, Any], profiles: list[RoleProfile], candidate: CandidateProfile) -> list[Opportunity]:
    payload = load_custom_payload(feed)
    fields = feed.get("fields", {}) if isinstance(feed.get("fields"), dict) else {}
    defaults = feed.get("defaults", {}) if isinstance(feed.get("defaults"), dict) else {}
    source_name = clean_text(feed.get("name")) or "Custom JSON"
    jobs: list[Opportunity] = []
    if isinstance(payload, list):
        rows = payload
    else:
        rows = items_at_path(payload, str(feed.get("items_path", "")))
    for item in rows:
        if not isinstance(item, dict):
            continue
        summary = field_value(item, fields, "summary", "description")
        summary_extra = field_value(item, fields, "summary_fields", "")
        if summary_extra:
            summary = f"{clean_text(summary)} {clean_text(summary_extra)}"
        job = build_opportunity(
            field_value(item, fields, "title", "title"),
            field_value(item, fields, "company", "company") or defaults.get("company"),
            field_value(item, fields, "location", "location") or defaults.get("location"),
            field_value(item, fields, "url", "url"),
            source_name,
            field_value(item, fields, "published_at", "published_at"),
            summary,
            config,
            profiles,
            candidate,
        )
        if job:
            jobs.append(job)
    return jobs


def stable_key(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:18]


def job_key(job: Opportunity) -> str:
    company = SPACE_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", job.company.lower())).strip()
    title = SPACE_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", job.title.lower())).strip()
    return stable_key(f"{company}|{title}")


def load_history() -> dict[str, dict[str, object]]:
    if not HISTORY_OUT.exists():
        return {}
    try:
        data = json.loads(HISTORY_OUT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    rows = data.get("jobs", {}) if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def status_badge_for(status: str, score: int, first_seen: str, today: str, alert_score: int) -> str:
    if status == "New":
        return "New this refresh"
    if score >= alert_score:
        return "Repeated high match"
    if first_seen and first_seen != today:
        return "Still open"
    return "Watchlist"


def apply_history(jobs: list[Opportunity], history: dict[str, dict[str, object]], today: str, alert_score: int) -> tuple[list[Opportunity], dict[str, dict[str, object]]]:
    enriched: list[Opportunity] = []
    for job in jobs:
        key = job_key(job)
        old = history.get(key, {}) if isinstance(history.get(key, {}), dict) else {}
        first_seen = clean_text(old.get("first_seen")) or today
        status = "Still open" if old and first_seen != today else "New"
        badge = status_badge_for(status, job.score, first_seen, today, alert_score)
        enriched.append(replace(job, first_seen=first_seen, last_seen=today, status=status, status_badge=badge))
        history[key] = {"title": job.title, "company": job.company, "source": job.source, "first_seen": first_seen, "last_seen": today, "last_score": job.score}
    return enriched, history


def dedupe(jobs: Iterable[Opportunity]) -> list[Opportunity]:
    best: dict[str, Opportunity] = {}
    for job in jobs:
        key = job_key(job)
        existing = best.get(key)
        if existing is None or job.score > existing.score:
            best[key] = job
    return sorted(best.values(), key=lambda j: (-j.score, j.company.lower(), j.title.lower()))




def matches_selector(job: Opportunity, selector: str) -> bool:
    return term_matches(f"{job.source} {job.company} {job.location}", selector)


def select_published_jobs(ranked: list[Opportunity], max_items: int, config: dict[str, Any]) -> list[Opportunity]:
    selected: list[Opportunity] = []
    selected_keys: set[str] = set()
    minimums = config.get("source_minimums", {}) if isinstance(config.get("source_minimums"), dict) else {}
    for selector, raw_count in minimums.items():
        try:
            count = max(0, int(raw_count))
        except (TypeError, ValueError):
            continue
        for job in ranked:
            if len(selected) >= max_items or count <= 0:
                break
            key = job_key(job)
            if key not in selected_keys and matches_selector(job, str(selector)):
                selected.append(job)
                selected_keys.add(key)
                count -= 1
    for job in ranked:
        if len(selected) >= max_items:
            break
        key = job_key(job)
        if key not in selected_keys:
            selected.append(job)
            selected_keys.add(key)
    return selected

def to_dict(job: Opportunity) -> dict[str, object]:
    return {
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "source": job.source,
        "published_at": job.published_at,
        "summary": job.summary,
        "tags": list(job.tags),
        "score": job.score,
        "matched_profiles": list(job.matched_profiles),
        "why_match": job.why_match,
        "next_action": job.next_action,
        "skillsets_to_build": list(job.skillsets_to_build),
        "certifications_to_consider": list(job.certifications_to_consider),
        "learning_gaps": list(job.learning_gaps),
        "status_badge": job.status_badge,
        "first_seen": job.first_seen,
        "last_seen": job.last_seen,
        "status": job.status,
    }


def llm_prompt(job: Opportunity, config: dict[str, Any]) -> str:
    return json.dumps({
        "task": "Personalize career action guidance for a public static opportunity radar card.",
        "rules": [
            "Return JSON only with keys: next_action, skillsets_to_build, certifications_to_consider, learning_gaps.",
            "Do not include URLs, emails, phone numbers, secrets, private contact details, or markdown.",
            "Keep every list item concise, practical, and applicable to the role.",
            "Use the configured search focus and matched profiles; do not invent private facts about the employer.",
        ],
        "search_focus": clean_text(config.get("search_focus"), 500),
        "opportunity": {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "source": job.source,
            "summary": job.summary,
            "score": job.score,
            "matched_profiles": list(job.matched_profiles),
            "why_match": job.why_match,
            "existing_next_action": job.next_action,
            "existing_skillsets_to_build": list(job.skillsets_to_build),
            "existing_certifications_to_consider": list(job.certifications_to_consider),
            "existing_learning_gaps": list(job.learning_gaps),
        },
    }, ensure_ascii=False)


def enrich_jobs_with_llm(jobs: list[Opportunity], config: dict[str, Any]) -> list[Opportunity]:
    if not llm_enrichment_enabled(config):
        return jobs
    settings = llm_settings(config)
    max_items = max(0, int(settings.get("max_items_to_enrich", 6) or 6))
    enriched: list[Opportunity] = []
    for idx, job in enumerate(jobs):
        if idx >= max_items:
            enriched.append(job)
            continue
        try:
            data = call_llm(llm_prompt(job, config), config)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warn: llm enrichment failed for {job.company}/{job.title}: {clean_text(exc, 120)}", file=sys.stderr)
            enriched.append(job)
            continue
        next_action = clean_text(data.get("next_action"), 220) or job.next_action
        skills = compact_list(data.get("skillsets_to_build"), 4) or job.skillsets_to_build
        certs = compact_list(data.get("certifications_to_consider"), 3) or job.certifications_to_consider
        gaps = compact_list(data.get("learning_gaps"), 4) or job.learning_gaps
        enriched.append(replace(
            job,
            next_action=next_action,
            skillsets_to_build=tuple(skills),
            certifications_to_consider=tuple(certs),
            learning_gaps=tuple(gaps),
        ))
        time.sleep(float(settings.get("request_delay_seconds", 0.2) or 0.2))
    return enriched


def main() -> int:
    config = load_config()
    profiles = role_profiles(config)
    if not profiles:
        raise SystemExit("config must define at least one role profile")
    candidate = load_private_profile(config, profiles)
    all_jobs: list[Opportunity] = []
    health: list[dict[str, object]] = []
    sources = config.get("sources", {}) if isinstance(config.get("sources"), dict) else {}

    for row in sources.get("greenhouse", []):
        if isinstance(row, dict):
            jobs, h = source_result(f"Greenhouse/{row.get('company')}", lambda r=row: from_greenhouse(str(r.get("company")), str(r.get("board")), config, profiles, candidate))
            all_jobs.extend(jobs); health.append(h); time.sleep(0.1)
    for row in sources.get("lever", []):
        if isinstance(row, dict):
            jobs, h = source_result(f"Lever/{row.get('company')}", lambda r=row: from_lever(str(r.get("company")), str(r.get("slug")), config, profiles, candidate))
            all_jobs.extend(jobs); health.append(h); time.sleep(0.1)
    for row in sources.get("ashby", []):
        if isinstance(row, dict):
            jobs, h = source_result(f"Ashby/{row.get('company')}", lambda r=row: from_ashby(str(r.get("company")), str(r.get("board")), config, profiles, candidate))
            all_jobs.extend(jobs); health.append(h); time.sleep(0.1)
    for query in sources.get("remotive_queries", []):
        jobs, h = source_result(f"Remotive/{query}", lambda q=str(query): from_remotive(q, config, profiles, candidate))
        all_jobs.extend(jobs); health.append(h); time.sleep(0.1)
    if sources.get("remoteok"):
        jobs, h = source_result("RemoteOK", lambda: from_remoteok(config, profiles, candidate))
        all_jobs.extend(jobs); health.append(h)
    for query in sources.get("mycareersfuture_queries", []):
        jobs, h = source_result(f"MyCareersFuture/{query}", lambda q=str(query): from_mycareersfuture(q, config, profiles, candidate, int(sources.get("mycareersfuture_limit", 20) or 20)))
        all_jobs.extend(jobs); health.append(h); time.sleep(0.1)
    for row in sources.get("custom_json", []):
        if isinstance(row, dict) and (row.get("url") or row.get("path") or isinstance(row.get("items"), list)):
            jobs, h = source_result(f"Custom/{row.get('name', row.get('url'))}", lambda r=row: from_custom(r, config, profiles, candidate))
            all_jobs.extend(jobs); health.append(h); time.sleep(0.1)

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat().replace("+00:00", "Z")
    today = now_dt.date().isoformat()
    alert_score = int(config.get("alert_score", 82))
    ranked = dedupe(all_jobs)
    ranked, history = apply_history(ranked, load_history(), today, alert_score)
    ranked = sorted(ranked, key=lambda j: (0 if j.status == "New" and j.score >= alert_score else 1, -j.score, j.company.lower(), j.title.lower()))
    max_items = int(config.get("max_items", 12))
    published = enrich_jobs_with_llm(select_published_jobs(ranked, max_items, config), config)
    alerts = [job for job in ranked if job.score >= alert_score][:6]
    data = {
        "title": clean_text(config.get("title")) or "Merlion Radar",
        "updated_at": now,
        "search_focus": clean_text(config.get("search_focus"), 500),
        "sources": [h["source"] for h in health],
        "source_health": health,
        "stats": {"candidates_scored": len(ranked), "published_count": len(published), "alert_count": len(alerts)},
        "alerts": [to_dict(job) for job in alerts],
        "jobs": [to_dict(job) for job in published],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    HISTORY_OUT.write_text(json.dumps({"updated_at": now, "jobs": history}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(published)} published opportunities from {len(ranked)} scored candidates to {OUT}")
    print(f"alerts={len(alerts)} sources_ok={sum(1 for h in health if h.get('status') == 'ok')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
