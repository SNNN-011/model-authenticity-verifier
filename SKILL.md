---
name: model-authenticity-verifier
description: "Audit a third-party LLM API/gateway with an honest black-box evidence model - infrastructure and service checks, one-directional self-report probes, a seedable capability battery, differential testing against a trusted baseline, and an optional logprobs distribution probe. Use when a cheap or free gateway claims to serve a specific model, or to check for hidden prompt injection, output caps, or feature stripping."
---

# Model Authenticity Verifier (v2.2)

Audit what a third-party LLM gateway *actually* does: what infrastructure it runs,
what it modifies about your requests and responses, how capable the served model is,
and whether any evidence contradicts the claimed model identity.

This is v2.2. v1 scored self-report probes as positive evidence and claimed
"6/6 signals = real model". That was wrong (see Anti-Patterns). v2.2 follows the
published black-box auditing research: **similarity-based evidence against a
baseline**, one-directional self-report, strict separation of identity vs service
findings, and explicit acknowledgment that authenticity is not provable without
weights, logprobs, or TEE attestation. New in v2.2: seedable dynamic battery,
dual-depth needle (25%/75%), logprobs distribution probe (--logprobs), client-side
anti-forgery cross-check on token counts, latency/throughput telemetry.

## When to Activate

- A cheap/free gateway claims to serve a specific famous model (Claude, GPT, Gemini, ...)
- You suspect prompt injection, hidden output caps, or feature stripping (reasoning removed, etc.)
- You want to compare a suspect endpoint against the official vendor endpoint
- Before making a gateway your primary provider

## When NOT to Activate

- You have no API key / cannot send test traffic to the endpoint
- The question is purely about price - just read the official pricing page
- You expect a binary "real/fake" answer - black-box methods give evidence, not proof

## Ground Rules (epistemic contract)

1. **Self-report is one-directional.** If the model names a *different* family than
   the claim, that is real evidence (STRONG AGAINST). If it names the *right* family,
   that is NOT evidence - one injected system prompt produces any identity. Never
   score agreement as positive.
2. **Wrapper fields identify the gateway stack, not the model.** `base_resp`,
   `anyrouter_metadata`, `providerChain`, `cost`, `type:new_api_error` tell you which
   proxy software is running. A clean body does not prove a genuine model.
   `system_fingerprint: fp_*` is an OpenAI-native field - if it appears while the
   claim is a non-OpenAI model, that is a provenance hint worth noting.
3. **Capability results need a baseline.** A pass/fail battery on the suspect alone
   only detects weak substitutes. The strongest available check is differential
   testing: same prompts against the official vendor endpoint, compare outputs,
   pass rates, and token counts at temperature 0.
4. **Repetition first.** All self-report probes are sampled twice - answers that
   change between identical requests are themselves a finding.
5. **Dead is not fake.** Consistent timeouts = endpoint unavailable. Report it as
   that, not as rejection.
6. **Never hardcode keys.** Use `--key` or `VERIFY_API_KEY`.
7. **Identity and service findings never mix.** An output cap, a mutated response,
   or a failed long-context recall is a SERVICE finding (`AGAINST-SERVICE`). It can
   never drive the identity verdict. A capped genuine model is still genuine.
8. **Usage numbers are testimony, not measurement.** `completion_tokens` is only
   believed after a client-side cross-check: the response text must actually match
   the forced prompt, and chars/token must sit in the 3.0-5.5 band. Cross-checked
   token equality = WEAK FOR, never more (a proxy can re-tokenize and forge usage).

## What can actually be proven, and how

| Method | Strength | Requirement |
|---|---|---|
| Weight inspection / fingerprint embedding | Definitive | Model owner cooperation or weight access |
| TEE attestation (signed inference) | Definitive (current state of art) | Provider runs attested hardware (rare) |
| **Differential testing vs official endpoint** | Strong evidence | Official API key for the claimed model |
| Logprobs distribution comparison (`--logprobs`) | Strong evidence | Both endpoints expose logprobs (gateways often strip it) |
| Tokenizer signature (completion_tokens on forced output, or input-side diff - see manual probe below) | Weak (usage is forgeable; cross-checked before use) | Baseline for comparison |
| Capability battery + context/long-output probes | Moderate (detects tier gaps) | Working endpoint |
| Behavioral/style profiling | Moderate (top-tier discrimination) | Careful analysis, converging capabilities |
| Gateway field/header fingerprint | Infrastructure attribution only | Working endpoint |
| Self-report identity / cutoff | STRONG only when contradictory | Working endpoint |

