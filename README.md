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
options: it is what the label says, it is a smaller model with a bigger label, or it is
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
T3 js_code, T4 cjk+emoji+math, T5 arab+misc. Active baselines live in
`baselines/*.json` and were measured with exactly those texts. Each active
file stores a `probe_set_id` computed from the probe set at load time; the
loader recomputes it from `PROBES` and refuses the file when they differ.
Changing one byte of the probe texts invalidates every active baseline by
design.

| family key | models | signature (T1..T5) | text set |
|---|---|---|---|
| `deepseek` | deepseek/deepseek-v4.1-flash-1017, deepseek-v4.1-flash (measured); deepseek/deepseek-v4-pro (inferred: HTTP 402 on this text set) | 14 20 18 27 22 | collector's, 2026-11-16 (`baselines/deepseek.json`) |
| `anthropic-4.6` | anthropic/claude-sonnet-4.6 (measured); anthropic/claude-opus-4.6 (inferred: not in the 2026-11-16 field notes) | 18 25 20 36 38 | collector's, 2026-11-16, single vikey run (`baselines/anthropic-4.6.json`) |

The `anthropic-4.6` values come from one vikey run (control 23) and are read
as deltas, by analogy with the deepseek row in `references/field-notes-2026-11.md`,
not as raw token counts; that assumption is recorded in the file's
provenance notes.

The older rows come from the 2026-10-29 scan, whose text set was never
published. They live in `baselines/_reference_only.json` and are reference
only: a delta taken from the collector's texts cannot be compared with them
(R6). Reference-only entries carry family/model names only, never a
signature; a family that appears only there yields `UNMEASURED FAMILY` with
an R6 note. The old `deepseek` (27 26 23 32 25) and `anthropic-4.6`
(35 34 29 46 47) 2026-10-29 rows are kept there as superseded history.
Measured signatures, anomalies and usage rules:
[`references/tokenizer-baselines-2026-10.md`](references/tokenizer-baselines-2026-10.md).

Baseline loading is fail closed: corrupt JSON, an unreadable file, a
non-object root, an unknown `schema_version`, a wrong signature length, a
`probe_set_id` mismatch, a duplicate family or a malformed file stops the
run with an error naming the file (exit 1); nothing is skipped silently.
Every numeric field is guarded: booleans, floats and negative values are
rejected in `signature`, `control_range`, `provenance.n_runs` and
`schema_version`. Every verdict that uses an active baseline (`SIGNATURE MATCH`,
`PARTIAL MATCH`, `SIGNATURE MISMATCH`, `UNSTABLE ACCOUNTING`) carries a
third-party-gateway caveat: a wrapper can re-tokenize locally or forge
usage, so this is WEAK evidence for the counting dialect, never the weights
(R1).

