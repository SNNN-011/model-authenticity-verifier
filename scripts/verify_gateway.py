#!/usr/bin/env python3
"""verify_gateway.py - v2.2 evidence collector for third-party LLM gateways.

Honest design rules (see references/methodology.md):
  R1  Self-report probes are ONE-DIRECTIONAL: a contradiction is evidence;
      agreement with the claimed identity is NOT evidence (trivially injectable).
  R2  Capability results are observations. Only differential comparison against
      a trusted baseline (e.g. the official vendor endpoint) yields strong
      identity evidence.
  R3  Wrapper fields/headers identify the gateway software stack, not the model.
      (Note: system_fingerprint fp_* is an OpenAI-native field, not a wrapper.)
  R4  Never hardcode keys; never print keys. Environment: VERIFY_API_KEY.
  R5  Identity verdicts and service verdicts are SEPARATE. An output cap or a
      mutated response is a SERVICE finding, never an identity contradiction.
  R6  Usage-derived evidence (completion_tokens) is only believed after a
      client-side cross-check (exact text match + chars/token sanity band).

v2.2 upgrades over v2:
  - Dynamic seedable battery (defeats prompt caching / static router rules).
    The seed and the full battery spec are logged for reproducibility.
  - Dual-depth needle-in-a-haystack (25% and 75% depth) with reasoning_content
    grading, to catch middle-out and tail context truncation.
  - Optional logprobs distribution probe in differential mode (--logprobs):
    compares top-1 next-token and |delta logprob| across endpoints.
  - Anti-forgery cross-check for completion_tokens on the forced output.
  - Latency telemetry and end-to-end throughput (completion_tokens/sec).
  - Retry-After support on HTTP 429; strict identity/service verdict split.

Stdlib only. Cross-platform. ASCII output.

Usage:
  python verify_gateway.py --base https://api.example.com/v1 --model claude-x --key sk-...
  python verify_gateway.py --base <gw> --model X --baseline-base <official> \\
      --baseline-model X --baseline-key OK [--logprobs]
  python verify_gateway.py selftest
"""

import concurrent.futures
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 90
SLEEP_BETWEEN = 2.0

# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------


def http_post_json(url, key, payload, timeout=TIMEOUT):
    """POST JSON. Returns (status, headers_dict, body_text, error_str, elapsed_s)."""
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, dict(r.headers.items()), body, None, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        hdrs = dict(getattr(e, "headers", {}).items() if getattr(e, "headers", None) else [])
        return e.code, hdrs, body, "HTTP %s" % e.code, time.perf_counter() - t0
    except Exception as e:  # URLError, timeout, socket errors
        return None, {}, "", "%s: %s" % (type(e).__name__, e), time.perf_counter() - t0


def _parse_retry_after(headers, default=15.0):
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return min(60.0, max(1.0, float(v)))
            except (TypeError, ValueError):
                pass
    return default


def make_caller(base, key, model, timeout=TIMEOUT):
    """Return call(messages, max_tokens, temperature=None, extra=None) -> resp dict.

    Handles: base path auto-fix on 404, 429 retry with Retry-After, SSE detection,
    stream=false, temperature fallback, latency tracking (_latency_s).
    """
    state = {"base": base.rstrip("/")}

    def call(messages, max_tokens=500, temperature=None, extra=None):
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "stream": False}
        if temperature is not None:
            payload["temperature"] = temperature
        if extra:
            payload.update(extra)

        def once(b):
            return http_post_json(b + "/chat/completions", key, payload, timeout)

        status, headers, body, err, elapsed = once(state["base"])
        # auto-fix missing /v1 on 404
        if status == 404 and "/v1" not in state["base"]:
            status2, headers2, body2, err2, elapsed2 = once(state["base"] + "/v1")
            if status2 is not None and status2 < 400:
                state["base"] = state["base"] + "/v1"
                state["fixed"] = True
                status, headers, body, err, elapsed = status2, headers2, body2, err2, elapsed2
        # retry up to twice on rate limit (respecting Retry-After)
        retries = 0
        while status == 429 and retries < 2:
            time.sleep(_parse_retry_after(headers, 15.0 * (retries + 1)))
            status, headers, body, err, elapsed = once(state["base"])
            retries += 1
        # retry once without temperature if rejected by reasoning models
        if status is not None and 400 <= status < 500 and temperature is not None \
                and "temperature" in body.lower():
            payload.pop("temperature", None)
            status, headers, body, err, elapsed = once(state["base"])
        if err:
            return {"_error": err, "_body": (body or "")[:300],
                    "_status": status, "_latency_s": elapsed}
        if looks_like_sse(body):
            return {"_error": "SSE response despite stream=false",
                    "_body": body[:300], "_status": status, "_sse": True,
                    "_latency_s": elapsed}
        try:
            resp = json.loads(body)
        except Exception as e:
            return {"_error": "JSON parse failed: %s" % e,
                    "_body": body[:300], "_latency_s": elapsed}
        resp["_status"] = status
        resp["_headers"] = headers
        resp["_latency_s"] = elapsed
        return resp

    call.state = state
    return call


def looks_like_sse(body):
    return body.lstrip().startswith("data:")


def content_of(resp):
    try:
        msg = resp["choices"][0]["message"]
        return (msg.get("content") or "").strip(), msg.get("reasoning_content") or ""
    except Exception:
        return "", ""


def usage_of(resp):
    return resp.get("usage") or {}


def err_of(resp):
    return resp.get("_error") if isinstance(resp, dict) else "bad response"


# ----------------------------------------------------------------------------
# Classification helpers (pure functions - covered by selftest)
# ----------------------------------------------------------------------------

FAMILY_PATTERNS = {
    "anthropic": [r"\banthropic\b", r"\bclaude\b"],
    "openai": [r"\bopenai\b", r"\bchatgpt\b", r"\bgpt-[345o]\w*", r"\bgpt\s*[345]\b",
               r"\bo[134]-mini\b", r"\bo[13]\b"],
    "google": [r"\bgoogle\b", r"\bgemini\b", r"\bdeepmind\b"],
    "deepseek": [r"\bdeepseek\b"],
    "qwen": [r"\bqwen\d*\b", r"\balibaba\b", r"\btongyi\b"],
    "meta": [r"\bmeta\s+ai\b", r"\bmeta's\b", r"\bllama[-\s]?\d*\b"],
    "mistral": [r"\bmistral\b", r"\bmixtral\b"],
    "xai": [r"\bxai\b", r"\bx\.ai\b"],  # 'grok' handled separately, see below
    "moonshot": [r"\bmoonshot\b", r"\bkimi\b"],
    "minimax": [r"\bminimax\b"],
    "zhipu": [r"\bzhipu\b", r"\bchatglm\b", r"\bglm-\d\w*", r"\bglm\b"],
    "xiaomi": [r"\bxiaomi\b", r"\bmimo\b"],
    "cohere": [r"\bcohere\b", r"\bcommand-r\b"],
}


def _grok_is_model_noun(text):
    """Case-aware rule for 'Grok' (xAI) without a POS tagger.

    - 'grok' lowercase mid-sentence is the English verb -> never a model.
    - 'Grok' capitalized mid-sentence -> model noun.
    - 'Grok' anywhere in a sentence mentioning model/ai context words -> model.
    - Digit form ('grok-2', 'Grok 3') is handled by pattern rule A in families_in.
    """
    context_words = ("model", "ai", "assistant", "xai", "elon", "built",
                     "created", "developed")
    for sentence in re.split(r"[.!?\n]+", text or ""):
        s = sentence.strip()
        if not s:
            continue
        low = s.lower()
        has_context = any(w in low for w in context_words)
        for m in re.finditer(r"\bGrok\b", s):  # case-sensitive on purpose
            if m.start() > 0 or has_context:
                return True
    return False


