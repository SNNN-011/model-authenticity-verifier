#!/usr/bin/env python3
# model-authenticity-verifier v3.0 - single-method tokenizer-diff collector.
# Method: delta(T) = tokens(T) - tokens("a") on the same path, compared to family signatures.
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

VERSION = "3.0"
TOOL = "model-authenticity-verifier"
CONTROL_TEXT = "a"
USER_AGENT = "model-authenticity-verifier/3.0"
# Reasoning routes burn the budget and answer 502; prompt_tokens does not depend on max_tokens.
MAX_TOKENS_LADDER = [64, 512, 2048]
RETRYABLE_HTTP = (429, 500, 502, 503, 504)

# Probe texts: the public text set the reference signatures were measured with
# (control "a" plus five family-dividing texts). The exact strings matter: the
# signatures below only compare against deltas taken from these texts.
PROBES = [
    ("T1 id+emoji", "Aku menuduh kau berbohong tentang model ini \U0001F521"),
    ("T2 jv+price", "Aku tuku sega pecel Rp17.500 ing pasar, rasane enak tenan"),
    ("T3 js_code", "const total = items.reduce((a, b) => a + b.price, 0);"),
    ("T4 cjk+emoji+math", "\u6df7\u5408\u4e2d\u6587\u3001\u65e5\u672c\u8a9e\u3068emoji \U0001F30C\U0001F680 \u3068\u8a08\u7b97: 17\u00d723=391\u3001128\u00f74=32"),
    ("T5 arab+misc", "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645 \u0661\u0662\u0663 \u03a9\u22483.14 \u2211(x_i) \u2014 na\u00efve caf\u00e9"),
]

# Reference signatures: 5 deltas (T1..T5).
# Only the deepseek family was measured with the probe texts above, so it is the
# only signature this collector can judge. The other families are known as
# families, but their numbers come from the 2026-10-29 scan, whose texts were
# never published, so they stay out of the comparison (R6).
REFERENCE = {
    "deepseek": [14, 20, 18, 27, 22],
}

# ids both resolve. `deepseek-v4-flash` matches none of these on purpose.
_MODEL_KEYS = {
    "anthropic-core": ["anthropic/claude-opus-4.7", "claude-opus-4.8", "claude-opus-5", "anthropic/claude-sonnet-5"],
    "anthropic-4.6": ["anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6"],
    "openai": ["openai/gpt-5.6-terra", "openai/gpt-5.6-sol"],
    "deepseek": ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"],
}

def _family_keys():
    keys = []
    for family, models in _MODEL_KEYS.items():
        for m in models:
            keys.append((m, family))
            if "/" in m:
                keys.append((m.rsplit("/", 1)[1], family))
    # de-dup, longest key first so specific keys beat generic ones
    seen = {}
    for key, family in keys:
        seen[key.lower()] = family
    out = sorted(seen.items(), key=lambda kv: len(kv[0]), reverse=True)
    return out

def map_family(model_id):
    mid = (model_id or "").lower()
    for key, family in _family_keys():
        if key in mid:
            return family
    return None

def tolerance(reference):
    return max(2, math.ceil(0.08 * reference))

def compute_delta(tokens_text, tokens_control):
    return tokens_text - tokens_control

def text_matches(delta, reference):
    return abs(delta - reference) <= tolerance(reference)

def parse_usage_tokens(body):
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    for field in ("input_tokens", "prompt_tokens"):
        v = usage.get(field)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
    return None

def family_verdict(matches):
    # 5 of 5 is the only clean family agreement. A single text that misses is an
    # anomaly, not a family split (R3), so 4 of 5 stays undecided and 3 or fewer
    # is a disagreement.
    if matches >= 5:
        return "SIGNATURE MATCH"
    if matches == 4:
        return "PARTIAL MATCH"
    return "SIGNATURE MISMATCH"

def is_unstable(control_counts):
    return len(set(control_counts)) > 1

# Decision order: endpoint unavailable > no usage reported > probe incomplete >
# unstable accounting > family verdict.
def decide(endpoint_failed=False, usage_missing=False, incomplete=False,
           unstable=False, family=None, matches=None):
    if endpoint_failed:
        return "ENDPOINT UNAVAILABLE"
    if usage_missing:
        return "NO USAGE REPORTED"
    if incomplete:
        return "PROBE INCOMPLETE"
    if unstable:
        return "UNSTABLE ACCOUNTING"
    if family is None:
        return "UNMEASURED FAMILY"
    if matches is None:
        return "UNMEASURED FAMILY"
    return family_verdict(matches)