Active baselines also enumerate `models_measured` and `models_inferred`
(schema_version stays 1): both must be lists of non-empty strings, disjoint
from each other, with a union equal to `models`, and `models_measured` must
not be empty; the loader fails closed otherwise, naming the file. A verdict
that uses an active baseline appends a caveat when the requested model is in
that baseline's `models_inferred` ("model ini belum diukur langsung pada
text set ini; dipetakan lewat keluarga"), and, when the baseline's
`confidence` is `single-route`, a second caveat ("single-route caveat: the
signature was measured on a single route, so a route-specific wrapper cannot
be ruled out (R1)"). `control_range` is a note, never a verdict input: when
the measured control falls outside it, the note records the fact and the
verdict is unchanged (R2).

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

## Usage consistency

The report carries a `consistency` block beside the verdict. It compares the
input-side units a response reports, control vs probe: `input_tokens`,
`prompt_tokens` and `total_minus_output` (`total_tokens` minus the completion
or output tokens, when both are reported and the result is not negative).
`input_tokens` is skipped for a response that reported cache units
(`cache_read_input_tokens` / `cache_creation_input_tokens`), because cached
tokens are excluded from it; `input_plus_cache` is recorded but never
compared. A response where two or more comparable units are present and they
differ is recorded as a disagreement. A negative delta (probe reports fewer
tokens than the control on the same unit) is also recorded as a failure.

Both of these are notes only. They are appended to the `note` string, never
used to pick or change a verdict (R1, R5). When every delta in every unit is a
multiple of 5 and at least one delta exists, the note gains `usage_suspect`
with the quantization step: the largest k in the ladder 1, 2, 3, 5, 8 that
divides every delta. `quantization_k` is 0 when there are no deltas.

Every HTTP attempt, including retries and the max_tokens ladder rungs, passes
through the request budget (`--max-requests`). The estimated request count is
printed before the run starts; it counts the control plus the texts per
repeat and does not include retries or ladder rungs.

## Consistency suite (notes only)

Beside the probe run, the collector can measure three more properties of a
route's usage accounting. Everything here is a note: no result changes the
verdict (R1). Every HTTP attempt passes through the request budget
(`--max-requests`), and the estimate is printed before the first request.

Run only the suite:

```bash
python scripts/verify_gateway.py consistency --base URL --model ID \
    [--key-stdin] [--sleep] [--timeout] [--max-requests] [--out FILE] [--stream]
```

Or run it after a probe run with `--with-consistency` on the normal command.
The family verdict is byte-identical with and without the flag; the suite's
results are appended to the report under `consistency["linearity"]`,
`consistency["determinism"]`, `consistency["stream"]` (only with `--stream`),
`consistency["side_evidence"]` and `consistency["notes"]`, and a short
`consistency:` block is printed after the verdict. Combined with `--out`, the
combined estimate is reflected in `budget.estimated`.

### Usage linearity vs repetition

Three plain unit texts are measured at repetition counts
`k = 1, 2, 3, 5, 8`: an ASCII sentence, a CJK-Japanese mix and an emoji
sequence. The text for `k` is the unit text joined into `k` newline-separated
repetitions, measured on the same path as everything else. `delta =
tokens(text_k) - tokens(control)` with the control measured once. For each
unit, `statistics.linear_regression(k_values, deltas)` fits the repetition
series; the report records `slope`, `intercept`, `max_residual` (max
`|delta - (slope*k + intercept)|`), `monotonic_increasing` (strictly
increasing at every step), the residual tolerance
`max(2, ceil(0.08 * max(deltas)))`, and a `usage_suspect` flag with a short
reason. A unit is flagged `usage_suspect` when its series is constant, its
slope is `<= 0`, the deltas are not strictly increasing, or the max residual
exceeds the tolerance. This `k` is a repetition count, unrelated to the
`quantization_k` ladder of the usage-unit consistency check.

### Determinism

The same unit text (the ASCII one) is sent twice and the two input-side
token counts are compared exactly. A difference is recorded
(`deterministic: false`) and noted; it never changes a verdict (R1).

### Streaming (`--stream`)

The same unit text is sent once non-streaming and once with
`"stream": true`. The SSE body is parsed (lines starting with `data:`; the
final object carrying `usage` wins; `[DONE]` is ignored) and its input-side
token count is compared with the non-streaming value. If the route does not
answer the streaming request with a usable usage block, `agree` is null and
the note says `stream unavailable`. Never changes the verdict (R1).

### Side evidence

The headers on the first consistency response are recorded (`Server`,
`Via`, `X-Request-Id`), together with the model id the server echoes in the
response body versus the model that was requested (`model_matches`,
case-insensitive). A differing echo is recorded as a side-evidence note
only (R1). Side evidence alone never changes a verdict.

### Budget and estimate

Every consistency request goes through the same `RequestBudget` as the
probes; exhaustion stops the suite and records `consistency incomplete:
request budget exhausted` as a note only. The consistency-only path prints
`estimated consistency requests: 16 (1 control + 3 units x 5 k values; plus
2 determinism, plus 2 when --stream, plus 0 for headers because they ride
on existing responses)` before the first request; `--with-consistency`
prints the probe estimate followed by a combined line, e.g.
`estimated requests: 22 (6 probe + 16 consistency)`. Headers ride on
existing responses, so they add no requests.

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
- **Only the two active baselines are comparable.** `deepseek` and
  `anthropic-4.6` were measured with the collector's own texts on 2026-11-16
  and live in `baselines/`; `anthropic-4.6`'s values are read as deltas by
  analogy with the deepseek row. The other families come from the 2026-10-29
  scan, whose texts were never published, so they are reference only (R6).
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
- **Per-model status is part of the baseline.** `models_measured` were seen
  on the collector's own texts; `models_inferred` are mapped through the
  family and carry the caveat above. An inferred reading is one step further
  from the measurement.
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
baselines/                            active baselines (deepseek, anthropic-4.6) plus _reference_only.json
references/methodology.md             the research behind the method, WEAK by construction
references/tokenizer-baselines-2026-10.md   measured signatures, per-model deltas, usage rules
references/field-notes-2026-10.md     raw numbers behind the 2026-10-29 baselines
references/field-notes-2026-11.md     the 2026-11-16 run on the collector's own texts
docs/                                 the one-page site (GitHub Pages, no build step)
```

## License

MIT - see [LICENSE](LICENSE).
