<div align="center">

# Model Authenticity Verifier

**Is the model behind that gateway really what the label says?**

![version](https://img.shields.io/badge/skill-v2.2-1f6feb)
![python](https://img.shields.io/badge/python-stdlib%20only-3776AB?logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-2ea44f)

Part of the [Everything Claude Code](https://github.com/affaan-m/ECC) skill collection

</div>

---

A gateway sells `claude-opus-4-8` at a tenth of the official price. Three options:
it is genuine, it is a smaller model with a bigger label, or it is the real thing
behind a broken proxy. This skill settles the question with **evidence instead of
vibes**: probe the gateway, build a transparent evidence ledger, and report exactly
what the data supports — nothing more.

> [!IMPORTANT]
> Black-box testing cannot *prove* authenticity. This skill finds contradictions,
> measures service degradation, and says "no contradiction found" without ever
> dressing that up as "confirmed genuine".

## What it does

- **Infrastructure & service audit** — wrapper-stack fingerprints (NewAPI etc.),
  headers, catalog oddities, hidden output caps, response mutation, feature stripping.
- **Capability battery** — a seedable randomized battery plus a dual-depth needle
  (25% / 75% into a 120k-character haystack). A "frontier" model that fails
  multi-step arithmetic or long-context recall has answered the question already.
- **One-directional self-report** — identity and cutoff probes, sampled twice.
  Contradiction is evidence; agreement is never scored.
- **Differential testing** — same prompts against the official vendor endpoint:
  pass-rate parity, exact-output agreement at temperature 0, forced-output
  tokenizer signature, open-ended similarity.
- **Logprobs distribution probe** *(optional)* — top-1 next-token and
  `|delta logprob|` comparison, the strongest non-attested signal when both
  endpoints expose it.
- **Input-side tokenizer diff** *(manual probe)* — fingerprint the tokenizer
  from `usage.input_tokens` / `prompt_tokens` of prompt-only messages:
  `tokens(T) - tokens("a")` per path cancels per-path overhead without a
  third control route. Works when completions are capped/mutated. Measured
  per-family the gateway deltas ship in
  [`references/tokenizer-baselines-2026-10.md`](references/tokenizer-baselines-2026-10.md).

## How it works

```
                 +-----------------------+
                 |    verify_gateway.py  |
                 |   Python 3 - stdlib   |
                 +-----------+-----------+
                             |
             +---------------+----------------+
             v                                v
   +-------------------+            +-------------------+
   |  suspect gateway  |            |  baseline (opt.)  |
   |   the claimed id  |            |  official vendor  |
   +---------+---------+            +---------+---------+
             +---------------+----------------+
                             v
                 +-----------------------+
                 |    evidence ledger    |
                 | STRONG/MODERATE/WEAK  |
                 |     FOR / AGAINST     |
                 +-----------+-----------+
                             v
        CONTRADICTIONS - TENSIONS - SERVICE
        DEGRADATION - CONSISTENT - INCONCLUSIVE
```

## Quick start

Python 3, standard library only — no `pip install`, runs on Windows / macOS / Linux.

```bash
# Install as a skill for Claude Code / OpenCode / any harness that reads skills
git clone https://github.com/SNNN-011/model-authenticity-verifier \
    ~/.claude/skills/model-authenticity-verifier

# 0. Offline logic check: 75 assertions, no network
python scripts/verify_gateway.py selftest

# 1. Full probe, no baseline (finds contradictions, cannot prove identity)
VERIFY_API_KEY=sk-... python scripts/verify_gateway.py \
    --base https://gw.example.com/v1 \
    --model "vendor/model-id"
```

```bash
# 2. Differential vs the official endpoint (the strongest available check)
python scripts/verify_gateway.py \
    --base https://gw.example.com/v1 --model "vendor/model-id" \
    --baseline-base https://api.official.example/v1 --baseline-model "official-id" \
    --logprobs

# 3. Reproduce a previous dynamic battery / needle exactly
... --seed 42 --out report.json
```

On Windows use `python` instead of `python3`; set the key with
`$env:VERIFY_API_KEY = "sk-..."`.

## Probe reference

| Flag | What it records |
|---|---|
| `--probes infra` | wrapper stack, headers, catalog fingerprint, token overhead |
| `--probes service` | hidden output caps, response mutation, reasoning/logprobs passthrough |
| `--probes caps` | seedable capability battery + dual-depth needle (`--needle-chars 120000`, `--skip-needle` to drop it) |
| `--probes self` | identity / cutoff self-report, sampled twice |
| `--baseline-base` + `--baseline-model` | differential testing (pass rates, exact-output agreement, forced-output tokenizer check) |
| `--logprobs` | logprobs distribution comparison (requires baseline) |
| `--seed N` | reproduce the exact battery + needle from a previous run |
| `--out file.json` | full machine-readable report |

Default probe set is `infra,service,caps,self`. Keys come from `--key` /
`--baseline-key`, or `VERIFY_API_KEY` / `VERIFY_BASELINE_API_KEY`. Keys are never
printed or written to the report.

## Models with a measured tokenizer baseline (the gateway, 2026-10-29)

The input-side diff was measured against `the audited gateway` (11 of 32 models,
partial run). A new audit can compare a gateway's deltas straight against
these signatures (full method, anomalies, and usage rules: the
[baseline file](references/tokenizer-baselines-2026-10.md)):

| Claimed model → check against | Delta signature T1–T5 |
|---|---|
| `claude-opus-4.7` / `-4.8` / `-5`, `claude-sonnet-5` (Anthropic core) | `45 40 35 45 47` |
| `claude-opus-4.6` / `claude-sonnet-4.6` (older-generation branch) | `35 34 29 46 47` |
| `gpt-5.6-terra` / `gpt-5.6-sol` (OpenAI family) | `24 24 22 30 29` |
| `deepseek-v4-pro`, `deepseek-v4.1-flash-1017` | `27 26 23 32 25` |
| `deepseek-v4-flash` | anomalous - do not use as a reference until re-run |

Not yet measured on the gateway: the 5.5-generation Claude models (opus/sonnet/haiku),
gpt-6*, gemini, glm/zai, minimax, moonshot kimi, qwen, xiaomi, and the
`uncensored/*` uncensored twin. Two fable models and `gpt-6-astra` were excluded
by request. A signature match is WEAK evidence (usage is forgeable, rule R8) -
score family agreement across texts, never a single text.

## Evidence model (the ground rules)

- **Self-report is one-directional.** Naming a *different* family is real evidence.
  Naming the *right* family is not — one injected system prompt produces any identity.
- **Usage numbers are testimony, not measurement.** `completion_tokens` is believed
  only after a client-side cross-check against the raw text, and even then capped at WEAK.
- **Identity and service findings never mix.** An output cap is a service finding;
  a capped genuine model is still genuine.
- **Dead is not fake.** Consistent timeouts are reported as unavailable, not as rejection.

## Verdict semantics

| Verdict | Meaning |
|---|---|
| `CONTRADICTIONS FOUND` | hard evidence against the label — do not trust it |
| `IDENTITY TENSIONS FOUND` | mixed signals — investigate the listed items |
| `SERVICE DEGRADATION DETECTED` | model may be genuine, but capped or mutated |
| `CONSISTENT` | no contradictions found — **not** proof of authenticity |
| `INCONCLUSIVE` | no evidence in either direction |
| `ENDPOINT UNAVAILABLE` | dead, not fake |

## What can and cannot be proven

| Method | Strength | Requirement |
|---|---|---|
| Weight inspection / fingerprint embedding | Definitive | Owner cooperation or weights |
| TEE attestation | Definitive | Provider runs attested hardware (rare) |
| Differential vs official endpoint | Strong evidence | Official API key |
| Logprobs distribution comparison | Strong evidence | Both endpoints expose logprobs |
| Capability battery + long-context probes | Moderate | Working endpoint |
| Tokenizer signature (forced output) | Weak (usage is forgeable) | Baseline for comparison |
| Self-report identity / cutoff | Strong *only when contradictory* | Working endpoint |

## Field notes

Real sessions, raw numbers, including the disappointing parts:

- **July 2026** — a NewAPI stack caught by the identity probe alone (model
  confessed to being a different family); a native vendor endpoint passed.
  → [`references/probe-sessions-july2026.md`](references/probe-sessions-july2026.md)
- **October 2026** — a gateway selling a `deepseek/*` label whose route
  self-identified as OpenAI across every sample; logprobs silently stripped on
  both routes; a canonical-answer caveat on battery agreement; and the
  identical-text tokenizer comparison as a WEAK fallback probe.
  → [`references/probe-sessions-october2026.md`](references/probe-sessions-october2026.md)

## Files

```
SKILL.md                        agent-facing instructions, ground rules, workflow
scripts/verify_gateway.py       the collector (stdlib only, selftest included)
references/methodology.md       research basis, probe-to-paper mapping, rules R1-R8
references/probe-sessions-*.md  real field logs with raw numbers
```

## License

MIT — see [LICENSE](LICENSE).
