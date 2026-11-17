#!/usr/bin/env python3
# model-authenticity-verifier v3.1 - single-method tokenizer-diff collector.
# Method: delta(T) = tokens(T) - tokens("a") on the same path, compared to family signatures.
import argparse
import contextlib
import hashlib
import http.server
import io
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

VERSION = "3.1"
TOOL = "model-authenticity-verifier"
CONTROL_TEXT = "a"
USER_AGENT = "model-authenticity-verifier/3.1"
# Reasoning routes burn the budget and answer 502; prompt_tokens does not depend on max_tokens.
MAX_TOKENS_LADDER = [64, 512, 2048]
RETRYABLE_HTTP = (429, 500, 502, 503, 504)
# Selftest only: skip retry backoff so the offline check stays fast.
FAST_SLEEP = False

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

# Baseline loading (fail closed). Active baselines live in baselines/*.json and
# carry a probe_set_id computed from PROBES below; changing one byte of the
# probe set invalidates every active baseline by design. Reference-only family
# names live in baselines/_reference_only.json and never supply a signature.
BASELINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "baselines"))
REFERENCE_ONLY_FILE = "_reference_only.json"
BASELINE_SCHEMA_VERSION = 1
BASELINE_CAVEAT = "third-party-gateway caveat: a wrapper can re-tokenize locally or forge usage, so this is WEAK evidence for the counting dialect, never the weights (R1)"
INFERRED_MODEL_CAVEAT = "model ini belum diukur langsung pada text set ini; dipetakan lewat keluarga"
SINGLE_ROUTE_CAVEAT = "single-route caveat: the signature was measured on a single route, so a route-specific wrapper cannot be ruled out (R1)"


class BaselineError(Exception):
    pass


def probe_set_id():
    payload = [CONTROL_TEXT] + [text for _, text in PROBES]
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]

# ids both resolve. `deepseek-v4-flash` matches none of these on purpose.
_MODEL_KEYS = {
    "anthropic-core": ["anthropic/claude-opus-4.7", "claude-opus-4.8", "claude-opus-5", "anthropic/claude-sonnet-5"],
    "anthropic-4.6": ["anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6"],
    "openai": ["openai/gpt-5.6-terra", "openai/gpt-5.6-sol"],
    "deepseek": ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"],
}


class ProbeFailure(Exception):
    pass


class RequestBudget:
    """Count every HTTP attempt, including retries and ladder rungs."""

    def __init__(self, limit=0):
        self.limit = int(limit or 0)
        self.used = 0
        self.exceeded = False

    def acquire(self):
        if self.limit and self.used >= self.limit:
            self.exceeded = True
            raise ProbeFailure("request budget exhausted after %d request(s)" % self.used)
        self.used += 1


