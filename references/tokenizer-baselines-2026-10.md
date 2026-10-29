# the gateway tokenizer delta baselines (partial - 11 of 32 models)

Measured 2026-10-29 against `the audited gateway` with the input-side diff probe
(`SKILL.md`, control "a", 5 family-dividing texts, Anthropic dialect
`/v1/messages`, stream off; on 502 a `max_tokens` ladder 16->256->2048).
API key was temporary and revoked after the run; no key material in this repo.
Raw scans live on the audit machine only. Remaining 21 models were NOT scanned
this run (fable + gpt-astra excluded by request; includes haiku/sonnet/opus-5.5).
`deepseek-v4.1-flash-1017` was run separately same day with a per-label
16->2048 fallback after mt16 502 (hidden-reasoning budget burn).

delta(T) = input_tokens(T) - input_tokens("a"), per path, stream off.

## Per-model deltas

| Model | dialek | T1 id+accuse | T2 jv+price | T3 code | T4 cjk+emoji | T5 arab+misc |
|---|---|---|---|---|---|---|
| anthropic/claude-opus-4.6 | messages | 35 | 34 | 29 | 46 | 47 |
| anthropic/claude-sonnet-4.6 | messages | 35 | 34 | 29 | 46 | 47 |
| anthropic/claude-opus-4.7 | messages | 45 | 40 | 35 | 46 | 47 |
| anthropic/claude-opus-4.8 | messages | 45 | 40 | 35 | 45 | 47 |
| anthropic/claude-opus-5 | messages | 45 | 40 | 35 | 45 | 47 |
| anthropic/claude-sonnet-5 | messages | 45 | 40 | 35 | 45 | 184* |
| deepseek/deepseek-v4-pro | messages | 27 | 26 | 23 | 32 | 25 |
| deepseek/deepseek-v4.1-flash-1017 | messages | 27 | 26 | 23 | 32 | 25 |
| deepseek/deepseek-v4-flash | messages | 27 | 88* | 85* | 94* | 87* |
| openai/gpt-5.6-terra | messages | 24 | 24 | 22 | 30 | 29 |
| openai/gpt-5.6-sol | messages | 24 | 24 | 22 | 30 | 29 |

`*` = anomaly, see below.

## Family signatures

- **anthropic core** (opus-4.7/4.8/5, sonnet-5): (45, 40, 35, 45, 47).
  T1 = 45 matches the official Claude Code path of the public 2026-10-18 recipe.
- **anthropic-4.6 branch** (opus-4.6, sonnet-4.6): (35, 34, 29, 46, 47) -
  a distinct signature shared by exactly the 4.6 generation pair. Tentatively
  an older tokenizer branch; unverified, treat as its own signature.
- **openai** (gpt-5.6-terra/sol): (24, 24, 22, 30, 29). T1 = 24 matches the
  public recipe's GPT-family route.
- **deepseek-v4-pro and -v4.1-flash-1017**: (27, 26, 23, 32, 25); T1 = 27
  matches the public recipe's DeepSeek route. The two models report
  different raw control counts (5 vs 31) - different path overhead, equal
  deltas, which is exactly what the subtraction is for.
- **deepseek-v4-flash**: (27, 88, 85, 94, 87) - anomalous. Same T1 as pro but
  3-4x higher on the other four texts; this route also 502s at small
  `max_tokens` (hidden-reasoning budget), so its usage path is not trusted
  until re-run with a larger budget.

## Anomalies

- `sonnet-5` T5 = 184 (siblings 47) and `opus-4.7` T4 = 46 (siblings 45):
  single-text one-offs inside an agreeing family. Do not trust any single
  text; score family agreement over several texts (SKILL.md rule), and
  re-run flagged texts before treating them as family splits.

## Use

When auditing another gateway that serves a claimed model, compare its deltas
against these family signatures. Matching one family on 4+/5 texts = WEAK FOR
(R8 - usage is forgeable). Matching none while landing near an excluded or
unscaned family is disagreement evidence only if the gap exceeds tokenizer
sloppiness margins - re-run with fresh texts before concluding.
