"""Configuration: everything comes from the environment so the service unit stays declarative."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
DEFAULT_ENV_FILE = Path(os.environ.get("JUDGE_ENV_FILE", HOME / ".hermes" / ".env"))
DEFAULT_LOG = Path(os.environ.get("JUDGE_LOG", HOME / ".approval-judge-bridge" / "decisions.jsonl"))


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    backend: str = "typesafe"
    host: str = "127.0.0.1"
    port: int = 3999
    env_file: Path = DEFAULT_ENV_FILE
    log_path: Path = DEFAULT_LOG
    # Thresholds apply to probability-carrying backends (typesafe). A backend that returns a
    # bare word (openai/rules) is taken at its word.
    auto_accept: float = 0.65
    min_margin: float = 0.30
    timeout: float = 12.0
    # Backend options
    typesafe_api_url: str = "https://api.typesafe.ai/v1/systemone"
    typesafe_model: str = "jev-latest"
    typesafe_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    openai_api_key: str = ""
    openai_max_tokens: int = 16
    openai_retry_max_tokens: int = 256
    rules_path: Path = field(default_factory=lambda: Path(os.environ.get("JUDGE_RULES_PATH", "rules.json")))

    @classmethod
    def from_env(cls) -> "Config":
        from .backends import read_secret_from_env_file

        env_file = Path(os.environ.get("JUDGE_ENV_FILE", str(DEFAULT_ENV_FILE)))
        return cls(
            backend=os.environ.get("JUDGE_BACKEND", "typesafe").strip().lower(),
            host=os.environ.get("JUDGE_HOST", "127.0.0.1"),
            port=_int("JUDGE_PORT", 3999),
            env_file=env_file,
            log_path=Path(os.environ.get("JUDGE_LOG", str(DEFAULT_LOG))),
            auto_accept=_float("JUDGE_AUTO_ACCEPT", 0.65),
            min_margin=_float("JUDGE_MIN_MARGIN", 0.30),
            timeout=_float("JUDGE_UPSTREAM_TIMEOUT", 12.0),
            typesafe_api_url=os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone"),
            typesafe_model=os.environ.get("TYPESAFE_MODEL", "jev-latest"),
            typesafe_api_key=read_secret_from_env_file(env_file, "TYPESAFE_API_KEY", "MCP_JEV_API_KEY"),
            openai_base_url=os.environ.get("JUDGE_OPENAI_BASE_URL", ""),
            openai_model=os.environ.get("JUDGE_OPENAI_MODEL", ""),
            openai_api_key=read_secret_from_env_file(env_file, "JUDGE_OPENAI_API_KEY"),
            openai_max_tokens=_int("JUDGE_OPENAI_MAX_TOKENS", 16),
            openai_retry_max_tokens=_int("JUDGE_OPENAI_RETRY_MAX_TOKENS", 256),
            rules_path=Path(os.environ.get("JUDGE_RULES_PATH", "rules.json")),
        )

    def backend_options(self) -> dict:
        """Options for the selected backend — validated here so a misconfigured unit fails at
        startup, loudly, instead of escalating every command."""
        if self.backend == "typesafe":
            return {"api_url": self.typesafe_api_url, "model": self.typesafe_model, "api_key": self.typesafe_api_key,
                    "auto_accept": self.auto_accept, "min_margin": self.min_margin, "timeout": self.timeout}
        if self.backend == "openai":
            if not self.openai_base_url or not self.openai_model:
                raise ValueError("JUDGE_OPENAI_BASE_URL and JUDGE_OPENAI_MODEL are required for the openai backend")
            return {"base_url": self.openai_base_url, "model": self.openai_model, "api_key": self.openai_api_key,
                    "max_tokens": self.openai_max_tokens, "retry_max_tokens": self.openai_retry_max_tokens,
                    "timeout": self.timeout}
        if self.backend == "rules":
            return {"path": self.rules_path}
        raise ValueError(f"unknown JUDGE_BACKEND: {self.backend!r} (expected typesafe, openai or rules)")
