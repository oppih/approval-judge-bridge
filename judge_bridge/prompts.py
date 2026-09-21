"""Pull the pieces of a host's approval-guardian prompt out of an OpenAI-shaped request.

Hosts (Hermes's smart approvals among them) build the guardian call themselves: a system
prompt describing the reviewer contract, then a user message carrying the flagged command
inside delimiters plus any operator policy. All this layer does is find those parts again —
it never trusts the command text, and it never treats text inside the command as instruction.
"""

from __future__ import annotations

import re
import string

COMMAND_RE = re.compile(r"<command>\s*(.*?)\s*</command>", re.S)
FLAGGED_RE = re.compile(r"flagged as:\s*(.+)")
POLICY_MARKER = "Additional policy rules from the operator"

# Overlong commands must be reviewed whole, never judged from a benign prefix.
MAX_COMMAND_CHARS = 6000


def envelope_problem(user_text: str) -> str | None:
    """Reject ambiguous boundaries before extracting any untrusted command text."""
    opening_count = user_text.count("<command>")
    closing_count = user_text.count("</command>")
    if opening_count > 1 or closing_count > 1:
        return "multiple_command_envelopes"
    if opening_count == 0:
        return "no_command_envelope"
    if closing_count == 0:
        return "missing_command_close"
    prefix, rest = user_text.split("<command>", 1)
    if "</command>" in prefix:
        return "invalid_command_envelope"
    command = rest.split("</command>", 1)[0].strip()
    if not command:
        return "invalid_command_envelope"
    if len(command) > MAX_COMMAND_CHARS:
        return "command_too_long"
    return None


def extract_command(user_text: str) -> str:
    """Extract a complete command; callers validate the envelope before judging it."""
    problem = envelope_problem(user_text)
    if problem:
        raise ValueError(problem)
    return COMMAND_RE.search(user_text).group(1).strip()


def extract_flagged_as(user_text: str) -> str:
    match = FLAGGED_RE.search(user_text or "")
    return match.group(1).strip() if match else ""


def extract_policy(system_text: str) -> str:
    """Operator rules live in the SYSTEM message — the trusted channel. Never read them
    from the user message: that is where untrusted command text lives."""
    index = (system_text or "").find(POLICY_MARKER)
    return system_text[index:].strip() if index != -1 else ""


def normalize_word(answer: str) -> str:
    """An exact verdict, optionally followed by one ASCII punctuation character."""
    if not isinstance(answer, str):
        return ""
    cleaned = answer.strip().upper()
    if cleaned and cleaned[-1] in string.punctuation:
        cleaned = cleaned[:-1]
    return cleaned if cleaned in {"APPROVE", "DENY", "ESCALATE"} else ""
