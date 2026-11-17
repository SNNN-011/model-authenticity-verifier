---
name: model-authenticity-verifier
description: "Audit a third-party LLM gateway with one black-box method: input-side tokenizer diff against measured family signatures, from a prompt-only request and a control, compared per-family. Use when a cheap or free gateway claims to serve a specific model and you want to know whether the route counts tokens like that family. Three inputs: base URL, API key, model id."
---

# Model Authenticity Verifier (v3.1)

One method, stated plainly: send five family-dividing probe texts plus a
one-character control to the endpoint, read the prompt token count from
`usage`, subtract the control, and compare the five deltas against the
measured signature of the claimed model's family.

This is v3.1. v2.x carried six probe families (infrastructure audit, service
checks, a seedable capability battery, dual-depth needle, self-report,
differential and logprobs testing). All of that is gone. What remains is the
single method below, which is cheap, needs no baseline endpoint and works on
routes that cap or mutate completions. It yields a WEAK signal, every time,
by construction.

## When to Use

- A cheap or free gateway claims to serve a specific model and you want a
  fast first read on whether the route counts tokens like that family
- You have only a base URL, a key and a model id
- The route caps or mutates completions, so output-side checks are useless

## When NOT to Use

- You need proof of authenticity: impossible over plain black-box HTTP, with
  or without this tool. See `references/methodology.md`
- The endpoint reports no token usage in its responses: the method cannot
  run, verdict `NO USAGE REPORTED`
- The claimed model's family has no measured reference: verdict
  `UNMEASURED FAMILY`, raw deltas only
- The question is price, latency or marketing: not this tool's job

## Ground Rules

R1. **Usage numbers are testimony, not measurement.** A proxy can
    re-tokenize locally and forge `usage`. A signature match is WEAK
    evidence: it gates the counting dialect, never the weights.
R2. **Subtract the control.** `delta(T) = tokens(T) - tokens("a")` on the
    same path cancels that path's template and system-prompt overhead, so no
    third control route is needed. Never compare raw counts across paths.
R3. **Never trust one text.** Score family agreement across several texts.
    Plain repeated ASCII and long plain English agree across most families
    and prove nothing, which is why the probe set uses family-dividing texts
    only. Single-text one-offs inside an agreeing family are anomalies, not
    family splits.
R4. **Dead is not fake.** Consistent timeouts are reported as
    `ENDPOINT UNAVAILABLE`, never as rejection.
R5. **Unstable accounting invalidates a run.** If the control count moves
    between repeats, the route's usage numbers are not deterministic and
    nothing can be concluded from that run (`UNSTABLE ACCOUNTING`). A text
    whose delta moves beyond tolerance is not scored as a match or a
    mismatch: it is reported as `unstable`, stays in the N denominator, and
    is counted in the note. A family mismatch needs at least two stably
    measured misses; with fewer than two, the result is `UNSTABLE ACCOUNTING`,
    not `SIGNATURE MISMATCH`.
R6. **The text set is part of the measurement.** The active baselines were
    measured on 2026-11-16 with the collector's own built-in texts and are
    stored in `baselines/*.json`; the loader recomputes a `probe_set_id`
    from the probe set and refuses any baseline whose stored id does not
    match, so the comparison is only ever made inside one text set. The
    older 2026-10-29 signatures were measured with a text set of the same
    composition (id+accuse, jv+price, code, cjk+emoji, arab+misc) whose
    exact strings live only on the audit machine. Those rows are reference
    only and live in `baselines/_reference_only.json`, which never supplies
    a signature; a family that appears only there yields `UNMEASURED FAMILY`
    and the deltas are reported raw.

## Workflow