Important caveat from the research: parameter-independent modifications (system
prompts, RAG, wrappers) *degrade* fingerprint reliability, and top-tier models
converge on core capabilities - so style/structure agreement carries information
that raw capability scores do not. See `references/methodology.md`.

## Input-side tokenizer diff (manual probe - no forced output needed)

The collector's tokenizer comparison runs from the output side (completion_tokens
of a forced response). The input side works too, from `usage.input_tokens`
(Anthropic dialect) or `usage.prompt_tokens` (OpenAI dialect), so it also covers
gateways that cap or mutate completions. Two prompt-only requests per item:

1. On the SAME path, send the probe text T, then a 1-token control ("a").
   Both prompt-only, `"stream": false`.
2. delta(T) = tokens(T) - tokens("a"). The subtraction cancels that path's
   template / system-prompt overhead, so no third control route is needed
   (contrast: the identical-text check in October-2026 session 1).
3. Compare deltas across paths - suspect gateway vs official endpoint or a
   documented per-family reference table. Matching deltas = same tokenizer.

Field lessons (public recipe, 2026-10-18, see session notes):

- Pick texts that DIVIDE families: mixed-script multilingual prose, emoji
  sequences, code one-liners, mixed arithmetic. A 4-text delta battery matched
  the official path 4/4 while wrong-family routes read 1.2-1.9x lower; one
  text is not a fingerprint, family agreement over several texts is.
- Plain repeated ASCII ("aaaa") and long plain English prose agree across most
  families - they prove nothing. Weird text only.
- Terminal rule stays R8: usage is computed by whatever counts on the gateway
  side, and a determined proxy can count with a locally kept copy of the claimed
  family's tokenizer without ever serving it. Delta match = WEAK FOR; it gates
  the dialect, not the weights.

## Workflow

```
1. Economic sanity check (30 sec, do this first)
   Official pricing vs gateway pricing. "Opus for $0.001/M" is not a model,
   it is a story. Also check: does the gateway list a suspiciously large model count?

2. Infrastructure + service audit (2-3 min)
   python scripts/verify_gateway.py --base <URL> --model <ID> --key <K> \
       --probes infra,service
   Records: wrapper fields, x-headers, model echo, prompt-token overhead,
   reasoning passthrough, hidden output caps.

3. Capability battery (3-5 min)
   ... --probes caps [--needle-chars 120000]
   A seedable randomized battery + dual-depth needle (25% and 75% into the
   haystack). A claimed frontier model that fails multi-step arithmetic or
   30k-token recall has answered the question already. The seed is printed and
   the full battery spec lands in the JSON report - rerun with --seed N to
   reproduce exactly.

4. Self-report probes (1 min)
   ... --probes self
   One-directional. Contradiction = STRONG AGAINST. Agreement = NEUTRAL.

5. Differential testing (the strongest check, when you have official access)
   ... --baseline-base https://api.<vendor>.com/v1 --baseline-model <OFFICIAL_ID> \
       --baseline-key <OFFICIAL_KEY> [--logprobs]
   Compares pass rates, exact-output agreement at temperature 0, and
   completion_tokens on a forced output (tokenizer signature, cross-checked
   against the raw text before it is scored). With --logprobs it additionally
   compares the top-1 next-token and |delta logprob| on short prompts.

6. Read the ledger. Verdict semantics:
   - CONTRADICTIONS FOUND       -> do not trust the label
   - IDENTITY TENSIONS FOUND    -> investigate the listed items
   - SERVICE DEGRADATION        -> model may be genuine but capped/mutated
   - CONSISTENT                 -> no contradictions; NOT proof of authenticity
   - INCONCLUSIVE               -> no evidence either way
```

## Running the script

```bash
# Full probe, no baseline (finds contradictions, cannot prove identity)
python scripts/verify_gateway.py --base https://gw.example.com/v1 \
    --model "claude-opus-4-8" --key "$KEY"

# With differential testing against the official endpoint
python scripts/verify_gateway.py --base https://gw.example.com/v1 --model M --key K \
    --baseline-base https://api.<vendor>.com/v1 --baseline-model M --baseline-key OK

# Add the logprobs distribution probe (baseline required)
... --baseline-base ... --baseline-model ... --logprobs

# Reproduce a previous dynamic battery/needle exactly
... --seed 123456

# Offline logic test (no network except localhost)
python scripts/verify_gateway.py selftest

# JSON report for later diffing (includes seed + full battery spec)
... --out report.json
```

Notes: base URL should normally include `/v1`; the script auto-tries `+ /v1` on 404.
Key is masked in output; `VERIFY_API_KEY` / `VERIFY_BASELINE_API_KEY` env vars work too.
Default sleep 2s between requests (raise with `--sleep` for free gateways).
429 responses are retried twice honoring `Retry-After`. `--static-battery` falls back
to the fixed calibration battery.

