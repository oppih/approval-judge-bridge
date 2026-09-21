"""Decision object + the threshold mapping shared by every backend.

The one safety property that must survive any threshold: **only an `approve` classification
may be auto-approved.** A `deny` or `escalate` winner is handed to the human no matter how
confident the judge is, because "very sure it is dangerous" is not a licence to skip review.
Thresholds therefore trade prompts for quiet, never protection.
"""

from __future__ import annotations

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
    confidence: float | None = None,
    usage: dict | None = None,
) -> Decision:
    """Map a judged class + probability distribution onto a verdict.

    `probabilities` may be empty (an LLM backend that answers with one word); a recognised
    class then counts as an auto decision, exactly as the host would read the word itself.
    """
    if classification not in VERDICT_BY_CLASS:
        return escalate(f"unknown_class:{classification or 'empty'}")

    probs = {k: float(v) for k, v in (probabilities or {}).items() if isinstance(v, (int, float))}
    verdict = Decision(
        verdict=VERDICT_BY_CLASS[classification], classification=classification,
        decision="auto", probabilities=probs, confidence=confidence, usage=usage,
    )
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