def families_in(text):
    """Extract model families with word-boundary regexes (no substring FPs)."""
    low = (text or "").lower()
    out = set()
    for fam, pats in FAMILY_PATTERNS.items():
        if any(re.search(p, low) for p in pats):
            out.add(fam)
    # xAI: 'grok' is also an English verb.
    if re.search(r"\bgrok[-\s]?\d", low):       # grok-2, Grok 3 (digit form)
        out.add("xai")
    elif _grok_is_model_noun(text):
        out.add("xai")
    return out


def classify_identity(response_text, claimed_model):
    """One-directional: CONTRADICTION / MATCH_CLAIM / GENERIC / INFO."""
    claimed = families_in(claimed_model)
    mentioned = families_in(response_text)
    if mentioned and claimed:
        if mentioned.isdisjoint(claimed):
            return {"verdict": "CONTRADICTION", "mentioned": sorted(mentioned),
                    "claimed": sorted(claimed)}
        return {"verdict": "MATCH_CLAIM", "mentioned": sorted(mentioned),
                "claimed": sorted(claimed)}
    if mentioned and not claimed:
        return {"verdict": "INFO", "mentioned": sorted(mentioned), "claimed": []}
    return {"verdict": "GENERIC", "mentioned": [], "claimed": sorted(claimed)}


def extract_cutoff(text):
    m = re.search(r"\b(20\d{2}-(?:0[1-9]|1[0-2]))\b", text)
    return m.group(1) if m else None


def normalize(s):
    s = (s or "").strip().lower()
    s = s.strip("`'\"*.")
    s = re.sub(r"\s+", " ", s)
    return s


def grade_item(item, response_text):
    """Deterministic grading for battery items. Returns True/False."""
    kind, expect = item["kind"], item["expect"]
    n = normalize(response_text)
    if not n:
        return False
    if kind == "num":
        # "Answer with only the number" - reject long rambling answers.
        if len(n) > 80:
            return False
        nums = re.findall(r"-?\d+(?:\.\d+)?", n)
        if not nums:
            return False
        # Grade ONLY the final number (the model's conclusion), so
        # "1, 2, 3, 4 -> total is 4" cannot pass on the stray 5.
        try:
            return str(int(float(nums[-1]))) == expect
        except ValueError:
            return False
    if kind in ("word", "letter"):
        if n == expect:
            return True
        return bool(re.search(r"\b" + re.escape(expect) + r"\b", n)) and len(n) <= len(expect) + 25
    if kind == "lines3":
        lines = [l.strip() for l in (response_text or "").strip().splitlines() if l.strip()]
        return len(lines) == 3 and all(not re.match(r"^[-*\d]", l) for l in lines)
    return False


def scan_wrapper_fields(resp):
    """Infrastructure attribution. NOT model identity. Skips _internal keys."""
    found = []
    known = {
        "base_resp": "Moonshot/Kimi-style wrapper",
        "anyrouter_metadata": "AnyRouter aggregator",
        "providerChain": "aggregator router",
        "cost": "community gateway pricing",
        "provider": "routing metadata",
    }
    for k, label in known.items():
        if k in resp:
            found.append("%s (%s)" % (k, label))
    if isinstance(resp.get("type"), str) and "new_api_error" in resp.get("type", ""):
        found.append("type=new_api_error (NewAPI/one-api stack)")
    sf = resp.get("system_fingerprint")
    if sf:
        found.append("system_fingerprint=%s (OpenAI-native field; provenance hint, not a wrapper)" % sf)
    return found


def classify_overhead(pt):
    if pt is None:
        return "unknown"
    if pt <= 10:
        return "minimal (<=10 tokens for PONG; note: a proxy may compute usage locally)"
    if pt <= 100:
        return "small template overhead (%d tokens)" % pt
    return "notable hidden prompt (%d tokens) - inspect before trusting" % pt


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suffix)


# ----------------------------------------------------------------------------
# Dynamic battery & dual-needle helpers
# ----------------------------------------------------------------------------

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def make_battery(rng=None):
    """Generate a seedable dynamic battery to defeat static gateway caching.

    rng=None returns the fixed calibration battery (used by selftest).
    """
    if rng is None:
        a, b, c = 17, 24, 89
        start_idx, offset_days = DAYS.index("Wednesday"), 100
        mod_base = 17
        word_target, letter_target, exp_letter_cnt = "assessments", "s", "5"
        nth_idx = 5
        dist_km, time_min, target_hr = 60, 45, 2
    else:
        a = rng.randint(13, 29)
        b = rng.randint(14, 28)
        c = rng.randint(40, 99)
        start_idx = rng.randint(0, 6)
        offset_days = rng.randint(50, 150)
        mod_base = rng.choice([13, 17, 19, 23])
        word_target, letter_target, exp_letter_cnt = rng.choice([
            ("assessments", "s", "5"),
            ("mississippi", "s", "4"),
            ("mississippi", "i", "4"),
            ("abracadabra", "a", "5"),
            ("hippopotamus", "p", "3"),
        ])
        nth_idx = rng.choice([3, 4, 5, 6, 7])
        dist_km, time_min, target_hr = rng.choice([
            (60, 45, 2), (90, 45, 2), (80, 40, 3), (45, 30, 3),
        ])
    start_day = DAYS[start_idx]
    exp_day = DAYS[(start_idx + offset_days) % 7].lower()
    speed_kmh = dist_km / (time_min / 60.0)
    exp_speed = str(int(round(speed_kmh * target_hr)))
    nth_sent = "The early bird catches the worm every single morning."
    nth_words = [w.strip(".,!?").lower() for w in nth_sent.split()]
    exp_nth = nth_words[nth_idx - 1]
    return [
        {"id": "arith",
         "prompt": "Compute %d*%d+%d. Answer with only the number." % (a, b, c),
         "kind": "num", "expect": str(a * b + c)},
        {"id": "day",
         "prompt": "If today is %s, what day of the week is %d days from today? "
                   "Answer with only the day name." % (start_day, offset_days),
         "kind": "word", "expect": exp_day},
        {"id": "mod",
         "prompt": "What is %d^3 mod 100? Answer with only the number." % mod_base,
         "kind": "num", "expect": str((mod_base ** 3) % 100)},
        {"id": "letters",
         "prompt": "How many letter '%s' are in the word '%s'? Answer with only the number."
                   % (letter_target, word_target),
         "kind": "num", "expect": exp_letter_cnt},
        {"id": "nthword",
         "prompt": "Reply with exactly the %s word of this sentence and nothing else: %s"
                   % (ordinal(nth_idx), nth_sent),
         "kind": "word", "expect": exp_nth},
        {"id": "speed",
         "prompt": "A train travels %d km in %d minutes. At the same speed, how far does it "
                   "travel in %d hours? Answer with only the number of km."
                   % (dist_km, time_min, target_hr),
         "kind": "num", "expect": exp_speed},
        {"id": "logic",
         "prompt": "All bloops are razzies. Some razzies are lazzies. Which statement must be "
                   "true? (a) All bloops are lazzies (b) Some bloops are lazzies "
                   "(c) Neither can be concluded. Answer with only the letter.",
         "kind": "letter", "expect": "c"},
        {"id": "count3",
         "prompt": "List exactly 3 European capital cities, one per line. "
                   "No numbering, no bullets, no other text.",
         "kind": "lines3", "expect": "3"},
    ]


BATTERY = make_battery(None)  # fixed calibration battery (also used by selftest)

