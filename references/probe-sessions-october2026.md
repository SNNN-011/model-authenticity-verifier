# Probe Sessions — October 2026

Reference data from v2.2 field sessions. Read alongside
`probe-sessions-july2026.md` (v1 data — v1 scored self-report as *positive*
evidence; v2.2 does not, see `methodology.md` R5/R6).

---

## Session 1: the audited gateway (multi-vendor gateway — label mismatch found)

**Date:** 2026-10-17
**Collector:** `scripts/verify_gateway.py` v2.2 (selftest 75/75)
**Key material:** redacted (test keys were revoked after the session)

### Catalog
- 32 models across vendors (`openai/*`, `deepseek/*`, `claude/*`, `glm/*`,
  `qwen/*`, `xiaomi/*`)
- Both `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4.1-flash` listed

### `deepseek/deepseek-v4-flash` — CONTRADICTIONS FOUND (strong)

| Probe | Result | Reading |
|---|---|---|
| Identity self-report | 5+ samples self-identified as OpenAI / ChatGPT / GPT | contradiction (R5: one-directional; a model can claim anything, so agreement is never scored, only mismatch) |
| Cutoff | inconsistent across samples | contradiction |
| Capability battery | 8/8 | service OK |
| Dual needle (25% / 75%, 120k chars) | PASS (`prompt_tokens` 22,357) | service OK |
| Throughput | ~180 tok/s | telemetry |
| Small budgets | HTTP 502 `empty_response_error` / `reasoning_exhausted_budget` at `max_tokens` 5 and 10; a 600-word task also 502'd at 4,000 | hidden reasoning burns the budget |

**Cause undetermined** (misrouting vs contaminated weights). It is route-level,
not gateway-wide: a discriminator run on other routes of the same gateway
returned the correct vendor (`claude/*` → Anthropic, `glm/*` → Z.ai,
`qwen/*` → Alibaba), so no blanket prompt injection is applied. One spot-check
was mixed rather than clean: `xiaomi/mimo-v2.6-pro` self-identified as Anthropic
in 1 of 3 samples and as MiMo in the other 2 — tension, not strong evidence.

### `openai/gpt-6-luna` — INCONCLUSIVE

| Probe | Result |
|---|---|
| Identity self-report | 2/2 MATCH_CLAIM — not scored as positive evidence by design (R5) |
| Capability battery | 8/8 |
| Dual needle | PASS (`prompt_tokens` 22,042) |
| Throughput / overhead | ~51 tok/s; prompt overhead ~18 tokens |
| Cutoff | 2024-06, consistent across samples |

### Cross-route differential: `deepseek/deepseek-v4-flash` (suspect) vs `openai/gpt-6-luna` (baseline)

Same gateway, `--seed 42`, `--probes caps --skip-needle --logprobs`.

- Battery: 8/8 vs 8/8, exact-output match 6/8 → ledger `[MODERATE][FOR]`.
  **Weak in practice:** nearly every battery answer is canonical (a single
  number, letter, or word), so any two competent models agree on most items. A
  separate baseline-only run (same seed) differed from the suspect on one item
  only (`count3`, a free-choice list of capital cities) — the agreement signal
  is mostly structural, not a same-model tell.
- Forced-output cross-check: both repeated the 142-character text exactly, but
  `completion_tokens` = 68 (2.09 chars/token) for the suspect vs 31 (4.58) for
  the baseline. Identical visible output, ~37 extra counted tokens → hidden
  reasoning is included in the suspect route's `completion_tokens`. The
  tokenizer comparison was skipped because the suspect ratio sits outside the
  3.0-5.5 sanity band — the client-side cross-check refused to compare apples
  to oranges, as designed.
- Open-ended temperature 0: word Jaccard 0.46 → inconclusive band, no signal.
- Logprobs: **neither route returned logprobs** — the gateway silently strips
  the feature on both endpoints. The strongest non-attested probe is therefore
  unavailable here; recorded as a service observation, not identity evidence.

### Identical-text tokenization check (standalone, outside the collector)

Same 100,024-character text to both routes, temperature 0, `max_tokens` 2500:

| Route | `prompt_tokens` | Latency |
|---|---|---|
| `openai/gpt-6-luna` | 20,177 | 4.6 s |
| `deepseek/deepseek-v4-flash` | 20,182 | 4.8 s |

Delta = **5 tokens (0.025%)**. Two genuinely different tokenizers do not land
5 tokens apart over 100k characters of English — a real mismatch runs 1-2%, i.e.
hundreds to thousands of tokens — so the 5-token gap is template /
system-prompt sized, and the input tokenization is effectively identical.
**Caveat (R6):** if the gateway re-tokenizes locally with a single tokenizer for
all routes, this says nothing about the upstream model. Distinguishing the two
cases needs a third control route, which was not run in this session.

### Lessons

- On gateways that strip logprobs, the identical-text `prompt_tokens`
  comparison is the best remaining tokenizer probe — but it is WEAK (R6) and
  only meaningful against a control route.
- Reasoning-budget gateways 502 at small `max_tokens`; the differential and
  logprobs probes use 2,500 for that reason.
- High exact-output agreement on a capability battery is not strong evidence of
  same-model when most items have canonical answers. Weight by item type.
- `completion_tokens` inflation on identical visible output is a useful
  hidden-reasoning tell on OpenAI-compatible routes.
