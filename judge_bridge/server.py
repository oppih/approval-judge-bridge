"""The HTTP surface: an OpenAI-compatible ``/v1/chat/completions`` that answers a host's
approval-guardian call with a judged verdict.

Why a shim at all: hosts build their guardian call against an OpenAI-shaped client, and a
judge is not a chat model. This adapts the call instead of asking the host to grow a
provider-specific code path — nothing in the host is patched, so a host upgrade cannot lose
the integration.
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .backends import build_backend
from .config import Config
from .decision import Decision, escalate
from .prompts import extract_command, extract_flagged_as, extract_policy

MAX_LOG_BYTES = 16 * 1024 * 1024


class JudgeService:
    """Backend + decision log. One instance per process; the handler is stateless."""

    def __init__(self, config: Config):
        self.config = config
        self.backend = build_backend(config.backend, config.backend_options())

    def log(self, record: dict) -> None:
        try:
            path = self.config.log_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:  # keep one generation
                path.replace(path.with_suffix(path.suffix + ".1"))
            with path.open("a") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # observability must never break the gate

    def judge_request(self, body: dict) -> tuple[str, Decision, dict]:
        """(model, decision, log record) for one guardian call."""
        messages = body.get("messages") or []
        system_text = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
        user_text = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "user")
        command = extract_command(user_text)
        flagged_as = extract_flagged_as(user_text)
        policy = extract_policy(system_text)
        started = time.monotonic()
        try:
            decision = self.backend.judge(command, flagged_as, policy)
        except Exception as exc:  # a backend bug must not open the gate
            decision = escalate(f"{type(exc).__name__}")
        record = {
            "ts": time.time(),
            "backend": self.backend.name,
            "model": body.get("model") or self.backend.name,
            "flagged_as": flagged_as,
            "command": command[:400],
            "verdict": decision.verdict,
            "classification": decision.classification,
            "decision": decision.decision,
            "top": decision.top,
            "margin": decision.margin,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "latency_s": round(time.monotonic() - started, 3),
        }
        return str(body.get("model") or self.backend.name), decision, record


class Handler(BaseHTTPRequestHandler):
    server_version = "approval-judge-bridge/1.0"
    service: JudgeService  # set on the server class by make_server()

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path.startswith("/healthz"):
            described = self.service.backend.describe()
            self._send(200, {"status": "ok", "object": "health", "auto_accept": self.service.config.auto_accept,
                             "min_margin": self.service.config.min_margin, **described})
        elif self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": self.service.backend.name, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib signature
        if not self.path.startswith("/v1/chat/completions"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception as exc:  # malformed request → fail closed, still a valid completion
            self.service.log({"ts": time.time(), "verdict": "ESCALATE", "reason": f"bad_request: {type(exc).__name__}"})
            self._send(200, self._completion("ESCALATE"))
            return
        model, decision, record = self.service.judge_request(body)
        self.service.log(record)
        self._send(200, self._completion(decision.verdict, model=model))

    @staticmethod
    def _completion(verdict: str, model: str = "approval-judge-bridge") -> dict:
        return {
            "id": f"chatcmpl-judge-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": verdict}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
        }

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # the decision log is the record; stderr stays clean for systemd


def make_server(config: Config) -> ThreadingHTTPServer:
    service = JudgeService(config)
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(config: Config | None = None) -> int:
    config = config or Config.from_env()
    server = make_server(config)
    described = server.service.backend.describe()  # type: ignore[attr-defined]
    print(f"[bridge] backend={config.backend} on http://{config.host}:{config.port} "
          f"auto_accept={config.auto_accept} min_margin={config.min_margin} log={config.log_path} "
          f"details={described}", flush=True)
    if not described.get("key_present", True) and config.backend != "rules":
        print(f"[bridge] WARNING: no API key found for backend {config.backend} "
              f"({config.env_file} or the environment) — every call will ESCALATE", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