```
1. Collect three inputs: base URL, API key, model id. Nothing else is needed.
   The key comes from env VERIFY_API_KEY, or from one line on stdin with
   --key-stdin. --key is refused because argv is visible in the process list.
   VERIFY_API_KEY=... python scripts/verify_gateway.py --base <URL> --model <ID>

2. Read the report. The verdict comes with per-text deltas, the tolerance
   rule and the family comparison. Key material never appears in the report.
   With --repeat N, every repeat is stored per text and the report shows the
   repeat values, min/max and a text_unstable flag. Optional flags:
   --key-stdin, --sleep S (default 2), --timeout S (default 60),
   --repeat N (default 1; enables the R5 stability check),
   --max-requests N (stop and report PROBE INCOMPLETE when the request budget
   is exhausted; the estimate is printed before the run),
   --out FILE (full JSON), --texts FILE (alternate text set, reference
   comparison skipped), --with-consistency (also run the consistency suite;
   notes only, the verdict is unchanged), --stream (with --with-consistency,
   add the streaming check). Suite-only: `verify_gateway.py consistency
   --base <URL> --model <ID> [--stream]`. Offline logic check:
   `python scripts/verify_gateway.py selftest`, exits 0 on pass.

3. Decide. All N texts match: `SIGNATURE MATCH` (WEAK FOR). Exactly N-1
   match: `PARTIAL MATCH`; re-run with fresh texts. N-2 or fewer match with
   at least two stably measured misses: `SIGNATURE MISMATCH`. If unstable
   texts leave fewer than two stable misses, the result is
   `UNSTABLE ACCOUNTING`, not a mismatch. For the default N=5 this is 5/5
   MATCH, 4/5 PARTIAL, and 3/5 or fewer MISMATCH when at least two stable
   texts miss. If anything is ambiguous, re-run with `--repeat` > 1 or
   `--texts` before drawing a conclusion. The exit code is 0 for any verdict,
   1 for usage or CLI errors, 2 for selftest failure.
```

## Reading the Verdicts

| Verdict | Meaning |
|---|---|
| `SIGNATURE MATCH` | All N texts agree with the claimed family's reference signature. WEAK FOR: same tokenizer dialect, not proof of weights. |
| `PARTIAL MATCH` | Exactly N-1 agree. One text that misses is an anomaly, not a family split (R3): re-run with fresh texts before concluding anything. |
| `SIGNATURE MISMATCH` | N-2 or fewer agree and at least two stably measured texts miss. The tokenizer does not look like the claimed family's. If unstable texts leave fewer than two stable misses, the result is `UNSTABLE ACCOUNTING` instead. |
| `UNMEASURED FAMILY` | no reference signature for the claimed model. The deltas are reported raw so they can be baselined later. |
| `UNSTABLE ACCOUNTING` | the control's token count moved between repeats, or unstable texts leave fewer than two stably measured misses (only checked with `--repeat` > 1). The route's usage accounting is not stable, so no conclusion is drawn. |
| `PROBE INCOMPLETE` | the route answered but some texts could not be measured. The deltas are partial, so no family verdict is drawn. Re-run. |
| `ENDPOINT UNAVAILABLE` | the endpoint did not answer usefully: timeout, refusal or an HTTP error. The note carries the status. Dead is not fake, and neither is a refusal. |
| `NO USAGE REPORTED` | responses carried no token usage, so the method could not run. |

Decision order: endpoint unavailable > no usage reported > probe incomplete >
unstable accounting > family verdict.

A text matches its reference when
`abs(delta - reference) <= max(2, ceil(0.08 * reference))`. A text is
`unstable` when its delta range across repeats exceeds the same tolerance
(exact equality when there is no reference). Unstable texts stay in the N
denominator and are reported in the note, never silently dropped.

## Usage consistency

The report carries a `consistency` block. It compares the input-side units a
response reports, control vs probe: `input_tokens`, `prompt_tokens` and
`total_minus_output` (`total_tokens` minus the completion or output tokens,
when both are reported and the result is not negative). `input_tokens` is
skipped for a response that reported cache units, because cached tokens are
excluded from it; `input_plus_cache` is recorded but never compared. A
response whose comparable units differ, and any negative delta, are recorded
as notes only: they are appended to the `note` string and never change the
verdict (R1, R5). When every delta in every unit is a multiple of 5 (and at
least one delta exists), the note gains `usage_suspect` with the
`quantization_k`: the largest k in the ladder 1, 2, 3, 5, 8 dividing every
delta; 0 when there are no deltas. Every HTTP attempt, retries and
max_tokens ladder rungs included, passes through the request budget, and the
estimated request count is printed before the run starts.

