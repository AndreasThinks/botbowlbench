import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeOpenRouter(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible endpoint that plays (badly) through the bench's tools."""
    calls = 0
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenRouter.calls += 1
        n = FakeOpenRouter.calls
        if FakeOpenRouter.status != 200:
            self._send(FakeOpenRouter.status, {"error": {"message": "User not found.", "code": 401}})
            return
        assert body["tools"] and body["messages"][0]["role"] == "system"
        last = body["messages"][-1]
        content = last.get("content") or ""
        calls = []
        if n % 11 == 0:
            msg = {"role": "assistant", "content": "Hmm, let me think."}  # no tool call -> nudged
        else:
            if last["role"] == "user" and "CURRENT SITUATION" in content:
                calls = [("get_state", {}), ("send_message", {"text": f"hello #{n}"})]
            elif "choose a player to activate" in content:
                calls = [("end_turn", {})]
            else:
                m = re.search(r"^\s+([A-Z_]{3,})\b", content, re.M)
                name = m.group(1) if m else "END_TURN"
                if name == "PLACE_PLAYER":
                    name = "SETUP_FORMATION_SPREAD"
                calls = [("take_action", {"action_type": name})]
            tool_calls = []
            for i, (name, args) in enumerate(calls):
                raw = json.dumps(args) if n % 7 else "{not json"
                tool_calls.append({"id": f"c{n}_{i}", "type": "function", "function": {"name": name, "arguments": raw}})
            msg = {"role": "assistant", "content": "", "tool_calls": tool_calls}
        self._send(200, {"choices": [{"message": msg}],
                         "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.0001}})

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
