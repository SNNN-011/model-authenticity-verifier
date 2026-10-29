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
cases needs a third control route, which was not run in this session. The
session-2 input-side diff (baseline subtraction per path) removes the need for
the control route to interpret overhead.

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

---

## Session 2: input-side tokenizer diff via public recipe (2026-10-18, field note)

Method circulated publicly (a public audit post post, date published with the raw delta
table). Recorded here as the field basis for the manual probe in SKILL.md.

### Recipe, as published

- On each path to be compared, send two prompt-only messages, `"stream": false`:
  the probe text T, then the control "a" (1 token).
- delta(T) = input_tokens(T) - input_tokens("a") per path; the subtraction
  cancels that path's template/system overhead, so raw counts need not line up
  and no second control route is required.
- Compare deltas across paths, not raw counts.

### Published delta table (2026-10-18), claim: Claude Opus 5.5 via Anthropic dialect

| Probe text | Official Anthropic path | suspect path | DeepSeek-family route |
|---|---|---|---|
| Indonesian accusation sentence + clown emoji | 45 | 45 | 27 |
| Javanese + food, price "Rp17.500" | 42 | 42 | 29 |
| JS one-liner (`items.reduce(`) | 36 | 36 | 30 |
| Mixed CJK + emoji + arithmetic | 33 | 33 | 20 |

4/4 delta match on the suspect path; the DeepSeek-family tokenization reads
1.2-1.9x lower per text. On the first text the GPT-6.x and Qwen-family routes
read 24/25 - different families give different deltas, family agreement over
several texts is the signal.

### Lessons (folded into SKILL.md)

- Baseline subtraction ("a") kills the overhead-size problem session 1's
  identical-text check hit: that check needed a third control route to explain
  a 5-token gap; the "a"-baseline needs nothing extra.
- Probe selection is a failure mode in itself: repeated plain ASCII ("aaaa")
  tokenizes the same across families and proves nothing. The differentiator is
  multilingual mixed-script + emoji + code text (1.2-1.9x family spread here).
- Works when completions are capped/mutated - prompt-only input, no forced
  output needed - and it is two cheap requests per text.
- Still R8-WEAK: counts can be forged by counting with the claimed family's
  tokenizer kept locally; match gates the dialect, not the weights. Repeat the
  text set (4 different texts here) before calling anything "consistent".