def load_texts(path):
    texts = []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            if "=" in line:
                name, content = line.split("=", 1)
                name, content = name.strip(), content
            else:
                name, content = "line%d" % (len(texts) + 1), line
            texts.append((name, content))
    return texts

def post_json(url, payload, key, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))

def probe_once(base, model, key, text, dialect_holder, timeout, attempts=3):
    """One prompt-only request, with a max_tokens ladder and retries.

    A reasoning route burns its budget and answers 502, so the ladder climbs
    instead of failing; prompt_tokens does not change with max_tokens. A
    transient drop is retried, because one dropped connection is not a dead
    route (R4). A 404/405 on the OpenAI dialect switches the run to the
    Anthropic dialect.
    """
    last = "endpoint did not answer"
    switched = False
    for rung, max_tokens in enumerate(MAX_TOKENS_LADDER):
        path = "/chat/completions" if dialect_holder[0] == "openai" else "/messages"
        body = {"model": model, "messages": [{"role": "user", "content": text}],
                "max_tokens": max_tokens, "stream": False}
        for attempt in range(attempts):
            try:
                _, resp_body = post_json(base.rstrip("/") + path, body, key, timeout)
                return resp_body
            except urllib.error.HTTPError as e:
                code = e.code
                if code in (404, 405) and dialect_holder[0] == "openai" and not switched:
                    dialect_holder[0] = "anthropic"
                    switched = True
                    break
                last = "endpoint returned HTTP %d" % code
                if code in RETRYABLE_HTTP:
                    if code == 502 and rung + 1 < len(MAX_TOKENS_LADDER):
                        break
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ProbeFailure(last)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = "endpoint did not answer (%s)" % type(e).__name__
                time.sleep(1.5 * (attempt + 1))
                continue
        if switched:
            break
    if switched:
        return probe_once(base, model, key, text, dialect_holder, timeout, attempts)
    raise ProbeFailure(last)

class ProbeFailure(Exception):
    pass

def build_report(base, model, dialect, control_tokens_list, probe_results, family,
                 signature, matches, verdict, note, failed_probes=None):
    report = {
        "tool": TOOL,
        "version": VERSION,
        "base": base,
        "model": model,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dialect": dialect,
        "control": {"text": CONTROL_TEXT, "tokens": control_tokens_list[0] if control_tokens_list else None},
        "reference": ({"family": family, "signature": signature} if family else None),
        "probes": probe_results,
        "matches": matches,
        "verdict": verdict,
        "note": note,
        "failed_probes": list(failed_probes or []),
    }
    return report