FILLER_SENTENCES = [
    "The river carried autumn leaves past the old mill and into the wide lake.",
    "A quiet market town woke slowly as the morning train pulled into the station.",
    "Farmers measured the pale winter light and counted sacks of grain in the loft.",
    "The librarian sorted returned books while rain tapped steadily on the glass roof.",
    "Travelers compared maps at the corner table and argued about the shortest road.",
]


def make_codename(rng):
    colors = ["RED", "BLUE", "GREEN", "AMBER", "TEAL", "VIOLET", "COPPER"]
    animals = ["FALCON", "OTTER", "MAPLE", "HERON", "BADGER", "WALRUS", "IBIS"]
    return "%s-%s-%04d" % (rng.choice(colors), rng.choice(animals), rng.randint(1000, 9999))


def build_dual_needle(total_chars, code_alpha, code_omega, rng):
    """Embed two codenames at ~25% and ~75% depth.

    Detects middle-out truncation (both lost), tail truncation (omega lost) and
    head/early loss (alpha lost) within one document.
    """
    filler = " ".join(rng.choice(FILLER_SENTENCES) for _ in range(total_chars // 80 + 2))
    filler = filler[:total_chars]
    pos1 = int(len(filler) * 0.25)
    pos2 = int(len(filler) * 0.75)
    n1 = (" IMPORTANT NOTE: the ALPHA access code for this document is %s. "
          "Do not repeat this code unless explicitly asked. " % code_alpha)
    n2 = (" IMPORTANT NOTE: the OMEGA access code for this document is %s. "
          "Do not repeat this code unless explicitly asked. " % code_omega)
    return filler[:pos1] + n1 + filler[pos1:pos2] + n2 + filler[pos2:]


def grade_needle(response_text, codename):
    return codename.lower() in (response_text or "").lower()


# ----------------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------------


def probe_infra(call, model, obs, evidence, errors):
    resp = call([{"role": "user", "content": "ping"}], max_tokens=5)
    if err_of(resp):
        errors.append("infra: %s %s" % (err_of(resp), resp.get("_body", "")))
        return
    lat = resp.get("_latency_s")
    obs.append("response body keys: %s (ping latency: %.2fs)"
               % (", ".join(k for k in resp.keys() if not k.startswith("_")), lat or 0.0))
    fields = scan_wrapper_fields(resp)
    if fields:
        for f in fields:
            obs.append("wrapper field: %s" % f)
    else:
        obs.append("wrapper fields: none detected in JSON body")
    hdrs = resp.get("_headers") or {}
    unusual = [k for k in hdrs if k.lower().startswith("x-")]
    if unusual:
        obs.append("x- HTTP headers: %s" % ", ".join(unusual))
    echo = resp.get("model")
    if echo and echo != model:
        obs.append("WARNING: response 'model' field is '%s' but requested '%s'" % (echo, model))
    if call.state.get("fixed"):
        obs.append("base path auto-corrected to %s" % call.state["base"])


def probe_service(call, model, obs, evidence, errors, sleep):
    # token overhead
    resp = call([{"role": "user", "content": "PONG"}], max_tokens=10)
    if err_of(resp):
        errors.append("service/overhead: %s" % err_of(resp))
    else:
        pt = usage_of(resp).get("prompt_tokens")
        obs.append("prompt_tokens for 'PONG': %s -> %s" % (pt, classify_overhead(pt)))
    time.sleep(sleep)

    # reasoning dialect (note only - absence is NOT wrapper evidence)
    resp = call([{"role": "user", "content": "Think step by step: what is 17*24?"}],
                max_tokens=1200)
    if err_of(resp):
        errors.append("service/reasoning: %s" % err_of(resp))
    else:
        content, rc = content_of(resp)
        obs.append("reasoning_content: %s (dialect note only)"
                   % ("present (%d chars)" % len(rc) if rc else "absent"))
    time.sleep(sleep)

    # output cap scan + throughput
    cap_hits = []
    tps_samples = []
    for words in (100, 300, 600):
        resp = call([{"role": "user", "content":
                      "Write exactly %d words about AI safety. Count your words precisely." % words}],
                    max_tokens=4000, temperature=0)
        if err_of(resp):
            errors.append("service/cap(%dw): %s" % (words, err_of(resp)))
        else:
            content, _ = content_of(resp)
            wc = len((content or "").split())
            ct = usage_of(resp).get("completion_tokens")
            lat = resp.get("_latency_s") or 0.0
            if ct and lat > 0:
                tps_samples.append(ct / lat)
            fr = ""
            try:
                fr = resp["choices"][0].get("finish_reason") or ""
            except Exception:
                pass
            obs.append("cap probe %dw -> %d words, finish_reason=%s (%.2fs)"
                       % (words, wc, fr or "?", lat))
            if fr == "length" and wc < words * 0.7:
                cap_hits.append((words, wc))
        time.sleep(sleep)
    if tps_samples:
        obs.append("observed generation speed: ~%.1f completion_tokens/sec (end-to-end)"
                   % (sum(tps_samples) / len(tps_samples)))
    if cap_hits:
        # SERVICE finding only - must never drive an identity verdict (rule R5).
        evidence.append(("STRONG", "AGAINST-SERVICE",
                         "output cap detected: truncated at ~%d words (requested %dw)"
                         % (min(wc for _, wc in cap_hits), min(w for w, _ in cap_hits))))


def probe_caps(call, battery, obs, evidence, errors, sleep, needle_chars, rng):
    results = []
    for item in battery:
        resp = call([{"role": "user", "content": item["prompt"]}], max_tokens=800, temperature=0)
        if err_of(resp):
            errors.append("caps/%s: %s" % (item["id"], err_of(resp)))
            results.append({"id": item["id"], "pass": None, "output": ""})
            time.sleep(sleep)
            continue
        content, _ = content_of(resp)
        ok = grade_item(item, content)
        results.append({"id": item["id"], "pass": ok, "output": content})
        time.sleep(sleep)
    graded = [r for r in results if r["pass"] is not None]
    passes = sum(1 for r in graded if r["pass"])
    obs.append("capability battery: %d/%d deterministic items passed" % (passes, len(graded)))

    needle_info = None
    if needle_chars > 0:
        code_alpha = make_codename(rng)
        code_omega = make_codename(rng)
        while code_omega == code_alpha:
            code_omega = make_codename(rng)
        hay = build_dual_needle(needle_chars, code_alpha, code_omega, rng)
        prompt = (hay + "\n\nQuestion: The text above contains an ALPHA access code and an OMEGA "
                  "access code. Reply with both codes separated by a comma, and nothing else.")
        resp = call([{"role": "user", "content": prompt}], max_tokens=2500, temperature=0)
        if err_of(resp):
            errors.append("caps/needle: %s %s" % (err_of(resp), resp.get("_body", "")))
        else:
            content, rc = content_of(resp)
            combined_out = content + " " + (rc or "")  # reasoning models may answer in rc
            pt = usage_of(resp).get("prompt_tokens")
            pass_alpha = grade_needle(combined_out, code_alpha)
            pass_omega = grade_needle(combined_out, code_omega)
            needle_info = {
                "pass": pass_alpha and pass_omega,
                "pass_25pct": pass_alpha,
                "pass_75pct": pass_omega,
                "prompt_tokens": pt,
                "chars": needle_chars,
                "codes": [code_alpha, code_omega],
            }
            obs.append("dual-needle: %d chars, prompt_tokens=%s -> 25%%=%s, 75%%=%s"
                       % (needle_chars, pt,
                          "PASS" if pass_alpha else "FAIL",
                          "PASS" if pass_omega else "FAIL"))
            if not needle_info["pass"]:
                # Not attributable: gateway truncation OR weaker model. SERVICE-side.
                evidence.append(("MODERATE", "AGAINST-SERVICE",
                                 "long-context recall failed (25%%=%s, 75%%=%s at %d chars) - "
                                 "context handling, not identity proof"
                                 % ("PASS" if pass_alpha else "FAIL",
                                    "PASS" if pass_omega else "FAIL", needle_chars)))
    return results, needle_info


def probe_self(call, model, obs, evidence, errors, sleep):
    contradictions = []
    for i in range(2):
        q = ("Which AI model are you exactly? Give the model name and the company that made you."
             if i == 0 else
             "State your exact model identity: model name, developer, version.")
        resp = call([{"role": "user", "content": q}], max_tokens=1000, temperature=0)
        if err_of(resp):
            errors.append("self/identity: %s" % err_of(resp))
            time.sleep(sleep)
            continue
        content, _ = content_of(resp)
        verdict = classify_identity(content, model)
        obs.append("identity sample %d: [%s] %s"
                   % (i + 1, verdict["verdict"], content[:160].replace("\n", " ")))
        if verdict["verdict"] == "CONTRADICTION":
            contradictions.append(verdict)
        time.sleep(sleep)

    if contradictions:
        evidence.append(("STRONG", "AGAINST",
                         "self-report contradiction (%d/2 samples): claimed '%s' but model names %s"
                         % (len(contradictions), model,
                            "/".join(contradictions[0]["mentioned"]))))
    else:
        evidence.append(("WEAK", "NEUTRAL",
                         "identity self-report: no contradiction (agreement proves nothing - "
                         "one system prompt can produce any claim)"))

    cutoffs = []
    for i in range(2):
        resp = call([{"role": "user", "content":
                      "Reply with ONLY your training-data knowledge cutoff date in YYYY-MM format."}],
                    max_tokens=1000, temperature=0)
        if err_of(resp):
            errors.append("self/cutoff: %s" % err_of(resp))
            time.sleep(sleep)
            continue
        content, _ = content_of(resp)
        cutoffs.append(extract_cutoff(content) or "none/%s" % normalize(content)[:40])
        time.sleep(sleep)
    obs.append("cutoff self-reports: %s (unreliable - hallucination + injection; never scored positive)"
               % ", ".join(cutoffs))
    if len(set(cutoffs)) > 1:
        obs.append("cutoff answers INCONSISTENT between identical requests: %s" % cutoffs)


# ----------------------------------------------------------------------------
# Differential testing (+ logprobs probe)
# ----------------------------------------------------------------------------

FORCED = ("the old clock tower chimes seven times across the quiet valley while the river "
          "carries autumn leaves past the mill and into the wide blue lake")

OPEN_DIFF_PROMPT = (
    "Explain the core difference between TCP and UDP in networking in exactly two sentences, "
    "focusing on connection state and packet ordering."
)

LOGPROB_PROMPTS = [
    "The capital of France is",
    "2 + 2 =",
    "The opposite of hot is",
]


def _word_jaccard(a, b):
    sa = set(re.findall(r"\b\w+\b", normalize(a)))
    sb = set(re.findall(r"\b\w+\b", normalize(b)))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _ratio_sane(text, completion_tokens):
    """Chars per completion token for English text should sit near 3.0-5.5."""
    if not completion_tokens or completion_tokens <= 0:
        return False, None
    ratio = len(text or "") / float(completion_tokens)
    return (3.0 <= ratio <= 5.5), ratio


def _extract_top_token(resp):
    """Return (token, logprob, top_map) for the first generated position, or None.

    Handles OpenAI chat style (logprobs.content[]) and legacy completion style
    (logprobs.top_logprobs[] / tokens[] + token_logprobs[]).
    """
    try:
        lp = (resp.get("choices") or [{}])[0].get("logprobs")
        if not lp:
            return None
        content = lp.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            first = content[0]
            top = {}
            for t in (first.get("top_logprobs") or []):
                if isinstance(t, dict) and t.get("token") is not None:
                    top[t["token"]] = t.get("logprob")
            if first.get("token") is not None:
                return first.get("token"), first.get("logprob"), top
        top_list = lp.get("top_logprobs")
        if isinstance(top_list, list) and top_list and isinstance(top_list[0], dict):
            items = list(top_list[0].items())
            items.sort(key=lambda kv: (kv[1] is not None,
                                       kv[1] if kv[1] is not None else -1e9), reverse=True)
            if items:
                return items[0][0], items[0][1], dict(items)
        tokens = lp.get("tokens")
        tls = lp.get("token_logprobs")
        if isinstance(tokens, list) and tokens and isinstance(tls, list) and tls:
            return tokens[0], tls[0], {}
    except Exception:
        pass
    return None


def _norm_token(t):
    return (t or "").strip().lower()


def probe_logprobs(suspect_call, baseline_call, obs, evidence, errors, sleep):
    """Optional (--logprobs): compare next-token distributions vs baseline.

    - identical top-1 tokens, mean |delta logprob| < 0.15 -> [STRONG][FOR]
    - top-1 token differs on all compared prompts          -> [STRONG][AGAINST]
    - baseline exposes logprobs but suspect does not       -> feature stripping
      (recorded in observations; NOT identity evidence)
    Caveat: a proxy can forge logprobs; still the strongest non-attested signal.
    """
    extra = {"logprobs": True, "top_logprobs": 5}
    pairs = []
    for p in LOGPROB_PROMPTS:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            # generous budget: reasoning-budget gateways 502 (empty_response) on tiny max_tokens
            f_s = ex.submit(suspect_call, [{"role": "user", "content": p}], 2500, 0, extra)
            f_b = ex.submit(baseline_call, [{"role": "user", "content": p}], 2500, 0, extra)
            rs, rb = f_s.result(), f_b.result()
        pairs.append((rs, rb))
        time.sleep(sleep)

    compare = []
    s_errs, s_missing, b_ok_count = [], 0, 0
    for rs, rb in pairs:
        if err_of(rb):
            continue
        b_lp = _extract_top_token(rb)
        if b_lp is None:
            continue
        b_ok_count += 1
        if err_of(rs):
            s_errs.append(err_of(rs))
            continue
        s_lp = _extract_top_token(rs)
        if s_lp is None:
            s_missing += 1
            continue
        compare.append((s_lp, b_lp))

    if b_ok_count == 0 and not compare:
        obs.append("logprobs probe: neither endpoint returned logprobs; skipped "
                   "(many OpenAI-compat gateways silently drop this feature)")
        return
    if b_ok_count > 0 and not compare:
        detail = ("HTTP errors: " + "; ".join(s_errs[:2])) if s_errs else \
                 ("ignored silently (%d/%d prompts)" % (s_missing, b_ok_count))
        obs.append("logprobs probe: baseline returned logprobs but suspect did not (%s) "
                   "-> possible FEATURE STRIPPING; NOT identity evidence" % detail)
        return

    agree, disagree, deltas = 0, 0, []
    for (st, sl, _), (bt, bl, _) in compare:
        if _norm_token(st) == _norm_token(bt):
            agree += 1
            if sl is not None and bl is not None:
                deltas.append(abs(sl - bl))
        else:
            disagree += 1
    mean_d = (sum(deltas) / len(deltas)) if deltas else None
    obs.append("logprobs probe: %d prompts compared, top-1 agree %d / differ %d, "
               "mean |delta logprob| = %s"
               % (len(compare), agree, disagree,
                  ("%.3f" % mean_d) if mean_d is not None else "?"))
    if agree >= 2 and disagree == 0 and mean_d is not None and mean_d < 0.15:
        evidence.append(("STRONG", "FOR",
                         "logprobs distribution match: identical top-1 tokens on %d/%d prompts, "
                         "mean |delta logprob| = %.3f (< 0.15)"
                         % (agree, len(compare), mean_d)))
    elif disagree >= 2 and agree == 0:
        evidence.append(("STRONG", "AGAINST",
                         "logprobs distribution mismatch: top-1 next-token differs on all %d "
                         "compared prompts" % disagree))
    else:
        obs.append("logprobs probe inconclusive (mixed agreement)")
    if s_errs:
        obs.append("logprobs probe: suspect HTTP errors on some prompts: %s"
                   % "; ".join(s_errs[:2]))


def probe_diff(suspect_call, baseline_call, battery, suspect_results, obs, evidence, errors, sleep):
    """Differential testing vs a trusted baseline serving the claimed model."""
    base_results = []
    for item in battery:
        resp = baseline_call([{"role": "user", "content": item["prompt"]}],
                             max_tokens=800, temperature=0)
        if err_of(resp):
            errors.append("diff/baseline/%s: %s" % (item["id"], err_of(resp)))
            base_results.append({"id": item["id"], "pass": None, "output": ""})
            time.sleep(sleep)
            continue
        content, _ = content_of(resp)
        base_results.append({"id": item["id"], "pass": grade_item(item, content), "output": content})
        time.sleep(sleep)

    s_map = {r["id"]: r for r in suspect_results}
    b_map = {r["id"]: r for r in base_results}
    both = [i for i in s_map if s_map[i]["pass"] is not None
            and b_map.get(i, {}).get("pass") is not None]
    exact = sum(1 for i in both if normalize(s_map[i]["output"]) == normalize(b_map[i]["output"]))
    s_pass = sum(1 for i in both if s_map[i]["pass"])
    b_pass = sum(1 for i in both if b_map[i]["pass"])
    obs.append("differential battery: capability %d/%d (suspect) vs %d/%d (baseline); "
               "exact-output match %d/%d"
               % (s_pass, len(both), b_pass, len(both), exact, len(both)))
    if both:
        if b_pass - s_pass >= 3:
            evidence.append(("STRONG", "AGAINST",
                             "differential: baseline passes %d more deterministic tasks than "
                             "suspect (%d vs %d) - capability gap inconsistent with same model"
                             % (b_pass - s_pass, b_pass, s_pass)))
        elif exact == 0 and b_pass >= len(both) // 2:
            evidence.append(("MODERATE", "AGAINST",
                             "differential: zero exact-output agreement at temperature 0 on "
                             "%d items" % len(both)))
        elif exact >= max(1, len(both) - 2) and abs(b_pass - s_pass) <= 1:
            evidence.append(("MODERATE", "FOR",
                             "differential: high battery agreement with baseline "
                             "(%d/%d exact, capability parity)" % (exact, len(both))))

    # --- forced-output tokenizer signature with client-side cross-check (R6) ---
    ask_forced = "Repeat the following text exactly, with no changes and no extra words: " + FORCED
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_s = ex.submit(suspect_call, [{"role": "user", "content": ask_forced}], 2500, 0)
        f_b = ex.submit(baseline_call, [{"role": "user", "content": ask_forced}], 2500, 0)
        resp_s, resp_b = f_s.result(), f_b.result()

    t, texts = {}, {}
    for label, resp in (("suspect", resp_s), ("baseline", resp_b)):
        if err_of(resp):
            errors.append("diff/tokens/%s: %s" % (label, err_of(resp)))
            continue
        content, _ = content_of(resp)
        t[label] = usage_of(resp).get("completion_tokens")
        texts[label] = content

    if t.get("suspect") is not None and t.get("baseline") is not None:
        s_ok_text = normalize(texts.get("suspect", "")) == normalize(FORCED)
        b_ok_text = normalize(texts.get("baseline", "")) == normalize(FORCED)
        s_ok_ratio, s_ratio = _ratio_sane(texts.get("suspect", ""), t.get("suspect"))
        b_ok_ratio, b_ratio = _ratio_sane(texts.get("baseline", ""), t.get("baseline"))
        obs.append("forced-output completion_tokens: suspect=%s (chars/token=%s) "
                   "baseline=%s (chars/token=%s)"
                   % (t.get("suspect"), ("%.2f" % s_ratio) if s_ratio else "?",
                      t.get("baseline"), ("%.2f" % b_ratio) if b_ratio else "?"))
        if not (s_ok_text and b_ok_text):
            obs.append("forced-output cross-check: text NOT repeated exactly "
                       "(suspect_ok=%s, baseline_ok=%s) -> tokenizer comparison skipped"
                       % (s_ok_text, b_ok_text))
        elif not (s_ok_ratio and b_ok_ratio):
            obs.append("forced-output cross-check: chars/token outside 3.0-5.5 "
                       "(suspect=%s, baseline=%s) -> tokenizer comparison skipped"
                       % (("%.2f" % s_ratio) if s_ratio else "?",
                          ("%.2f" % b_ratio) if b_ratio else "?"))
        else:
            delta = abs(t["suspect"] - t["baseline"])
            if delta == 0:
                evidence.append(("WEAK", "FOR",
                                 "tokenizer signature (usage-derived, cross-check passed): "
                                 "identical completion_tokens on forced output"))
            elif delta >= 3:
                evidence.append(("MODERATE", "AGAINST",
                                 "tokenizer signature: completion_tokens differ by %d on an "
                                 "identical forced output (both repeated the text exactly)" % delta))
            else:
                obs.append("tokenizer signature inconclusive (delta=%d, noise floor=3)" % delta)

    # --- open-ended determinism probe (single sample; weak evidence) ---
    time.sleep(sleep)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_os = ex.submit(suspect_call, [{"role": "user", "content": OPEN_DIFF_PROMPT}], 2500, 0)
        f_ob = ex.submit(baseline_call, [{"role": "user", "content": OPEN_DIFF_PROMPT}], 2500, 0)
        open_s, open_b = f_os.result(), f_ob.result()
    if not err_of(open_s) and not err_of(open_b):
        cs, _ = content_of(open_s)
        cb, _ = content_of(open_b)
        sim = _word_jaccard(cs, cb)
        obs.append("open-ended temp=0 word Jaccard similarity: %.2f (single sample; weak)" % sim)
        if sim >= 0.75:
            evidence.append(("WEAK", "FOR",
                             "open-ended temp=0 lexical overlap with baseline (Jaccard=%.2f, "
                             "single sample - repeat for confidence)" % sim))
        elif sim < 0.25:
            evidence.append(("WEAK", "AGAINST",
                             "open-ended temp=0 diverges from baseline (Jaccard=%.2f, "
                             "single sample - repeat before acting)" % sim))

    return base_results


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------


def render(obs, evidence, errors, title):
    print("=" * 64)
    print(" GATEWAY EVIDENCE REPORT (v2.2)")
    print(" %s" % title)
    print("=" * 64)

    print("\n-- OBSERVATIONS (infrastructure / service) --")
    for o in obs or ["(none)"]:
        print("  * %s" % o)

    print("\n-- IDENTITY EVIDENCE LEDGER --")
    print("  (STRONG = hard to explain away; MODERATE = suggestive; WEAK/NEUTRAL = no signal)")
    if not evidence:
        print("  (no evidence items)")
    for tier, direction, text in evidence:
        print("  [%s][%s] %s" % (tier, direction, text))

    print("\n-- ERRORS / GAPS --")
    for e in errors or ["(none)"]:
        print("  ! %s" % e)

    # Strict separation (R5): identity vs service.
    against_identity = [e for e in evidence if e[1] == "AGAINST"]
    against_service = [e for e in evidence if e[1] == "AGAINST-SERVICE"]
    for_ev = [e for e in evidence if e[1] == "FOR"]
    strong_id = [e for e in against_identity if e[0] == "STRONG"]
    mod_id = [e for e in against_identity if e[0] == "MODERATE"]
    weak_id = [e for e in against_identity if e[0] == "WEAK"]

    print("\n-- VERDICT --")
    if strong_id:
        print("  CONTRADICTIONS FOUND (strong). Evidence points AGAINST the claimed model identity.")
        print("  Do not trust this endpoint's model label without an explanation.")
        if against_service:
            print("  ALSO: service degradation detected (see ledger).")
    elif mod_id:
        print("  IDENTITY TENSIONS FOUND. Listed items deserve investigation before trusting.")
        if against_service:
            print("  ALSO: service degradation detected (see ledger).")
    elif against_service:
        print("  SERVICE DEGRADATION DETECTED (output cap / mutation found, "
              "but no model identity contradiction).")
        print("  The served model may still be genuine, but the gateway restricts it.")
    elif for_ev:
        print("  CONSISTENT: no identity contradictions found; some positive evidence exists.")
        print("  NOTE: black-box checks cannot PROVE authenticity. See limitations below.")
    elif weak_id:
        print("  WEAK SIGNALS ONLY (see ledger). Not actionable without a baseline or more samples.")
    else:
        print("  INCONCLUSIVE: no strong evidence in either direction.")
        print("  Add --baseline-base/--baseline-model for the strongest available check.")

    print("\n-- LIMITATIONS (read every time) --")
    print("  * Without baseline/logprobs/TEE attestation, authenticity is NOT provable.")
    print("  * Self-report agreement is never scored as positive evidence (injectable).")
    print("  * Token usage can be forged if the gateway re-tokenizes locally; the forced-output")
    print("    check cross-validates it against the raw text (R6) and caps its weight at WEAK.")
    print("  * Logprobs can also be forged (rare); treat them as the strongest non-attested")
    print("    signal, not as proof.")
    print("  * A pass means 'no contradiction found', not 'confirmed genuine'.")
    print("=" * 64)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def run(args):
    key = args.key or os.environ.get("VERIFY_API_KEY") or ""
    probes = [p.strip() for p in args.probes.split(",") if p.strip()]
    seed = args.seed if args.seed is not None else random.randrange(1, 10 ** 6)
    rng = random.Random(seed)
    battery = make_battery(None if args.static_battery else rng)
    obs, evidence, errors = [], [], []
    battery_results, needle_info = [], None

    if not key:
        print("WARNING: no --key and VERIFY_API_KEY not set; requests will likely fail with 401.")

    call = make_caller(args.base, key, args.model, timeout=args.timeout)
    title = "%s @ %s" % (args.model, args.base)
    print("Probing %s (key %s)" % (title, ("****" + key[-4:]) if key else "none"))
    print("seed: %d (%s battery; pass --seed %d to reproduce)"
          % (seed, "static" if args.static_battery else "dynamic", seed))

    if "infra" in probes:
        probe_infra(call, args.model, obs, evidence, errors)
        time.sleep(args.sleep)
    if "service" in probes:
        probe_service(call, args.model, obs, evidence, errors, args.sleep)
    if "caps" in probes:
        battery_results, needle_info = probe_caps(
            call, battery, obs, evidence, errors, args.sleep,
            0 if args.skip_needle else args.needle_chars, rng)
    if "self" in probes:
        probe_self(call, args.model, obs, evidence, errors, args.sleep)

    if args.baseline_base and args.baseline_model:
        bkey = args.baseline_key or os.environ.get("VERIFY_BASELINE_API_KEY") or ""
        bcall = make_caller(args.baseline_base, bkey, args.baseline_model, timeout=args.timeout)
        print("Baseline: %s @ %s" % (args.baseline_model, args.baseline_base))
        if battery_results:
            probe_diff(call, bcall, battery, battery_results, obs, evidence, errors, args.sleep)
        else:
            errors.append("diff skipped: run with --probes including 'caps'")
        if args.logprobs:
            probe_logprobs(call, bcall, obs, evidence, errors, args.sleep)
    elif args.logprobs:
        errors.append("--logprobs requires --baseline-base/--baseline-model (nothing to compare)")

    if errors and not obs and not evidence:
        print("\nENDPOINT UNAVAILABLE: every probe failed. Dead endpoint or wrong base path,")
        print("which is different from 'fake'. First error: %s" % errors[0])

    render(obs, evidence, errors, title)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({
                "title": title,
                "seed": seed,
                "battery_static": bool(args.static_battery),
                "battery_spec": [{"id": b["id"], "prompt": b["prompt"],
                                  "kind": b["kind"], "expect": b["expect"]} for b in battery],
                "observations": obs,
                "evidence": [{"tier": t, "direction": d, "text": x} for t, d, x in evidence],
                "errors": errors,
                "battery": battery_results,
                "needle": needle_info,
            }, f, indent=2)
        print("JSON report written: %s" % args.out)


# ----------------------------------------------------------------------------
# Selftest (no network beyond localhost)
# ----------------------------------------------------------------------------


def selftest():
    import contextlib
    import http.server
    import io
    import threading

    passed, failed = [], []

    def check(name, cond):
        (passed if cond else failed).append(name)

    # --- grading ---
    check("grade num exact", grade_item(BATTERY[0], "497"))
    check("grade num prose", grade_item(BATTERY[0], "The answer is 497."))
    check("grade num reject wrong", not grade_item(BATTERY[0], "498"))
    check("grade num reject multi-guess", not grade_item(BATTERY[3], "1, 2, 3, 4, 5 -> total is 4"))
    check("grade num accept final-number", grade_item(BATTERY[3], "Counting 1, 2, 3, 4, so total is 5."))
    check("grade num reject rambling", not grade_item(BATTERY[0], "x" * 90))
    check("grade day", grade_item(BATTERY[1], "Friday"))
    check("grade mod", grade_item(BATTERY[2], "13"))
    check("grade letters", grade_item(BATTERY[3], "5"))
    check("grade nthword", grade_item(BATTERY[4], "the"))
    check("grade speed", grade_item(BATTERY[5], "160 km"))
    check("grade logic", grade_item(BATTERY[6], "c"))
    check("grade lines3 ok", grade_item(BATTERY[7], "Paris\nBerlin\nRome"))
    check("grade lines3 reject bullets", not grade_item(BATTERY[7], "- Paris\n- Berlin\n- Rome"))
    check("grade lines3 reject 2", not grade_item(BATTERY[7], "Paris\nBerlin"))

    # --- dynamic battery & ordinal ---
    dyn1 = make_battery(random.Random(42))
    dyn2 = make_battery(random.Random(99))
    check("dynamic battery varies with seed", dyn1[0]["expect"] != dyn2[0]["expect"])
    check("dynamic battery is gradable", all(grade_item(b, b["expect"])
                                             or b["kind"] == "lines3" for b in dyn2))
    check("ordinal 3rd", ordinal(3) == "3rd")
    check("ordinal 4th", ordinal(4) == "4th")
    check("ordinal 11th", ordinal(11) == "11th")

    # --- identity / families ---
    r = classify_identity("I am Claude 3.5 Sonnet, created by Anthropic.", "gpt-5")
    check("identity contradiction", r["verdict"] == "CONTRADICTION")
    r = classify_identity("I am GPT-5 by OpenAI.", "gpt-5")
    check("identity match", r["verdict"] == "MATCH_CLAIM")
    r = classify_identity("I am a helpful AI assistant.", "gpt-5")
    check("identity generic", r["verdict"] == "GENERIC")
    check("no FP on verb 'grok' (truly)", "xai" not in families_in("I truly grok your question."))
    check("no FP on verb 'grok' (completely)", "xai" not in families_in("I completely grok your question."))
    check("no FP on 'grokking'", "xai" not in families_in("grokking is a phenomenon in deep learning"))
    check("detects 'Grok 3' digit form", "xai" in families_in("I am Grok 3 built by xAI."))
    check("detects 'grok-2' lowercase digit", "xai" in families_in("grok-2 is the model id"))
    check("detects capitalized noun mid-sentence", "xai" in families_in("I am Grok, created by xAI."))
    check("no FP on sentence-initial verb", "xai" not in families_in("Grok is a verb."))
    check("detects sentence-initial noun with context", "xai" in families_in("Grok is xAI's model."))
    check("no FP on substring 'glm'", "zhipu" not in families_in("This is a doglma example."))
    check("detects GLM model id", "zhipu" in families_in("I am GLM-4 from Zhipu."))

    # --- misc helpers ---
    check("cutoff extract", extract_cutoff("My cutoff is 2025-08.") == "2025-08")
    check("sse detect", looks_like_sse("data: {}\n\n"))
    check("sse reject json", not looks_like_sse('{"a":1}'))
    check("retry-after parse", _parse_retry_after({"Retry-After": "7"}, 15.0) == 7.0)
    check("retry-after default", _parse_retry_after({}, 15.0) == 15.0)
    fields = scan_wrapper_fields({"base_resp": {}, "id": "x", "_status": 200})
    check("wrapper field base_resp", any("base_resp" in f for f in fields))
    check("wrapper field ignores internal", all(not f.startswith("_") for f in fields))
    check("overhead classify", "minimal" in classify_overhead(5))
    check("overhead classify heavy", "notable" in classify_overhead(500))
    sane, ratio = _ratio_sane("a" * 40, 10)
    check("ratio sane band", sane and 3.9 < ratio < 4.1)
    sane2, _ = _ratio_sane("a" * 40, 100)
    check("ratio rejects weird", not sane2)

    # --- needle ---
    rng = random.Random(1)
    c1, c2 = make_codename(rng), make_codename(rng)
    hay = build_dual_needle(4000, c1, c2, rng)
    check("dual needle embedded", (c1 in hay) and (c2 in hay))
    check("needle grade", grade_needle("codes: " + c1, c1))
    check("needle reject", not grade_needle("no idea", c1))

    # --- generic canned response helper ---
    def canned(content, ct=None, pt=5, reason="stop", reasoning=None):
        resp = {"id": "x", "model": "m",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": reason}],
                "usage": {"prompt_tokens": pt,
                          "completion_tokens": ct if ct is not None else max(1, len(content.split()))},
                "_status": 200, "_headers": {}, "_latency_s": 0.1}
        if reasoning is not None:
            resp["choices"][0]["message"]["reasoning_content"] = reasoning
        return resp

    # --- probe_infra ---
    def infra_fake(messages, max_tokens=5, temperature=None, extra=None):
        return {"id": "x", "model": "m", "base_resp": {"code": 0},
                "choices": [{"index": 0, "message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 3},
                "_status": 200, "_headers": {"X-Gateway": "one-api"}, "_latency_s": 0.05}
    infra_fake.state = {}
    obs, ev, errs = [], [], []
    probe_infra(infra_fake, "m", obs, ev, errs)
    check("infra detects wrapper field", any("base_resp" in o for o in obs))
    check("infra detects x-headers", any("x- HTTP headers" in o for o in obs))

    # --- probe_service ---
    def svc_fake(messages, max_tokens=500, temperature=None, extra=None):
        q = messages[-1]["content"]
        if q == "PONG":
            return canned("PONG", ct=1, pt=4)
        if q.lower().startswith("think step by step"):
            return canned("408", ct=3, pt=8, reasoning="17*24 = 408")
        if q.startswith("Write exactly"):
            return canned(" ".join(["word"] * 30), ct=30, pt=6, reason="length")
        return canned("ok", ct=1)
    obs, ev, errs = [], [], []
    probe_service(svc_fake, "m", obs, ev, errs, 0)
    check("service records overhead", any("prompt_tokens for 'PONG'" in o for o in obs))
    check("service flags output cap as SERVICE evidence",
          any(t == "STRONG" and d == "AGAINST-SERVICE" for t, d, _ in ev))
    check("service records tok/s", any("generation speed" in o for o in obs))

    # --- probe_caps (with reasoning_content needle fallback) ---
    ans = {}
    for b in BATTERY:
        ans[b["prompt"]] = "Paris\nBerlin\nRome" if b["kind"] == "lines3" else b["expect"]

    def caps_fake(messages, max_tokens=500, temperature=None, extra=None):
        q = messages[-1]["content"]
        if q in ans:
            return canned(ans[q], ct=3)
        if "access code" in q:
            codes = re.findall(r"code for this document is ([A-Z]+-[A-Z]+-\d{4})", q)
            if len(codes) >= 2:
                return canned("", ct=8,
                              reasoning="The ALPHA code is %s and the OMEGA code is %s."
                                        % (codes[0], codes[1]))
        return canned("idk", ct=1)
    rng2 = random.Random(7)
    obs, ev, errs = [], [], []
    results, ninfo = probe_caps(caps_fake, BATTERY, obs, ev, errs, 0, 4000, rng2)
    check("caps battery runs", sum(1 for r in results if r["pass"]) >= 7)
    check("caps needle passes via reasoning_content",
          ninfo is not None and ninfo["pass"] and ninfo["pass_25pct"] and ninfo["pass_75pct"])

    # --- probe_self ---
    def self_fake(messages, max_tokens=500, temperature=None, extra=None):
        q = messages[-1]["content"]
        if "which ai model" in q.lower() or "state your exact model" in q.lower():
            return canned("I am Claude, made by Anthropic.", ct=6)
        if "cutoff" in q.lower():
            return canned("2025-08", ct=2)
        return canned("idk", ct=1)
    self_fake.state = {}
    obs, ev, errs = [], [], []
    probe_self(self_fake, "gpt-5", obs, ev, errs, 0)
    check("probe_self flags contradiction", any(t == "STRONG" and d == "AGAINST" for t, d, _ in ev))
    check("probe_self records cutoff", any("2025-08" in o for o in obs))

    # --- probe_diff: full agreement path ---
    def diff_fake(wrong_forced=False):
        def f(messages, max_tokens=500, temperature=None, extra=None):
            q = messages[-1]["content"]
            if q in ans:
                return canned(ans[q], ct=3)
            if q.startswith("Repeat the following"):
                if wrong_forced:
                    return canned("the old clock tower chimes", ct=6)
                return canned(FORCED, ct=max(1, len(FORCED) // 4))
            if q == OPEN_DIFF_PROMPT:
                return canned("TCP is connection-oriented and guarantees ordered delivery while "
                              "UDP is connectionless and does not.", ct=18)
            return canned("idk", ct=1)
        return f
    s_results = [{"id": b["id"], "pass": True, "output": ans[b["prompt"]]} for b in BATTERY]
    obs, ev, errs = [], [], []
    probe_diff(diff_fake(), diff_fake(), BATTERY, s_results, obs, ev, errs, 0)
    check("diff battery agreement evidence", any("high battery agreement" in x for _, _, x in ev))
    check("diff tokenizer WEAK FOR after cross-check",
          any(t == "WEAK" and d == "FOR" and "tokenizer" in x for t, d, x in ev))
    check("diff open-ended WEAK FOR", any(t == "WEAK" and d == "FOR" and "Jaccard" in x
                                          for t, d, x in ev))

    # --- probe_diff: cross-check must veto the tokenizer signal ---
    obs, ev, errs = [], [], []
    probe_diff(diff_fake(wrong_forced=True), diff_fake(), BATTERY, s_results, obs, ev, errs, 0)
    check("diff cross-check vetoes tokenizer evidence",
          not any("tokenizer" in x for _, _, x in ev))
    check("diff cross-check recorded skip", any("skipped" in o for o in obs))

    # --- probe_logprobs: match / mismatch / stripping ---
    def lp_fake(tok, lp):
        def f(messages, max_tokens=500, temperature=None, extra=None):
            if not (extra and extra.get("logprobs")):
                return canned(tok, ct=1)
            return {"id": "x", "model": "m",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": tok},
                                 "logprobs": {"content": [{"token": tok, "logprob": lp,
                                              "top_logprobs": [{"token": tok, "logprob": lp}]}]}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                    "_status": 200, "_headers": {}, "_latency_s": 0.05}
        return f

    def lp_ignore(messages, max_tokens=500, temperature=None, extra=None):
        return canned("Paris", ct=1)
    obs, ev, errs = [], [], []
    probe_logprobs(lp_fake(" Paris", -0.20), lp_fake(" Paris", -0.25), obs, ev, errs, 0)
    check("logprobs match -> STRONG FOR",
          any(t == "STRONG" and d == "FOR" and "logprobs" in x for t, d, x in ev))
    obs, ev, errs = [], [], []
    probe_logprobs(lp_fake(" London", -0.20), lp_fake(" Paris", -0.25), obs, ev, errs, 0)
    check("logprobs mismatch -> STRONG AGAINST",
          any(t == "STRONG" and d == "AGAINST" and "logprobs" in x for t, d, x in ev))
    obs, ev, errs = [], [], []
    probe_logprobs(lp_ignore, lp_fake(" Paris", -0.25), obs, ev, errs, 0)
    check("logprobs stripping recorded as service note",
          any("FEATURE STRIPPING" in o for o in obs) and not ev)

    # --- _extract_top_token: both API shapes ---
    chat_style = {"choices": [{"logprobs": {"content": [
        {"token": " Paris", "logprob": -0.3,
         "top_logprobs": [{"token": " Paris", "logprob": -0.3},
                          {"token": " Lyon", "logprob": -1.2}]}]}}]}
    comp_style = {"choices": [{"logprobs": {"top_logprobs": [{"Paris": -0.3, "Lyon": -1.2}]}}]}
    check("logprobs chat shape", _extract_top_token(chat_style)[0] == " Paris")
    check("logprobs completion shape", _extract_top_token(comp_style)[0] == "Paris")

    # --- render verdict separation ---
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render([], [("STRONG", "AGAINST-SERVICE", "cap")], [], "t")
    out = buf.getvalue()
    check("render: service-only -> SERVICE DEGRADATION",
          "SERVICE DEGRADATION DETECTED" in out and "CONTRADICTIONS FOUND" not in out)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render([], [("STRONG", "AGAINST", "identity contradiction")], [], "t")
    out = buf.getvalue()
    check("render: identity -> CONTRADICTIONS", "CONTRADICTIONS FOUND" in out)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render([], [("WEAK", "AGAINST", "single-sample jaccard")], [], "t")
    out = buf.getvalue()
    check("render: weak identity alone -> WEAK SIGNALS ONLY",
          "WEAK SIGNALS ONLY" in out and "CONTRADICTIONS FOUND" not in out)

    # --- real HTTP round-trip (localhost mock) ---
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln)
            if self.path.startswith("/sse"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[]}\n\ndata: [DONE]\n\n')
                return
            if not self.path.startswith("/v1"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.loads(body or b"{}")
            resp = {"id": "x", "model": payload.get("model", "?"),
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": "PONG"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d/v1" % port
        c = make_caller(base, "test-key", "m1", timeout=10)
        resp = c([{"role": "user", "content": "ping"}], max_tokens=5)
        check("http roundtrip content", content_of(resp)[0] == "PONG")
        check("http roundtrip usage", usage_of(resp).get("prompt_tokens") == 3)
        check("http latency tracked", isinstance(resp.get("_latency_s"), float)
              and resp["_latency_s"] >= 0)
        st, hd, body, err, _ = http_post_json("http://127.0.0.1:%d/sse" % port, "",
                                              {"a": 1}, timeout=10)
        check("sse detection over http", looks_like_sse(body))
        c2 = make_caller("http://127.0.0.1:%d" % port, "test-key", "m1", timeout=10)
        resp2 = c2([{"role": "user", "content": "ping"}], max_tokens=5)
        check("base auto-fix to /v1", content_of(resp2)[0] == "PONG"
              and c2.state.get("fixed") is True)
    finally:
        srv.shutdown()

    # --- 429 retry respecting Retry-After (sleep patched) ---
    class H429(http.server.BaseHTTPRequestHandler):
        hits = 0

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            self.rfile.read(ln)
            if self.path.startswith("/v1") and H429.hits >= 2:
                data = json.dumps({"id": "x", "model": "m",
                                   "choices": [{"index": 0, "finish_reason": "stop",
                                                "message": {"content": "PONG"}}],
                                   "usage": {"prompt_tokens": 3}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            H429.hits += 1
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv429 = http.server.HTTPServer(("127.0.0.1", 0), H429)
    port429 = srv429.server_address[1]
    threading.Thread(target=srv429.serve_forever, daemon=True).start()
    real_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        c3 = make_caller("http://127.0.0.1:%d/v1" % port429, "k", "m1", timeout=10)
        r3 = c3([{"role": "user", "content": "ping"}], max_tokens=5)
        check("429 retry with Retry-After succeeds", content_of(r3)[0] == "PONG")
    finally:
        time.sleep = real_sleep
        srv429.shutdown()

    print("SELFTEST (v2.2): %d passed, %d failed" % (len(passed), len(failed)))
    for f in failed:
        print("  FAILED: %s" % f)
    return 1 if failed else 0


def main(argv):
    if len(argv) > 1 and argv[1] == "selftest":
        return selftest()
    import argparse
    p = argparse.ArgumentParser(description="LLM gateway evidence collector v2.2 (honest black-box model)")
    p.add_argument("--base", required=True, help="Base URL, e.g. https://api.example.com/v1")
    p.add_argument("--model", required=True, help="Model ID as claimed by the gateway")
    p.add_argument("--key", default="", help="API key (or set VERIFY_API_KEY). Never printed.")
    p.add_argument("--probes", default="infra,service,caps,self")
    p.add_argument("--baseline-base", default="",
                   help="OpenAI-compatible base URL for the official baseline")
    p.add_argument("--baseline-model", default="")
    p.add_argument("--baseline-key", default="")
    p.add_argument("--logprobs", action="store_true",
                   help="Run the logprobs distribution probe (requires baseline)")
    p.add_argument("--needle-chars", type=int, default=120000)
    p.add_argument("--skip-needle", action="store_true")
    p.add_argument("--static-battery", action="store_true",
                   help="Use the fixed calibration battery instead of the randomized one")
    p.add_argument("--sleep", type=float, default=SLEEP_BETWEEN, help="Seconds between requests")
    p.add_argument("--timeout", type=int, default=TIMEOUT)
    p.add_argument("--seed", type=int, default=None,
                   help="RNG seed for reproducible dynamic battery and needle codes")
    p.add_argument("--out", default="", help="Write JSON report to this path")
    args = p.parse_args(argv[1:])
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
