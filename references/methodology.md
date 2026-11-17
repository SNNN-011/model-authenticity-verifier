# Methodology: input-side tokenizer diff - what the research supports

Reference for `model-authenticity-verifier` (v3.1). One method only:
**input-side tokenizer diff against measured family signatures**. This file
records why that method is the one left standing, what the published
literature does and does not licence here, and where the signal runs out.

## TL;DR

- The method reads the token count of the **prompt** from the response's
  `usage` block, subtracts a control, and compares the five deltas against a
  measured per-family signature. It is a tokenizer fingerprint built from
  engineering practice, not from a formal paper.
- The signal it yields is **WEAK by construction**. Usage numbers are
  testimony, not measurement (R1): a proxy can re-tokenize locally with the
  claimed family's tokenizer and forge `usage` without ever serving the
  claimed model. A signature match gates the counting dialect, never the
  weights.
- What proof would require is not available over plain black-box HTTP. Nothing
  short of it should be sold as authenticity verification.

## Why a tokenizer fingerprint works at all

Tokenizers differ between model families: vocabulary size, merge rules,
byte-level fallbacks, handling of mixed script, emoji and code. The same
string almost never tokenizes identically across two families'
tokenizers, and engineering practice (no formal paper) treats the input-side
token count of a fixed text as a cheap, reproducible fingerprint.

Two supporting premises from the literature:

- **Natural fingerprints** (arXiv 2504.14871). Models remain distinguishable
  even when trained on identical data with different seeds. This is the
  premise that makes any black-box comparison meaningful, including this one.
- **Statistical consistency testing** (arXiv 2606.07996). Hypothesis testing
  on hit rates against a reference corpus with significance thresholds. The
  practical reading here: repeat the measurement, and treat instability
  itself as a finding. That is exactly what `--repeat` does. Every probe text
  is measured `--repeat` times and checked against the same tolerance rule as
  a baseline. A text whose count moves beyond tolerance is marked unstable.
  If the control moves, the whole run is discarded (`UNSTABLE ACCOUNTING`,
  R5).

The related control-subtraction step is also engineering practice, not a
paper. `delta(T) = tokens(T) - tokens("a")` on the same path cancels that
path's template and system-prompt overhead, so counts are comparable across
routes without a third control endpoint (R2). Never compare raw token
counts across paths.

## Unstable texts stay in the denominator

Repeat stability is checked per text, not only on the control. An unstable
text is not silently dropped, because dropping it would flatter the
comparison: the route can still show its instability and the sample cannot be
quietly reshaped. Unstable texts therefore stay in the denominator N.

The mismatch rule follows from that:

- a miss only counts when it was measured stably;
- `SIGNATURE MISMATCH` needs at least two stably measured misses (R5);
- `SIGNATURE MATCH` requires every text to be stable and match;
- `PARTIAL MATCH` requires exactly one non-matching text out of at least two,
  and that text may be a stable miss or an unstable text;
- any other result with an unstable text is `UNSTABLE ACCOUNTING`, never a
  mismatch.

Decision order is fixed: `ENDPOINT UNAVAILABLE` > `NO USAGE REPORTED` >
`PROBE INCOMPLETE` > `UNSTABLE ACCOUNTING` > family verdict. A family verdict
is never printed over a failed prerequisite.

## Family-dividing texts

A single text is not a fingerprint (R3). Plain repeated ASCII and long
plain English agree across most families and prove nothing, which is why the
probe set uses only texts that divide families: mixed-script multilingual
prose, emoji sequences, code one-liners, mixed arithmetic. Family agreement
is scored across several texts; single-text outliers inside an agreeing
family are anomalies, not splits. The measured anomalies are recorded in
`field-notes-2026-10.md`.

The collector's built-in texts are public stand-ins for the 2026-10-29
measured set (id+accuse, jv+price, code, cjk+emoji, arab+misc), not the
same bytes (R6). The active baselines in `baselines/` were measured on
2026-11-16 with the collector's own built-in texts, so for those families
the comparison happens inside one text set. The 2026-10-29 signatures
remain reference only: a delta taken from the collector's texts cannot be
compared with them.

## Baselines and fail-closed loading