## Consistency suite (notes only)

`--with-consistency` runs a second suite after the probe run; `python
scripts/verify_gateway.py consistency --base <URL> --model <ID>` runs only
the suite. Both are notes only (R1): no result changes the verdict. Every
request passes through the request budget, and the estimate is printed
before the first request. The family verdict is byte-identical with and
without `--with-consistency`.

- **Usage linearity vs repetition.** Three unit texts (ASCII, CJK-Japanese
  mix, emoji) at repetition counts k = 1, 2, 3, 5, 8. Text for k is the unit
  text joined into k newline-separated repetitions; `delta = tokens(text_k)
  - tokens(control)`, control measured once. Each unit fits
  `statistics.linear_regression(k_values, deltas)`; the report records k
  list, deltas, slope, intercept, max residual, monotonic_increasing and a
  residual tolerance `max(2, ceil(0.08 * max(deltas)))`. `usage_suspect` is
  set when the series is constant, slope is `<= 0`, the deltas are not
  strictly increasing, or the max residual exceeds the tolerance; the reason
  says which. This k is a repetition count, unrelated to the
  `quantization_k` ladder above.
- **Determinism.** The same unit text is sent twice and the input-side
  token counts compared exactly. A difference is recorded and noted, never
  a verdict change.
- **Streaming (`--stream`).** The same unit text is sent once non-streaming
  and once with `"stream": true`. The SSE body is parsed (lines starting
  with `data:`; the final object carrying `usage` wins; `[DONE]` ignored)
  and its input-side count is compared with the non-streaming value. If the
  route rejects streaming or answers without a usable usage block,
  `agree` is null and the note says `stream unavailable`.
- **Side evidence.** `Server`, `Via` and `X-Request-Id` from the first
  consistency response are recorded, plus the model id the server echoes vs
  the requested model (`model_matches`, case-insensitive). A differing echo
  is a side-evidence note only (R1).

The `consistency` report block gains `linearity`, `determinism`, `stream`
(only with `--stream`), `side_evidence` and `notes`. The consistency-only
path prints `estimated consistency requests: 16 (1 control + 3 units x 5 k
values; plus 2 determinism, plus 2 when --stream, plus 0 for headers
because they ride on existing responses)` before the first request;
`--with-consistency` prints the probe estimate then a combined line, e.g.
`estimated requests: 22 (6 probe + 16 consistency)`. Consistency
exhaustion of the budget stops the suite and records `consistency
incomplete: request budget exhausted` as a note only.

## What This Can and Cannot Show

| Can show | Cannot show |
|---|---|
| Whether the route counts tokens like the claimed family's tokenizer | Whether the route serves that family's weights |
| That usage accounting is stable or not (`--repeat`) | That a match is anything stronger than WEAK FOR |
| Which family a route's deltas sit closest to | Exact identity when the family is unmeasured or accounting is nondeterministic |
| Deviation from a measured signature, per text | Why a deviation exists (wrong model, wrapper, re-tokenizer, forged usage) |

## Reference Signatures

