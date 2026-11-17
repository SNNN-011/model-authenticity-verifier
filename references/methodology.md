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

The probe set was measured on 2026-10-29 against the audited gateway, with a
text set whose exact strings live only on the audit machine. The collector's
built-in texts are public stand-ins with the same composition (id+accuse,
jv+price, code, cjk+emoji, arab+misc), not the same bytes (R6). An exact
match is therefore indicative, and a deviation has to be re-measured before
it counts.

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
| AEX / gateway-provenance threat (arXiv 2603.14283, 2606.22560) | reason the method cannot prove identity | report wording: WEAK FOR / mismatch, never "verified genuine" |

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
- The built-in texts are stand-ins for the measured set; exact-match
  indications degrade gracefully into "re-measure before you conclude" (R6).
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
