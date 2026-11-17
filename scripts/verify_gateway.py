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
import statistics
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

# Consistency suite (Phase 2b): usage-linearity vs repetition, determinism,
# streaming and side evidence. Notes only; never feeds the verdict (R1).
CONSISTENCY_UNITS = [
    ("ascii", "the quick brown fox jumps over the lazy dog"),
    ("cjk", "混合中文、日本語と計算のテスト"),
    ("emoji", "🚀🌌🔡🚀🌌🔡"),
]
LINEARITY_K = (1, 2, 3, 5, 8)

# Baseline loading (fail closed). Active baselines live in baselines/*.json and
# carry a probe_set_id computed from PROBES below; changing one byte of the
# probe set invalidates every active baseline by design. Reference-only family
# names live in baselines/_reference_only.json and never supply a signature.
BASELINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "baselines"))
REFERENCE_ONLY_FILE = "_reference_only.json"
BASELINE_SCHEMA_VERSION = 1
BASELINE_CAVEAT = "third-party-gateway caveat: a wrapper can re-tokenize locally or forge usage, so this is WEAK evidence for the counting dialect, never the weights (R1)"
INFERRED_MODEL_CAVEAT = "model not measured directly on this text set; mapped through the family"
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


def _usage_int(value):
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def usage_units(body):
    """Ordered input-side counts the response reports, unit name -> int.

    Only ints >= 0 are accepted (booleans, floats and negatives are rejected).
    Missing units are simply absent. input_plus_cache counts cached tokens on
    top of input_tokens, which excludes them.
    """
    units = {}
    if not isinstance(body, dict):
        return units
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return units
    inp = _usage_int(usage.get("input_tokens")) if "input_tokens" in usage else None
    if inp is not None:
        units["input_tokens"] = inp
    prm = _usage_int(usage.get("prompt_tokens")) if "prompt_tokens" in usage else None
    if prm is not None:
        units["prompt_tokens"] = prm
    total = _usage_int(usage.get("total_tokens")) if "total_tokens" in usage else None
    out = None
    if "completion_tokens" in usage:
        out = _usage_int(usage.get("completion_tokens"))
    elif "output_tokens" in usage:
        out = _usage_int(usage.get("output_tokens"))
    if total is not None and out is not None and total - out >= 0:
        units["total_minus_output"] = total - out
    if inp is not None:
        cache_present = False
        cache_ok = True
        cache_total = 0
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if key in usage:
                cache_present = True
                value = _usage_int(usage[key])
                if value is None:
                    cache_ok = False
                else:
                    cache_total += value
        if cache_present and cache_ok:
            units["input_plus_cache"] = inp + cache_total
    return units


def cache_fields_seen(body):
    if not isinstance(body, dict):
        return False
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return False
    return ("cache_read_input_tokens" in usage) or ("cache_creation_input_tokens" in usage)


COMPARABLE_UNITS = ("input_tokens", "prompt_tokens", "total_minus_output")
QUANTIZATION_LADDER = (8, 5, 3, 2, 1)  # checked largest first; k=1 always divides


def _response_cache_seen(units, explicit):
    if explicit is not None:
        return bool(explicit)
    return "input_plus_cache" in units


def _comparable_units(units, cache_seen):
    out = []
    for unit in COMPARABLE_UNITS:
        if unit not in units:
            continue
        # Anthropic excludes cached tokens from input_tokens, so it is not
        # comparable for a response that reported cache units.
        if unit == "input_tokens" and cache_seen:
            continue
        out.append(unit)
    return out


def _pair_diffs(pairs, label, units):
    out = []
    for u1, u2 in pairs:
        if u1 in units and u2 in units:
            out.append(((u1, u2), label, units[u1] - units[u2], units))
    return out


def usage_consistency(control_units_by_rep, probe_units_by_name,
                      control_cache=None, probe_cache=None, dialect=None):
    """Compare the input-side units a response reports, control vs probe.

    Pure function. control_units_by_rep is a list indexed by repeat (None for a
    missing control); probe_units_by_name maps a text name to a list indexed by
    repeat (None for a missing probe). control_cache/probe_cache optionally
    carry per-repeat booleans for "cache fields were seen"; when absent, the
    presence of input_plus_cache is used as the signal. dialect optionally
    carries the dialect holder value; "anthropic" forces the units check to
    not applicable. Notes only: this never feeds the verdict.

    A unit difference that is identical in every response is a constant offset:
    it cancels under the control subtraction, is recorded in constant_offsets
    and is never a disagreement. A difference that changes across responses is
    recorded in disagreements and fails the check. The check is only
    applicable when at least one response reported total_tokens (as
    total_minus_output) and the dialect is not anthropic; otherwise agree is
    None and no disagreement is produced.
    """
    control_units = list(control_units_by_rep or [])
    control_cache_list = list(control_cache) if control_cache is not None else [None] * len(control_units)
    while len(control_cache_list) < len(control_units):
        control_cache_list.append(None)
    probe_map = {name: list(arr or []) for name, arr in (probe_units_by_name or {}).items()}
    probe_cache_map = {name: list(arr or []) for name, arr in (probe_cache or {}).items()}

    pair_candidates = [(COMPARABLE_UNITS[i], COMPARABLE_UNITS[j])
                       for i in range(len(COMPARABLE_UNITS))
                       for j in range(i + 1, len(COMPARABLE_UNITS))]

    disagreements = []
    negative_deltas = []
    deltas = []
    units_with_deltas = []
    qualifying_responses = 0
    saw_total = False
    pair_entries = {pair: [] for pair in pair_candidates}

    reps = max([len(control_units)] + [len(v) for v in probe_map.values()] + [0])
    for rep in range(reps):
        c_units = control_units[rep] if rep < len(control_units) else None
        c_comp = []
        c_cache = None
        if isinstance(c_units, dict):
            c_cache = _response_cache_seen(c_units, control_cache_list[rep])
            c_comp = _comparable_units(c_units, c_cache)
            if "total_minus_output" in c_units:
                saw_total = True
            if len(c_comp) >= 2:
                qualifying_responses += 1
                label = "control repeat %d" % (rep + 1)
                present = set(c_comp)
                for (u1, u2), plabel, diff, snapshot in _pair_diffs(pair_candidates, label, c_units):
                    if u1 in present and u2 in present:
                        pair_entries[(u1, u2)].append((plabel, diff, snapshot))
        for name, arr in probe_map.items():
            p_units = arr[rep] if rep < len(arr) else None
            if not isinstance(p_units, dict):
                continue
            p_cache_exp = None
            if name in probe_cache_map:
                pc = probe_cache_map[name]
                p_cache_exp = pc[rep] if rep < len(pc) else None
            p_cache = _response_cache_seen(p_units, p_cache_exp)
            p_comp = _comparable_units(p_units, p_cache)
            if "total_minus_output" in p_units:
                saw_total = True
            if len(p_comp) >= 2:
                qualifying_responses += 1
                label = "%s repeat %d" % (name, rep + 1)
                present = set(p_comp)
                for (u1, u2), plabel, diff, snapshot in _pair_diffs(pair_candidates, label, p_units):
                    if u1 in present and u2 in present:
                        pair_entries[(u1, u2)].append((plabel, diff, snapshot))
            if not isinstance(c_units, dict):
                continue
            for unit in COMPARABLE_UNITS:
                if unit == "input_tokens" and (c_cache or p_cache):
                    continue
                if unit not in c_units or unit not in p_units:
                    continue
                delta = p_units[unit] - c_units[unit]
                deltas.append(delta)
                if unit not in units_with_deltas:
                    units_with_deltas.append(unit)
                if delta < 0:
                    negative_deltas.append(("%s repeat %d" % (name, rep + 1), delta, unit))

    if deltas:
        quantization_k = 0
        for candidate in QUANTIZATION_LADDER:
            if all(d % candidate == 0 for d in deltas):
                quantization_k = candidate
                break
        usage_suspect = all(d % 5 == 0 for d in deltas)
    else:
        quantization_k = 0
        usage_suspect = False

    applicable = bool(saw_total) and dialect != "anthropic"
    constant_offsets = []
    if applicable:
        for (u1, u2), entries in pair_entries.items():
            if not entries:
                continue
            signed = [diff for _, diff, _ in entries]
            canonical = signed[0]
            if all(diff == canonical for diff in signed):
                if canonical != 0:
                    constant_offsets.append(
                        "%s - %s = %d in every response; cancelled by control subtraction" % (
                            u1, u2, abs(canonical)))
            else:
                for label, diff, snapshot in entries:
                    if diff != canonical:
                        disagreements.append("%s: %s" % (
                            label, " ".join("%s=%d" % (u, snapshot[u]) for u in (u1, u2))))
        for label, delta, unit in negative_deltas:
            disagreements.append("%s: negative delta %d (%s)" % (label, delta, unit))
        agree = len(disagreements) == 0 if qualifying_responses else None
        reason = None
    else:
        agree = None
        reason = "not applicable: this dialect reports no total_tokens"

    return {
        "agree": agree,
        "applicable": applicable,
        "reason": reason,
        "units": [u for u in COMPARABLE_UNITS if u in units_with_deltas],
        "disagreements": disagreements,
        "constant_offsets": constant_offsets,
        "deltas_checked": len(deltas),
        "quantization_k": quantization_k,
        "usage_suspect": usage_suspect,
    }


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


