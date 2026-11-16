# Field notes: October 2026 measurements

Raw numbers that still matter. Everything here feeds either
`tokenizer-baselines-2026-10.md` or the verdict logic. Identity probes,
capability batteries and other probe families are out of scope and are not
recorded here.

## Identical-text check (2026-10-29, session 1)

The same 100,024-character text on two routes returned `prompt_tokens`
20,177 vs 20,182: a delta of 5 tokens (0.025%). Input tokenization is
effectively identical across those routes.

Caveat (R6): a gateway that re-tokenizes locally with one tokenizer for all
routes will show exactly this, so identical counts on one text say nothing
about the upstream model. The check only shows that the counting path is the
same, never what is behind it.

## Forced-output comparison (refused)

Two routes produced identical visible output with `completion_tokens` 68
(2.09 chars/token) vs 31 (4.58). Both outside the 3.0-5.5 sanity band for
normal prose-plus-code. The client-side cross-check declined to compare the
routes, so no tokenizer signature was taken from that run.

## Cross-provider check (2026-10-29)

`deepseek-ai/DeepSeek-V4.1-Flash-1017` on **api.river.ai**, OpenAI dialect,
prompt-only, `max_tokens` 16, control "a" = 5 tokens: deltas
(27, 26, 23, 32, 25), a 5/5 match against the measured deepseek family
signature. Same tokenizer family across providers. Proves the counting
dialect, not the weights (R1).

## deepseek-v4-flash retest (2026-10-29, two passes)

Control "a" moved 22 -> 5 -> 31 across runs and `max_tokens`. T2 moved
110 -> 31. At `max_tokens` 2048, T4 computed to 6 against a 31-token
control. Usage accounting on this route is nondeterministic, so no usable
reference signature exists for it. It maps to `UNMEASURED FAMILY`, never to a
verdict.

## Anomalies inside agreeing families

- `claude-sonnet-5` T5 = 184 (siblings 47)
- `claude-opus-4.7` T4 = 46 (siblings 45)

Single-text one-offs inside otherwise agreeing families. Not family
splits. Re-run flagged texts before treating them as anything else (R3).

## 4-text delta battery (2026-10-18 recipe)

One family matched 4/4 while wrong-family routes read 1.2-1.9x lower.
Evidence that family-dividing texts separate tokenizer families; not evidence
about weights.
