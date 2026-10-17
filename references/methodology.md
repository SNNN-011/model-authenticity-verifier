# Methodology: verifying a model behind an API - what research actually supports

Reference for `model-authenticity-verifier` (v2.2). Summarizes the published black-box
auditing literature and maps each technique to the probes in `scripts/verify_gateway.py`.

## TL;DR

- Black-box model verification is **evidence accumulation and hypothesis testing**, not proof.
  Proof needs weights, a vendor-embedded keyed fingerprint, or TEE attestation.
- The strongest practical black-box method for a user holding an official API key is
  **differential testing against the official endpoint** (output-space similarity).
- **Self-report is one-directional**: contradiction is evidence, agreement is not.
- Wrapping a model (system prompts, RAG, templates) *degrades* fingerprint reliability -
  and that is exactly what gateways do. So "no match to baseline" is weaker evidence
  than a positive contradiction.
- Core capabilities of top-tier models converge; **style and behavioral structure** is
  where top-tier discrimination lives.

## Taxonomy

- Fingerprinting vs watermarking: watermarking proves text came from a watermarked
  model; fingerprinting tries to *identify* the model behind a black box without
  cooperation. Keyed verification can be definitive; similarity-based attribution is
  statistical. (Survey: arXiv 2605.29245.)
- White-box (weights/gradients) vs black-box (queries only). Black-box splits into
  untargeted (what is this?) and targeted (is this model X?).
  (SoK: arXiv 2508.19843.)
- The SoK's LeaFBench benchmark (149 model instances, 13 post-development techniques
  including parameter-independent ones such as system-prompt conditioning and RAG)
  shows parameter-independent modifications measurably degrade fingerprint accuracy.
  Consequence for this skill: a gateway injecting a system prompt can shift behavior
  away from the stock model, so capability drift alone is not proof of substitution.

## Techniques that work black-box (ranked by evidence strength)

1. **Output-space similarity / differential testing** (arXiv 2407.01235).
   Model outputs occupy a characteristic subspace; comparing victim vs suspect output
   distributions across the same prompts separates models. Practical version: run the
   same deterministic battery against suspect and official endpoints at temperature 0
   and compare pass rates, exact outputs, and token counts.
2. **Behavioral profiling** (arXiv 2509.04504).
   Diagnostic prompt suites + judging produce behavioral profiles (style, alignment,
   refusals). Caveat from the same field: raw capabilities converge among top models,
   so profile *shape* discriminates where scores do not.
3. **Statistical consistency testing** (arXiv 2606.07996).
   Hypothesis testing on hit rates against a reference corpus with significance
   thresholds. Practical version: repeat probes; inconsistency between identical
   requests is itself a finding.
4. **Natural fingerprints** (arXiv 2504.14871).
   Models remain distinguishable even when trained on the same data with different
   seeds - the premise that makes black-box comparison meaningful at all.
5. **Tokenizer / usage signatures** (engineering practice, no formal paper needed).
   `completion_tokens` for the same forced output is effectively a tokenizer
   fingerprint; different tokenizers almost never agree on a 100+ char string.
6. **Capability discrimination** (weakest of the useful ones).
   A deterministic battery (multi-step math, counting, logic, format constraints) plus
   a long-context needle detects *tier* substitution reliably, and top-tier
   substitution poorly. Use it to catch cheap substitutes, not to bless expensive ones.

## What proof requires (not available via plain black-box HTTP)

- **Embedded keyed fingerprints** (arXiv 2401.12255). Definitive, but the model owner
  must implant them - not usable against a third-party gateway.
- **TEE / hardware attestation.** AttestLLM (arXiv 2509.06326) verifies on-device model
  integrity via attestation; confidential-computing deployments such as Petridish
  (arXiv 2409.19134) run inference inside enclaves with signed receipts.
