# Field notes: November 2026 measurements

Raw numbers from the 2026-11-16 run of the rebuilt collector. The method is
the input-side diff: control `a` plus the five family-dividing texts, OpenAI
dialect, prompt-only, `max_tokens` ladder 64 -> 512 -> 2048.

The texts are the ones the collector ships, so these deltas are comparable
with the `deepseek` signature in `tokenizer-baselines-2026-10.md`. They are not
comparable with the 2026-10-29 numbers, which came from a text set that was
never published.

## DeepSeek family routes, 2026-11-16

| route | control | T1 | T2 | T3 | T4 | T5 |
|---|---|---|---|---|---|---|
| sumopod `deepseek-v4.1-flash-1017` | 5 | 14 | 20 | 18 | 27 | 22 |
| sumopod `deepseek-v4.1-flash-1017` (second run) | 5 | 14 | 20 | 18 | 26 | 22 |
| tokenharbor `deepseek-v4.1-flash:free` | 5 | 14 | 20 | 18 | 26 | 22 |
| tokenharbor `deepseek-v4-flash:free` | 5 | 14 | 20 | 18 | 26 | 22 |
| vikey `deepseek/deepseek-v4.1-flash-1017` | 31 | 14 | 20 | 18 | 26 | 22 |
| yeta `deepseek-v4.1-flash` | 31 | 14 | 20 | 18 | 26 | 22 |

Family signature used by the collector: **14 20 18 27 22**, the T4 reading of
the two runs that agree with the older baseline. T4 was 26 on four routes and
27 on two, which is inside the tolerance (max(2, ceil(0.08 x 27)) = 3) and
does not change any verdict.

## One text that misses: tokenharbor's block

On one tokenharbor run, `deepseek-v4.1-flash:free` reported T1 = 45 prompt
tokens against a 5-token control, delta 40 instead of 14: a block of about 26
tokens that no other route showed. It did not come from the request headers:
the same text read 45 with a plain User-Agent, with the collector's
User-Agent, and with the Anthropic headers as well. Token Harbor falls back
between upstream providers for the same model, so the counting template can
change between calls.

That is the case the verdict thresholds exist for: 4 of 5 texts is
`PARTIAL MATCH`, not `SIGNATURE MATCH`. One text that misses is an anomaly,
not a family split.

## Anthropic and other families on the same texts

| route | control | T1 | T2 | T3 | T4 | T5 |
|---|---|---|---|---|---|---|
| vikey `anthropic/claude-sonnet-4.6` | 23 | 18 | 25 | 20 | 36 | 38 |

The claude-opus-4.7 / 4.8 / 5 branch was not measured on this text set, so the
collector has no live signature for it: `UNMEASURED FAMILY`.

## Paid routes that could not be measured

| route | answer |
|---|---|
| tokenharbor `deepseek-v4-pro`, `v4-flash`, `v4.1-flash`, `v4.1-flash-1017`, `-fast`, `-ultrafast`, `v3.2`, `r1-0528` | HTTP 402, no balance on the key |
| openrouter `anthropic/claude-opus-5`, `claude-sonnet-5.5`, `openai/gpt-5.6-luna-pro`, `deepseek/deepseek-v4.1-flash-1017`, `z-ai/glm-5.3-flash` | HTTP 402, no credits |
| tokenharbor `deepseek-v4.1-flash-1017-free` | HTTP 404, not a model id |

## Not measured on purpose

The `deepseek-v4-flash` route stays without a reference: its usage accounting
is nondeterministic (see `field-notes-2026-10.md`), so it maps to no family.
