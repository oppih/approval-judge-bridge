# TODO

Two open bottlenecks, in the order they should be worked on. Both come from the
2026-09-21 audit and deployment, and both are about *keeping* the gate useful rather than
making it work — the fail-closed behaviour itself is tested.

## 1. Judge failover: the gate is only as available as its upstream judge

**Why.** Fail-closed means an unreachable judge turns into "escalate", so the fallback for an
upstream outage is *prompting the human for every flagged command* — the exact symptom this
project exists to remove. Measured on 2026-09-21, while the Jev endpoint was overloaded, the same
17-command battery produced 15 × `http_529` and 1 × `TimeoutError`: 16 of 17 commands escalated
without ever being judged. The upstream recovered minutes later and the same battery passed
(9/9 benign, 8/8 danger). The production log carries one `http_529` from real traffic in that
window too, so this is not a test-only failure mode.

**What to build.**
- A fallback chain, configured like `JUDGE_BACKEND=typesafe` + `JUDGE_FALLBACK_BACKEND=yajev`
  (possibly a list). Promote the fallback only for *transport* failures — connection error,
  timeout, HTTP 5xx, 429 after the existing retry — never for a judged verdict. A `deny` from the
  primary must never be re-judged by a more permissive judge.
- Each judge in the chain keeps its own thresholds: thresholds are not transferable between
  judges (the classify judge saturates at 0.99+, so its rubric, not its threshold, is the
  calibration surface).
- A judge may only be used as a fallback if it passes `tools/replay_battery.py` itself; a silent
  downgrade to an uncalibrated judge is worse than escalating.
- Make the degradation visible: log which judge answered and why the primary was skipped, and
  expose it on `/healthz`. Today an outage is invisible from the outside.
- Decide explicitly what "all judges unavailable" should do. Current behaviour — `ESCALATE` for
  everything — is safe but noisy; if that stays, say so in the failure-semantics table.

**Acceptance.**
- A test where the primary raises a connection error / 529 and the fallback answers: the verdict
  comes from the fallback and the log names both judges.
- A test where the primary answers `deny` and the fallback would have answered `approve`: the
  answer stays `DENY`.
- The battery passes with the primary made unreachable.

## 2. Calibration regression: the hand-tuned battery does not catch wording drift

**Why.** The battery is 17 commands tuned over four rubric rewrites, and one command can flip
between `approve` and `deny` on wording alone, in both directions. Thresholds do not transfer
across judges, and on the classify judge the probabilities saturate, so the rubric is the only
calibration surface. Meanwhile the real sample is small: 37 distinct commands in the production
log, which rotates after one generation.

**What to build.**
- A fixture builder that turns the decision log into a replayable command set — command, expected
  verdict, and the reason the verdict is expected (battery groups plus real work commands).
- A test that replays those fixtures against the judge and fails when a verdict changes, so a
  rubric edit cannot silently flip a command that was already decided.
- A "rubric diff" mode: run the old and the new rubric over the same fixtures and print the flips
  before shipping, instead of discovering them in production.
- Fixture hygiene: no secrets, keys or tokens in the committed fixtures; the log path stays
  outside the repo and the builder redacts before writing.

**Acceptance.**
- Changing a rubric string makes at least one fixture fail when it flips a real command.
- `python3 -m unittest discover` stays offline and dependency-free (fixtures replay against
  stubbed judge responses; a live-judge mode is opt-in).
- The fixture set is documented in the README's calibration section, replacing "the battery is 17
  commands" with where the regression data comes from.
