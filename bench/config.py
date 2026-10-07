"""Loads the competitor list (models.yaml) and runtime settings from the environment."""
import os
import re
from typing import Dict, List

import yaml

DEFAULT_SETTINGS = {
    "game_mode": "5",                 # players per side: 1, 3, 5, 7 or 11
    "team": "human",                  # roster file used by both sides (mirror matches)
    "max_tool_calls_per_turn": 40,
    "turn_time_limit": 300,           # seconds of wall time per team turn
    "max_illegal_streak": 3,
    "max_messages_per_turn": 2,
    "max_message_len": 280,
    "decision_timeout": 900,          # hard safety net per decision, seconds
    "budget_usd_per_game": 2.0,       # per model per game; null = unlimited
    "legs": 2,                        # 2 = every pairing is played home and away
    "pause_between_matches": 5,       # seconds
}


def data_dir() -> str:
    d = os.environ.get("DATA_DIR") or os.path.join(os.getcwd(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def models_path() -> str:
    return os.environ.get("MODELS_CONFIG") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "models.yaml")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_models_file(path: str = None) -> Dict:
    path = path or models_path()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw.get("settings") or {})
    models: List[dict] = []
    seen = set()
    for m in raw.get("models") or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        m = dict(m)
        m.setdefault("provider", "openrouter")
        m.setdefault("enabled", True)
        if m["provider"] == "openrouter" and not m.get("model"):
            raise ValueError(f"Model '{m['name']}' needs an OpenRouter 'model' id")
        m["id"] = m.get("id") or slugify(m["name"])
        if m["id"] in seen:
            raise ValueError(f"Duplicate model id '{m['id']}' in {path}")
        seen.add(m["id"])
        models.append(m)
    return {"settings": settings, "models": models}
