"""approval-judge-bridge — an OpenAI-compatible judge endpoint for agent approval gates.

A host that gates flagged shell commands through a "guardian" model call needs exactly one
word back: APPROVE, DENY or ESCALATE. This bridge answers that call with a *typed* judgment
(Jev), any OpenAI-compatible chat model, or a deterministic rule file — and it fails closed.
"""

from .backends import JE_V_CRITERIA, REVIEWER_SYSTEM, Backend, OpenAICompatibleBackend, RulesBackend, TypesafeBackend
from .config import Config
from .decision import Decision, escalate, from_classification
from .server import JudgeService, make_server

__all__ = [
    "Backend", "Config", "Decision", "JudgeService", "JE_V_CRITERIA", "OpenAICompatibleBackend",
    "REVIEWER_SYSTEM", "RulesBackend", "TypesafeBackend", "escalate", "from_classification", "make_server",
]
__version__ = "1.0.0"