def _origin(url):
    """(scheme, hostname, port) with default ports filled in; None if unparseable."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return (scheme, host, port)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that would carry the auth headers to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_url = req.full_url
        target = urllib.parse.urljoin(old_url, newurl)
        old_origin = _origin(old_url)
        new_origin = _origin(target)
        if old_origin is None or new_origin is None or old_origin != new_origin:
            raise ProbeFailure(
                "redirect to a different origin refused (HTTP %d): scheme, host or port changed; auth headers are not forwarded" % code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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


def _check_int_list(value, length, label, fname):
    """Fail-closed integer-list check: rejects a wrong length, booleans,
    floats, non-integers and negative elements. Error names the file."""
    if not isinstance(value, list) or len(value) != length:
        raise BaselineError("%s: %s must be a list of exactly %d integers" % (fname, label, length))
    for i, v in enumerate(value):
        if isinstance(v, bool):
            raise BaselineError("%s: %s[%d] is a boolean; integers only" % (fname, label, i))
        if isinstance(v, float):
            raise BaselineError("%s: %s[%d] is a float (%r); integers only" % (fname, label, i, v))
        if not isinstance(v, int):
            raise BaselineError("%s: %s[%d] is not an integer (%r)" % (fname, label, i, v))
        if v < 0:
            raise BaselineError("%s: %s[%d] is negative (%r)" % (fname, label, i, v))
    return list(value)


def _model_id_matches(requested, candidate):
    r = (requested or "").strip().lower()
    c = (candidate or "").strip().lower()
    if not r:
        return False
    return r == c or r.split("/")[-1] == c.split("/")[-1]


def _load_json_file(path, fname):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        raise BaselineError("%s: cannot read file: %s" % (fname, e))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BaselineError("%s: invalid JSON: %s" % (fname, e))
    if not isinstance(data, dict):
        raise BaselineError("%s: root must be a JSON object" % fname)
    version = data.get("schema_version")
    if (not isinstance(version, int) or isinstance(version, bool)
            or version != BASELINE_SCHEMA_VERSION):
        if isinstance(version, int) and not isinstance(version, bool) and version < 0:
            raise BaselineError("%s: schema_version %d is negative; expected %d" % (
                fname, version, BASELINE_SCHEMA_VERSION))
        raise BaselineError("%s: unknown schema_version %r (expected %d)" % (
            fname, version, BASELINE_SCHEMA_VERSION))
    return data


def _validate_reference_only(data, fname):
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise BaselineError("%s: root object must carry a 'rows' list" % fname)
    names = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BaselineError("%s: rows[%d] must be an object" % (fname, i))
        family = row.get("family")
        models = row.get("models")
        if not isinstance(family, str) or not family.strip():
            raise BaselineError("%s: rows[%d] missing non-empty 'family' string" % (fname, i))
        if (not isinstance(models, list) or not models
                or not all(isinstance(m, str) and m.strip() for m in models)):
            raise BaselineError("%s: rows[%d] family %r needs a non-empty 'models' list of strings" % (fname, i, family))
        if "signature" in row:
            raise BaselineError("%s: rows[%d] family %r carries a 'signature' key; "
                                "reference-only rows never supply a signature" % (fname, i, family))
        if "superseded_signature" in row:
            if row.get("superseded") is not True:
                raise BaselineError("%s: rows[%d] family %r carries superseded_signature without \"superseded\": true" % (fname, i, family))
            _check_int_list(row["superseded_signature"], len(PROBES), "superseded_signature", fname)
        names.append(family)
    return names


def _validate_active(data, fname):
    family = data.get("family")
    models = data.get("models")
    pid = data.get("probe_set_id")
    sig = data.get("signature")
    cr = data.get("control_range")
    provenance = data.get("provenance")
    models_measured = data.get("models_measured")
    models_inferred = data.get("models_inferred")
    for field, value in (("family", family), ("models", models), ("probe_set_id", pid),
                         ("signature", sig), ("control_range", cr), ("provenance", provenance),
                         ("models_measured", models_measured), ("models_inferred", models_inferred)):
        if value is None:
            raise BaselineError("%s: missing required field %r" % (fname, field))
    if data.get("superseded"):
        raise BaselineError("%s: active baseline %r is marked superseded; "
                            "superseded rows belong in %s" % (fname, family, REFERENCE_ONLY_FILE))
    if not isinstance(family, str) or not family.strip():
        raise BaselineError("%s: 'family' must be a non-empty string" % fname)
    if (not isinstance(models, list) or not models
            or not all(isinstance(m, str) and m.strip() for m in models)):
        raise BaselineError("%s: 'models' must be a non-empty list of non-empty strings" % fname)
    for m in models:
        if map_family(m) != family:
            raise BaselineError("%s: model %r does not map to family %r via map_family" % (fname, m, family))
    for field, value in (("models_measured", models_measured), ("models_inferred", models_inferred)):
        if not isinstance(value, list) or not all(isinstance(m, str) and m.strip() for m in value):
            raise BaselineError("%s: %r must be a list of non-empty strings" % (fname, field))
    measured = list(models_measured)
    inferred = list(models_inferred)
    if len(set(measured)) != len(measured) or len(set(inferred)) != len(inferred):
        raise BaselineError("%s: 'models_measured' and 'models_inferred' must not contain duplicates" % fname)
    if set(measured) & set(inferred):
        raise BaselineError("%s: 'models_measured' and 'models_inferred' overlap: %s" % (
            fname, ", ".join(sorted(set(measured) & set(inferred)))))
    if set(measured) | set(inferred) != set(models):
        missing = sorted(set(models) - (set(measured) | set(inferred)))
        extra = sorted((set(measured) | set(inferred)) - set(models))
        raise BaselineError("%s: 'models_measured' and 'models_inferred' must cover exactly the 'models' list "
                            "(missing: %s; extra: %s)" % (fname, ", ".join(missing) or "-", ", ".join(extra) or "-"))
    if not measured:
        raise BaselineError("%s: 'models_measured' must not be empty; at least one model must be measured on this text set" % fname)
    if pid != probe_set_id():
        raise BaselineError("%s: probe_set_id %r does not match the current probe set %r; "
                            "the baseline is invalidated" % (fname, pid, probe_set_id()))
    sig = _check_int_list(sig, len(PROBES), "'signature'", fname)
    cr = _check_int_list(cr, 2, "'control_range'", fname)
    if cr[0] > cr[1]:
        raise BaselineError("%s: 'control_range' min %d exceeds max %d" % (fname, cr[0], cr[1]))
    if not isinstance(provenance, dict):
        raise BaselineError("%s: 'provenance' must be an object" % fname)
    if provenance.get("endpoint_kind") != "third-party-gateway":
        raise BaselineError("%s: provenance.endpoint_kind must be 'third-party-gateway'" % fname)
    routes = provenance.get("routes")
    if (not isinstance(routes, list) or not routes
            or not all(isinstance(r, str) and r.strip() for r in routes)):
        raise BaselineError("%s: provenance.routes must be a non-empty list of strings" % fname)
    if not isinstance(provenance.get("date"), str) or not provenance["date"].strip():
        raise BaselineError("%s: provenance.date must be a non-empty string" % fname)
    n_runs = provenance.get("n_runs")
    if isinstance(n_runs, bool):
        raise BaselineError("%s: provenance.n_runs must be an integer >= 1, got boolean" % fname)
    if isinstance(n_runs, float):
        raise BaselineError("%s: provenance.n_runs must be an integer >= 1, got float %r" % (fname, n_runs))
    if not isinstance(n_runs, int) or n_runs < 1:
        raise BaselineError("%s: provenance.n_runs must be an integer >= 1" % fname)
    if not isinstance(provenance.get("notes"), str):
        raise BaselineError("%s: provenance.notes must be a string" % fname)
    confidence = data.get("confidence")
    if confidence is not None and not isinstance(confidence, str):
        raise BaselineError("%s: 'confidence' must be a string when present" % fname)
    return {
        "family": family,
        "models": list(models),
        "models_measured": measured,
        "models_inferred": inferred,
        "probe_set_id": pid,
        "signature": list(sig),
        "control_range": list(cr),
        "confidence": confidence,
        "provenance": dict(provenance),
        "source": fname,
    }


def load_baselines(directory=None):
    """Load and validate baselines/*.json. Fail closed: every bad file raises
    BaselineError naming the file; nothing is skipped silently.

    Returns (active_by_family, reference_only_names). Reference-only rows are
    used for lookup only and never supply a signature.
    """
    directory = directory or BASELINE_DIR
    try:
        entries = sorted(os.listdir(directory))
    except OSError as e:
        raise BaselineError("cannot read baseline directory %s: %s" % (directory, e))
    active = {}
    reference_only_names = []
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        path = os.path.join(directory, fname)
        data = _load_json_file(path, fname)
        if fname == REFERENCE_ONLY_FILE:
            reference_only_names.extend(_validate_reference_only(data, fname))
            continue
        record = _validate_active(data, fname)
        family = record["family"]
        if family in active:
            raise BaselineError("%s: duplicate family %r (already defined by %s)" % (
                fname, family, active[family]["source"]))
        active[family] = record
    return active, reference_only_names


def tolerance(reference):
    return max(2, math.ceil(0.08 * reference))


def compute_delta(tokens_text, tokens_control):
    return tokens_text - tokens_control


def text_matches(delta, reference):
    return abs(delta - reference) <= tolerance(reference)


def stability_tolerance(reference):
    # A reference-less text set has no scale, so repeats must agree exactly.
    return tolerance(reference) if reference is not None else 0


def text_moved(deltas, reference):
    if len(deltas) < 2:
        return False
    return (max(deltas) - min(deltas)) > stability_tolerance(reference)


def median_int(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


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


def family_verdict(matches, total, unstable=0):
    # All texts must agree for a match. One miss stays undecided (R3). A
    # mismatch needs two stably measured misses; with fewer than that, unstable
    # texts leave the family undecided instead of turning into a mismatch (R5).
    if matches is None or total is None or total <= 0:
        return "UNMEASURED FAMILY"
    if unstable < 0:
        unstable = 0
    stable_mismatches = total - matches - unstable
    if unstable == 0 and matches >= total:
        return "SIGNATURE MATCH"
    if total >= 2 and matches == total - 1:
        return "PARTIAL MATCH"
    if stable_mismatches >= 2:
        return "SIGNATURE MISMATCH"
    if unstable > 0:
        return "UNSTABLE ACCOUNTING"
    return "SIGNATURE MISMATCH"


def is_unstable(control_counts):
    return len(set(control_counts)) > 1


# Decision order: endpoint unavailable > no usage reported > probe incomplete >
# unstable accounting > family verdict.
def decide(endpoint_failed=False, usage_missing=False, incomplete=False,
           unstable=False, family=None, matches=None, total=5, unstable_texts=0):
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
    return family_verdict(matches, total, unstable_texts)


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


def read_key(args):
    """Key only from env or stdin; argv is visible to other processes."""
    if getattr(args, "key_stdin", False):
        try:
            key = sys.stdin.readline().strip()
        except (AttributeError, OSError):
            return None, "--key-stdin was given but stdin could not be read"
        if not key:
            return None, "--key-stdin was given but stdin was empty"
        return key, None
    key = os.environ.get("VERIFY_API_KEY")
    if not key:
        return None, "API key required via VERIFY_API_KEY or --key-stdin"
    return key, None


def reject_key_argv(argv):
    for token in argv:
        if token == "--key" or token.startswith("--key="):
            return True
    return False


def estimate_run_requests(n_texts, repeat):
    return max(0, repeat) * (1 + n_texts)


def pause(seconds):
    if seconds > 0 and not FAST_SLEEP:
        time.sleep(seconds)


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
    opener = urllib.request.build_opener(SafeRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))


def probe_once(base, model, key, text, dialect_holder, timeout, attempts=3, budget=None):
    """One prompt-only request, with a max_tokens ladder and retries.

    A reasoning route burns its budget and answers 502, so the ladder climbs
    instead of failing; prompt_tokens does not change with max_tokens. A
    transient drop is retried, because one dropped connection is not a dead
    route (R4). A 404/405 on the OpenAI dialect switches the run to the
    Anthropic dialect. A cross-host redirect is refused before it can carry
    the auth headers to another host.
    """
    last = "endpoint did not answer"
    switched = False
    for rung, max_tokens in enumerate(MAX_TOKENS_LADDER):
        path = "/chat/completions" if dialect_holder[0] == "openai" else "/messages"
        body = {"model": model, "messages": [{"role": "user", "content": text}],
                "max_tokens": max_tokens, "stream": False}
        for attempt in range(attempts):
            if budget is not None:
                budget.acquire()
            try:
                _, resp_body = post_json(base.rstrip("/") + path, body, key, timeout)
                return resp_body
            except ProbeFailure:
                raise
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
                    pause(1.5 * (attempt + 1))
                    continue
                raise ProbeFailure(last)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = "endpoint did not answer (%s)" % type(e).__name__
                pause(1.5 * (attempt + 1))
                continue
        if switched:
            break
    if switched:
        return probe_once(base, model, key, text, dialect_holder, timeout, attempts, budget)
    raise ProbeFailure(last)


def build_report(base, model, dialect, control_tokens_list, probe_results, family,
                 signature, matches, verdict, note, failed_probes=None,
                 total_texts=None, unstable_texts=None, budget=None,
                 estimated_requests=None, baseline=None):
    control_unstable = (len(control_tokens_list) > 1 and is_unstable(control_tokens_list))
    if family:
        reference = {"family": family, "signature": signature}
        if baseline:
            reference["probe_set_id"] = baseline.get("probe_set_id")
            if baseline.get("confidence") is not None:
                reference["confidence"] = baseline["confidence"]
            reference["models_measured"] = list(baseline.get("models_measured") or [])
            reference["models_inferred"] = list(baseline.get("models_inferred") or [])
            reference["provenance"] = baseline.get("provenance")
            reference["source"] = baseline.get("source")
    else:
        reference = None
    report = {
        "tool": TOOL,
        "version": VERSION,
        "base": base,
        "model": model,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dialect": dialect,
        "control": {
            "text": CONTROL_TEXT,
            "tokens": control_tokens_list[0] if control_tokens_list else None,
            "repeats": list(control_tokens_list),
            "min": min(control_tokens_list) if control_tokens_list else None,
            "max": max(control_tokens_list) if control_tokens_list else None,
            "unstable": control_unstable,
        },
        "reference": reference,
        "probes": probe_results,
        "matches": matches,
        "texts_total": total_texts if total_texts is not None else len(probe_results),
        "unstable_texts": list(unstable_texts or []),
        "verdict": verdict,
        "note": note,
        "failed_probes": list(failed_probes or []),
        "budget": {
            "estimated": estimated_requests,
            "limit": budget.limit if budget is not None else 0,
            "used": budget.used if budget is not None else 0,
            "exceeded": bool(budget is not None and budget.exceeded),
        },
    }
    return report


def format_values(values):
    return ",".join(str(v) for v in values)


def run(args, api_key=None):
    if not args.base or not args.model:
        print("error: --base and --model are required", file=sys.stderr)
        return 1
    if api_key is None:
        key, key_error = read_key(args)
        if key_error:
            print("error: %s" % key_error, file=sys.stderr)
            return 1
    else:
        key = api_key
    if args.repeat < 1:
        print("error: --repeat must be >= 1", file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("error: --timeout must be > 0", file=sys.stderr)
        return 1
    if args.sleep < 0:
        print("error: --sleep must be >= 0", file=sys.stderr)
        return 1
    if getattr(args, "max_requests", 0) is None or getattr(args, "max_requests", 0) < 0:
        print("error: --max-requests must be >= 0", file=sys.stderr)
        return 1

    # Fail closed before any network request: a bad baseline file is an error, not a skip.
    try:
        active_baselines, reference_only_names = load_baselines()
    except BaselineError as e:
        print("error: %s" % e, file=sys.stderr)
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
    baseline = active_baselines.get(family) if family else None
    signature = baseline["signature"] if baseline else None
    n_texts = len(texts)
    estimated = estimate_run_requests(n_texts, args.repeat)
    print("estimated requests: %d (control + %d texts, repeat %d; retries and the max_tokens ladder can add more)" % (
        estimated, n_texts, args.repeat))
    max_requests = getattr(args, "max_requests", 0) or 0
    if max_requests:
        print("request limit: %d" % max_requests)
        if estimated > max_requests:
            print("warning: the estimate exceeds --max-requests; the run will stop and report PROBE INCOMPLETE")

    budget = RequestBudget(max_requests)

    dialect_holder = ["openai"]
    control_tokens_all = []
    tokens_by_name = {name: [] for name, _ in texts}
    endpoint_error = None
    usage_missing = False
    failed_probes = []
    budget_stopped = False

    for rep in range(args.repeat):
        # control first, then probes, same path, same body shape
        try:
            ctrl_body = probe_once(args.base, args.model, key, CONTROL_TEXT, dialect_holder,
                                   args.timeout, budget=budget)
        except ProbeFailure as e:
            if budget.exceeded:
                budget_stopped = True
                break
            endpoint_error = str(e)
            break
        ctrl_tok = parse_usage_tokens(ctrl_body)
        if ctrl_tok is None:
            usage_missing = True
            break
        control_tokens_all.append(ctrl_tok)
        pause(args.sleep)
        for i, (name, text) in enumerate(texts):
            try:
                body = probe_once(args.base, args.model, key, text, dialect_holder,
                                  args.timeout, budget=budget)
            except ProbeFailure as e:
                if budget.exceeded:
                    budget_stopped = True
                    break
                failed_probes.append("%s (%s)" % (name, e))
                continue
            tok = parse_usage_tokens(body)
            if tok is None:
                failed_probes.append("%s (no token usage in the response)" % name)
                continue
            tokens_by_name[name].append((rep, tok))
            if i < len(texts) - 1 or rep < args.repeat - 1:
                pause(args.sleep)
        if budget_stopped:
            break
        if endpoint_error or usage_missing:
            break

    dialect = dialect_holder[0]
    control_unstable = (args.repeat > 1 and len(control_tokens_all) > 1 and is_unstable(control_tokens_all))

    probe_results = []
    matches = None
    unstable_texts = []
    if control_tokens_all and not endpoint_error and not usage_missing:
        for idx, (name, text) in enumerate(texts):
            pairs = tokens_by_name[name]
            ref = signature[idx] if signature and idx < len(signature) else None
            tokens_repeats = [tok for _, tok in pairs]
            deltas = [tok - control_tokens_all[rep] for rep, tok in pairs if rep < len(control_tokens_all)]
            unstable_text = text_moved(deltas, ref)
            delta = median_int(deltas) if deltas else None
            if unstable_text:
                status = "unstable"
                unstable_texts.append(name)
            elif ref is None:
                status = "unmeasured"
            elif control_unstable or failed_probes or budget_stopped:
                status = "unscored"
            elif text_matches(delta, ref):
                status = "match"
            else:
                status = "mismatch"
            probe_results.append({
                "name": name,
                "tokens": tokens_repeats[0] if tokens_repeats else None,
                "tokens_repeats": tokens_repeats,
                "delta": delta,
                "delta_repeats": deltas,
                "min": min(deltas) if deltas else None,
                "max": max(deltas) if deltas else None,
                "reference": ref,
                "match": (True if status == "match" else False if status == "mismatch" else None),
                "text_unstable": unstable_text,
                "status": status,
                "chars": len(text),
            })
        if signature and not control_unstable and not failed_probes and not budget_stopped:
            matches = sum(1 for p in probe_results if p["status"] == "match")

    failed_names = []
    for entry in failed_probes:
        name = entry.split(" (", 1)[0]
        if name not in failed_names:
            failed_names.append(name)

    if endpoint_error:
        verdict = decide(endpoint_failed=True)
        note = "%s; no usable answer, which is not evidence about the model (R4)" % endpoint_error
    elif usage_missing:
        verdict = decide(usage_missing=True)
        note = "responses carried no token usage; the method could not run"
    elif budget_stopped:
        measured = sum(1 for p in probe_results if p["tokens"] is not None)
        verdict = decide(incomplete=True)
        note = "request budget of %d exhausted after %d request(s); only %d of %d texts were measured, so the probe is incomplete" % (
            budget.limit, budget.used, measured, n_texts)
    elif failed_probes:
        verdict = decide(incomplete=True)
        note = "the route answered but %d of %d texts could not be measured (%s); the deltas below are partial, re-run (R4)" % (
            len(failed_names), n_texts, "; ".join(failed_probes))
    elif control_unstable:
        verdict = decide(unstable=True)
        note = "control token count moved across repeats (%s); usage accounting is not stable, no conclusion drawn (R5)" % (
            ",".join(str(c) for c in control_tokens_all))
    elif custom_texts:
        verdict = decide(family=None)
        note = "alternate text set; reference comparison skipped, deltas reported raw"
        if unstable_texts:
            note += "; %d text(s) moved beyond tolerance across repeats and were not scored (R5)" % len(unstable_texts)
    elif family is None:
        verdict = decide(family=None)
        note = "no reference signature for the claimed model; deltas reported raw so they can be baselined later"
        if unstable_texts:
            note += "; %d text(s) moved beyond tolerance across repeats and were not scored (R5)" % len(unstable_texts)
    elif signature is None:
        verdict = decide(family=None)
        if family in reference_only_names:
            note = "the %s family appears only in %s, whose rows carry no signature; deltas reported raw so they can be baselined later (R6)" % (
                family, REFERENCE_ONLY_FILE)
        else:
            note = "the %s family is known, but no signature was measured with these texts; deltas reported raw (R6)" % family
        if unstable_texts:
            note += "; %d text(s) moved beyond tolerance across repeats and were not scored (R5)" % len(unstable_texts)
    else:
        verdict = decide(family=family, matches=matches, total=n_texts,
                         unstable_texts=len(unstable_texts))
        note = verdict_note(verdict, matches, n_texts, len(unstable_texts))

    if baseline is not None and verdict in (
            "SIGNATURE MATCH", "PARTIAL MATCH", "SIGNATURE MISMATCH", "UNSTABLE ACCOUNTING"):
        note += "; " + BASELINE_CAVEAT
        if any(_model_id_matches(args.model, m) for m in baseline.get("models_inferred", [])):
            note += "; " + INFERRED_MODEL_CAVEAT
        if baseline.get("confidence") == "single-route":
            note += "; " + SINGLE_ROUTE_CAVEAT
        cr = baseline.get("control_range")
        if cr and control_tokens_all:
            first_control = control_tokens_all[0]
            if first_control < cr[0] or first_control > cr[1]:
                note += "; control %d is outside the recorded control range [%d,%d] for %s; recorded as a note only, the verdict is unchanged (R2)" % (
                    first_control, cr[0], cr[1], baseline["family"])

    report = build_report(args.base, args.model, dialect, control_tokens_all,
                          probe_results, family, signature,
                          matches, verdict, note, failed_probes,
                          total_texts=n_texts, unstable_texts=unstable_texts,
                          budget=budget, estimated_requests=estimated,
                          baseline=baseline)

    print("base: %s" % args.base)
    print("model: %s" % args.model)
    print("dialect: %s" % dialect)
    if control_tokens_all:
        print("control \"%s\": %s token(s)" % (CONTROL_TEXT, ",".join(str(c) for c in control_tokens_all)))
    for p in probe_results:
        tokens_s = format_values(p["tokens_repeats"]) if len(p["tokens_repeats"]) > 1 else (
            str(p["tokens"]) if p["tokens"] is not None else "?")
        delta_s = format_values(p["delta_repeats"]) if len(p["delta_repeats"]) > 1 else (
            str(p["delta"]) if p["delta"] is not None else "?")
        spread = ""
        if len(p["delta_repeats"]) > 1:
            spread = " min=%s max=%s" % (p["min"], p["max"])
        if p["reference"] is None:
            print("%s: tokens=%s delta=%s%s chars=%d" % (p["name"], tokens_s, delta_s, spread, p["chars"]))
        else:
            match_s = "unstable" if p["text_unstable"] else ("skip" if p["match"] is None else ("yes" if p["match"] else "no"))
            print("%s: tokens=%s delta=%s ref=%d match=%s%s chars=%d" % (
                p["name"], tokens_s, delta_s, p["reference"], match_s, spread, p["chars"]))
    if matches is not None:
        print("matches: %d/%d" % (matches, n_texts))
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


def verdict_note(verdict, matches, total, unstable_count=0):
    suffix = ""
    if unstable_count:
        suffix = "; %d text(s) moved beyond tolerance across repeats and were not scored (R5)" % unstable_count
    if verdict == "SIGNATURE MATCH":
        return "%d/%d texts agree with the claimed family; WEAK evidence for tokenizer dialect only, never weights (R1)%s" % (
            matches, total, suffix)
    if verdict == "PARTIAL MATCH":
        return "%d/%d texts agree; one text that misses is an anomaly, not a family split, re-run with fresh texts (R3)%s" % (
            matches, total, suffix)
    if verdict == "UNSTABLE ACCOUNTING":
        return "%d/%d texts matched and fewer than 2 stably measured texts disagreed; %d text(s) were unstable across repeats, so no family verdict is drawn (R5)" % (
            matches, total, unstable_count)
    return "%d/%d texts agree; the tokenizer does not look like the claimed family's%s" % (
        matches, total, suffix)


def sleep_between(seconds):
    pause(seconds)


def main():
    # never let a non-ASCII probe name kill output on a legacy console codepage
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    argv = sys.argv[1:]
    if reject_key_argv(argv):
        print("error: --key is not accepted because command-line arguments are visible in the process list; "
              "use VERIFY_API_KEY or --key-stdin", file=sys.stderr)
        return 1
    if argv and argv[0] == "selftest":
        return selftest()

    parser = VerifierArgumentParser(
        description="Model-authenticity verifier: input-side tokenizer diff against measured family signatures.")
    parser.add_argument("--base", help="base URL of the gateway, e.g. https://gw.example.com/v1")
    parser.add_argument("--model", help="model id to check, e.g. anthropic/claude-opus-4.8")
    parser.add_argument("--key-stdin", action="store_true",
                        help="read the API key from one line on stdin; env VERIFY_API_KEY is the other option")
    parser.add_argument("--sleep", type=float, default=2, help="seconds between requests (default 2)")
    parser.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds (default 60)")
    parser.add_argument("--repeat", type=int, default=1, help="repeat the probe set N times (default 1)")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="stop after N HTTP requests and report PROBE INCOMPLETE (default 0 = unlimited)")
    parser.add_argument("--out", help="write the full JSON report to FILE")
    parser.add_argument("--texts", help="alternate text set, one text per line, 'name=content' allowed")
    args = parser.parse_args()
    return run(args)


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
    check("verdict 5/5 signature match", lambda: family_verdict(5, 5) == "SIGNATURE MATCH")
    check("verdict 4/5 is undecided", lambda: family_verdict(4, 5) == "PARTIAL MATCH")
    check("verdict 3/5 mismatch", lambda: family_verdict(3, 5) == "SIGNATURE MISMATCH")
    check("verdict 2/5 mismatch", lambda: family_verdict(2, 5) == "SIGNATURE MISMATCH")
    check("verdict 1/5 mismatch", lambda: family_verdict(1, 5) == "SIGNATURE MISMATCH")
    check("verdict 0/5 mismatch", lambda: family_verdict(0, 5) == "SIGNATURE MISMATCH")
    check("verdict 3/3 match", lambda: family_verdict(3, 3) == "SIGNATURE MATCH")
    check("verdict 2/3 partial", lambda: family_verdict(2, 3) == "PARTIAL MATCH")
    check("verdict 1/3 mismatch", lambda: family_verdict(1, 3) == "SIGNATURE MISMATCH")
    check("verdict 4/4 match", lambda: family_verdict(4, 4) == "SIGNATURE MATCH")
    check("verdict 3/4 partial", lambda: family_verdict(3, 4) == "PARTIAL MATCH")
    check("verdict 2/4 mismatch", lambda: family_verdict(2, 4) == "SIGNATURE MISMATCH")
    check("moved control is unstable", lambda: is_unstable([5, 5, 7]))
    check("steady control is stable", lambda: not is_unstable([5, 5, 5]))
    check("decision: endpoint unavailable beats all", lambda: decide(endpoint_failed=True, usage_missing=True, unstable=True, family="openai", matches=5) == "ENDPOINT UNAVAILABLE")
    check("decision: no usage beats unstable", lambda: decide(usage_missing=True, unstable=True, family="openai", matches=5) == "NO USAGE REPORTED")
    check("decision: unstable beats family verdict", lambda: decide(unstable=True, family="openai", matches=5) == "UNSTABLE ACCOUNTING")
    check("decision: unmeasured family when no family", lambda: decide(family=None) == "UNMEASURED FAMILY")

    # --- repeat stability and the unstable-text verdict rule ---
    check("delta within tolerance is stable", lambda: not text_moved([14, 16], 14))
    check("delta beyond tolerance is unstable", lambda: text_moved([14, 17], 14))
    check("single repeat is stable", lambda: not text_moved([14], 14))
    check("custom text requires exact repeat", lambda: text_moved([3, 4], None))
    check("custom text exact repeat is stable", lambda: not text_moved([3, 3], None))
    check("median keeps an integer", lambda: median_int([14, 15]) == 15)
    check("median single value", lambda: median_int([14]) == 14)
    check("3 match + 2 unstable is unstable accounting", lambda: family_verdict(3, 5, 2) == "UNSTABLE ACCOUNTING")
    check("4 match + 1 unstable is partial", lambda: family_verdict(4, 5, 1) == "PARTIAL MATCH")
    check("2 match + 1 unstable + 2 mismatch is mismatch", lambda: family_verdict(2, 5, 1) == "SIGNATURE MISMATCH")
    check("3 match + 1 unstable + 1 mismatch is unstable", lambda: family_verdict(3, 5, 1) == "UNSTABLE ACCOUNTING")
    check("decision: 4 match 1 unstable is partial", lambda: decide(family="deepseek", matches=4, total=5, unstable_texts=1) == "PARTIAL MATCH")
    check("decision: 3 match 2 unstable is unstable", lambda: decide(family="deepseek", matches=3, total=5, unstable_texts=2) == "UNSTABLE ACCOUNTING")
    check("decision: 2 match 1 unstable 2 mismatch is mismatch", lambda: decide(family="deepseek", matches=2, total=5, unstable_texts=1) == "SIGNATURE MISMATCH")

    check("verdict total=0 is unmeasured family", lambda: family_verdict(0, 0) == "UNMEASURED FAMILY")
    check("verdict total=0 no matches is unmeasured family", lambda: family_verdict(None, 0) == "UNMEASURED FAMILY")
    check("verdict total=1 one match is signature match", lambda: family_verdict(1, 1, 0) == "SIGNATURE MATCH")
    check("verdict total=1 mismatch falls through to signature mismatch", lambda: family_verdict(0, 1, 0) == "SIGNATURE MISMATCH")
    check("verdict total=1 all unstable is unstable accounting", lambda: family_verdict(0, 1, 1) == "UNSTABLE ACCOUNTING")
    check("family_verdict matches the v3.0 rows for total=5 unstable=0", lambda: [family_verdict(m, 5, 0) for m in range(6)] == [
        "SIGNATURE MISMATCH", "SIGNATURE MISMATCH", "SIGNATURE MISMATCH", "SIGNATURE MISMATCH",
        "PARTIAL MATCH", "SIGNATURE MATCH"])
    # --- ladder, retries and the partial-run verdict (group I, 7) ---
    check("ladder starts at 64", lambda: MAX_TOKENS_LADDER[0] == 64)
    check("ladder ends at 2048", lambda: MAX_TOKENS_LADDER[-1] == 2048)
    check("ladder climbs", lambda: MAX_TOKENS_LADDER == sorted(MAX_TOKENS_LADDER))
    check("502 is retryable", lambda: 502 in RETRYABLE_HTTP)
    check("402 is not retryable", lambda: 402 not in RETRYABLE_HTTP)
    check("decision: incomplete beats unstable", lambda: decide(incomplete=True, unstable=True, family="openai", matches=5) == "PROBE INCOMPLETE")
    check("decision: endpoint beats incomplete", lambda: decide(endpoint_failed=True, incomplete=True) == "ENDPOINT UNAVAILABLE")

    # --- key handling and request estimates ---
    check("--key is detected", lambda: reject_key_argv(["--key", "secret"]))
    check("--key=VALUE is detected", lambda: reject_key_argv(["--key=secret"]))
    check("--key-stdin is not rejected", lambda: not reject_key_argv(["--key-stdin"]))
    check("estimate one repeat", lambda: estimate_run_requests(5, 1) == 6)
    check("estimate two repeats", lambda: estimate_run_requests(5, 2) == 12)
    check("budget starts unlimited", lambda: RequestBudget(0).limit == 0)
    check("budget counts attempts", lambda: _budget_used(3) == 3)

    def read_stdin_key(value):
        old = sys.stdin
        sys.stdin = io.StringIO(value)
        try:
            return read_key(argparse.Namespace(key_stdin=True))
        finally:
            sys.stdin = old

    check("key from stdin", lambda: read_stdin_key("sk-test\n") == ("sk-test", None))
    check("empty key from stdin is rejected", lambda: read_stdin_key("\n")[0] is None)

    def reject_flag(flag):
        old = sys.argv
        sys.argv = ["verify_gateway.py", flag, "secret"]
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                code = main()
        finally:
            sys.argv = old
        return code == 1 and "process list" in err.getvalue() and "secret" not in err.getvalue()

    check("--key exits before any request", lambda: reject_flag("--key"))
    check("--key=VALUE exits before any request", lambda: reject_flag("--key=secret"))

    # --- redirect guard ---
    def redirect_with(old_url, new_url):
        handler = SafeRedirectHandler()
        req = urllib.request.Request(old_url)
        try:
            return handler.redirect_request(req, None, 302, "Found", {}, new_url)
        except ProbeFailure:
            return None

    def cross_host_redirect_refused():
        old = redirect_with("http://a.example/v1/chat/completions", "http://b.example/steal")
        return old is None

    def different_port_redirect_refused():
        return redirect_with("http://a.example:8080/v1/chat/completions", "http://a.example:9090/v1/steal") is None

    def https_to_http_same_host_refused():
        return redirect_with("https://a.example/v1/chat/completions", "http://a.example/v1/messages") is None

    def _redirect_error_clean():
        handler = SafeRedirectHandler()
        req = urllib.request.Request("http://a.example/v1/chat/completions",
                                     headers={"Authorization": "Bearer selftest-key"})
        try:
            handler.redirect_request(req, None, 302, "Found", {}, "http://b.example/steal")
            return False
        except ProbeFailure as e:
            return "different origin" in str(e) and "selftest-key" not in str(e)

    def same_host_redirect_allowed():
        return isinstance(redirect_with("http://a.example/v1/chat/completions", "http://a.example/v1/messages"),
                          urllib.request.Request)

    def same_origin_https_redirect_allowed():
        return isinstance(redirect_with("https://a.example/v1/chat/completions", "https://a.example/v1/messages"),
                          urllib.request.Request)

    check("cross-host redirect is refused", cross_host_redirect_refused)
    check("cross-host redirect carries no key material", lambda: _redirect_error_clean())
    check("different port redirect is refused", different_port_redirect_refused)
    check("https to http same host redirect is refused", https_to_http_same_host_refused)
    check("same-host redirect is allowed", same_host_redirect_allowed)
    check("same-origin https redirect is allowed", same_origin_https_redirect_allowed)

    # --- post_json opener wiring (stdlib fakes, no network) ---
    def post_json_fake():
        captured = {}
        real_build_opener = urllib.request.build_opener
        real_urlopen = urllib.request.urlopen

        class FakeResponse:
            status = 200

            def read(self):
                return b'{"usage": {"prompt_tokens": 7}}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                captured["req"] = req
                captured["timeout"] = timeout
                return FakeResponse()

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return FakeOpener()

        def fake_urlopen(*args, **kwargs):
            captured["urlopen_called"] = True
            raise AssertionError("urlopen must not be called")

        urllib.request.build_opener = fake_build_opener
        urllib.request.urlopen = fake_urlopen
        try:
            captured["result"] = post_json("http://a.example/v1/chat/completions", {"x": 1}, "selftest-key", 5)
        finally:
            urllib.request.build_opener = real_build_opener
            urllib.request.urlopen = real_urlopen
        return captured

    post_fake = post_json_fake()
    check("post_json builds opener with SafeRedirectHandler", lambda: (
        isinstance(post_fake.get("handlers"), tuple)
        and len(post_fake["handlers"]) == 1
        and isinstance(post_fake["handlers"][0], SafeRedirectHandler)))
    check("post_json does not use urllib.request.urlopen", lambda: not post_fake.get("urlopen_called"))
    check("post_json returns status and parsed body from the opener", lambda: (
        post_fake.get("result") == (200, {"usage": {"prompt_tokens": 7}})
        and post_fake.get("timeout") == 5))

    # --- README probe block matches PROBES (permanent guard) ---
    def readme_probe_texts():
        readme = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")
        with io.open(readme, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        try:
            start = next(i for i, l in enumerate(lines) if l.strip() == "Control: `a`")
        except StopIteration:
            raise ValueError("'Control: `a`' line not found")
        i = start + 1
        while i < len(lines) and lines[i].strip() != "```":
            i += 1
        if i >= len(lines):
            raise ValueError("opening ``` fence after 'Control: `a`' not found")
        block = []
        i += 1
        while i < len(lines) and lines[i].strip() != "```":
            block.append(lines[i])
            i += 1
        if i >= len(lines):
            raise ValueError("closing ``` fence not found")
        probe_names = {n for n, _ in PROBES}
        texts = []
        i = 0
        while i < len(block):
            stripped = block[i].strip()
            if not stripped:
                i += 1
                continue
            if stripped.endswith(":") and stripped[:-1].strip() in probe_names:
                name = stripped[:-1].strip()
                i += 1
                if i >= len(block) or not block[i].strip():
                    raise ValueError("probe %r has no text line" % name)
                texts.append((name, block[i]))
                i += 1
                continue
            raise ValueError("unexpected line in probe block: %r" % block[i])
        if [n for n, _ in texts] != [n for n, _ in PROBES]:
            raise ValueError("probe order/count differs from PROBES: %s" % [n for n, _ in texts])
        return texts

    def readme_matches_probes():
        try:
            texts = readme_probe_texts()
        except (OSError, ValueError) as e:
            print("README probe block parse error: %s" % e, file=sys.stderr)
            return False
        expected = list(PROBES)
        if texts != expected:
            for (got_name, got_text), (want_name, want_text) in zip(texts, expected):
                if got_name != want_name or got_text != want_text:
                    print("README probe block mismatch on %s" % want_name, file=sys.stderr)
                    for col, (a, b) in enumerate(zip(got_text, want_text)):
                        if a != b:
                            print("  first diff at col %d: U+%04X vs U+%04X" % (col, ord(a), ord(b)), file=sys.stderr)
                            break
                    else:
                        print("  length differs: %d vs %d codepoints" % (len(got_text), len(want_text)), file=sys.stderr)
                    return False
            return len(texts) == len(expected)
        return True

    check("README probe block parses and matches PROBES exactly", readme_matches_probes)

    # --- report safety & shape (group H, 2) ---
    sentinel = "sk-sentinel-never-in-report"
    report = build_report("https://gw.example.com/v1", "m", "openai", [5], [], "openai",
                          [24, 24, 22, 30, 29], 5, "SIGNATURE MATCH", "note",
                          total_texts=5, unstable_texts=[])
    report_json = json.dumps(report)
    check("key material never appears in report", lambda: sentinel not in report_json and "key" not in report)
    check("report carries tool and version", lambda: report["tool"] == TOOL and report["version"] == VERSION)
    check("report stores control repeats", lambda: report["control"]["repeats"] == [5])
    check("report stores text total", lambda: report["texts_total"] == 5)

    # --- local mock gateway, no real network ---
    def start_gateway(scenario, signature=(14, 20, 18, 27, 22)):
        state = {"control": 0, "counts": {}}

        def base_token(text):
            if scenario == "constant":
                return 42
            if text == CONTROL_TEXT:
                if scenario == "drifting_control":
                    state["control"] += 1
                    return 4 + state["control"]
                return 5
            for i, (_, probe) in enumerate(PROBES):
                if text == probe:
                    if scenario == "rounded":
                        # Coarse 5-token buckets: every reading is a multiple of 5,
                        # and the deltas no longer line up with the family.
                        return 30
                    if scenario == "shift_t1" and i == 0:
                        return 45
                    tok = 5 + signature[i]
                    if scenario in ("drifting_text", "drifting_two_texts") and i < (1 if scenario == "drifting_text" else 2):
                        state["counts"][text] = state["counts"].get(text, 0) + 1
                        if state["counts"][text] >= 2:
                            tok += 3
                    return tok
            return 5 + max(1, len(text) // 4)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, code, obj):
                raw = json.dumps(obj).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    payload = {}
                if scenario == "payment":
                    self.send_json(402, {"error": "payment required"})
                    return
                if scenario == "cross_host_redirect":
                    try:
                        self.send_response(302)
                        self.send_header("Location", "http://example.invalid/steal")
                        self.end_headers()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if scenario == "anthropic_only" and self.path.endswith("/chat/completions"):
                    self.send_json(404, {"error": "not found"})
                    return
                if scenario == "timeout":
                    time.sleep(0.3)
                try:
                    text = payload["messages"][0]["content"]
                except Exception:
                    text = ""
                token = base_token(text)
                body = {
                    "model": payload.get("model", "mock-model"),
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                }
                if scenario != "no_usage":
                    if self.path.endswith("/messages"):
                        body["usage"] = {"input_tokens": token}
                    else:
                        body["usage"] = {"prompt_tokens": token}
                self.send_json(200, body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, "http://127.0.0.1:%d/v1" % server.server_port

    def stop_gateway(server):
        server.shutdown()
        server.server_close()

    def args_for(base, **overrides):
        defaults = {
            "base": base,
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "key_stdin": False,
            "sleep": 0,
            "timeout": 5,
            "repeat": 1,
            "max_requests": 0,
            "out": None,
            "texts": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def run_capture(args):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run(args, api_key="selftest-key")
        return code, out.getvalue() + err.getvalue()

    def scenario_run(scenario, signature=None, **overrides):
        if signature is None:
            server, base = start_gateway(scenario)
        else:
            server, base = start_gateway(scenario, signature=signature)
        try:
            return run_capture(args_for(base, **overrides))
        finally:
            stop_gateway(server)

    code, out = scenario_run("honest")
    check("mock honest gateway yields signature match", lambda: code == 0 and "verdict: SIGNATURE MATCH" in out)
    check("mock honest gateway scores all texts", lambda: "matches: 5/5" in out)
    check("run prints request estimate", lambda: "estimated requests: 6" in out)

    code, out = scenario_run("constant")
    check("mock constant usage yields mismatch", lambda: "verdict: SIGNATURE MISMATCH" in out and "matches: 0/5" in out)

    code, out = scenario_run("rounded")
    check("mock rounded usage yields mismatch", lambda: "verdict: SIGNATURE MISMATCH" in out)

    code, out = scenario_run("shift_t1")
    check("mock one shifted text yields partial", lambda: "verdict: PARTIAL MATCH" in out and "matches: 4/5" in out)

    code, out = scenario_run("anthropic_only")
    check("404 on openai dialect falls back to anthropic", lambda: "dialect: anthropic" in out and "verdict: SIGNATURE MATCH" in out)

    code, out = scenario_run("no_usage")
    check("mock no usage yields no usage verdict", lambda: "verdict: NO USAGE REPORTED" in out)

    code, out = scenario_run("payment")
    check("mock 402 yields endpoint unavailable", lambda: "verdict: ENDPOINT UNAVAILABLE" in out)

    code, out = scenario_run("cross_host_redirect")
    check("mock cross-host redirect yields endpoint unavailable", lambda: "verdict: ENDPOINT UNAVAILABLE" in out and "redirect" in out)

    global FAST_SLEEP
    old_fast = FAST_SLEEP
    FAST_SLEEP = True
    try:
        code, out = scenario_run("timeout", timeout=0.05)
    finally:
        FAST_SLEEP = old_fast
    check("mock timeout yields endpoint unavailable", lambda: "verdict: ENDPOINT UNAVAILABLE" in out)

    code, out = scenario_run("drifting_control", repeat=2)
    check("mock drifting control yields unstable accounting", lambda: "verdict: UNSTABLE ACCOUNTING" in out)

    code, out = scenario_run("drifting_text", repeat=2)
    check("mock one drifting text yields partial", lambda: "verdict: PARTIAL MATCH" in out and "match=unstable" in out)

    code, out = scenario_run("drifting_two_texts", repeat=2)
    check("mock two drifting texts yield unstable accounting", lambda: "verdict: UNSTABLE ACCOUNTING" in out and "matches: 3/5" in out)

    code, out = scenario_run("honest", max_requests=2)
    check("mock request budget yields probe incomplete", lambda: "verdict: PROBE INCOMPLETE" in out and "budget" in out)

    code, out = scenario_run("honest", repeat=2, max_requests=6)
    check("mock budget exhausted on repeat 2 control yields probe incomplete", lambda: (
        code == 0 and "verdict: PROBE INCOMPLETE" in out and "ENDPOINT UNAVAILABLE" not in out))

    code, out = scenario_run("anthropic_only", max_requests=1)
    check("mock budget exhausted on repeat 1 control yields probe incomplete", lambda: (
        code == 0 and "verdict: PROBE INCOMPLETE" in out and "ENDPOINT UNAVAILABLE" not in out))

    # --- baselines: active load & integrity ---
    try:
        active_baselines, reference_only_families = load_baselines()
        baselines_load_error = None
    except BaselineError as e:
        active_baselines, reference_only_families = {}, []
        baselines_load_error = str(e)

    check("active baselines load without error", lambda: baselines_load_error is None)
    check("every active probe_set_id matches probe_set_id()", lambda: bool(active_baselines) and all(
        b["probe_set_id"] == probe_set_id() for b in active_baselines.values()))
    check("active families are exactly deepseek and anthropic-4.6", lambda: sorted(active_baselines) == ["anthropic-4.6", "deepseek"])
    check("deepseek active signature and confidence", lambda: (
        active_baselines["deepseek"]["signature"] == [14, 20, 18, 27, 22]
        and active_baselines["deepseek"]["confidence"] == "multi-route-third-party"))
    check("anthropic-4.6 active signature and confidence", lambda: (
        active_baselines["anthropic-4.6"]["signature"] == [18, 25, 20, 36, 38]
        and active_baselines["anthropic-4.6"]["confidence"] == "single-route"))
    check("anthropic-4.6 provenance notes read deltas by deepseek analogy", lambda: (
        "read as deltas" in active_baselines["anthropic-4.6"]["provenance"]["notes"]
        and "deepseek" in active_baselines["anthropic-4.6"]["provenance"]["notes"]))

    pid = probe_set_id()

    def active_file(family, models, signature, **overrides):
        data = {"schema_version": BASELINE_SCHEMA_VERSION, "family": family, "models": models,
                "models_measured": list(models), "models_inferred": [],
                "probe_set_id": pid, "signature": signature, "control_range": [5, 31],
                "provenance": {"endpoint_kind": "third-party-gateway", "routes": ["route-a"],
                               "date": "2026-11-16", "n_runs": 1, "notes": "test row"}}
        data.update(overrides)
        return json.dumps(data)

    def baseline_error(files):
        with tempfile.TemporaryDirectory() as tmpdir:
            for name, content in files.items():
                with open(os.path.join(tmpdir, name), "w", encoding="utf-8") as fh:
                    fh.write(content)
            try:
                load_baselines(tmpdir)
            except BaselineError as e:
                return str(e)
        return None

    check("fail closed: corrupt JSON names the file", lambda: "bad.json" in (
        baseline_error({"bad.json": "{not json"}) or ""))
    check("fail closed: wrong signature length names the file", lambda: "short.json" in (
        baseline_error({"short.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [1, 2, 3])}) or ""))
    check("fail closed: unknown schema_version names the file", lambda: "v2.json" in (
        baseline_error({"v2.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"],
                                               [14, 20, 18, 27, 22], schema_version=2)}) or ""))
    check("fail closed: boolean schema_version names the file", lambda: "boolver.json" in (
        baseline_error({"boolver.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"],
                                                    [14, 20, 18, 27, 22], schema_version=True)}) or ""))
    check("fail closed: probe_set_id mismatch names the file", lambda: "pid.json" in (
        baseline_error({"pid.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"],
                                                [14, 20, 18, 27, 22], probe_set_id="deadbeef")}) or ""))
    check("fail closed: duplicate family names the file", lambda: "dup.json" in (
        baseline_error({"a.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22]),
                        "dup.json": active_file("deepseek", ["deepseek-v4.1-flash"], [14, 20, 18, 27, 22])}) or ""))
    check("fail closed: active superseded true names the file", lambda: "sup.json" in (
        baseline_error({"sup.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"],
                                                [14, 20, 18, 27, 22], superseded=True)}) or ""))
    check("fail closed: reference-only row carrying signature names the file", lambda: "_reference_only.json" in (
        baseline_error({"_reference_only.json": json.dumps(
            {"schema_version": BASELINE_SCHEMA_VERSION,
             "rows": [{"family": "ghost", "models": ["ghost/model"], "signature": [1, 2, 3, 4, 5]}]})}) or ""))
    check("fail closed: boolean signature element names the file", lambda: "sig_bool.json" in (
        baseline_error({"sig_bool.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [True, 20, 18, 27, 22])}) or ""))
    check("fail closed: float signature element names the file", lambda: "sig_float.json" in (
        baseline_error({"sig_float.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14.5, 20, 18, 27, 22])}) or ""))
    check("fail closed: negative signature element names the file", lambda: "sig_neg.json" in (
        baseline_error({"sig_neg.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [-1, 20, 18, 27, 22])}) or ""))
    check("fail closed: boolean control_range element names the file", lambda: "cr_bool.json" in (
        baseline_error({"cr_bool.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22], control_range=[True, 31])}) or ""))
    check("fail closed: float control_range element names the file", lambda: "cr_float.json" in (
        baseline_error({"cr_float.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22], control_range=[5.5, 31])}) or ""))
    check("fail closed: negative control_range element names the file", lambda: "cr_neg.json" in (
        baseline_error({"cr_neg.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22], control_range=[-1, 31])}) or ""))
    check("fail closed: float n_runs names the file", lambda: "n_runs_float.json" in (
        baseline_error({"n_runs_float.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22],
                                                    provenance={"endpoint_kind": "third-party-gateway", "routes": ["route-a"],
                                                                "date": "2026-11-16", "n_runs": 1.5, "notes": "test row"})}) or ""))
    check("fail closed: measured+inferred must cover models, names the file", lambda: "cover.json" in (
        baseline_error({"cover.json": active_file("deepseek", ["deepseek/deepseek-v4-pro", "deepseek-v4.1-flash"],
                                                  [14, 20, 18, 27, 22], models_measured=["deepseek/deepseek-v4-pro"])}) or ""))
    check("fail closed: measured and inferred overlap names the file", lambda: "overlap.json" in (
        baseline_error({"overlap.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22],
                                                    models_measured=["deepseek/deepseek-v4-pro"],
                                                    models_inferred=["deepseek/deepseek-v4-pro"])}) or ""))
    check("fail closed: empty models_measured names the file", lambda: "emptymeasured.json" in (
        baseline_error({"emptymeasured.json": active_file("deepseek", ["deepseek/deepseek-v4-pro"], [14, 20, 18, 27, 22],
                                                          models_measured=[],
                                                          models_inferred=["deepseek/deepseek-v4-pro"])}) or ""))

    def ghost_reference_only():
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, REFERENCE_ONLY_FILE), "w", encoding="utf-8") as fh:
                json.dump({"schema_version": BASELINE_SCHEMA_VERSION,
                           "rows": [{"family": "ghost-family", "models": ["ghost/model"],
                                     "superseded": True, "superseded_signature": [1, 2, 3, 4, 5]}]}, fh)
            active, names = load_baselines(tmpdir)
            return active == {} and names == ["ghost-family"]

    check("reference-only rows never supply a signature", ghost_reference_only)
    check("default reference-only file lists anthropic-core and openai", lambda: (
        "anthropic-core" in reference_only_families and "openai" in reference_only_families))

    def reference_only_rows_superseded():
        with open(os.path.join(BASELINE_DIR, REFERENCE_ONLY_FILE), "r", encoding="utf-8") as fh:
            rows = json.load(fh)["rows"]
        by_family = {row["family"]: row for row in rows}
        return (by_family["anthropic-4.6"].get("superseded") is True
                and by_family["deepseek"].get("superseded") is True
                and all("signature" not in row for row in rows))

    check("default reference-only file keeps old rows superseded without signature keys", reference_only_rows_superseded)

    code, out = scenario_run("honest", model="anthropic/claude-opus-4.8")
    check("mock reference-only family yields unmeasured family with R6 note", lambda: (
        "verdict: UNMEASURED FAMILY" in out and "_reference_only.json" in out and "R6" in out))

    code, out = scenario_run("honest")
    check("caveat present on signature match", lambda: (
        "verdict: SIGNATURE MATCH" in out and "third-party-gateway caveat" in out))

    code, out = scenario_run("constant")
    check("caveat present on signature mismatch", lambda: (
        "verdict: SIGNATURE MISMATCH" in out and "third-party-gateway caveat" in out))

    code, out = scenario_run("shift_t1")
    check("caveat present on partial match", lambda: (
        "verdict: PARTIAL MATCH" in out and "third-party-gateway caveat" in out))

    code, out = scenario_run("drifting_control", repeat=2)
    check("caveat present on unstable accounting", lambda: (
        "verdict: UNSTABLE ACCOUNTING" in out and "third-party-gateway caveat" in out))

    code, out = scenario_run("honest", model="deepseek/deepseek-v4-pro")
    check("inferred-model caveat on a model never measured on this text set", lambda: (
        "verdict: SIGNATURE MATCH" in out
        and "model ini belum diukur langsung pada text set ini; dipetakan lewat keluarga" in out))

    code, out = scenario_run("honest", model="deepseek/deepseek-v4.1-flash")
    check("measured model carries no inferred-model caveat", lambda: (
        "verdict: SIGNATURE MATCH" in out and "model ini belum diukur langsung" not in out))

    code, out = scenario_run("honest", signature=(18, 25, 20, 36, 38), model="anthropic/claude-sonnet-4.6")
    check("single-route caveat on the single-route baseline", lambda: (
        "verdict: SIGNATURE MATCH" in out
        and "single-route caveat: the signature was measured on a single route" in out))

    code, out = scenario_run("honest", max_requests=2)
    check("PROBE INCOMPLETE carries no baseline caveat", lambda: (
        "verdict: PROBE INCOMPLETE" in out and "third-party-gateway caveat" not in out))

    def mock_report_carries_baseline():
        server, base = start_gateway("honest")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                run_capture(args_for(base, out=target))
                with open(target, "r", encoding="utf-8") as fh:
                    report = json.load(fh)
        finally:
            stop_gateway(server)
        ref = report.get("reference") or {}
        return ref.get("probe_set_id") == probe_set_id() and ref.get("confidence") == "multi-route-third-party"

    check("mock report carries baseline probe_set_id and confidence", mock_report_carries_baseline)

    def mock_report_carries_measurement_status():
        server, base = start_gateway("honest")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                run_capture(args_for(base, model="deepseek/deepseek-v4-pro", out=target))
                with open(target, "r", encoding="utf-8") as fh:
                    report = json.load(fh)
        finally:
            stop_gateway(server)
        ref = report.get("reference") or {}
        return (ref.get("models_measured") == ["deepseek/deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"]
                and ref.get("models_inferred") == ["deepseek/deepseek-v4-pro"])

    check("mock report carries models_measured and models_inferred", mock_report_carries_measurement_status)

    def control_range_note_case():
        global BASELINE_DIR
        old_dir = BASELINE_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(old_dir, "deepseek.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["control_range"] = [99, 99]
            with open(os.path.join(tmpdir, "deepseek.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            BASELINE_DIR = tmpdir
            try:
                return scenario_run("honest", model="deepseek/deepseek-v4.1-flash")
            finally:
                BASELINE_DIR = old_dir

    code, out = control_range_note_case()
    check("out-of-range control is a note only; verdict unchanged", lambda: (
        "verdict: SIGNATURE MATCH" in out
        and "control 5 is outside the recorded control range [99,99] for deepseek" in out
        and "the verdict is unchanged (R2)" in out))

    total = len(results)
    failed = [d for d, ok in results if not ok]
    passed = total - len(failed)

    if total != 166:
        print("SELFTEST (v3.1): internal error, expected 166 assertions, ran %d" % total, file=sys.stderr)
        return 2
    if failed:
        for d in failed:
            print("FAIL: %s" % d, file=sys.stderr)
        print("SELFTEST (v3.1): %d passed, %d failed" % (passed, len(failed)))
        return 2
    print("SELFTEST (v3.1): %d passed, 0 failed" % total)
    return 0


def _budget_used(count):
    budget = RequestBudget(count)
    for _ in range(count):
        budget.acquire()
    return budget.used


if __name__ == "__main__":
    sys.exit(main())