- **Attested provenance for APIs.** AEX (arXiv 2603.14283) audits shadow APIs and notes
  existing methods - fingerprinting, model-equality testing, verifiable inference, TEE
  attestation - "either remain inferential or answer different questions". The
  evidence-bound gateway provenance work (arXiv 2606.22560) formalizes the threat:
  gateways decide routing, fallback, stream manipulation, and may forge provenance.
  Realistic recourse: only an attested gateway runtime produces proof.

## Mapping: research -> probes in verify_gateway.py

| Probe | Research basis | Can show | Cannot show |
|---|---|---|---|
| infra: wrapper fields, x-headers, model echo | engineering practice | which gateway/proxy stack runs | model identity |
| service: prompt-token overhead, output caps, reasoning dialect | service audit | what the gateway mutates in your traffic | who serves the traffic |
| caps: deterministic battery | capability discrimination (#6) | tier/family gaps; cheap substitutes | top-tier swaps |
| caps: needle-in-haystack | context-window claims | claimed vs usable context | exact weight identity |
| self: identity/cutoff x2 | one-directional rule | contradictions; response instability | never positive evidence |
| diff: battery outputs vs baseline | output-space similarity (#1), (#4) | strong same/different evidence | formal proof (batch nondeterminism) |
| diff: forced-output token counts | tokenizer signature (#5) | same/different tokenizer & upstream (WEAK: usage is forgeable; only scored after client-side text+ratio cross-check) | formal proof |
| diff: logprobs comparison (--logprobs) | distribution matching (#1) | strong same/different evidence (top-1 tokens + \|delta logprob\|) | rarely exposed by gateways; a proxy can compute plausible logprobs |

## Honest limitations of this skill

- No statistical significance machinery: the battery is 8 seedable items + dual-depth
  needle (single sample per item). Raise repetitions (future work) before making
  fine-grained claims. The open-ended Jaccard probe is single-sample and scored WEAK.
- Behavioral profiling (style/judge analysis) is manual; the script does not attempt it.
- Differential testing requires an official API key - the case where you least need the
  gateway. Gateways often also differ in *dialect* (token counts, empty content with
  reasoning), which the script notes rather than penalizes.
- Everything the script prints is subject to the LeaFBench caveat: parameter-independent
  wrapping degrades fingerprints. Treat a match as corroboration and a mismatch as
  grounds for investigation, not conviction.

## Sources

- 2605.29245 - Survey: fingerprinting/watermarking taxonomy (keyed verification vs
  similarity-based attribution). https://arxiv.org/abs/2605.29245
- 2508.19843 - SoK: LLM copyright auditing via fingerprinting; LeaFBench; black-box
  untargeted/targeted taxonomy. https://arxiv.org/abs/2508.19843
- 2407.01235 - A Fingerprint for Large Language Models (output-subspace similarity).
  https://arxiv.org/abs/2407.01235
- 2504.14871 - Natural Fingerprints of Large Language Models. https://arxiv.org/abs/2504.14871
- 2509.04504 - Behavioral Fingerprinting of Large Language Models.
  https://arxiv.org/abs/2509.04504
- 2606.07996 - MC-PDD: black-box pretraining-data detection via statistical hit rates.
  https://arxiv.org/abs/2606.07996
- 2603.14283 - AEX: non-intrusive multi-hop attestation and provenance for LLM APIs
  (shadow-API audits). https://arxiv.org/abs/2603.14283
- 2606.22560 - Evidence-bound gateway-path provenance for third-party LLM inference.
  https://arxiv.org/abs/2606.22560
- 2509.06326 - AttestLLM: hardware attestation for on-device LLM integrity.
  https://arxiv.org/abs/2509.06326
- 2409.19134 - Petridish: confidential-computing deployment for LLM inference.
  https://arxiv.org/abs/2409.19134
- 2401.12255 - Instructional Fingerprinting of Large Language Models (requires owner
  cooperation). https://arxiv.org/abs/2401.12255