Family signature = 5 deltas in the order T1..T5. The collector ships one text
set (control `a` plus T1 id+emoji, T2 jv+price, T3 js_code, T4 cjk+emoji+math,
T5 arab+misc). Active baselines live in `baselines/*.json` and were measured
with exactly those texts; each stores a `probe_set_id` that the loader
recomputes from `PROBES` at load time, so changing one byte of the probe
texts invalidates every active baseline by design. Loading is fail closed: a
corrupt, malformed or mismatched baseline file stops the run with an error
naming the file (exit 1); nothing is skipped silently. The older rows come
from the 2026-10-29 scan, whose text set was never published: they live in
`baselines/_reference_only.json` (reference only, never a signature, not
comparable with the collector's deltas, R6). The old 2026-10-29 `deepseek`
and `anthropic-4.6` rows are kept there as superseded history.

| family key | models | signature | text set |
|---|---|---|---|
| `deepseek` | deepseek/deepseek-v4.1-flash-1017, deepseek-v4.1-flash (measured); deepseek/deepseek-v4-pro (inferred: HTTP 402 on this text set) | 14 20 18 27 22 | collector's, 2026-11-16 |
| `anthropic-4.6` | anthropic/claude-sonnet-4.6 (measured); anthropic/claude-opus-4.6 (inferred: not in the 2026-11-16 field notes) | 18 25 20 36 38 | collector's, 2026-11-16, single vikey run; values read as deltas by analogy with the deepseek row |

Reference-only (`baselines/_reference_only.json`): `anthropic-core`
(45 40 35 45 47), `openai` (24 24 22 30 29), and the superseded 2026-10-29
`deepseek` (27 26 23 32 25) and `anthropic-4.6` (35 34 29 46 47) rows.
Reference-only entries carry family/model names only and never a signature;
a family that appears only there yields `UNMEASURED FAMILY` with an R6 note.
Every verdict that uses an active baseline (`SIGNATURE MATCH`,
`PARTIAL MATCH`, `SIGNATURE MISMATCH`, `UNSTABLE ACCOUNTING`) carries a
third-party-gateway caveat: a wrapper can re-tokenize locally or forge
usage, so this is WEAK evidence for the counting dialect, never the weights
(R1).

Active baselines also enumerate `models_measured` and `models_inferred`
(schema_version stays 1): disjoint lists of non-empty strings whose union
equals `models`, with `models_measured` non-empty; the loader fails closed
otherwise, naming the file. When the requested model is in the baseline's
`models_inferred`, the verdict note appends "model ini belum diukur langsung
pada text set ini; dipetakan lewat keluarga"; when the baseline's
`confidence` is `single-route`, the note appends "single-route caveat: the
signature was measured on a single route, so a route-specific wrapper cannot
be ruled out (R1)". Numeric loader fields reject booleans, floats and
negative values (`signature`, `control_range`, `provenance.n_runs`,
`schema_version`). `control_range` is a note, never a verdict input: an
out-of-range control is recorded in the note and the verdict is unchanged
(R2).

Model id to family matching: longest key first, case-insensitive substring
match on the model id. `deepseek-v4-flash` has no usable reference
(nondeterministic usage accounting, see `references/field-notes-2026-10.md`)
and maps to no family: `UNMEASURED FAMILY`.

## Limits

- Usage is forgeable; a determined proxy can count with a copied tokenizer.
  Every family verdict is WEAK FOR/AGAINST at best.
- Only the two active baselines (deepseek, anthropic-4.6) were measured with
  the collector's own texts; every other family is reference only (R6).
- Inside those baselines, `models_measured` lists the models seen on this
  text set; `models_inferred` lists models mapped through the family and
  carries the inferred-model caveat in the verdict note.
- Known anomaly routes: `claude-sonnet-5` T5 and `claude-opus-4.7` T4, single
  texts inside agreeing families. Re-run flagged texts.
- `deepseek-v4-flash` is not fingerprintable with this method today (R5).
- A text that moves across repeats is not evidence for either side. It stays
  in the denominator and is counted in the note; a family mismatch needs at
  least two stably measured misses.
- This is not proof of authenticity. Nothing black-box is. Weights, an
  owner-keyed fingerprint or attested hardware are required, and none of
  those ship over a bearer-key HTTP endpoint.
- Provenance note: a match means "this route counts tokens like the claimed
  family", never "this route serves the claimed model".

## Files

- `scripts/verify_gateway.py` - the collector (stdlib only, has selftest)
- `baselines/` - active baselines (`deepseek.json`, `anthropic-4.6.json`) and `_reference_only.json` (family/model lookup only, never a signature)
- `references/methodology.md` - the research basis, WEAK by construction
- `references/tokenizer-baselines-2026-10.md` - measured signatures, usage rules
- `references/field-notes-2026-10.md` - raw numbers behind the 2026-10-29 baselines
- `references/field-notes-2026-11.md` - the 2026-11-16 run on the collector's own texts
