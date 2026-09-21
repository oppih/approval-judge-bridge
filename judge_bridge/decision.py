"""Decision object + the threshold mapping shared by every backend.

The one safety property that must survive any threshold: **only an `approve` classification
may be auto-approved.** A `deny` or `escalate` winner is handed to the human no matter how
confident the judge is, because "very sure it is dangerous" is not a licence to skip review.
Thresholds therefore trade prompts for quiet, never protection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

VERDICT_BY_CLASS = {"approve": "APPROVE", "deny": "DENY", "escalate": "ESCALATE"}
CLASSES = ("approve", "deny", "escalate")


@dataclass
class Decision:
    """What the host will see, plus what the judge actually said (for the decision log)."""

    verdict: str  # APPROVE | DENY | ESCALATE — the single word the host reads
    classification: str = ""  # winning class, when the backend produces one
    decision: str = "review"  # auto | review
    top: float | None = None
    margin: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    usage: dict | None = None
    reason: str | None = None  # set when a failure forced the fail-closed ESCALATE


def escalate(reason: str) -> Decision:
    """The fail-closed answer: anything unexpected becomes a human decision."""
    return Decision(verdict="ESCALATE", decision="review", reason=reason)


def from_classification(
    classification: str,
    probabilities: dict[str, float] | None,
    *,
    auto_accept: float,
    min_margin: float,
    require_distribution: bool = False,
    confidence: float | None = None,
    usage: dict | None = None,
) -> Decision:
    """Map a judged class + probability distribution onto a verdict.

    Only `None` denotes an intentional bare-word answer. Invalid envelopes fail closed.
    """
    if classification not in VERDICT_BY_CLASS:
        return escalate(f"unknown_class:{classification or 'empty'}")

    verdict = Decision(
        verdict=VERDICT_BY_CLASS[classification], classification=classification,
        decision="auto", confidence=confidence, usage=usage,
    )

    def invalid(reason: str) -> Decision:
        # Preserve the reported winner for the log even when its envelope is untrustworthy.
        verdict.verdict, verdict.decision, verdict.reason = "ESCALATE", "review", reason
        return verdict

    probs = {}
    if probabilities is None:
        if require_distribution:
            return invalid("invalid_response")
    else:
        if not isinstance(probabilities, dict) or not probabilities:
            return invalid("invalid_response")
        for name, value in probabilities.items():
            if (name not in CLASSES or isinstance(value, bool)
                    or not isinstance(value, (int, float)) or not 0 <= value <= 1
                    or not math.isfinite(value)):
                return invalid("invalid_probabilities")
            probs[name] = float(value)
        if set(probs) != set(CLASSES):
            return invalid("incomplete_distribution")
        if classification not in probs or probs[classification] != max(probs.values()):
            return invalid("inconsistent_envelope")
    verdict.probabilities = probs
    if classification != "approve":
        # Explicit non-approve class: always the human's call.
        verdict.decision = "review"
        return verdict
    if not probs:
        return verdict
    ordered = sorted(probs.values(), reverse=True)
    top = ordered[0]
    margin = (ordered[0] - ordered[1]) if len(ordered) > 1 else top
    verdict.top, verdict.margin = top, margin
    if top < auto_accept or margin < min_margin:
        # Jev calling "review", or a near-tie / low top probability: never good enough to
        # auto-approve, so hand it over.
        verdict.verdict = "ESCALATE"
        verdict.decision = "review"
    return verdict
