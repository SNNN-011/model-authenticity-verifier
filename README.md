<div align="center">

# Model Authenticity Verifier

**Is the model behind that gateway really what the label says?**

![version](https://img.shields.io/badge/skill-v3.1-1f6feb)
![python](https://img.shields.io/badge/python-stdlib%20only-3776AB?logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-2ea44f)

Part of the [Everything Claude Code](https://github.com/affaan-m/ECC) skill collection

**[One-page site](https://snnn-011.github.io/model-authenticity-verifier/)** ·
[SKILL.md](SKILL.md) ·
[Methodology](references/methodology.md)

</div>

---

A gateway sells `claude-opus-4-8` at a tenth of the official price. Three
options: it is genuine, it is a smaller model with a bigger label, or it is
the real thing behind a broken proxy. This tool answers the only question a
bearer-key HTTP endpoint can answer honestly: **does the route count tokens
the way the claimed family's tokenizer does?**

## The question

Given a base URL, an API key and a model id, send prompt-only requests and
read the prompt token count from the response's `usage` block. Subtract a
one-character control on the same path. Compare the five deltas against the
measured signature of the claimed model's family.

That is the whole method. One probe family, one comparison, one verdict.

## What it does

- Sends five family-dividing texts plus the control "a" to
  `{base}/chat/completions` (OpenAI dialect) or `{base}/messages` (Anthropic
  dialect, tried on 404/405)
- Reads `usage.input_tokens` or `usage.prompt_tokens`
- Computes `delta(T) = tokens(T) - tokens("a")` per text, which cancels the
  path's template and system-prompt overhead
- With `--repeat`, stores every repeat per text and reports the repeat
  values, min/max, and a `text_unstable` flag. An unstable text is not scored
  as a match or a mismatch; it stays in the N denominator and is counted in
  the note
- Compares the deltas against the measured reference signature of the
  claimed family, per text, with tolerance
  `max(2, ceil(0.08 * reference))`
- Prints an estimated request count before the run; `--max-requests` stops
  the run and reports `PROBE INCOMPLETE` when the budget is exhausted
- Prints a verdict and a full JSON report. The key is never printed and
  never written to the report

> [!IMPORTANT]
> This method cannot prove authenticity. A match is WEAK evidence for the
> counting dialect, never for the weights. See "Honest limits" below.

## Quick start

Python 3, standard library only - no `pip install`, runs on Windows / macOS /
Linux. Three inputs: base URL, API key, model id.

```bash
VERIFY_API_KEY=sk-... python scripts/verify_gateway.py \
    --base https://gw.example.com/v1 \
    --model "anthropic/claude-opus-4.8"
```

The key comes from `VERIFY_API_KEY` or from one line on stdin with
`--key-stdin`. `--key` is refused: command-line arguments are visible in the
process list. On Windows use `python` instead of `python3`; set the key with
`$env:VERIFY_API_KEY = "sk-..."`. Offline logic check (no network):
`python scripts/verify_gateway.py selftest`.

## Reference signatures

Two text sets are in play, and a delta is only comparable within its own set.

The **collector ships one set**: control `a` plus T1 id+emoji, T2 jv+price,
T3 js_code, T4 cjk+emoji+math, T5 arab+misc. The `deepseek` signature below
was measured with exactly those texts on 2026-11-16, so it is the only row
this collector can judge.

The other three rows come from the 2026-10-29 scan, whose text set was never
published. They are reference only: a delta taken from the collector's texts
cannot be compared with them (R6). Measured signatures, anomalies and usage
rules: [`references/tokenizer-baselines-2026-10.md`](references/tokenizer-baselines-2026-10.md).

| family key | models | signature (T1..T5) | text set |
|---|---|---|---|
| `deepseek` | deepseek/deepseek-v4-pro, deepseek/deepseek-v4.1-flash-1017, deepseek-v4.1-flash | 14 20 18 27 22 | collector's, 2026-11-16 |
| `anthropic-core` | anthropic/claude-opus-4.7, claude-opus-4.8, claude-opus-5, claude-sonnet-5 | 45 40 35 45 47 | 2026-10-29, reference only |
| `anthropic-4.6` | anthropic/claude-opus-4.6, anthropic/claude-sonnet-4.6 | 35 34 29 46 47 | 2026-10-29, reference only |
| `openai` | openai/gpt-5.6-terra, openai/gpt-5.6-sol | 24 24 22 30 29 | 2026-10-29, reference only |

Model id to family matching: longest key first, case-insensitive substring
match. `deepseek-v4-flash` has no usable reference (nondeterministic usage
accounting) and maps to no family: `UNMEASURED FAMILY`.

## Verdicts

| Verdict | Meaning |
|---|---|
| `SIGNATURE MATCH` | All N texts agree with the claimed family's reference signature. WEAK FOR: same tokenizer dialect, not proof of weights. |
| `PARTIAL MATCH` | Exactly N-1 agree. One text that misses is an anomaly, not a family split: re-run with fresh texts before concluding anything. |
| `SIGNATURE MISMATCH` | N-2 or fewer agree and at least two stably measured texts miss. The tokenizer does not look like the claimed family's. If unstable texts leave fewer than two stable misses, the result is `UNSTABLE ACCOUNTING`. |
| `UNMEASURED FAMILY` | no reference signature for the claimed model. The deltas are reported raw so they can be baselined later. |
| `UNSTABLE ACCOUNTING` | the control's token count moved between repeats, or unstable texts leave fewer than two stably measured misses (`--repeat` > 1). The route's usage accounting is not stable, so no conclusion is drawn. |
| `PROBE INCOMPLETE` | the route answered but some texts could not be measured. The deltas are partial, so no family verdict is drawn. Re-run. |
| `ENDPOINT UNAVAILABLE` | the endpoint did not answer usefully: timeout, refusal or an HTTP error. The note carries the status. Dead is not fake, and neither is a refusal. |
| `NO USAGE REPORTED` | responses carried no token usage, so the method could not run. |

Decision order: endpoint unavailable > no usage reported > probe incomplete >
unstable accounting > family verdict. A text matches its reference when
`abs(delta - reference) <= max(2, ceil(0.08 * reference))`. A text is
`unstable` when its delta range across repeats exceeds that tolerance (exact
equality when there is no reference). Unstable texts stay in the N
denominator and are counted in the note; a mismatch needs at least two
stably measured misses.

## The probe texts

Five family-dividing texts plus the control `a`. Plain repeated ASCII and
long plain English agree across most families and prove nothing, which is why
these are not in the set.

Control: `a`

```
T1 id+emoji:
Aku menuduh kau berbohong tentang model ini 🔡

T2 jv+price:
Aku tuku sega pecel Rp17.500 ing pasar, rasane enak tenan

T3 js_code:
const total = items.reduce((a, b) => a + b.price, 0);

T4 cjk+emoji+math:
混合中文、日本語とemoji 🌌🚀 と計算: 17×23=391、128÷4=32

T5 arab+misc:
مرحبا بالعالم ١٢٣ Ω≈3.14 ∑(x_i) — naïve café
```

## Honest limits

- **Usage numbers are testimony, not measurement (R1).** A proxy can
  re-tokenize locally and forge `usage`. A match gates the counting dialect,
  never the weights.
- **Only the deepseek signature is comparable.** It was measured with the
  collector's own texts on 2026-11-16. The other three families come from the
  2026-10-29 scan, whose texts were never published, so they are reference
  only (R6).
- **One text proves nothing (R3).** Family agreement is scored across texts:
  5 of 5 is the only clean agreement, 4 of 5 is undecided, 3 or fewer is a
  disagreement. A single text that misses is an anomaly, not a family split:
  `claude-sonnet-5` T5 = 184 (siblings 47) and `claude-opus-4.7` T4 = 46
  (siblings 45) are the known cases.
- **Unstable accounting invalidates a run (R5).** `deepseek-v4-flash` is the
  known case: control "a" moved 22 -> 5 -> 31 across runs and max_tokens.
  It maps to `UNMEASURED FAMILY`. A text that moves across repeats is not
  evidence for either side: it is reported as `unstable`, stays in the
  denominator, and cannot by itself produce `SIGNATURE MISMATCH`.
- **Parameter-independent modifications degrade fingerprints** (wrappers,
  system prompts, RAG; see `references/methodology.md`). A mismatch is a
  reason to investigate, never a conviction.
- **Nothing here proves authenticity.** That requires weights, an
  owner-keyed fingerprint or attested hardware; none of those ship over a
  bearer-key HTTP endpoint.

## Files

```
SKILL.md                              agent-facing instructions, ground rules, workflow
scripts/verify_gateway.py             the collector (stdlib only, selftest included)
references/methodology.md             the research behind the method, WEAK by construction
references/tokenizer-baselines-2026-10.md   measured signatures, per-model deltas, usage rules
references/field-notes-2026-10.md     raw numbers behind the 2026-10-29 baselines
references/field-notes-2026-11.md     the 2026-11-16 run on the collector's own texts
docs/                                 the one-page site (GitHub Pages, no build step)
```

## License

MIT - see [LICENSE](LICENSE).
