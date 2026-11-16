# the gateway tokenizer delta baselines (partial - 11 of 32 models)

Measured 2026-10-29 against `the audited gateway` with the input-side
diff probe (control "a", 5 family-dividing texts, Anthropic dialect
`/v1/messages`, stream off; on 502 a `max_tokens` ladder 16->256->2048).

The scan used the 2026-10-29 text set, whose exact strings live only on the
audit machine. What is known publicly is the composition: id+accuse,
jv+price, code, cjk+emoji, arab+misc. The collector's built-in texts are
public stand-ins with the same composition but different bytes, so an exact
match is indicative, and a deviation has to be re-measured before it counts
(R6). API key was temporary and revoked after the run; no key material in
this repo. Raw scans live on the audit machine only. Remaining 21 models
were NOT scanned this run (fable + gpt-astra excluded by request; includes
haiku/sonnet/opus-5.5). `deepseek-v4.1-flash-1017` was run separately same
day with a per-label 16->2048 fallback after mt16 502 (hidden-reasoning
budget burn).

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

- **anthropic-core** (claude-opus-4.7/4.8/5, claude-sonnet-5): (45, 40, 35, 45, 47).
  T1 = 45 matches the official Claude Code path of the public 2026-10-18 recipe.
- **anthropic-4.6 branch** (claude-opus-4.6, claude-sonnet-4.6): (35, 34, 29, 46, 47) -
  a distinct signature shared by exactly the 4.6 generation pair. Tentatively
  an older tokenizer branch; unverified, treat as its own signature.
- **openai** (gpt-5.6-terra/sol): (24, 24, 22, 30, 29). T1 = 24 matches the
  public recipe's GPT-family route.
- **deepseek** (deepseek-v4-pro and deepseek-v4.1-flash-1017): (27, 26, 23, 32, 25); T1 = 27
  matches the public recipe's DeepSeek route. The two models report
  different raw control counts (5 vs 31) - different path overhead, equal
  deltas, which is exactly what the subtraction is for.
- **deepseek-v4-flash**: (27, 88, 85, 94, 87) on the first scan - **superseded
  by a retest, not usable as a reference**. Retest 2026-10-29 (two passes):
  at mt16 the deltas read 27, 26, 23, (T4 502), 26 - family-consistent;
  raw usage is unstable across runs/max_tokens (control "a" 22 -> 5 -> 31;
  T2 110 -> 31; at mt2048 T4 computes to 6 against a 31-token control, which
  contradicts its mt16 value). Treat this route's input accounting as
  nondeterministic (SERVICE finding): do not fingerprint it until it is stable.
  It maps to `UNMEASURED FAMILY`, never to a family verdict.

## Anomalies

- `sonnet-5` T5 = 184 (siblings 47) and `opus-4.7` T4 = 46 (siblings 45):
  single-text one-offs inside an agreeing family. Do not trust any single
  text; score family agreement over several texts (R3), and re-run flagged
  texts before treating them as family splits.

## How to use

Compare a route's five deltas against the claimed family's signature. A text
matches when `abs(delta - reference) <= max(2, ceil(0.08 * reference))`.

- Match on 4+/5 texts within tolerance: `SIGNATURE MATCH` (WEAK FOR, R1).
- Match on 2-3/5 texts: `PARTIAL MATCH`. Re-run with fresh texts before
  concluding anything.
- Match on 0-1/5 texts: `SIGNATURE MISMATCH`.
- No reference for the claimed family: `UNMEASURED FAMILY`; the deltas are
  reported raw so they can be baselined later.

If the control's token count moves between repeats (`--repeat` > 1), the
route's usage accounting is not stable and no conclusion is drawn:
`UNSTABLE ACCOUNTING` (R5). Re-run with fresh texts before concluding.

## Cross-provider check (2026-10-29)

`deepseek-ai/DeepSeek-V4.1-Flash-1017` on **api.river.ai** (OpenAI dialect,
prompt-only, mt16, control 5) measured deltas `(27, 26, 23, 32, 25)` -
5/5 match with the measured deepseek family signature above. Same tokenizer
family across providers; proves the counting dialect, not the weights (R1).

See `field-notes-2026-10.md` for the raw numbers behind these entries.
