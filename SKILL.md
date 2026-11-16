---
name: model-authenticity-verifier
description: "Audit a third-party LLM gateway with one black-box method: input-side tokenizer diff against measured family signatures, from a prompt-only request and a control, compared per-family. Use when a cheap or free gateway claims to serve a specific model and you want to know whether the route counts tokens like that family. Three inputs: base URL, API key, model id."
---

# Model Authenticity Verifier (v3.0)

One method, stated plainly: send five family-dividing probe texts plus a
one-character control to the endpoint, read the prompt token count from
`usage`, subtract the control, and compare the five deltas against the
measured signature of the claimed model's family.

This is v3.0. v2.x carried six probe families (infrastructure audit, service
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
    nothing can be concluded from that run (`UNSTABLE ACCOUNTING`).
R6. **The text set is part of the measurement.** The reference signatures
    were measured on 2026-10-29 with a text set of the same composition
    (id+accuse, jv+price, code, cjk+emoji, arab+misc) whose exact strings
    live only on the audit machine. The collector's built-in texts are public
    stand-ins with the same composition, so an exact match is indicative, and
    a deviation has to be re-measured before it counts.

## Workflow

```
1. Collect three inputs: base URL, API key, model id. Nothing else is needed.
   python scripts/verify_gateway.py --base <URL> --model <ID> --key <KEY>

2. Read the report. The verdict comes with per-text deltas, the tolerance
   rule and the family comparison. Key material never appears in the report.
   Optional flags: --sleep S (default 2), --timeout S (default 60),
   --repeat N (default 1; enables the R5 stability check),
   --out FILE (full JSON), --texts FILE (alternate text set, reference
   comparison skipped). Offline logic check: `python scripts/verify_gateway.py
   selftest`, exits 0 on pass.

3. Decide. 4-5/5 matches is `SIGNATURE MATCH` (WEAK FOR); 2-3 is
   `PARTIAL MATCH`, re-run with fresh texts; 0-1 is `SIGNATURE MISMATCH`.
   If anything is ambiguous, re-run with `--repeat` > 1 or `--texts` before
   drawing a conclusion. The exit code is 0 for any verdict, 1 for usage or
   CLI errors, 2 for selftest failure.
```

## Reading the Verdicts

| Verdict | Meaning |
|---|---|
| `SIGNATURE MATCH` | 4 or 5 of 5 texts agree with the claimed family's reference signature. WEAK FOR: same tokenizer dialect, not proof of weights. |
| `PARTIAL MATCH` | 2 or 3 of 5 agree. Mixed. Re-run with fresh texts before concluding anything. |
| `SIGNATURE MISMATCH` | 0 or 1 of 5 agree. The tokenizer does not look like the claimed family's. |
| `UNMEASURED FAMILY` | no reference signature for the claimed model. The deltas are reported raw so they can be baselined later. |
| `UNSTABLE ACCOUNTING` | the control's token count moved between repeats (only checked with `--repeat` > 1). The route's usage accounting is not stable, so no conclusion is drawn. |
| `ENDPOINT UNAVAILABLE` | the endpoint did not answer. Dead, not fake. |
| `NO USAGE REPORTED` | responses carried no token usage, so the method could not run. |

Decision order: endpoint unavailable > no usage reported > unstable
accounting > family verdict.

A text matches its reference when
`abs(delta - reference) <= max(2, ceil(0.08 * reference))`.

## What This Can and Cannot Show

| Can show | Cannot show |
|---|---|
| Whether the route counts tokens like the claimed family's tokenizer | Whether the route serves that family's weights |
| That usage accounting is stable or not (`--repeat`) | That a match is anything stronger than WEAK FOR |
| Which family a route's deltas sit closest to | Exact identity when the family is unmeasured or accounting is nondeterministic |
| Deviation from a measured signature, per text | Why a deviation exists (wrong model, wrapper, re-tokenizer, forged usage) |

## Reference Signatures

Family signature = 5 deltas in the order T1..T5. Measured 2026-10-29 against
the audited gateway with the 2026-10-29 text set (same composition as the
built-ins, different bytes; see R6).

| family key | models | signature |
|---|---|---|
| `anthropic-core` | anthropic/claude-opus-4.7, claude-opus-4.8, claude-opus-5, claude-sonnet-5 | 45 40 35 45 47 |
| `anthropic-4.6` | anthropic/claude-opus-4.6, anthropic/claude-sonnet-4.6 | 35 34 29 46 47 |
| `openai` | openai/gpt-5.6-terra, openai/gpt-5.6-sol | 24 24 22 30 29 |
| `deepseek` | deepseek/deepseek-v4-pro, deepseek/deepseek-v4.1-flash-1017 | 27 26 23 32 25 |

Model id to family matching: longest key first, case-insensitive substring
match on the model id. `deepseek-v4-flash` has no usable reference
(nondeterministic usage accounting, see `references/field-notes-2026-10.md`)
and maps to no family: `UNMEASURED FAMILY`.

## Limits

- Usage is forgeable; a determined proxy can count with a copied tokenizer.
  Every family verdict is WEAK FOR/AGAINST at best.
- The built-in texts are stand-ins for the measured 2026-10-29 set (R6).
- Known anomaly routes: `claude-sonnet-5` T5 and `claude-opus-4.7` T4, single
  texts inside agreeing families. Re-run flagged texts.
- `deepseek-v4-flash` is not fingerprintable with this method today (R5).
- This is not proof of authenticity. Nothing black-box is. Weights, an
  owner-keyed fingerprint or attested hardware are required, and none of
  those ship over a bearer-key HTTP endpoint.
- Provenance note: a match means "this route counts tokens like the claimed
  family", never "this route serves the claimed model".

## Files

- `scripts/verify_gateway.py` - the collector (stdlib only, has selftest)
- `references/methodology.md` - the research basis, WEAK by construction
- `references/tokenizer-baselines-2026-10.md` - measured signatures, usage rules
- `references/field-notes-2026-10.md` - raw numbers behind the baselines
