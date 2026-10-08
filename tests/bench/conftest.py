import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeOpenRouter(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible endpoint that plays (badly) through the bench's tools."""
    calls = 0
    status = 200
    bodies = []
    # When set, the next N replies are empty (no tools) with finish_reason=\"length\" — models that burn
    # the completion budget on reasoning. 0 disables. Separate from the usual n%11 text-only nudges.
    length_truncations_left = 0
    length_truncation_mode = "main"  # \"main\" | \"reflect\": only fire on main-loop / reflect-only calls
    # Scripted main-loop replies consumed first: "L" = empty finish_reason=length, "T" = text-only stop.
    script = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenRouter.calls += 1
        FakeOpenRouter.bodies.append(body)
        n = FakeOpenRouter.calls
        if FakeOpenRouter.status != 200:
            self._send(FakeOpenRouter.status, {"error": {"message": "User not found.", "code": 401}})
            return
        assert body["tools"] and body["messages"][0]["role"] == "system"
        last = body["messages"][-1]
        content = last.get("content") or ""
        if isinstance(content, list):  # content blocks (e.g. with cache_control)
            content = "".join(b.get("text", "") for b in content)
        names = [t["function"]["name"] for t in body["tools"]]
        reflect_only = names == ["reflect"]
        mode = FakeOpenRouter.length_truncation_mode
        want_length = FakeOpenRouter.length_truncations_left > 0 and (
            mode == "any"
            or (mode == "main" and not reflect_only)
            or (mode == "reflect" and reflect_only)
        )
        if want_length:
            FakeOpenRouter.length_truncations_left -= 1
            msg = {"role": "assistant", "content": "", "reasoning": "…" * 200}
            finish = "length"
            usage_comp = int(body.get("max_tokens") or 2048)
            self._send(200, {"id": f"gen-{n}", "model": body["model"] + "-20260901", "provider": "FakeCloud",
                             "choices": [{"message": msg, "finish_reason": finish}],
                             "usage": {"prompt_tokens": 1000, "completion_tokens": usage_comp, "cost": 0.0001,
                                       "prompt_tokens_details": {"cached_tokens": 600},
                                       "completion_tokens_details": {"reasoning_tokens": usage_comp}}})
            return
        scripted = FakeOpenRouter.script.pop(0) if FakeOpenRouter.script and not reflect_only else None
        if scripted == "L":
            self._send(200, {"id": f"gen-{n}", "model": body["model"], "provider": "FakeCloud",
                             "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.0001}})
            return
        calls = []
        if scripted == "T" or (scripted is None and n % 11 == 0):
            msg = {"role": "assistant", "content": "Hmm, let me think."}  # no tool call -> nudged
            finish = "stop"
        else:
            if reflect_only:
                calls = [("reflect", {"plan": "Score next turn.", "prediction": "They will blitz."})]
            elif last["role"] == "user" and "CURRENT SITUATION" in content:
                calls = [("get_state", {}), ("send_message", {"text": f"hello #{n}"})]
            elif "choose a player to activate" in content:
                calls = [("end_turn", {"plan": "Advance on the left.", "prediction": "They will blitz my carrier."})]
            else:
                m = re.search(r"^\s+([A-Z_]{3,})\b", content, re.M)
                name = m.group(1) if m else "END_TURN"
                if name == "PLACE_PLAYER":
                    name = "SETUP_FORMATION_SPREAD"
                if name == "END_TURN":
                    calls = [("end_turn", {"plan": "Regroup.", "prediction": "They attack."})]
                else:
                    calls = [("take_action", {"action_type": name})]
            tool_calls = []
            for i, (name, args) in enumerate(calls):
                raw = json.dumps(args) if n % 7 else "{not json"
                tool_calls.append({"id": f"c{n}_{i}", "type": "function", "function": {"name": name, "arguments": raw}})
            msg = {"role": "assistant", "content": "", "tool_calls": tool_calls,
                   "reasoning_details": [{"type": "reasoning.encrypted", "data": f"sig-{n}"}]}
            finish = "tool_calls"
        self._send(200, {"id": f"gen-{n}", "model": body["model"] + "-20260901", "provider": "FakeCloud",
                         "choices": [{"message": msg, "finish_reason": finish}],
                         "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.0001,
                                   "prompt_tokens_details": {"cached_tokens": 600}}})

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_openrouter(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenRouter)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    FakeOpenRouter.calls = 0
    FakeOpenRouter.status = 200
    FakeOpenRouter.bodies = []
    FakeOpenRouter.length_truncations_left = 0
    FakeOpenRouter.length_truncation_mode = "main"
    FakeOpenRouter.script = []
    import bench.llm
    monkeypatch.setattr(bench.llm, "OPENROUTER_URL", f"http://127.0.0.1:{server.server_port}/chat/completions")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    yield FakeOpenRouter
    server.shutdown()


@pytest.fixture
def bench_env(tmp_path, monkeypatch):
    models = tmp_path / "models.yaml"
    models.write_text("""
settings:
  pause_between_matches: 0
models:
  - name: Scripted baseline
    provider: scripted
  - name: Random baseline
    provider: random
  - name: Random two
    provider: random
""")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MODELS_CONFIG", str(models))
    import bench.scheduler
    monkeypatch.setattr(bench.scheduler, "_bench", None)
    return models
