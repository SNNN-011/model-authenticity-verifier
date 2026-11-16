<div align="center">

# Model Authenticity Verifier

**Is the model behind that gateway really what the label says?**

![version](https://img.shields.io/badge/skill-v3.0-1f6feb)
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
- Compares the five deltas against the measured reference signature of the
  claimed family, per text, with tolerance
  `max(2, ceil(0.08 * reference))`
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

On Windows use `python` instead of `python3`; set the key with
`$env:VERIFY_API_KEY = "sk-..."`. Offline logic check (no network):
`python scripts/verify_gateway.py selftest`.

## Reference signatures

Measured 2026-10-29 against the audited gateway, with a text set of the same
composition as the built-ins (id+accuse, jv+price, code, cjk+emoji,
arab+misc) whose exact strings live only on the audit machine. The
collector's built-in texts are public stand-ins, so an exact match is
indicative and a deviation has to be re-measured before it counts (R6).
Measured signatures, anomalies and usage rules:
[`references/tokenizer-baselines-2026-10.md`](references/tokenizer-baselines-2026-10.md).

| family key | models | signature (T1..T5) |
|---|---|---|
| `anthropic-core` | anthropic/claude-opus-4.7, claude-opus-4.8, claude-opus-5, claude-sonnet-5 | 45 40 35 45 47 |
| `anthropic-4.6` | anthropic/claude-opus-4.6, anthropic/claude-sonnet-4.6 | 35 34 29 46 47 |
| `openai` | openai/gpt-5.6-terra, openai/gpt-5.6-sol | 24 24 22 30 29 |
| `deepseek` | deepseek/deepseek-v4-pro, deepseek/deepseek-v4.1-flash-1017 | 27 26 23 32 25 |

Model id to family matching: longest key first, case-insensitive substring
match. `deepseek-v4-flash` has no usable reference (nondeterministic usage
accounting) and maps to no family: `UNMEASURED FAMILY`.

## Verdicts

| Verdict | Meaning |
|---|---|
| `SIGNATURE MATCH` | 4 or 5 of 5 texts agree with the claimed family's reference signature. WEAK FOR: same tokenizer dialect, not proof of weights. |
| `PARTIAL MATCH` | 2 or 3 of 5 agree. Mixed. Re-run with fresh texts before concluding anything. |
| `SIGNATURE MISMATCH` | 0 or 1 of 5 agree. The tokenizer does not look like the claimed family's. |
| `UNMEASURED FAMILY` | no reference signature for the claimed model. The deltas are reported raw so they can be baselined later. |
| `UNSTABLE ACCOUNTING` | the control's token count moved between repeats (`--repeat` > 1). The route's usage accounting is not stable, so no conclusion is drawn. |
| `ENDPOINT UNAVAILABLE` | the endpoint did not answer. Dead, not fake. |
| `NO USAGE REPORTED` | responses carried no token usage, so the method could not run. |

Decision order: endpoint unavailable > no usage reported > unstable
accounting > family verdict. A text matches its reference when
`abs(delta - reference) <= max(2, ceil(0.08 * reference))`.

## The probe texts

Five family-dividing texts plus the control `a`. Plain repeated ASCII and
long plain English agree across most families and prove nothing, which is why
these are not in the set.

Control: `a`

```
T1 id+accuse:
Dia menuduh saya mencuri charger laptop di ruang rapat, tetapi rekaman CCTV menunjukkan saya sedang memperbaiki proyektor.

T2 jv+price:
Regane dagangan ing pasar kutha iki pitung pitulas; regane jaran 25.50, lan ora ana barang liya kanggo dijual.

T3 code:
def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)  # memoize with a dict cache

T4 cjk+emoji:
用户说这个功能在生产环境会失败，请先修复再发布，否则客户会流失。🚀🔥✅

T5 arab+misc:
هل يمكنك ترجمة هذا النص إلى الإنجليزية؟ also: ∑ 3+4=7, 100% ✓ ok?
```

## Honest limits

- **Usage numbers are testimony, not measurement (R1).** A proxy can
  re-tokenize locally and forge `usage`. A match gates the counting dialect,
  never the weights.
- **The built-in texts are stand-ins** for the measured 2026-10-29 set (R6).
  An exact match is indicative, and a deviation has to be re-measured before
  it counts.
- **One text proves nothing (R3).** Family agreement is scored across texts;
  known anomalies are `claude-sonnet-5` T5 = 184 (siblings 47) and
  `claude-opus-4.7` T4 = 46 (siblings 45), both single-text one-offs inside
  agreeing families.
- **Unstable accounting invalidates a run (R5).** `deepseek-v4-flash` is the
  known case: control "a" moved 22 -> 5 -> 31 across runs and max_tokens.
  It maps to `UNMEASURED FAMILY`.
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
references/field-notes-2026-10.md     raw numbers behind the baselines
docs/                                 the one-page site (GitHub Pages, no build step)
```

## License

MIT - see [LICENSE](LICENSE).
