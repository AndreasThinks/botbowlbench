"""What a competitor is, for its profile page: a short description and, for LLMs, its OpenRouter listing.

LLM descriptions come from OpenRouter's public model list (no API key needed), cached in memory and on disk
for a day so a page view never waits on more than one fetch. A ``description`` in models.yaml wins; the
offline baselines fall back to the short blurbs below.
"""
import json
import os
import threading
import time
from typing import Dict, Optional

import httpx

from bench import config

CATALOG_URL = "https://openrouter.ai/api/v1/models"
CACHE_SECONDS = 24 * 3600
RETRY_SECONDS = 600           # after a failed fetch, wait this long before trying again
FETCH_TIMEOUT = 8

BASELINE_DESCRIPTIONS = {
    "random": "Picks a uniformly random legal action every time, using the same tools as the "
              "models. The floor: any model that can follow the rules should beat it.",
    "scripted": "A few lines of hand-written heuristics: make the safest blocks, move the ball "
                "carrier towards the end zone, pick up a loose ball, then end the turn. A \"does the model "
                "beat a simple script?\" reference point.",
    "botbowl": "A bot from the botbowl framework, plugged into the bench so it plays through "
               "the same game as the models.",
}

_lock = threading.Lock()
_catalog: Optional[Dict[str, dict]] = None
_fetched_at = 0.0
_failed_at = 0.0


def _cache_path() -> str:
    return os.path.join(config.data_dir(), "openrouter_models.json")


def _fetch() -> Dict[str, dict]:
    r = httpx.get(CATALOG_URL, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    out = {}
    for m in r.json().get("data") or []:
        if not m.get("id"):
            continue
        pricing = m.get("pricing") or {}
        entry = {"name": m.get("name"), "description": (m.get("description") or "").strip(),
                        "context_length": m.get("context_length"), "created": m.get("created"),
                        "knowledge_cutoff": m.get("knowledge_cutoff"),
                        "prompt_price": pricing.get("prompt"), "completion_price": pricing.get("completion")}
        if m.get("canonical_slug"):  # a dated id (openai/gpt-5-mini-2025-08-07) finds its listing too
            out.setdefault(m["canonical_slug"], entry)
        out[m["id"]] = entry
    return out


def catalog() -> Dict[str, dict]:
    """OpenRouter's models by id; {} when it can't be reached and nothing is cached."""
    global _catalog, _fetched_at, _failed_at
    with _lock:
        now = time.time()
        if _catalog is None:
            try:
                with open(_cache_path(), encoding="utf-8") as f:
                    cached = json.load(f)
                _catalog, _fetched_at = cached["models"], cached["fetched_at"]
            except (OSError, ValueError, KeyError):
                pass
        if now - _fetched_at > CACHE_SECONDS and now - _failed_at > RETRY_SECONDS:
            try:
                _catalog, _fetched_at = _fetch(), now
                with open(_cache_path(), "w", encoding="utf-8") as f:
                    json.dump({"fetched_at": now, "models": _catalog}, f)
            except (httpx.HTTPError, OSError, ValueError):
                _failed_at = now
        return _catalog or {}


def about(cfg: dict) -> dict:
    """{description, source, url, ...} for one model's config (a models.yaml entry)."""
    provider = cfg.get("provider", "openrouter")
    own = (cfg.get("description") or "").strip()
    if provider != "openrouter":
        return {"description": own or BASELINE_DESCRIPTIONS.get(provider, ""), "source": "bench"}
    model_id = cfg.get("model") or ""
    out = {"url": f"https://openrouter.ai/{model_id}", "source": "bench" if own else None, "description": own}
    entry = catalog().get(model_id)
    if entry:
        out.update({k: entry.get(k) for k in ("context_length", "created", "knowledge_cutoff", "prompt_price",
                                              "completion_price")})
        out["openrouter_name"] = entry.get("name")
        if not own and entry.get("description"):
            out["description"], out["source"] = entry["description"], "openrouter"
    return out