Active baselines live in `baselines/*.json`. Each active file carries
`schema_version`, `family`, `models`, `models_measured`, `models_inferred`,
`probe_set_id`, `signature`, `control_range`, `provenance` (endpoint kind,
routes, date, run count, notes) and optionally `confidence`. The
`probe_set_id` is computed from `PROBES` by the collector (sha256 over the
control and probe texts, truncated to 8 hex chars) and stored in the file;
at load time the loader recomputes it from the current probe set and refuses
the file when they differ. Changing one byte of the probe texts invalidates
every active baseline by design.

`models_measured` lists the models seen on the collector's own text set;
`models_inferred` lists models mapped through the family without a direct
measurement (e.g. deepseek-v4-pro, HTTP 402 in the 2026-11-16 field notes;
claude-opus-4.6, absent from the 2026-11-16 field notes). Both must be lists
of non-empty strings, disjoint from each other, with a union equal to
`models`; `models_measured` must not be empty. The loader fails closed
otherwise, naming the file.

Loading is fail closed. Corrupt JSON, an unreadable file, a non-object
root, an unknown `schema_version`, a signature whose length does not match
the probe set, a `probe_set_id` mismatch, a duplicate family, an active
file marked superseded or a file whose models do not map back to its
family all raise an explicit error naming the file; the run exits 1 before
any network request. Nothing is skipped silently. Every numeric field is
guarded: booleans, floats and negative values are rejected in `signature`,
`control_range`, `provenance.n_runs` and `schema_version` (which must equal
1). `control_range` is a note, never a verdict input: when the measured
control falls outside it, the note records the fact and the verdict is
unchanged (R2).

`baselines/_reference_only.json` carries family and model names only,
never a signature. Rows there are for lookup and the historical record
(the superseded 2026-10-29 deepseek and anthropic-4.6 values live under
`superseded_signature`); the loader ignores `superseded_signature` for
scoring. A family that appears only in `_reference_only.json` yields
`UNMEASURED FAMILY` with a note naming the file and citing R6; the deltas
are reported raw so they can be baselined later.

Every family verdict that uses an active baseline (`SIGNATURE MATCH`,
`PARTIAL MATCH`, `SIGNATURE MISMATCH`, `UNSTABLE ACCOUNTING`, including
the control-instability and family-unstable cases) carries a
third-party-gateway caveat: a wrapper can re-tokenize locally or forge
usage, so a match is WEAK evidence for the counting dialect, never the
weights (R1). When the requested model sits in that baseline's
`models_inferred`, the note also carries "model ini belum diukur langsung
pada text set ini; dipetakan lewat keluarga". When the baseline's
`confidence` is `single-route`, the note also carries "single-route
caveat: the signature was measured on a single route, so a route-specific
wrapper cannot be ruled out (R1)".

The `anthropic-4.6` baseline comes from a single vikey run
(anthropic/claude-sonnet-4.6, control 23). Its values 18 25 20 36 38 are
read as deltas, by analogy with the deepseek row in
`field-notes-2026-11.md`, not as raw token counts; the assumption is
recorded in the file's provenance notes. Treat it as single-route,
low-confidence evidence; every verdict that uses this baseline carries the
single-route caveat.

## The fingerprint-degradation caveat (arXiv 2508.19843)

The SoK on LLM copyright auditing via fingerprinting introduces LeaFBench
(149 model instances, 13 post-development techniques, including
parameter-independent ones such as system-prompt conditioning and RAG) and
shows that parameter-independent modifications measurably degrade fingerprint
accuracy.

Consequence here: gateways wrap models. A wrapper, system prompt or
re-tokenizer sits between the caller and the model, and each of those can
shift what gets counted or how. So:

- a **match** is corroboration, never confirmation;
- a **mismatch** is grounds for investigation, never conviction;
- the method cannot separate "wrong model" from "wrapped right model with
  degraded fingerprinting" without additional evidence.

Treat every family verdict with this caveat attached.

## What proof would require (not available over plain black-box HTTP)

- **Weights.** Inspection or an embedded keyed fingerprint requires owner
  cooperation (e.g. arXiv 2401.12255). Not usable against a third-party
  gateway.
- **TEE / hardware attestation.** AttestLLM (arXiv 2509.06326) verifies
  on-device model integrity via attestation. Confidential-computing
  deployments such as Petridish (arXiv 2409.19134) run inference inside
  enclaves with signed receipts. Only an attested runtime produces proof a
  caller can check; almost no public gateway offers one.