def run(args):
    if not args.base or not args.model:
        print("error: --base and --model are required", file=sys.stderr)
        return 1
    key = args.key or os_environ_key()
    if not key:
        print("error: API key required via --key or VERIFY_API_KEY", file=sys.stderr)
        return 1
    if args.repeat < 1:
        print("error: --repeat must be >= 1", file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("error: --timeout must be > 0", file=sys.stderr)
        return 1
    if args.sleep < 0:
        print("error: --sleep must be >= 0", file=sys.stderr)
        return 1

    custom_texts = bool(args.texts)
    if custom_texts:
        try:
            texts = load_texts(args.texts)
        except OSError as e:
            print("error: cannot read --texts file: %s" % e, file=sys.stderr)
            return 1
        if not texts:
            print("error: --texts file contained no texts", file=sys.stderr)
            return 1
    else:
        texts = PROBES

    family = None if custom_texts else map_family(args.model)
    signature = REFERENCE.get(family) if family else None

    dialect_holder = ["openai"]
    control_tokens_all = []
    probe_bodies = []  # raw tokens per probe for report (first repeat only)
    endpoint_error = None
    usage_missing = False
    failed_probes = []

    for rep in range(args.repeat):
        # control first, then probes, same path, same body shape
        try:
            ctrl_body = probe_once(args.base, args.model, key, CONTROL_TEXT, dialect_holder, args.timeout)
        except ProbeFailure as e:
            endpoint_error = str(e)
            break
        ctrl_tok = parse_usage_tokens(ctrl_body)
        if ctrl_tok is None:
            usage_missing = True
            break
        control_tokens_all.append(ctrl_tok)
        if rep == 0:
            probe_bodies = []
        sleep_between(args.sleep)
        for i, (name, text) in enumerate(texts):
            try:
                body = probe_once(args.base, args.model, key, text, dialect_holder, args.timeout)
            except ProbeFailure as e:
                failed_probes.append("%s (%s)" % (name, e))
                continue
            tok = parse_usage_tokens(body)
            if tok is None:
                failed_probes.append("%s (no token usage in the response)" % name)
                continue
            if rep == 0:
                probe_bodies.append((name, text, tok))
            if i < len(texts) - 1 or rep < args.repeat - 1:
                sleep_between(args.sleep)
        if endpoint_error or usage_missing:
            break

    dialect = dialect_holder[0]
    unstable = (args.repeat > 1 and len(control_tokens_all) > 1 and is_unstable(control_tokens_all))

    probe_results = []
    matches = None
    if not endpoint_error and not usage_missing and probe_bodies:
        ctrl = control_tokens_all[0]
        matched = 0
        for idx, (name, text, tok) in enumerate(probe_bodies):
            delta = compute_delta(tok, ctrl)
            ref = signature[idx] if signature and idx < len(signature) else None
            # unstable accounting: deltas reported raw, no match scoring at all (R5)
            m = (text_matches(delta, ref) if ref is not None and not unstable and not failed_probes else None)
            if m:
                matched += 1
            probe_results.append({
                "name": name,
                "tokens": tok,
                "delta": delta,
                "reference": ref,
                "match": m,
                "chars": len(text),
            })
        matches = (matched if signature and not unstable and not failed_probes else None)

    if endpoint_error:
        verdict = decide(endpoint_failed=True)
        note = "%s; no usable answer, which is not evidence about the model (R4)" % endpoint_error
    elif usage_missing:
        verdict = decide(usage_missing=True)
        note = "responses carried no token usage; the method could not run"
    elif failed_probes:
        verdict = decide(incomplete=True)
        note = "the route answered but %d of %d texts could not be measured (%s); the deltas below are partial, re-run (R4)" % (
            len(failed_probes), len(texts), "; ".join(failed_probes))
    elif unstable:
        verdict = decide(unstable=True)
        note = "control token count moved across repeats (%s); usage accounting is not stable, no conclusion drawn (R5)" % (
            ",".join(str(c) for c in control_tokens_all))
    elif custom_texts:
        verdict = decide(family=None)
        note = "alternate text set; reference comparison skipped, deltas reported raw"
    elif family is None:
        verdict = decide(family=None)
        note = "no reference signature for the claimed model; deltas reported raw so they can be baselined later"
    elif signature is None:
        verdict = decide(family=None)
        note = "the %s family is known, but no signature was measured with these texts; deltas reported raw (R6)" % family
    else:
        verdict = decide(family=family, matches=matches)
        note = verdict_note(verdict, matches)

    report = build_report(args.base, args.model, dialect, control_tokens_all,
                          probe_results, family, signature,
                          matches, verdict, note, failed_probes)

    print("base: %s" % args.base)
    print("model: %s" % args.model)
    print("dialect: %s" % dialect)
    if control_tokens_all:
        print("control \"%s\": %s token(s)" % (CONTROL_TEXT, ",".join(str(c) for c in control_tokens_all)))
    for p in probe_results:
        if p["reference"] is None:
            print("%s: tokens=%d delta=%d chars=%d" % (p["name"], p["tokens"], p["delta"], p["chars"]))
        else:
            match_s = "skip" if p["match"] is None else ("yes" if p["match"] else "no")
            print("%s: tokens=%d delta=%d ref=%d match=%s chars=%d" % (
                p["name"], p["tokens"], p["delta"], p["reference"], match_s, p["chars"]))
    if matches is not None:
        print("matches: %d/5" % matches)
    for f in failed_probes:
        print("not measured: %s" % f)
    print("verdict: %s" % verdict)
    print("note: %s" % note)

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            print("report written to %s" % args.out)
        except OSError as e:
            print("error: cannot write --out file: %s" % e, file=sys.stderr)
            return 1
    return 0

def verdict_note(verdict, matches):
    if verdict == "SIGNATURE MATCH":
        return "%d/5 texts agree with the claimed family; WEAK evidence for tokenizer dialect only, never weights (R1)" % matches
    if verdict == "PARTIAL MATCH":
        return "%d/5 texts agree; one text that misses is an anomaly, not a family split, re-run with fresh texts (R3)" % matches
    return "%d/5 texts agree; the tokenizer does not look like the claimed family's" % matches

def sleep_between(seconds):
    if seconds > 0:
        time.sleep(seconds)

def os_environ_key():
    return os.environ.get("VERIFY_API_KEY")

class VerifierArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print("error: %s" % message, file=sys.stderr)
        sys.exit(1)

def selftest():
    results = []

    def check(desc, fn):
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        results.append((desc, ok))

    # --- delta arithmetic & tolerance (group A, 16) ---
    check("delta subtracts control", lambda: compute_delta(50, 8) == 42)
    check("delta zero when equal", lambda: compute_delta(8, 8) == 0)
    check("delta can go negative", lambda: compute_delta(3, 8) == -5)
    check("tolerance ref 45 is 4", lambda: tolerance(45) == 4)
    check("tolerance ref 22 floors at 2", lambda: tolerance(22) == 2)
    check("tolerance ref 25 exact ceil is 2", lambda: tolerance(25) == 2)
    check("tolerance ref 47 is 4", lambda: tolerance(47) == 4)
    check("tolerance ref 10 floors at 2", lambda: tolerance(10) == 2)
    check("match at upper boundary", lambda: text_matches(49, 45))
    check("match at lower boundary", lambda: text_matches(41, 45))
    check("no match just above boundary", lambda: not text_matches(50, 45))
    check("match below reference within tol", lambda: text_matches(41, 45))
    check("match within tol ref 22", lambda: text_matches(24, 22))
    check("no match beyond tol ref 22", lambda: not text_matches(25, 22))
    check("match within tol ref 23", lambda: text_matches(25, 23))
    check("match within tol ref 46", lambda: text_matches(50, 46))

    # --- family mapping (groups B+C, 16) ---
    check("map opus-4.8 core", lambda: map_family("anthropic/claude-opus-4.8") == "anthropic-core")
    check("map bare opus-4.7 core", lambda: map_family("claude-opus-4.7") == "anthropic-core")
    check("map opus-5 core", lambda: map_family("claude-opus-5") == "anthropic-core")
    check("map sonnet-5 core", lambda: map_family("anthropic/claude-sonnet-5") == "anthropic-core")
    check("map opus-4.6 to 4.6", lambda: map_family("anthropic/claude-opus-4.6") == "anthropic-4.6")
    check("map sonnet-4.6 to 4.6", lambda: map_family("claude-sonnet-4.6") == "anthropic-4.6")
    check("map gpt-5.6-terra openai", lambda: map_family("openai/gpt-5.6-terra") == "openai")
    check("map gpt-5.6-sol openai", lambda: map_family("gpt-5.6-sol") == "openai")
    check("map deepseek-v4-pro", lambda: map_family("deepseek/deepseek-v4-pro") == "deepseek")
    check("map deepseek-v4.1-flash-1017", lambda: map_family("deepseek/deepseek-v4.1-flash-1017") == "deepseek")
    check("deepseek-v4-flash maps to no family", lambda: map_family("deepseek-v4-flash") is None)
    check("opus-4.6 does not map to anthropic-core", lambda: map_family("anthropic/claude-opus-4.6") != "anthropic-core")
    check("family match is case-insensitive", lambda: map_family("ANTHROPIC/Claude-Opus-4.8") == "anthropic-core")
    check("family keys sorted longest first", lambda: [len(k) for k, _ in _family_keys()] == sorted((len(k) for k, _ in _family_keys()), reverse=True))
    check("opus-4.7 does not map to 4.6", lambda: map_family("anthropic/claude-opus-4.7") == "anthropic-core")
    check("unknown model maps to no family", lambda: map_family("totally-unknown-model") is None)

    # --- usage parsing (group D, 8) ---
    check("parse openai prompt_tokens", lambda: parse_usage_tokens({"usage": {"prompt_tokens": 12}}) == 12)
    check("parse anthropic input_tokens", lambda: parse_usage_tokens({"usage": {"input_tokens": 34}}) == 34)
    check("input_tokens preferred over prompt_tokens", lambda: parse_usage_tokens({"usage": {"input_tokens": 34, "prompt_tokens": 12}}) == 34)
    check("empty usage yields None", lambda: parse_usage_tokens({"usage": {}}) is None)
    check("missing usage yields None", lambda: parse_usage_tokens({}) is None)
    check("non-int prompt_tokens rejected", lambda: parse_usage_tokens({"usage": {"prompt_tokens": "12"}}) is None)
    check("zero prompt_tokens is valid", lambda: parse_usage_tokens({"usage": {"prompt_tokens": 0}}) == 0)
    check("non-dict body yields None", lambda: parse_usage_tokens(None) is None)

    # --- verdict selection (groups E+F+G, 12) ---
    check("verdict 5/5 signature match", lambda: family_verdict(5) == "SIGNATURE MATCH")
    check("verdict 4/5 is undecided", lambda: family_verdict(4) == "PARTIAL MATCH")
    check("verdict 3/5 mismatch", lambda: family_verdict(3) == "SIGNATURE MISMATCH")
    check("verdict 2/5 mismatch", lambda: family_verdict(2) == "SIGNATURE MISMATCH")
    check("verdict 1/5 mismatch", lambda: family_verdict(1) == "SIGNATURE MISMATCH")
    check("verdict 0/5 mismatch", lambda: family_verdict(0) == "SIGNATURE MISMATCH")
    check("moved control is unstable", lambda: is_unstable([5, 5, 7]))
    check("steady control is stable", lambda: not is_unstable([5, 5, 5]))
    check("decision: endpoint unavailable beats all", lambda: decide(endpoint_failed=True, usage_missing=True, unstable=True, family="openai", matches=5) == "ENDPOINT UNAVAILABLE")
    check("decision: no usage beats unstable", lambda: decide(usage_missing=True, unstable=True, family="openai", matches=5) == "NO USAGE REPORTED")
    check("decision: unstable beats family verdict", lambda: decide(unstable=True, family="openai", matches=5) == "UNSTABLE ACCOUNTING")
    check("decision: unmeasured family when no family", lambda: decide(family=None) == "UNMEASURED FAMILY")

    # --- ladder, retries and the partial-run verdict (group I, 7) ---
    check("ladder starts at 64", lambda: MAX_TOKENS_LADDER[0] == 64)
    check("ladder ends at 2048", lambda: MAX_TOKENS_LADDER[-1] == 2048)
    check("ladder climbs", lambda: MAX_TOKENS_LADDER == sorted(MAX_TOKENS_LADDER))
    check("502 is retryable", lambda: 502 in RETRYABLE_HTTP)
    check("402 is not retryable", lambda: 402 not in RETRYABLE_HTTP)
    check("decision: incomplete beats unstable", lambda: decide(incomplete=True, unstable=True, family="openai", matches=5) == "PROBE INCOMPLETE")
    check("decision: endpoint beats incomplete", lambda: decide(endpoint_failed=True, incomplete=True) == "ENDPOINT UNAVAILABLE")

    # --- report safety & shape (group H, 2) ---
    sentinel = "sk-sentinel-never-in-report"
    report = build_report("https://gw.example.com/v1", "m", "openai", [5], [], "openai",
                          [24, 24, 22, 30, 29], 5, "SIGNATURE MATCH", "note")
    report_json = json.dumps(report)
    check("key material never appears in report", lambda: sentinel not in report_json and "key" not in report)
    check("report carries tool and version", lambda: report["tool"] == TOOL and report["version"] == VERSION)

    total = len(results)
    failed = [d for d, ok in results if not ok]
    passed = total - len(failed)

    if total != 61:
        print("SELFTEST (v3.0): internal error, expected 61 assertions, ran %d" % total, file=sys.stderr)
        return 2
    if failed:
        for d in failed:
            print("FAIL: %s" % d, file=sys.stderr)
        print("SELFTEST (v3.0): %d passed, %d failed" % (passed, len(failed)))
        return 2
    print("SELFTEST (v3.0): 61 passed, 0 failed")
    return 0

def main():
    # never let a non-ASCII probe name kill output on a legacy console codepage
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    argv = sys.argv[1:]
    if argv and argv[0] == "selftest":
        return selftest()

    parser = VerifierArgumentParser(
        description="Model-authenticity verifier: input-side tokenizer diff against measured family signatures.")
    parser.add_argument("--base", help="base URL of the gateway, e.g. https://gw.example.com/v1")
    parser.add_argument("--model", help="model id to check, e.g. anthropic/claude-opus-4.8")
    parser.add_argument("--key", help="API key; falls back to env VERIFY_API_KEY. Never printed or stored.")
    parser.add_argument("--sleep", type=float, default=2, help="seconds between requests (default 2)")
    parser.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds (default 60)")
    parser.add_argument("--repeat", type=int, default=1, help="repeat the probe set N times (default 1)")
    parser.add_argument("--out", help="write the full JSON report to FILE")
    parser.add_argument("--texts", help="alternate text set, one text per line, 'name=content' allowed")
    args = parser.parse_args()
    return run(args)

if __name__ == "__main__":
    sys.exit(main())
