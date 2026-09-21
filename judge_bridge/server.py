"""The HTTP surface: an OpenAI-compatible ``/v1/chat/completions`` that answers a host's
approval-guardian call with a judged verdict.

Why a shim at all: hosts build their guardian call against an OpenAI-shaped client, and a
judge is not a chat model. This adapts the call instead of asking the host to grow a
provider-specific code path — nothing in the host is patched, so a host upgrade cannot lose
the integration.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .backends import build_backend
from .config import Config
from .decision import Decision, escalate
from .prompts import envelope_problem, extract_command, extract_flagged_as, extract_policy

# Fixed limits keep local malformed/stalled callers from consuming unbounded resources.
MAX_REQUEST_BODY_BYTES = 1024 * 1024  # 1 MiB; reject before reading the body
REQUEST_READ_TIMEOUT_SECONDS = 5.0  # socket idle timeout and total body-read deadline

MAX_LOG_BYTES = 16 * 1024 * 1024


class JudgeService:
    """Backend + decision log. One instance per process; the handler is stateless."""

    def __init__(self, config: Config):
        # Handlers share this service; rotation and append must be one atomic operation.
        self._log_lock = threading.Lock()
        self.config = config
        self.backend = build_backend(config.backend, config.backend_options())

    def log(self, record: dict) -> None:
        with self._log_lock:
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
        started = time.monotonic()
        model = self.backend.name
        command = flagged_as = policy = ""
        try:
            if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
                raise ValueError("bad_request_shape")
            messages = body["messages"]
            for message in messages:
                if (not isinstance(message, dict)
                        or (message.get("content") is not None and not isinstance(message["content"], str))
                        or (message.get("role") is not None and not isinstance(message["role"], str))):
                    raise ValueError("bad_request_shape")
            model = str(body.get("model") or self.backend.name)
            system_text = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
            user_text = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "user")
            problem = envelope_problem(user_text)
            if problem:
                raise ValueError(problem)
            command = extract_command(user_text)
            flagged_as = extract_flagged_as(user_text)
            policy = extract_policy(system_text)
        except Exception as exc:
            decision = escalate(str(exc) if isinstance(exc, ValueError) else "bad_request_shape")
        else:
            try:
                decision = self.backend.judge(command, flagged_as, policy)
            except Exception as exc:  # a backend bug must not open the gate
                decision = escalate(f"{type(exc).__name__}")
        record = {
            "ts": time.time(),
            "backend": self.backend.name,
            "model": model,
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
        return model, decision, record


class Handler(BaseHTTPRequestHandler):
    server_version = "approval-judge-bridge/1.0"
    service: JudgeService  # set on the server class by make_server()

    def setup(self):
        super().setup()
        # Also bound idle reads while the stdlib handler parses request headers.
        self.connection.settimeout(REQUEST_READ_TIMEOUT_SECONDS)

    def _read_body(self, length: int) -> bytes:
        deadline = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS
        chunks = []
        remaining = length
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("request_read_timeout")
            self.connection.settimeout(budget)
            # read1 returns after one underlying read, so trickled bytes cannot reset
            # the total deadline as they would with a single buffered read(length).
            chunk = self.rfile.read1(min(remaining, 65536))
            if not chunk:
                raise ValueError("incomplete_request_body")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

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
            if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                raise ValueError("request_too_large")
            body = json.loads(self._read_body(length).decode() or "{}")
        except Exception as exc:  # malformed request → fail closed, still a valid completion
            reason = ("request_read_timeout" if isinstance(exc, TimeoutError) else
                      "request_too_large" if str(exc) == "request_too_large" else
                      f"bad_request: {type(exc).__name__}")
            # Unread bytes must not become another request on this connection.
            self.close_connection = True
            self.connection.settimeout(REQUEST_READ_TIMEOUT_SECONDS)
            self.service.log({"ts": time.time(), "verdict": "ESCALATE", "reason": reason})
            self._send(200, self._completion("ESCALATE"))
            return
        self.connection.settimeout(REQUEST_READ_TIMEOUT_SECONDS)
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
    if described.get("key_required", True) and not described.get("key_present", True):
        print(f"[bridge] WARNING: no API key found for backend {config.backend} "
              f"({config.env_file} or the environment) — every call will ESCALATE", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
