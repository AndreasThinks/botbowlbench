"""Harness versioning so every match records exactly what code and prompts produced it."""
import hashlib
import json
import os
import subprocess

# Bump when the rules of the benchmark change in a way that makes results incomparable
# (prompt wording, tool semantics, budgets, reflection protocol, ...).
# 1.1: output_truncations vs no_tool; no default harness output-token cap
# 1.2: turn_time_limit is a hard deadline that cancels in-flight model calls; default output-token ceiling
PROTOCOL_VERSION = "1.2"
LEGACY_PROTOCOL_VERSION = "1.0"  # matches recorded before protocol versions were enforced have no version in meta


def match_protocol(m: dict) -> str:
    """The protocol a (decoded) match row was played under; a missing version means the legacy 1.0."""
    meta = m.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    harness = meta.get("harness") if isinstance(meta, dict) else None
    v = harness.get("protocol_version") if isinstance(harness, dict) else None
    return str(v) if v else LEGACY_PROTOCOL_VERSION


def is_current_protocol(m: dict) -> bool:
    """Only current-protocol results feed ratings, leaderboards and placement; older ones stay as archives."""
    return match_protocol(m) == PROTOCOL_VERSION


def git_sha() -> str:
    for var in ("RAILWAY_GIT_COMMIT_SHA", "SOURCE_COMMIT", "GIT_COMMIT", "GITHUB_SHA"):
        if os.environ.get(var):
            return os.environ[var][:12]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
        if out.returncode == 0:
            return out.stdout.strip()[:12]
    except Exception:
        pass
    return "unknown"


_fingerprint = None


def prompt_fingerprint() -> str:
    """Hash of the system prompt template, rules primer and tool schemas the models see."""
    global _fingerprint
    if _fingerprint is None:
        from bench.driver import DriverLimits, system_prompt
        from bench.tools import RULES_PRIMER, TOOL_DOCS
        blob = json.dumps({"system": system_prompt("<MODEL>", DriverLimits(), "home", "<OPPONENT>"),
                           "rules": RULES_PRIMER, "tools": TOOL_DOCS}, sort_keys=True)
        _fingerprint = hashlib.sha256(blob.encode()).hexdigest()[:12]
    return _fingerprint
