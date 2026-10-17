# Probe Session Logs — July 2026

Reference data from real verification sessions. Use for pattern comparison.

---

## Session 1: api.iamhc.cn (NewAPI Stack — FAKE)

**Date:** 2026-07-08

### Catalog Fingerprint
- `created: 1626777600` on ALL models — hardcoded NewAPI placeholder
- `owned_by`: "openai" for DeepSeek, "claude" for GLM — inconsistent/wrong
- 25+ models listed (DeepSeek, GLM, Qwen, Kimi, MiniMax, step, sensenova, Spark)

### DeepSeek-V4-Pro Probe

| Signal | Result | Score |
|---|---|---|
| 1. Reasoning | Timeout (inconclusive) | 0 |
| 2. Tokens | Timeout (inconclusive) | 0 |
| 3. Cutoff | "2024-07" — wrong for DeepSeek V4 (would be 2025+) | 0 |
| 4. Identity | "I'm Claude 3.5 Sonnet, created by Anthropic" | -1 |
| 5. Wrapper | service_tier extra key, prompt_tokens=5 (clean), reasoning_content key present | +1 |
| 6. Cap | 50w=48 100w=99 200w=208 — all pass, no cap | +1 |

**Verdict: FAKE (1/6)** — Model confesses to being Claude 3.5 Sonnet. Gateway does pure label rename without system prompt injection.

### DeepSeek-V4-Flash — All probes timeout (30-45s). Dead.
### GLM-5.1 — Returns openai_error / bad_response_status_code. Broken.
### GLM-5.2 — All probes timeout (30-60s). Dead.

**Lesson:** Lazy wrappers (rename-only) caught by Signal 4 alone.

---

## Session 2: api.xiaomimimo.com (Xiaomi Native — REAL)

**Date:** 2026-07-08

### Catalog Fingerprint
- `owned_by: "xiaomi"` on ALL models — consistent and correct
- 6 models: mimo-v2.5, mimo-v2.5-pro, mimo-v2.5-asr, mimo-v2.5-tts, mimo-v2.5-tts-voiceclone, mimo-v2.5-tts-voicedesign

### MiMo-V2.5-Pro Probe

| Signal | Result | Score |
|---|---|---|
| 1. Reasoning | PRESENT (221 chars) — genuine step-by-step thinking | +1 |
| 2. Tokens | prompt_tokens=253, reasoning_tokens=9, cached_tokens=192 | 0 (moderate) |
| 3. Cutoff | "20" — garbled/truncated | 0 |
| 4. Identity | "I'm MiMo-v2.5-pro, created by Xiaomi MiMo Team" | +1 |
| 5. Wrapper | CLEAN — zero extra keys, no system_fingerprint | +1 |
| 6. Cap | 50w=timeout, 100w=101, 200w=198 — 2/3 pass | +1 |

**Verdict: REAL NATIVE (4/6)**

### MiMo-V2.5 Quick Check
- Identity: "I am MiMo-v2.5, developed by Xiaomi LLM Core Team" ✓
- prompt_tokens: 260 (consistent with pro)
- response_model: mimo-v2.5 ✓

**Notable:** reasoning_tokens and cached_tokens are NATIVE features, not wrapper artifacts. 253 prompt_tokens for "PONG" = Xiaomi's internal prompt framework, not wrapper injection.