## Interpreting results

Two separate outputs, deliberately not merged into one score:

1. **Observations** - factual infrastructure/service findings (wrapper fields,
   overhead tokens, caps, reasoning dialect, feature stripping). Read like a
   mechanic's report.
2. **Identity evidence ledger** - items tagged `[STRONG|MODERATE|WEAK] [FOR|AGAINST|NEUTRAL]`,
   plus service-only items tagged `AGAINST-SERVICE`. Only `AGAINST` items change
   your trust decision; `FOR` items raise confidence but never to certainty,
   and `AGAINST-SERVICE` items explain caps/restrictions without touching identity.

The honest summary: a cheap gateway that passes everything you measured is
**"no contradiction found"** - not **"verified genuine"**. A gateway with an output
cap but no identity contradiction reports **SERVICE DEGRADATION DETECTED** - the
model may be exactly what it claims, just throttled.

## Anti-Patterns (lessons from v1 - do not repeat)

- Scoring self-report identity/cutoff as +1 points. Injectable in one prompt. Banned.
- "6/6 signals = REAL NATIVE" verdicts. 6 weak/infrastructural signals do not sum to proof.
- Treating `system_fingerprint: fp_*` as a wrapper marker - it is OpenAI's own field.
- Treating `cached_tokens` or reasoning-field absence as wrapper evidence - both are
  native behaviors of multiple vendors.
- Single-shot probes for identity/cutoff. Sample twice; inconsistency is a finding.
- Letting a service finding (output cap, mutated text) print as an identity
  contradiction. A throttled genuine model is not a fake model. The render keeps
  `AGAINST` and `AGAINST-SERVICE` strictly apart.
- Trusting usage-derived `completion_tokens` without a client-side cross-check
  (text must match the forced prompt; chars/token must be in band). Even then it
  is WEAK evidence, because a proxy can re-tokenize locally and forge usage.
- Treating `logprobs` output as proof. A proxy can compute plausible logprobs;
  treat a match as strong non-attested evidence, never as attestation.
- Prompt-extraction "jailbreaks" as a verification method (ToS risk, unreliable).
- Summing everything into one score that hides which signals were weak.

## Pitfalls

- **SSE by default:** some gateways stream unless `"stream": false` is set. The script
  always sets it; if SSE still arrives it reports it as an error for that probe.
- **Reasoning eats max_tokens:** heavy-reasoning models can consume the whole budget
  in `reasoning_content`, leaving `content` empty. The script uses generous budgets
  (1000+) for self-report probes; empty content plus `finish_reason: length` is a
  retry-with-more-tokens situation, not a finding. Some gateways instead return
  HTTP 502 `reasoning_exhausted_budget` on tiny budgets - the differential and
  logprobs probes use 2500 to survive that.
- **Rate limits:** free gateways 429 aggressively. The script retries twice, honoring
  the server's `Retry-After` header (fallback 15s/30s); raise `--sleep` if probes
  keep failing. Differential probes issue suspect/baseline calls as a parallel pair
  (2 concurrent), which doubles instantaneous request rate - raise `--sleep` on
  strict gateways.
- **Logprobs support varies:** some OpenAI-compatible gateways accept `logprobs: true`
  and silently drop it, others 400. When the baseline returns logprobs but the
  suspect does not, that is recorded as possible feature stripping - it is a service
  observation, NOT identity evidence.
- **Grok rule is case-aware, not magic:** `grok-2`/`Grok 3` (digit forms) always
  match; capitalized `Grok` as a mid-sentence noun matches; lowercase verb `grok`
  never counts. Exotic casing can still fool any regex heuristic - verify borderline
  hits by eye before acting on them.
- **Path conventions:** `/v1/chat/completions` vs `/chat/completions`. Pass base with
  `/v1`; the script auto-corrects once on 404.
- **python3 vs python on Windows:** run with whichever exists; the script is plain
  stdlib Python 3, no dependencies.

## Files

- `scripts/verify_gateway.py` - the collector (stdlib only, cross-platform, has selftest)
- `references/methodology.md` - the research basis and evidence model in detail
- `references/probe-sessions-july2026.md` - v1 field notes (historical raw data;
  checkout the Xiaomi and iamhc sessions as calibration examples)
- `references/probe-sessions-october2026.md` - v2.2 field notes (a gateway that
  strips logprobs, the weak-battery-agreement wash-out check, and the
  identical-text tokenizer comparison as a fallback probe)

## Related

- `llm-provider-config` (if present in your setup) for wiring a verified provider
- `security-review` for key handling and secret hygiene