- **Attested provenance for APIs.** AEX (arXiv 2603.14283) audits shadow
  APIs and notes that existing methods - fingerprinting, model-equality
  testing, verifiable inference, TEE attestation - "either remain
  inferential or answer different questions". The evidence-bound
  gateway-path provenance work (arXiv 2606.22560) formalises the threat:
  gateways decide routing, fallback and stream manipulation, and may forge
  provenance.

Over black-box HTTP with a bearer key, none of this is reachable. The survey
taxonomy (arXiv 2605.29245) frames the situation cleanly: keyed verification
can be definitive; similarity-based attribution is statistical. This method
is the statistical kind.

## Research -> probe -> verdict

| Research / basis | Probe mechanism | Verdict effect |
|---|---|---|
| Tokenizer engineering practice | `usage.input_tokens` / `usage.prompt_tokens` deltas per text | all family verdicts |
| Control subtraction (engineering practice) | `delta(T) = tokens(T) - tokens("a")`, same path (R2) | makes cross-route deltas comparable |
| Natural fingerprints (arXiv 2504.14871) | premise: families tokenise distinguishably | `SIGNATURE MATCH` / `SIGNATURE MISMATCH` (WEAK) |
| Statistical consistency (arXiv 2606.07996) | `--repeat`, control and every text compared across repeats (R5) | `UNSTABLE ACCOUNTING` when the control moves or no stable verdict is possible |
| Parameter-independent degradation (arXiv 2508.19843) | caveat on every reading | caps match at WEAK FOR; mismatch = investigate, not convict |
| AEX / gateway-provenance threat (arXiv 2603.14283, 2606.22560) | reason the method cannot prove identity | report wording: WEAK FOR / mismatch, never stronger than WEAK FOR |

## Explicit statement on signal strength

This method yields a **WEAK signal by construction**. It can show that a
route counts tokens the way a claimed family's tokenizer does. It cannot show
that the route serves that family's weights, and it cannot exclude a proxy
that keeps the claimed family's tokenizer for counting while serving
something else entirely. Any stronger claim requires weights, an owner-keyed
fingerprint, or attested hardware, none of which a bearer-key HTTP endpoint
exposes.

## Honest limitations

- Usage is forgeable end to end; every verdict is capped at WEAK (R1).
- Only the two active baselines were measured with the collector's own
  texts; every other family is reference only and its 2026-10-29 numbers
  cannot be compared with the collector's deltas (R6).
- A route with nondeterministic usage accounting cannot be fingerprinted at
  all (`UNSTABLE ACCOUNTING`); `deepseek-v4-flash` is the known case.
- No statistical significance machinery beyond the tolerance rule and the
  per-text repeat check; no confidence intervals are computed or printed.
- `--max-requests` bounds the run, so a truncated run reports
  `PROBE INCOMPLETE` rather than borrowing a verdict from partial data.

## Sources

- 2504.14871 - Natural Fingerprints of Large Language Models. https://arxiv.org/abs/2504.14871
- 2606.07996 - MC-PDD: black-box pretraining-data detection via statistical hit rates. https://arxiv.org/abs/2606.07996
- 2508.19843 - SoK: LLM copyright auditing via fingerprinting; LeaFBench; black-box untargeted/targeted taxonomy. https://arxiv.org/abs/2508.19843
- 2401.12255 - Instructional Fingerprinting of Large Language Models (requires owner cooperation). https://arxiv.org/abs/2401.12255
- 2509.06326 - AttestLLM: hardware attestation for on-device LLM integrity. https://arxiv.org/abs/2509.06326
- 2409.19134 - Petridish: confidential-computing deployment for LLM inference. https://arxiv.org/abs/2409.19134
- 2603.14283 - AEX: non-intrusive multi-hop attestation and provenance for LLM APIs (shadow-API audits). https://arxiv.org/abs/2603.14283
- 2606.22560 - Evidence-bound gateway-path provenance for third-party LLM inference. https://arxiv.org/abs/2606.22560
- 2605.29245 - Survey: fingerprinting/watermarking taxonomy (keyed verification vs similarity-based attribution). https://arxiv.org/abs/2605.29245
