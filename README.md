**English** | [中文](README.zh-CN.md)

# approval-judge-bridge

An OpenAI-compatible endpoint that answers an agent's **approval-guardian call** with a judged
verdict — `APPROVE`, `DENY`, or `ESCALATE`. Four judges behind one interface: a typed judgement
model ([Jev](https://typesafe.ai)), any OpenAI-compatible chat model, a self-hosted Jev-style
classify judge, or a deterministic rule file. It fails closed, logs every decision, and ships the
calibration harness that decides how confident a judge has to be before a command runs without
asking a human.

Why it exists: hosts that gate flagged shell commands (Hermes's smart approvals are the reference
host) ask an auxiliary model for one word. A reasoning model can spend its whole 16-token budget on
hidden reasoning and return empty content — which maps to "escalate", so *every* flagged command
prompts the user, silently and forever ([hermes-agent#108163](https://github.com/NousResearch/hermes-agent/issues/108163)) —
and a prose verdict carries no confidence or margin, so thresholds stay guesses. The bridge fixes
both by adapting the call rather than patching the host, so a host upgrade cannot lose it.

- **Typed judgements.** Jev answers one Choice question (approve / deny / escalate) with a
  calibrated probability distribution — no free text, no rambling past a token cap.
- **Fail-closed everywhere.** Missing key, timeout, HTTP error, malformed or contradictory answer,
  empty answer after one retry — all `ESCALATE`. The bridge never approves what it cannot judge.
- **One safety invariant.** Only an `approve` winner may be auto-approved; `deny` and `escalate`
  reach the human at any threshold. Lowering thresholds trades prompts for quiet, never protection.

## Quickstart

```bash
git clone https://github.com/oppih/approval-judge-bridge
cd approval-judge-bridge
python3 -m judge_bridge            # binds 127.0.0.1:3999, needs TYPESAFE_API_KEY in ~/.hermes/.env
curl -s localhost:3999/healthz     # {"status":"ok","backend":"typesafe",...}
```

Point a host at it. For Hermes (`~/.hermes/config.yaml`):

```yaml
auxiliary:
  approval:
    provider: custom
    model: approval-judge-bridge
    base_url: http://127.0.0.1:3999/v1
    api_key: local-bridge          # the bridge ignores it; the judge key lives in its env
    timeout: 20
approvals:
  mode: smart                      # off | smart | manual
```

Any other host needs only a base URL and a model name: the endpoint speaks
`POST /v1/chat/completions` and returns one word in `choices[0].message.content`.

## Backends

| `JUDGE_BACKEND` | Judge | Needs |
|---|---|---|
| `typesafe` (default) | Jev (System One): one Choice question, probabilities + confidence | `TYPESAFE_API_KEY` (or `MCP_JEV_API_KEY`) |
| `openai` | any OpenAI-compatible chat endpoint used as the judge | `JUDGE_OPENAI_BASE_URL`, `JUDGE_OPENAI_MODEL`, optional `JUDGE_OPENAI_API_KEY` |
| `yajev` | classify-envelope judge (`POST /v1/classify`, `{context, schema}`); reference: a self-hosted Jev clone; keyless by design | none (optional `JUDGE_YAJEV_API_KEY`) |
| `rules` | deterministic regex policy from a JSON file | `JUDGE_RULES_PATH` (see `rules.example.json`) |

The host never sees the difference — it keeps sending its ordinary guardian call and reading one
word back — so adding a judge means adding a backend, never patching a host.

- `openai` rebuilds the reviewer prompt itself (so both machine judges answer the same question) and
  retries **once** with a larger budget when the answer is truncated or unusable, then fails closed.
- `rules` resolves `deny` → `escalate` → `approve` and escalates anything unmatched, so the absence
  of a rule is never permission. `approve` patterns must match the **whole** command (`deny` and
  `escalate` stay substring searches; a prefix cannot show a compound command is safe), and a
  non-empty operator policy escalates rather than being ignored.
- `yajev` speaks the classify envelope on purpose: `{context, schema}` in (enum or boolean fields),
  value + probability + per-class `[logit, probability]` out. Its reference is
  [dongxu's self-hosted Jev clone](https://yajev.0xfefe.me/) (14B on a home GPU, keyless, no SLA),
  and any self-hosted Jev-style service speaks it, so your own clone is a URL change. It is *not* a
  second URL for the typed backend: upstream Jev has no `/v1/classify`. With one `context` string,
  operator policy is marked inside it rather than carried in a trusted channel, so keep the host's
  injection defenses in front.

On this endpoint the probabilities **saturate** (benign work and misses alike land at 0.99+), so the
rubric, not the threshold, is the calibration surface (see Calibration). It also rate limits bursts
(~17 calls in ~10s drew HTTP 429 on 4), so the backend retries a 429 once after 0.4s and then fails
closed; latency is ~0.35s per call through the bridge. `typesafe` stays the default and the reference
deployment keeps Jev as its gate — the classify backend is the measured, passing compatibility option
for judges that arrive in that envelope.

Environment:

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_BACKEND` | `typesafe` | `typesafe`, `yajev`, `openai`, `rules` |
| `JUDGE_HOST` / `JUDGE_PORT` | `127.0.0.1` / `3999` | bind address |
| `JUDGE_AUTO_ACCEPT` / `JUDGE_MIN_MARGIN` | `0.65` / `0.30` | thresholds for probability-carrying judges |
| `JUDGE_LOG` | `~/.approval-judge-bridge/decisions.jsonl` | one JSON record per decision |
| `JUDGE_ENV_FILE` | `~/.hermes/.env` | where keys are read from (env wins) |
| `TYPESAFE_API_URL` / `TYPESAFE_MODEL` | `https://api.typesafe.ai/v1/systemone` / `jev-latest` | Jev endpoint + model |
| `JUDGE_YAJEV_URL` / `JUDGE_YAJEV_API_KEY` | `https://yajev.0xfefe.me/v1/classify` / empty | classify endpoint + optional key (keyless by default) |
| `JUDGE_YAJEV_RUBRIC_FILE` / `JUDGE_YAJEV_MAX_DESCRIPTION` | empty / `2000` | override the single-field rubric; safety cap on its length |
| `JUDGE_OPENAI_MAX_TOKENS` / `JUDGE_OPENAI_RETRY_MAX_TOKENS` | `16` / `256` | first attempt, and the truncation retry |
| `JUDGE_RULES_PATH` | `rules.json` | rule file for the `rules` backend |

Keys are read from the environment or the env file and are **never** logged.

## Input requirements

The bridge judges one complete, unambiguous command and nothing less:

- **Exactly one** `<command>…</command>` pair. Instruction text around it is fine — the reference
  host puts its description before the block and its one-word request after the closing delimiter —
  but a second delimiter (what a command *containing* one looks like) escalates, because the judged
  text would otherwise be a prefix of what actually runs.
- A command over `MAX_COMMAND_CHARS` (6000 characters) escalates; it is never shortened silently and
  judged from its benign beginning.
- Operator policy comes from the system message only — on the `yajev` backend, marked inside its
  single context field rather than carried in a separate channel.

## Calibration

Thresholds are a policy decision, so make it on data: every decision is logged with the winning
class, the full distribution, the margin and the latency, so a candidate threshold can be replayed
against the log before you ship it.

Measured on the reference deployment (57 logged judgements: 21 before recalibration, 36 after):

- Jev's own defaults, `0.85 / 0.60`, escalated **17 of 21 judgements (81%)** — including **12 of the
  13** real work commands, though Jev's winner was `approve` in 10 of them (approve probability
  0.65–0.81, margin 0.30–0.66).
- `0.65 / 0.30` plus a criteria rewrite that names routine work and narrows `escalate` to
  *altering* credentials (read-only inspection used to escalate at 0.88) gave 19 APPROVE / 10 DENY /
  3 low-confidence ESCALATE / 4 escalate-class ESCALATE, median latency 0.18s.
- Dangerous controls stayed blocked, including the two probes for an over-narrowed escalate class:
  `sed -i 's/mode: smart/mode: off/' …config.yaml` → ESCALATE, `base64 -d <<< … | bash` → DENY.
- Where a judge saturates, no threshold can help — the classify judge approved a credential copy at
  p=0.998, and a credential-explicit rubric still approved the approval-machinery edit at p≈0.998.
  Only the rubric closed those probes: a hard rule about the agent's own approval, audit and
  credential protection, plus a read/change carve-out that names the operation.

Run the battery before and after any change; it exits non-zero if a benign command fails to approve
or a dangerous one approves:

```bash
python3 tools/replay_battery.py           # 9 benign (must approve) + 8 danger (must not)
```

## Failure semantics

| Situation | Verdict |
|---|---|
| `approve` winner above both thresholds | `APPROVE` |
| `deny` winner, at any confidence | `DENY` |
| `escalate` winner, or `approve` below a threshold | `ESCALATE` |
| No API key, timeout, connection error, HTTP error | `ESCALATE` |
| HTTP 429 from the judge endpoint | one retry after 0.4s, then `ESCALATE` |
| Answer envelope missing, partial, non-finite or contradictory (winner not the argmax of the distribution), or empty / unrecognised after the `openai` retry, or status `content_filter` | `ESCALATE` |
| Command envelope missing, duplicated, unclosed or misordered; command over `MAX_COMMAND_CHARS` (6000) | `ESCALATE`, never judged from a truncated prefix |
| Malformed HTTP request, wrong-shaped JSON, body over `MAX_REQUEST_BODY_BYTES` (1 MiB), negative `Content-Length`, or a client that stalls mid-body | `ESCALATE` (HTTP 200, logged `bad_request_shape` / `request_too_large` / `request_read_timeout`) |
| Upstream response over `MAX_UPSTREAM_RESPONSE_BYTES` (1 MiB) | `ESCALATE` |
| Non-empty operator policy on the `rules` backend, or policy/context over budget on the classify backend | `ESCALATE` (`policy_not_supported`, `policy_too_long`, `context_too_long` — never truncated) |
| Thresholds outside [0, 1] or non-finite | the service refuses to start |

## Run as a service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/approval-judge-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now approval-judge-bridge
systemctl --user status approval-judge-bridge
```

The unit binds loopback only and restarts on failure. Rollback is one command —
`systemctl --user disable --now approval-judge-bridge` — plus pointing your host back at its
previous provider.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v     # 63 tests, no network, no dependencies
```

Offline and dependency-free: the thresholds invariant, every fail-closed path (bad distributions,
malformed request shapes, ambiguous command envelopes, truncated answers, oversized or stalled
bodies), the retry-with-headroom behaviour, prompt extraction, `rules` whole-command matching,
concurrent decision-log writes, and the HTTP surface over a real socket.

## What this is not

It is a **gate**, not a sandbox: a judge can be wrong, and a command that reaches review has been
judged safe enough to skip a prompt — not proven safe. Keep hardline blocks, allowlists and the
agent's own permission model underneath, keep the trust boundary intact (operator rules from the
system message, command text untrusted), and treat `ESCALATE` as normal for anything consequential.

## Open work

Two bottlenecks remain, and both are about keeping the gate useful rather than making it work —
the fail-closed behaviour itself is tested. Details and acceptance criteria: [TODO.md](TODO.md).

- **Judge failover.** Fail-closed means an unreachable judge escalates everything, so an upstream
  outage degrades the gate into prompting for every flagged command — the symptom this project
  exists to remove. Measured 2026-09-21: while Jev was overloaded, 16 of 17 battery commands
  escalated without ever being judged (15 × `http_529`, 1 × `TimeoutError`). The fallback chain has
  to be transport-only (never re-judge a verdict), carry its own thresholds, pass the battery
  itself, and show on `/healthz` which judge answered.
- **Calibration regression.** The 17-command battery is hand-tuned and a command can flip between
  `approve` and `deny` on rubric wording alone, while the real sample is 37 distinct commands in a
  log that rotates after one generation. Replay them as fixtures so a rubric edit cannot silently
  flip a command, and add a rubric-diff mode that prints the flips before release.

## License

MIT — see [LICENSE](LICENSE).