_OPENER = None


def _opener():
    """Lazily built module-level opener; building it re-reads the system cert
    store on every call on Windows, so it is built once and reused."""
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(SafeRedirectHandler())
    return _OPENER


def _reset_opener():
    """Test hook: drop the cached opener so the next call rebuilds it."""
    global _OPENER
    _OPENER = None


def post_json(url, payload, key, timeout, return_headers=False):
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
    opener = _opener()
    with opener.open(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
        if return_headers:
            return resp.status, body, dict(resp.headers.items())
        return resp.status, body


def post_stream_json(url, payload, key, timeout):
    """POST with stream requested; parse the SSE body.

    Lines starting with "data:" are JSON objects; the final object carrying
    "usage" wins. "[DONE]" and unparsable lines are ignored. Returns
    (status, usage_tokens or None, headers). Raises HTTPError on a >= 400
    response so the caller can treat streaming as unavailable.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    opener = _opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        headers = dict(resp.headers.items())
        status = resp.status
    usage_tokens = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if isinstance(usage, dict):
            tok = parse_usage_tokens({"usage": usage})
            if tok is not None:
                usage_tokens = tok
    return status, usage_tokens, headers


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


def _header(headers, name):
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def _consistency_request(base, model, key, text, dialect_holder, timeout, budget,
                         stream=False, return_headers=False):
    """One consistency-suite request. 404/405 on the OpenAI path switches the
    holder to the Anthropic dialect, mirroring probe_once. No ladder, no
    retries: the suite is a measurement, not a rescue."""
    for attempt_dialect in range(2):
        path = "/chat/completions" if dialect_holder[0] == "openai" else "/messages"
        url = base.rstrip("/") + path
        body = {"model": model, "messages": [{"role": "user", "content": text}],
                "max_tokens": MAX_TOKENS_LADDER[0], "stream": bool(stream)}
        budget.acquire()
        try:
            if stream:
                status, usage_tokens, headers = post_stream_json(url, body, key, timeout)
                return {"status": status, "body": None, "usage_tokens": usage_tokens,
                        "headers": headers}
            if return_headers:
                status, resp_body, headers = post_json(url, body, key, timeout,
                                                       return_headers=True)
                return {"status": status, "body": resp_body,
                        "usage_tokens": parse_usage_tokens(resp_body), "headers": headers}
            status, resp_body = post_json(url, body, key, timeout)
            return {"status": status, "body": resp_body,
                    "usage_tokens": parse_usage_tokens(resp_body), "headers": None}
        except urllib.error.HTTPError as e:
            if e.code in (404, 405) and dialect_holder[0] == "openai" and attempt_dialect == 0:
                dialect_holder[0] = "anthropic"
                continue
            raise ProbeFailure("endpoint returned HTTP %d" % e.code)
        except ProbeFailure:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ProbeFailure("endpoint did not answer (%s)" % type(e).__name__)
    raise ProbeFailure("endpoint did not answer")


def _budget_note(suite, budget, err=None):
    if budget.exceeded:
        suite["notes"].append("consistency incomplete: request budget exhausted")
    else:
        suite["notes"].append("consistency unavailable: %s" % (err or "unknown error"))


def run_consistency_suite(base, model, key, dialect_holder, timeout, sleep, budget,
                          stream=False):
    """Consistency suite. Notes only: results are appended to the report and
    printed, never used to pick or change a verdict (R1). Every HTTP attempt
    passes through the shared RequestBudget."""
    suite = {"linearity": {}, "determinism": {}, "side_evidence": {}, "notes": []}
    if stream:
        suite["stream"] = {}

    # --- control once, same path as everything else; headers ride on this one ---
    try:
        ctrl = _consistency_request(base, model, key, CONTROL_TEXT, dialect_holder,
                                    timeout, budget, return_headers=True)
    except ProbeFailure as e:
        _budget_note(suite, budget, e)
        return suite
    control_tokens = ctrl["usage_tokens"]
    if control_tokens is None:
        suite["notes"].append("consistency: control response carried no token usage; the consistency suite could not run")
        return suite

    # --- side evidence: headers on the first consistency response + model echo ---
    ctrl_body = ctrl["body"] if isinstance(ctrl.get("body"), dict) else {}
    echo = ctrl_body.get("model")
    if not isinstance(echo, str):
        echo = None
    if echo is None:
        model_matches = None
    else:
        model_matches = echo.strip().lower() == (model or "").strip().lower()
    headers = ctrl.get("headers") or {}
    suite["side_evidence"] = {
        "model_requested": model,
        "model_echo": echo,
        "model_matches": model_matches,
        "server": _header(headers, "Server"),
        "via": _header(headers, "Via"),
        "x_request_id": _header(headers, "X-Request-Id"),
    }
    if echo is not None and model_matches is False:
        suite["notes"].append("server echoed model %s while %s was requested; recorded as side evidence only, the verdict is unchanged (R1)" % (echo, model))
    pause(sleep)

    # --- linearity of usage vs text repetition ---
    for unit_name, unit_text in CONSISTENCY_UNITS:
        ks = list(LINEARITY_K)
        deltas = []
        unit_ok = True
        for k in ks:
            text = "\n".join([unit_text] * k)
            try:
                resp = _consistency_request(base, model, key, text, dialect_holder,
                                            timeout, budget)
            except ProbeFailure as e:
                _budget_note(suite, budget, e)
                return suite
            tok = resp["usage_tokens"]
            if tok is None:
                suite["notes"].append("consistency: response carried no token usage (unit %s, k=%d); the linearity check was skipped for this unit" % (unit_name, k))
                unit_ok = False
                break
            deltas.append(tok - control_tokens)
            pause(sleep)
        if not unit_ok:
            continue
        slope, intercept = statistics.linear_regression(ks, deltas)
        max_residual = max(abs(d - (slope * k + intercept)) for k, d in zip(ks, deltas))
        monotonic = all(deltas[i] < deltas[i + 1] for i in range(len(deltas) - 1))
        tol = tolerance(max(deltas))
        reasons = []
        if len(set(deltas)) == 1:
            reasons.append("constant series (slope %.3f)" % slope)
        if slope <= 0:
            reasons.append("slope <= 0 (%.3f)" % slope)
        if not monotonic:
            reasons.append("deltas are not strictly increasing")
        if max_residual > tol:
            reasons.append("max residual %.3f exceeds tolerance %d" % (max_residual, tol))
        usage_suspect = bool(reasons)
        reason = "; ".join(reasons) if reasons else "slope and residuals within tolerance"
        suite["linearity"][unit_name] = {
            "k": ks,
            "deltas": deltas,
            "slope": slope,
            "intercept": intercept,
            "max_residual": max_residual,
            "monotonic_increasing": monotonic,
            "tolerance": tol,
            "usage_suspect": usage_suspect,
            "reason": reason,
        }

    # --- determinism: the same unit text twice, exact comparison ---
    det_unit, det_text = CONSISTENCY_UNITS[0]
    det_tokens = []
    det_ok = True
    for _ in range(2):
        try:
            resp = _consistency_request(base, model, key, det_text, dialect_holder,
                                        timeout, budget)
        except ProbeFailure as e:
            _budget_note(suite, budget, e)
            return suite
        tok = resp["usage_tokens"]
        if tok is None:
            suite["notes"].append("consistency: response carried no token usage (determinism, unit %s); the determinism check was skipped" % det_unit)
            det_ok = False
            break
        det_tokens.append(tok)
        pause(sleep)
    if det_ok:
        deterministic = det_tokens[0] == det_tokens[1]
        suite["determinism"] = {"unit": det_unit, "tokens": det_tokens,
                                "deterministic": deterministic}
        if deterministic is False:
            suite["notes"].append("determinism check: repeated identical text returned different input token counts (%d vs %d); recorded as a note only, the verdict is unchanged (R1)" % (det_tokens[0], det_tokens[1]))

    # --- streaming vs non-streaming, only when --stream is set ---
    if stream:
        stream_text = CONSISTENCY_UNITS[0][1]
        stream_result = {"non_stream_tokens": None, "stream_tokens": None,
                         "agree": None, "note": "stream unavailable"}
        try:
            resp = _consistency_request(base, model, key, stream_text, dialect_holder,
                                        timeout, budget, stream=False)
            ns_tokens = resp["usage_tokens"]
        except ProbeFailure as e:
            if budget.exceeded:
                _budget_note(suite, budget, e)
                suite["stream"] = stream_result
                return suite
            ns_tokens = None
        if ns_tokens is None:
            stream_result["note"] = "stream unavailable"
        else:
            stream_result["non_stream_tokens"] = ns_tokens
            pause(sleep)
            try:
                resp = _consistency_request(base, model, key, stream_text, dialect_holder,
                                            timeout, budget, stream=True)
                st_tokens = resp["usage_tokens"]
            except ProbeFailure as e:
                if budget.exceeded:
                    _budget_note(suite, budget, e)
                    suite["stream"] = stream_result
                    return suite
                st_tokens = None
            if st_tokens is None:
                stream_result["note"] = "stream unavailable"
            else:
                stream_result["stream_tokens"] = st_tokens
                agree = ns_tokens == st_tokens
                stream_result["agree"] = agree
                if agree:
                    stream_result["note"] = "usage agrees"
                else:
                    stream_result["note"] = ("stream usage differs from non-streaming (%d vs %d); "
                                             "recorded as a note only, the verdict is unchanged (R1)" % (ns_tokens, st_tokens))
                    suite["notes"].append("stream check: %s" % stream_result["note"])
        if stream_result["agree"] is None and "stream unavailable" == stream_result["note"]:
            suite["notes"].append("stream unavailable: the route did not answer a streaming request with a usable usage block; recorded as a note only (R1)")
        suite["stream"] = stream_result
    return suite


def print_consistency_block(suite, usage=None):
    print("consistency:")
    for unit_name in (u for u, _ in CONSISTENCY_UNITS):
        entry = suite.get("linearity", {}).get(unit_name)
        if not entry:
            continue
        print("  linearity %s: k=%s slope=%.3f intercept=%.3f max_residual=%.3f tolerance=%d monotonic=%s usage_suspect=%s (%s)" % (
            unit_name, ",".join(str(k) for k in entry["k"]), entry["slope"],
            entry["intercept"], entry["max_residual"], entry["tolerance"],
            entry["monotonic_increasing"], entry["usage_suspect"], entry["reason"]))
    det = suite.get("determinism") or {}
    if det:
        tokens = det.get("tokens") or []
        print("  determinism: unit=%s tokens=%s deterministic=%s" % (
            det.get("unit"), ",".join(str(t) for t in tokens), det.get("deterministic")))
    st = suite.get("stream")
    if st is not None:
        print("  stream: non_stream_tokens=%s stream_tokens=%s agree=%s note=%s" % (
            st.get("non_stream_tokens"), st.get("stream_tokens"),
            st.get("agree"), st.get("note")))
    se = suite.get("side_evidence") or {}
    if se:
        print("  side_evidence: model_requested=%s model_echo=%s model_matches=%s server=%s via=%s x_request_id=%s" % (
            se.get("model_requested"), se.get("model_echo"), se.get("model_matches"),
            se.get("server"), se.get("via"), se.get("x_request_id")))
    for offset in (usage or {}).get("constant_offsets") or []:
        print("  constant_offset: %s" % offset)
    for note in suite.get("notes", []):
        print("  note: %s" % note)


CONSISTENCY_ESTIMATE = 18  # 1 control + 3 units x 5 k values + 2 determinism
CONSISTENCY_STREAM_EXTRA = 2  # one non-streaming plus one streaming request


def consistency_estimate(stream=False):
    return CONSISTENCY_ESTIMATE + (CONSISTENCY_STREAM_EXTRA if stream else 0)


def consistency_estimate_line(stream=False):
    if stream:
        return ("estimated consistency requests: 20 "
                "(1 control + 15 linearity + 2 determinism; plus 2 stream)")
    return ("estimated consistency requests: 18 "
            "(1 control + 15 linearity + 2 determinism; add 2 when --stream)")


def run_consistency(args, api_key=None):
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
    if args.timeout <= 0:
        print("error: --timeout must be > 0", file=sys.stderr)
        return 1
    if args.sleep < 0:
        print("error: --sleep must be >= 0", file=sys.stderr)
        return 1
    if getattr(args, "max_requests", 0) is None or getattr(args, "max_requests", 0) < 0:
        print("error: --max-requests must be >= 0", file=sys.stderr)
        return 1
    stream = bool(getattr(args, "stream", False))
    est = consistency_estimate(stream)
    print(consistency_estimate_line(stream))
    max_requests = getattr(args, "max_requests", 0) or 0
    if max_requests:
        print("request limit: %d" % max_requests)
        if est > max_requests:
            print("warning: the estimate exceeds --max-requests; the suite will stop and note that the request budget was exhausted")
    budget = RequestBudget(max_requests)
    dialect_holder = ["openai"]
    suite = run_consistency_suite(args.base, args.model, key, dialect_holder,
                                  args.timeout, args.sleep, budget, stream=stream)
    print("base: %s" % args.base)
    print("model: %s" % args.model)
    print("dialect: %s" % dialect_holder[0])
    print_consistency_block(suite)
    report = {
        "tool": TOOL,
        "version": VERSION,
        "mode": "consistency",
        "base": args.base,
        "model": args.model,
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dialect": dialect_holder[0],
        "consistency": suite,
        "budget": {
            "estimated": est,
            "limit": budget.limit,
            "used": budget.used,
            "exceeded": bool(budget.exceeded),
        },
    }
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            print("report written to %s" % args.out)
        except OSError as e:
            print("error: cannot write --out file: %s" % e, file=sys.stderr)
            return 1
    return 0


def build_report(base, model, dialect, control_tokens_list, probe_results, family,
                 signature, matches, verdict, note, failed_probes=None,
                 total_texts=None, unstable_texts=None, budget=None,
                 estimated_requests=None, baseline=None, control_units=None,
                 consistency=None):
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
            "units": list(control_units or []),
        },
        "reference": reference,
        "probes": probe_results,
        "matches": matches,
        "texts_total": total_texts if total_texts is not None else len(probe_results),
        "unstable_texts": list(unstable_texts or []),
        "verdict": verdict,
        "note": note,
        "failed_probes": list(failed_probes or []),
        "consistency": consistency or {
            "agree": None,
            "applicable": False,
            "reason": "not applicable: this dialect reports no total_tokens",
            "units": [],
            "disagreements": [],
            "constant_offsets": [],
            "deltas_checked": 0,
            "quantization_k": 0,
            "usage_suspect": False,
        },
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
    with_consistency = bool(getattr(args, "with_consistency", False))
    stream = bool(getattr(args, "stream", False))
    consistency_est = consistency_estimate(stream)
    if with_consistency:
        print("estimated requests: %d (%d probe + %d consistency)" % (
            estimated + consistency_est, estimated, consistency_est))
    max_requests = getattr(args, "max_requests", 0) or 0
    if max_requests:
        print("request limit: %d" % max_requests)
        if estimated > max_requests:
            print("warning: the estimate exceeds --max-requests; the run will stop and report PROBE INCOMPLETE")

    budget = RequestBudget(max_requests)

    dialect_holder = ["openai"]
    control_tokens_all = []
    tokens_by_name = {name: [] for name, _ in texts}
    control_units_by_rep = []
    control_cache_by_rep = []
    probe_units_by_name = {name: [] for name, _ in texts}
    probe_cache_by_name = {name: [] for name, _ in texts}
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
        control_units_by_rep.append(usage_units(ctrl_body))
        control_cache_by_rep.append(cache_fields_seen(ctrl_body))
        pause(args.sleep)
        for i, (name, text) in enumerate(texts):
            try:
                body = probe_once(args.base, args.model, key, text, dialect_holder,
                                  args.timeout, budget=budget)
            except ProbeFailure as e:
                if budget.exceeded:
                    budget_stopped = True
                    break
                probe_units_by_name[name].append(None)
                probe_cache_by_name[name].append(None)
                failed_probes.append("%s (%s)" % (name, e))
                continue
            tok = parse_usage_tokens(body)
            if tok is None:
                probe_units_by_name[name].append(None)
                probe_cache_by_name[name].append(None)
                failed_probes.append("%s (no token usage in the response)" % name)
                continue
            probe_units_by_name[name].append(usage_units(body))
            probe_cache_by_name[name].append(cache_fields_seen(body))
            tokens_by_name[name].append((rep, tok))
            if i < len(texts) - 1 or rep < args.repeat - 1:
                pause(args.sleep)
        if budget_stopped:
            break
        if endpoint_error or usage_missing:
            break

    dialect = dialect_holder[0]
    control_unstable = (args.repeat > 1 and len(control_tokens_all) > 1 and is_unstable(control_tokens_all))
    consistency = usage_consistency(control_units_by_rep, probe_units_by_name,
                                    control_cache_by_rep, probe_cache_by_name,
                                    dialect=dialect)
    consistency_suite = None
    if with_consistency:
        consistency_suite = run_consistency_suite(
            args.base, args.model, key, dialect_holder,
            args.timeout, args.sleep, budget, stream=stream)
        dialect = dialect_holder[0]
        consistency["linearity"] = consistency_suite["linearity"]
        consistency["determinism"] = consistency_suite["determinism"]
        if "stream" in consistency_suite:
            consistency["stream"] = consistency_suite["stream"]
        consistency["side_evidence"] = consistency_suite["side_evidence"]
        consistency["notes"] = consistency_suite["notes"]

    probe_results = []
    matches = None
    unstable_texts = []
    if control_tokens_all and not endpoint_error and not usage_missing:
        for idx, (name, text) in enumerate(texts):
            pairs = tokens_by_name[name]
            ref = signature[idx] if signature and idx < len(signature) else None
            unit_entries = probe_units_by_name[name]
            first_units = next((u for u in unit_entries if u is not None), {})
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
                "units": first_units,
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

    if verdict not in ("ENDPOINT UNAVAILABLE", "NO USAGE REPORTED"):
        if consistency["agree"] is False:
            note += "; usage-unit consistency failed (%s); recorded as a note only, the verdict is unchanged (R1)" % (
                "; ".join(consistency["disagreements"][:2]))
        elif consistency.get("constant_offsets"):
            note += "; usage-unit constant offset (%s); recorded as a note only, the verdict is unchanged (R1)" % (
                "; ".join(consistency["constant_offsets"][:2]))

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

    if consistency["usage_suspect"] and verdict not in ("ENDPOINT UNAVAILABLE", "NO USAGE REPORTED"):
        note = ("warning: every delta in every unit is a multiple of 5, so a mismatch can come "
                "from rounded or synthesized usage rather than from the tokenizer; ") + note

    report_estimated = estimated + (consistency_est if with_consistency else 0)
    report = build_report(args.base, args.model, dialect, control_tokens_all,
                          probe_results, family, signature,
                          matches, verdict, note, failed_probes,
                          total_texts=n_texts, unstable_texts=unstable_texts,
                          budget=budget, estimated_requests=report_estimated,
                          baseline=baseline, control_units=control_units_by_rep,
                          consistency=consistency)

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
    if consistency_suite is not None:
        print_consistency_block(consistency_suite, usage=consistency)

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
    if argv and argv[0] == "consistency":
        consistency_parser = VerifierArgumentParser(
            prog="verify_gateway.py consistency",
            description="Run only the consistency suite (usage linearity vs repetition, determinism, side evidence; --stream adds the streaming check). Notes only; the family verdict is untouched.")
        consistency_parser.add_argument("--base", help="base URL of the gateway, e.g. https://gw.example.com/v1")
        consistency_parser.add_argument("--model", help="model id to check, e.g. anthropic/claude-opus-4.8")
        consistency_parser.add_argument("--key-stdin", action="store_true",
                                        help="read the API key from one line on stdin; env VERIFY_API_KEY is the other option")
        consistency_parser.add_argument("--sleep", type=float, default=2, help="seconds between requests (default 2)")
        consistency_parser.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds (default 60)")
        consistency_parser.add_argument("--max-requests", type=int, default=0,
                                        help="stop after N HTTP requests (default 0 = unlimited)")
        consistency_parser.add_argument("--out", help="write the full JSON report to FILE")
        consistency_parser.add_argument("--stream", action="store_true",
                                        help="also send the unit text once non-streaming and once with stream true, and compare usage")
        return run_consistency(consistency_parser.parse_args(argv[1:]))

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
    parser.add_argument("--with-consistency", action="store_true",
                        help="after the probe run, also run the consistency suite and report it (notes only, the family verdict is unchanged)")
    parser.add_argument("--stream", action="store_true",
                        help="with --with-consistency, also check streaming vs non-streaming usage")
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

    # --- usage unit extraction ---
    check("total_minus_output derived from total minus completion", lambda: (
        usage_units({"usage": {"total_tokens": 100, "completion_tokens": 20}}) == {"total_minus_output": 80}))
    check("total_minus_output derived from output_tokens", lambda: (
        usage_units({"usage": {"total_tokens": 100, "output_tokens": 30}}) == {"total_minus_output": 70}))
    check("total_minus_output omitted when the result is negative", lambda: (
        "total_minus_output" not in usage_units({"usage": {"total_tokens": 10, "completion_tokens": 20}})))
    check("input_plus_cache with one cache field", lambda: (
        usage_units({"usage": {"input_tokens": 10, "cache_read_input_tokens": 4}})
        == {"input_tokens": 10, "input_plus_cache": 14}))
    check("input_plus_cache with both cache fields", lambda: (
        usage_units({"usage": {"input_tokens": 10, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 6}})
        == {"input_tokens": 10, "input_plus_cache": 20}))
    check("usage_units rejects boolean counts", lambda: usage_units({"usage": {"prompt_tokens": True}}) == {})
    check("usage_units rejects float counts", lambda: usage_units({"usage": {"prompt_tokens": 12.0}}) == {})
    check("usage_units rejects negative counts", lambda: usage_units({"usage": {"prompt_tokens": -1}}) == {})
    check("usage_units empty when usage missing", lambda: (
        usage_units({}) == {} and usage_units({"usage": {}}) == {}))

    # --- usage-unit consistency (pure function) ---
    def consistency_with_deltas(deltas):
        control = [{"prompt_tokens": 100} for _ in deltas]
        probes = {"T1": [{"prompt_tokens": 100 + d} for d in deltas]}
        return usage_consistency(control, probes)

    check("quantization k=2, not suspect", lambda: (lambda c: (
        c["quantization_k"] == 2 and c["usage_suspect"] is False))(consistency_with_deltas([14, 20, 18, 26, 22])))
    check("quantization k=5, usage_suspect", lambda: (lambda c: (
        c["quantization_k"] == 5 and c["usage_suspect"] is True))(consistency_with_deltas([15, 25, 20, 35, 30])))
    check("quantization k=8, not suspect", lambda: (lambda c: (
        c["quantization_k"] == 8 and c["usage_suspect"] is False))(consistency_with_deltas([16, 24, 40, 32, 8])))
    check("quantization k=3, not suspect", lambda: (lambda c: (
        c["quantization_k"] == 3 and c["usage_suspect"] is False))(consistency_with_deltas([21, 15, 27, 33, 39])))
    check("no deltas means k=0 and not suspect", lambda: (lambda c: (
        c["quantization_k"] == 0 and c["usage_suspect"] is False and c["deltas_checked"] == 0))(
        usage_consistency([], {})))
    check("agree is None when no response has >= 2 comparable units", lambda: (
        usage_consistency([{"prompt_tokens": 10}], {"T1": [{"prompt_tokens": 24}]})["agree"] is None))
    check("unit disagreement is recorded, agree False", lambda: (lambda c: (
        c["agree"] is False
        and c["disagreements"] == ["T1 repeat 1: prompt_tokens=40 total_minus_output=38"]))(
        usage_consistency([{"prompt_tokens": 10, "total_minus_output": 10}],
                          {"T1": [{"prompt_tokens": 40, "total_minus_output": 38}]})))
    check("negative delta counts as a consistency failure", lambda: (lambda c: (
        c["agree"] is False
        and any("negative delta" in d for d in c["disagreements"])))(
        usage_consistency([{"prompt_tokens": 100, "total_minus_output": 100}],
                          {"T1": [{"prompt_tokens": 90, "total_minus_output": 95}]})))
    check("input_tokens skipped when cache units were reported", lambda: (lambda c: (
        c["agree"] is True and c["deltas_checked"] == 2))(
        usage_consistency([{"input_tokens": 10, "prompt_tokens": 10, "total_minus_output": 10}],
                          {"T1": [{"input_tokens": 90, "prompt_tokens": 10, "total_minus_output": 10,
                                   "input_plus_cache": 90}]})))

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
    def post_json_fake(times=1):
        captured = {"count": 0}
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
            captured["count"] += 1
            return FakeOpener()

        def fake_urlopen(*args, **kwargs):
            captured["urlopen_called"] = True
            raise AssertionError("urlopen must not be called")

        _reset_opener()
        urllib.request.build_opener = fake_build_opener
        urllib.request.urlopen = fake_urlopen
        try:
            for i in range(times):
                captured.setdefault("results", []).append(
                    post_json("http://a.example/v1/chat/completions", {"x": i}, "selftest-key", 5))
            captured["result"] = captured["results"][-1]
        finally:
            urllib.request.build_opener = real_build_opener
            urllib.request.urlopen = real_urlopen
            _reset_opener()
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
    reuse_fake = post_json_fake(times=2)
    check("post_json reuses the cached opener across calls", lambda: reuse_fake["count"] == 1)

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
        state = {"control": 0, "counts": {}, "requests": 0}

        def base_token(text):
            if scenario == "constant":
                return 42
            unit_text_map = {}
            for uname, utext in CONSISTENCY_UNITS:
                for k in LINEARITY_K:
                    unit_text_map["\n".join([utext] * k)] = (uname, k)
            if text in unit_text_map:
                uname, k = unit_text_map[text]
                if scenario == "linear_honest":
                    return 5 + k * {"ascii": 3, "cjk": 2, "emoji": 4}[uname]
                if scenario == "linear_constant":
                    return 30
                if scenario == "linear_quadratic":
                    return 5 + 4 * k + 3 * k * k
                if scenario == "nondeterministic":
                    if text == CONSISTENCY_UNITS[0][1]:
                        state["counts"][text] = state["counts"].get(text, 0) + 1
                        return 10 + (state["counts"][text] % 2) * 2
                    return 5 + k * 3
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

            def send_json(self, code, obj, extra_headers=None):
                raw = json.dumps(obj).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    for name, value in (extra_headers or {}).items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def send_sse(self, model, usage):
                chunks = [
                    {"choices": [{"delta": {"role": "assistant", "content": "ok"}}]},
                    {"choices": [], "usage": usage},
                ]
                raw = "".join("data: %s\n\n" % json.dumps(c) for c in chunks)
                raw += "data: [DONE]\n\n"
                payload = raw.encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if scenario == "side_evidence":
                        self.send_header("Server", "mock-gateway")
                        self.send_header("Via", "1.1 mock-proxy")
                        self.send_header("X-Request-Id", "req-side-0001")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def do_POST(self):
                state["requests"] += 1
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
                if scenario in ("anthropic_only", "anthropic_dialect") and self.path.endswith("/chat/completions"):
                    self.send_json(404, {"error": "not found"})
                    return
                if scenario == "timeout":
                    time.sleep(0.3)
                try:
                    text = payload["messages"][0]["content"]
                except Exception:
                    text = ""
                echo_model = payload.get("model", "mock-model")
                if scenario == "side_evidence":
                    echo_model = "mock/echoed-other-model"
                if scenario != "no_usage" and payload.get("stream"):
                    if scenario == "stream_unavailable":
                        self.send_json(400, {"error": "streaming not supported"})
                        return
                    usage_tokens = base_token(text)
                    if scenario == "stream_diff":
                        usage_tokens += 3
                    if self.path.endswith("/messages"):
                        usage = {"input_tokens": usage_tokens}
                    else:
                        usage = {"prompt_tokens": usage_tokens}
                    self.send_sse(echo_model, usage)
                    return
                token = base_token(text)
                body = {
                    "model": echo_model,
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                }
                if scenario != "no_usage":
                    if self.path.endswith("/messages"):
                        body["usage"] = {"input_tokens": token}
                    else:
                        body["usage"] = {"prompt_tokens": token}
                if scenario in ("units_disagree", "units_varying"):
                    # prompt_tokens and total_minus_output both reported.
                    # units_disagree: they disagree by 7 on every response (constant offset).
                    # units_varying: the offset changes per request (strong disagreement).
                    usage = body["usage"]
                    base_t = usage.get("prompt_tokens", usage.get("input_tokens"))
                    offset = 7 if scenario == "units_disagree" else 7 + state["requests"]
                    usage["total_tokens"] = base_t + offset
                    usage["completion_tokens"] = 0
                if scenario == "side_evidence":
                    self.send_json(200, body, extra_headers={
                        "Server": "mock-gateway",
                        "Via": "1.1 mock-proxy",
                        "X-Request-Id": "req-side-0001",
                    })
                    return
                self.send_json(200, body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
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
            "with_consistency": False,
            "stream": False,
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

    code_ad, out_ad = scenario_run("anthropic_dialect", signature=(18, 25, 20, 36, 38),
                                   model="anthropic/claude-sonnet-4.6")
    check("anthropic_dialect yields SIGNATURE MATCH with the anthropic signature", lambda: (
        code_ad == 0 and "dialect: anthropic" in out_ad and "verdict: SIGNATURE MATCH" in out_ad))
    check("anthropic_dialect output carries no usage-unit consistency failure", lambda: (
        "usage-unit consistency failed" not in out_ad))

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

    # --- usage consistency: note-only behaviour (mock gateway) ---
    def scenario_report(scenario, signature=None, **overrides):
        if signature is None:
            server, base = start_gateway(scenario)
        else:
            server, base = start_gateway(scenario, signature=signature)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                opts = dict(overrides)
                opts["out"] = target
                run_capture(args_for(base, **opts))
                with open(target, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        finally:
            stop_gateway(server)

    code_ud, out_ud = scenario_run("units_disagree")
    code_h, out_h = scenario_run("honest")
    check("units_disagree verdict is unchanged (SIGNATURE MATCH)", lambda: (
        "verdict: SIGNATURE MATCH" in out_ud and "matches: 5/5" in out_ud))
    check("units_disagree note records a constant offset, not the strong failure", lambda: (
        "usage-unit constant offset" in out_ud
        and "prompt_tokens - total_minus_output = 7 in every response" in out_ud
        and "usage-unit consistency failed" not in out_ud))
    check("units_disagree note says the verdict is unchanged", lambda: "verdict is unchanged" in out_ud)

    def verdict_line(out):
        return next(l for l in out.splitlines() if l.startswith("verdict:"))

    check("units_disagree verdict line identical to honest run", lambda: verdict_line(out_ud) == verdict_line(out_h))

    ud_report = scenario_report("units_disagree")
    check("units_disagree report consistency block", lambda: (
        ud_report["consistency"]["agree"] is True
        and ud_report["consistency"]["applicable"] is True
        and ud_report["consistency"]["deltas_checked"] == 10
        and ud_report["consistency"]["quantization_k"] == 1
        and ud_report["consistency"]["usage_suspect"] is False
        and ud_report["consistency"]["disagreements"] == []
        and ud_report["consistency"]["constant_offsets"] == [
            "prompt_tokens - total_minus_output = 7 in every response; cancelled by control subtraction"]))
    check("units_disagree report carries units (control list, probe dicts)", lambda: (
        len(ud_report["control"]["units"]) == 1
        and ud_report["control"]["units"][0].get("prompt_tokens") == 5
        and ud_report["control"]["units"][0].get("total_minus_output") == 12
        and all(isinstance(p.get("units"), dict) for p in ud_report["probes"])))

    honest_report = scenario_report("honest")
    check("honest report consistency: not applicable, agree None, deltas 5", lambda: (
        honest_report["consistency"]["agree"] is None
        and honest_report["consistency"]["applicable"] is False
        and honest_report["consistency"]["reason"] == "not applicable: this dialect reports no total_tokens"
        and honest_report["consistency"]["deltas_checked"] == 5
        and honest_report["consistency"]["usage_suspect"] is False))

    code_ad, out_ad = scenario_run("anthropic_dialect", signature=(18, 25, 20, 36, 38),
                                   model="anthropic/claude-sonnet-4.6")
    ad_report = scenario_report("anthropic_dialect", signature=(18, 25, 20, 36, 38),
                                model="anthropic/claude-sonnet-4.6")
    check("anthropic_dialect report consistency is not applicable", lambda: (
        ad_report["consistency"]["applicable"] is False
        and ad_report["consistency"]["agree"] is None
        and ad_report["consistency"]["reason"] == "not applicable: this dialect reports no total_tokens"))

    code_uv, out_uv = scenario_run("units_varying")
    check("units_varying verdict is unchanged (SIGNATURE MATCH)", lambda: (
        "verdict: SIGNATURE MATCH" in out_uv and "matches: 5/5" in out_uv))
    check("units_varying note records the strong consistency failure", lambda: (
        "usage-unit consistency failed" in out_uv and "verdict is unchanged" in out_uv))
    uv_report = scenario_report("units_varying")
    check("units_varying report consistency: agree False, no constant offset", lambda: (
        uv_report["consistency"]["agree"] is False
        and uv_report["consistency"]["applicable"] is True
        and uv_report["consistency"]["constant_offsets"] == []
        and len(uv_report["consistency"]["disagreements"]) >= 1))

    code, out = scenario_run("rounded")
    check("rounded note gains usage_suspect and multiple of 5", lambda: (
        "verdict: SIGNATURE MISMATCH" in out
        and "note: warning: every delta in every unit is a multiple of 5" in out
        and "rounded or synthesized usage rather than from the tokenizer" in out))
    rounded_note_line = next(l for l in out.splitlines() if l.startswith("note:"))
    check("rounded note line starts with the warning", lambda: rounded_note_line.startswith("note: warning:"))
    check("rounded verdict line still carries SIGNATURE MISMATCH", lambda: (
        "verdict: SIGNATURE MISMATCH" in out))
    honest_note_line = next(l for l in out_h.splitlines() if l.startswith("note:"))
    check("honest note does not start with the warning", lambda: not honest_note_line.startswith("note: warning:"))
    rounded_report = scenario_report("rounded")
    check("rounded report usage_suspect True, quantization_k 5", lambda: (
        rounded_report["consistency"]["usage_suspect"] is True
        and rounded_report["consistency"]["quantization_k"] == 5))
    check("honest note has no usage_suspect", lambda: "usage_suspect" not in out_h)

    # --- every HTTP attempt goes through the budget (counter proof) ---
    def budget_counter_case():
        calls = {"n": 0}
        real_post = post_json

        def counting_post(url, payload, key, timeout, **kwargs):
            calls["n"] += 1
            return real_post(url, payload, key, timeout, **kwargs)

        globals()["post_json"] = counting_post
        try:
            first_report = scenario_report("honest", repeat=2)
            first_calls = calls["n"]
            calls["n"] = 0
            code2, out2 = scenario_run("honest", repeat=2, max_requests=4)
            return first_calls, first_report["budget"]["used"], calls["n"], out2
        finally:
            globals()["post_json"] = real_post

    b_calls, b_used, b4_calls, b4_out = budget_counter_case()
    check("repeat=2 unlimited: counter equals report budget.used (12)", lambda: (
        b_calls == 12 and b_used == 12))
    check("max_requests=4: counter is exactly 4, verdict PROBE INCOMPLETE", lambda: (
        b4_calls == 4 and "verdict: PROBE INCOMPLETE" in b4_out))

    # --- the request estimate prints before the first request ---
    def estimate_before_first_request():
        real_post = post_json

        def sentinel_post(url, payload, key, timeout, **kwargs):
            print("__mock_request__")
            return real_post(url, payload, key, timeout, **kwargs)

        globals()["post_json"] = sentinel_post
        try:
            code, out = scenario_run("honest")
        finally:
            globals()["post_json"] = real_post
        est = out.find("estimated requests: 6")
        first = out.find("__mock_request__")
        return est != -1 and first != -1 and est < first

    check("estimated requests: 6 line prints before the first request", estimate_before_first_request)
    code, out = scenario_run("honest", repeat=2)
    check("repeat=2 prints estimated requests: 12", lambda: "estimated requests: 12" in out)

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
        and "model not measured directly on this text set; mapped through the family" in out))

    code, out = scenario_run("honest", model="deepseek/deepseek-v4.1-flash")
    check("measured model carries no inferred-model caveat", lambda: (
        "verdict: SIGNATURE MATCH" in out and "model not measured directly" not in out))

    check("output caveats are ASCII-only and carry no Indonesian markers", lambda: all(
        all(ord(c) < 128 for c in caveat)
        and not any(marker in caveat for marker in ("dipetakan", "belum", "lewat", "keluarga"))
        for caveat in (BASELINE_CAVEAT, INFERRED_MODEL_CAVEAT, SINGLE_ROUTE_CAVEAT)))

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

    # --- consistency suite (Phase 2b) ---
    def consistency_scenario_report(scenario, **overrides):
        server, base = start_gateway(scenario)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                opts = dict(overrides)
                opts["out"] = target
                code, out = run_capture(args_for(base, with_consistency=True, **opts))
                with open(target, "r", encoding="utf-8") as fh:
                    return code, out, json.load(fh)
        finally:
            stop_gateway(server)

    def consistency_main_capture(argv):
        old = sys.argv
        old_env = os.environ.get("VERIFY_API_KEY")
        sys.argv = ["verify_gateway.py"] + argv
        os.environ["VERIFY_API_KEY"] = "selftest-key"
        out = io.StringIO()
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.argv = old
            if old_env is None:
                os.environ.pop("VERIFY_API_KEY", None)
            else:
                os.environ["VERIFY_API_KEY"] = old_env
        return code, out.getvalue() + err.getvalue()

    code_lh, out_lh, rep_lh = consistency_scenario_report("linear_honest")
    lin_lh = rep_lh["consistency"]["linearity"]
    check("linear_honest keeps the family verdict", lambda: (
        "verdict: SIGNATURE MATCH" in out_lh and "consistency:" in out_lh))
    check("linear_honest slopes equal the per-unit rates", lambda: (
        abs(lin_lh["ascii"]["slope"] - 3.0) < 1e-9
        and abs(lin_lh["cjk"]["slope"] - 2.0) < 1e-9
        and abs(lin_lh["emoji"]["slope"] - 4.0) < 1e-9))
    check("linear_honest residuals are zero and not suspect", lambda: all(
        abs(lin_lh[u]["max_residual"]) < 1e-9
        and lin_lh[u]["monotonic_increasing"] is True
        and lin_lh[u]["usage_suspect"] is False
        for u in ("ascii", "cjk", "emoji")))
    check("linear_honest determinism recorded and true", lambda: (
        rep_lh["consistency"]["determinism"]["deterministic"] is True
        and rep_lh["consistency"]["determinism"]["tokens"][0] == rep_lh["consistency"]["determinism"]["tokens"][1]))

    code_lc, out_lc, rep_lc = consistency_scenario_report("linear_constant")
    lin_lc = rep_lc["consistency"]["linearity"]
    check("linear_constant slope is zero and usage_suspect with a constant/slope reason", lambda: all(
        abs(lin_lc[u]["slope"]) < 1e-9
        and lin_lc[u]["usage_suspect"] is True
        and ("constant" in lin_lc[u]["reason"] or "slope" in lin_lc[u]["reason"])
        for u in ("ascii", "cjk", "emoji")))

    code_lq, out_lq, rep_lq = consistency_scenario_report("linear_quadratic")
    lin_lq = rep_lq["consistency"]["linearity"]
    check("linear_quadratic residual exceeds tolerance and usage_suspect is set", lambda: all(
        lin_lq[u]["max_residual"] > lin_lq[u]["tolerance"]
        and lin_lq[u]["usage_suspect"] is True
        for u in ("ascii", "cjk", "emoji")))
    check("linear_quadratic stays strictly increasing", lambda: all(
        lin_lq[u]["monotonic_increasing"] is True for u in ("ascii", "cjk", "emoji")))

    code_nd, out_nd, rep_nd = consistency_scenario_report("nondeterministic")
    check("nondeterministic repeats are recorded and flagged", lambda: (
        rep_nd["consistency"]["determinism"]["deterministic"] is False
        and "determinism check" in out_nd))

    code_so, out_so, rep_so = consistency_scenario_report("stream_ok", stream=True)
    check("stream_ok agrees with non-streaming usage", lambda: (
        rep_so["consistency"]["stream"]["agree"] is True
        and rep_so["consistency"]["stream"]["non_stream_tokens"]
        == rep_so["consistency"]["stream"]["stream_tokens"]))
    code_sd, out_sd, rep_sd = consistency_scenario_report("stream_diff", stream=True)
    check("stream_diff disagrees with non-streaming usage", lambda: (
        rep_sd["consistency"]["stream"]["agree"] is False
        and rep_sd["consistency"]["stream"]["stream_tokens"]
        == rep_sd["consistency"]["stream"]["non_stream_tokens"] + 3))
    code_su, out_su, rep_su = consistency_scenario_report("stream_unavailable", stream=True)
    check("stream_unavailable records agree=None and a note", lambda: (
        rep_su["consistency"]["stream"]["agree"] is None
        and "stream unavailable" in rep_su["consistency"]["stream"]["note"]
        and any("stream unavailable" in n for n in rep_su["consistency"]["notes"])))
    check("stream block absent without --stream", lambda: "stream" not in rep_lh["consistency"])

    code_se, out_se, rep_se = consistency_scenario_report("side_evidence")
    se = rep_se["consistency"]["side_evidence"]
    check("side evidence captured (headers and model echo)", lambda: (
        se["server"] == "mock-gateway" and se["via"] == "1.1 mock-proxy"
        and se["x_request_id"] == "req-side-0001"
        and se["model_requested"] == "deepseek/deepseek-v4.1-flash-1017"
        and se["model_echo"] == "mock/echoed-other-model"
        and se["model_matches"] is False))
    check("side evidence verdict line identical to the honest run", lambda: (
        verdict_line(out_se) == verdict_line(out_h)))
    check("side evidence note recorded as side evidence only", lambda: (
        "server echoed model mock/echoed-other-model" in out_se
        and "side evidence only" in out_se))

    code_h3, out_h3 = scenario_run("honest")
    check("--with-consistency verdict line identical to the plain run", lambda: (
        verdict_line(out_lh) == verdict_line(out_h3)))
    check("--with-consistency prints the combined estimate line", lambda: (
        "estimated requests: 24 (6 probe + 18 consistency)" in out_lh))
    check("--with-consistency prints a consistency block after the verdict", lambda: (
        "consistency:" in out_lh and out_lh.find("verdict:") < out_lh.find("consistency:")))
    check("--with-consistency report carries the consistency keys", lambda: all(
        k in rep_lh["consistency"] for k in ("linearity", "determinism", "side_evidence", "notes")))

    def consistency_estimate_before_first_request():
        real_post = post_json

        def sentinel_post(url, payload, key, timeout, **kwargs):
            print("__mock_request__")
            return real_post(url, payload, key, timeout, **kwargs)

        server, base = start_gateway("honest")
        globals()["post_json"] = sentinel_post
        try:
            code, out = consistency_main_capture(
                ["consistency", "--base", base, "--model", "deepseek/deepseek-v4.1-flash-1017",
                 "--sleep", "0", "--timeout", "5"])
        finally:
            globals()["post_json"] = real_post
            stop_gateway(server)
        est = out.find("estimated consistency requests: 18")
        first = out.find("__mock_request__")
        return est != -1 and first != -1 and est < first

    check("consistency-only estimate line printed before the first request", consistency_estimate_before_first_request)

    def consistency_budget_counter():
        calls = {"n": 0}
        real_post = post_json

        def counting_post(url, payload, key, timeout, **kwargs):
            calls["n"] += 1
            return real_post(url, payload, key, timeout, **kwargs)

        server, base = start_gateway("honest")
        globals()["post_json"] = counting_post
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                code, out = consistency_main_capture(
                    ["consistency", "--base", base, "--model", "deepseek/deepseek-v4.1-flash-1017",
                     "--sleep", "0", "--timeout", "5", "--out", target])
                with open(target, "r", encoding="utf-8") as fh:
                    report = json.load(fh)
        finally:
            globals()["post_json"] = real_post
            stop_gateway(server)
        return calls["n"], report["budget"]["used"], code, out

    c_calls, c_used, c_code, c_out = consistency_budget_counter()
    check("consistency-only: counter equals report budget.used", lambda: c_calls == c_used and c_code == 0)
    check("consistency-only: printed estimate 18 equals the unrestricted request count", lambda: (
        c_calls == 18 and "estimated consistency requests: 18" in c_out))

    def consistency_budget_counter_stream():
        calls = {"n": 0}
        real_post = post_json
        real_stream = post_stream_json

        def counting_post(url, payload, key, timeout, **kwargs):
            calls["n"] += 1
            return real_post(url, payload, key, timeout, **kwargs)

        def counting_stream(url, payload, key, timeout):
            calls["n"] += 1
            return real_stream(url, payload, key, timeout)

        server, base = start_gateway("honest")
        globals()["post_json"] = counting_post
        globals()["post_stream_json"] = counting_stream
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                code, out = consistency_main_capture(
                    ["consistency", "--base", base, "--model", "deepseek/deepseek-v4.1-flash-1017",
                     "--sleep", "0", "--timeout", "5", "--stream", "--out", target])
                with open(target, "r", encoding="utf-8") as fh:
                    report = json.load(fh)
        finally:
            globals()["post_json"] = real_post
            globals()["post_stream_json"] = real_stream
            stop_gateway(server)
        return calls["n"], report["budget"]["used"], code, out

    s_calls, s_used, s_code, s_out = consistency_budget_counter_stream()
    check("consistency --stream: counter equals report budget.used", lambda: s_calls == s_used and s_code == 0)
    check("consistency --stream: printed estimate 20 equals the unrestricted request count", lambda: (
        s_calls == 20 and "estimated consistency requests: 20" in s_out))
    check("--with-consistency --stream prints estimated requests: 26 (6 probe + 20 consistency)", lambda: (
        "estimated requests: 26 (6 probe + 20 consistency)" in out_so))

    def consistency_budget_stops_at_three():
        calls = {"n": 0}
        real_post = post_json

        def counting_post(url, payload, key, timeout, **kwargs):
            calls["n"] += 1
            return real_post(url, payload, key, timeout, **kwargs)

        server, base = start_gateway("honest")
        globals()["post_json"] = counting_post
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                code, out = consistency_main_capture(
                    ["consistency", "--base", base, "--model", "deepseek/deepseek-v4.1-flash-1017",
                     "--sleep", "0", "--timeout", "5", "--max-requests", "3", "--out", target])
        finally:
            globals()["post_json"] = real_post
            stop_gateway(server)
        return calls["n"], out

    c3_calls, c3_out = consistency_budget_stops_at_three()
    check("consistency-only: --max-requests 3 stops at exactly 3 requests", lambda: c3_calls == 3)
    check("consistency-only: budget exhaustion is recorded as a note only", lambda: (
        "consistency incomplete: request budget exhausted" in c3_out))

    def with_consistency_budget_counter():
        calls = {"n": 0}
        real_post = post_json

        def counting_post(url, payload, key, timeout, **kwargs):
            calls["n"] += 1
            return real_post(url, payload, key, timeout, **kwargs)

        server, base = start_gateway("honest")
        globals()["post_json"] = counting_post
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                target = os.path.join(tmpdir, "report.json")
                run_capture(args_for(base, with_consistency=True, out=target))
                with open(target, "r", encoding="utf-8") as fh:
                    report = json.load(fh)
        finally:
            globals()["post_json"] = real_post
            stop_gateway(server)
        return calls["n"], report["budget"]["used"]

    wc_calls, wc_used = with_consistency_budget_counter()
    check("--with-consistency: counter equals report budget.used (24)", lambda: wc_calls == wc_used == 24)

    check("consistency --help exits 0", lambda: (
        consistency_main_capture(["consistency", "--help"])[0] == 0
        and "usage:" in consistency_main_capture(["consistency", "--help"])[1].lower()))

    total = len(results)
    failed = [d for d, ok in results if not ok]
    passed = total - len(failed)

    if total != 238:
        print("SELFTEST (v3.1): internal error, expected 238 assertions, ran %d" % total, file=sys.stderr)
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
