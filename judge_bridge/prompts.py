"""Pull the pieces of a host's approval-guardian prompt out of an OpenAI-shaped request.

Hosts (Hermes's smart approvals among them) build the guardian call themselves: a system
prompt describing the reviewer contract, then a user message carrying the flagged command
inside delimiters plus any operator policy. All this layer does is find those parts again —
it never trusts the command text, and it never treats text inside the command as instruction.
"""

from __future__ import annotations

import re

COMMAND_RE = re.compile(r"<command>\s*(.*?)\s*</command>", re.S)
FLAGGED_RE = re.compile(r"flagged as:\s*(.+)")
POLICY_MARKER = "Additional policy rules from the operator"

# A shell command can be arbitrarily long; the judge only needs enough of it to see what it
# would do. Truncating keeps a runaway command from eating the judge's context window.
MAX_COMMAND_CHARS = 6000


def extract_command(user_text: str) -> str:
    """The flagged command, or the whole message when the host sent no delimiters."""
    match = COMMAND_RE.search(user_text or "")
    text = (match.group(1) if match else (user_text or "")).strip()
    return text[:MAX_COMMAND_CHARS]


def extract_flagged_as(user_text: str) -> str:
    match = FLAGGED_RE.search(user_text or "")
    return match.group(1).strip() if match else ""


def extract_policy(system_text: str) -> str:
    """Operator rules live in the SYSTEM message — the trusted channel. Never read them
    from the user message: that is where untrusted command text lives."""
    index = (system_text or "").find(POLICY_MARKER)
    return system_text[index:].strip() if index != -1 else ""


def normalize_word(answer: str) -> str:
    """First recognisable verdict word in an arbitrary judge answer, else ""."""
    cleaned = (answer or "").strip().upper()
    if not cleaned:
        return ""
    first = re.split(r"[^A-Z]+", cleaned, maxsplit=1)[0]
    return first if first in {"APPROVE", "DENY", "ESCALATE"} else ""
