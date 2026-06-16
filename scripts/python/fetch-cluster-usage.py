#!/usr/bin/env python3
"""Cluster usage report: configured CPU/mem limits & requests vs. real Thanos
usage, rolled up per namespace/workload/pod/container, plus an OOM-killed list
from live pod state + Thanos history. Self-contained (stdlib only); runs locally
via oc/kubectl and in-cluster as a CronJob. See scripts/README.md."""

import argparse
import atexit
import csv
import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


# ----------------------------------------------------------- quantity helpers

def parse_cpu(s):
    """Kubernetes CPU quantity -> cores (float). '100m'->0.1, '1'->1.0.

    Accepts milli ('m'), micro ('u'), nano ('n') suffixes and plain numbers.
    None/'' -> None.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip()
    if not s:
        return None
    try:
        if s.endswith("m"):
            return float(s[:-1]) / 1000
        if s.endswith("u"):
            return float(s[:-1]) / 1_000_000
        if s.endswith("n"):
            return float(s[:-1]) / 1_000_000_000
        return float(s)
    except ValueError:
        return None


def parse_mem(s):
    """Memory quantity -> bytes (int). Binary (Ki/Mi/Gi/Ti/Pi) and decimal
    (K/M/G/T/P) suffixes; plain numbers are bytes. None/invalid -> None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "Pi": 1024**5, "K": 1000, "M": 10**6, "G": 10**9, "T": 10**12,
             "P": 10**15}
    # Match the longest suffix first so binary "Ki"/"Mi" win over "K"/"M"
    # regardless of dict iteration order (robust on Python 3.6, too).
    for suf in sorted(units, key=len, reverse=True):
        mul = units[suf]
        if s.endswith(suf):
            try:
                return int(float(s[:-len(suf)]) * mul)
            except ValueError:
                return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def fmt_cores(v):
    """Cores -> Kubernetes-style human string. None -> '-', 0 -> '0c'.

    >=1 core: trimmed decimal + 'c' ('24.5c', '1c'); sub-core: milli ('500m',
    '2.8m'); sub-milli: micro ('0.525µ', the SI µ sign). The 'c' (cores) suffix
    keeps every CPU value text — without it a bare '18.5' is read by German-locale
    Excel as a date (18. Mai) or a thousands-grouped number. Tiny Thanos peaks
    stay readable instead of flattening to '0.000', and never use scientific
    notation."""
    if v is None:
        return "-"
    if v == 0:
        return "0c"
    a = abs(v)
    if a >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".") + "c"
    if a >= 1e-3:
        return f"{v * 1e3:.3g}m"
    return f"{v * 1e6:.3g}µ"


def fmt_bytes(n):
    """Bytes -> short unit string ('1.5Ki', '2.0Gi'). None -> '-'."""
    if n is None:
        return "-"
    v = float(n)
    for unit in ["B", "Ki", "Mi", "Gi", "Ti"]:
        if v < 1024:
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}Pi"


def fmt_pct(v):
    """Percentage float -> '95.0%'. None -> '-'."""
    return "-" if v is None else f"{v:.1f}%"


def round_up_cpu_10m(cores):
    """Round CPU cores UP to the next 10 millicores (0.01 cores). None -> None.
    An exact multiple is left unchanged (the 1e-9 nudge absorbs float noise so
    100m does not creep to 110m)."""
    if cores is None:
        return None
    return math.ceil(cores * 1000.0 / 10.0 - 1e-9) * 10 / 1000.0


def round_up_mem_mi(b):
    """Round bytes UP to the next Mi (1048576 bytes). None -> None. Exact
    multiples unchanged."""
    if b is None:
        return None
    mi = 1024 * 1024
    return math.ceil(b / mi - 1e-9) * mi


# ------------------------------------------------------------ stage detection

STAGE_KEYWORDS = ("ref", "prod", "test", "phase", "pnext")


def detect_stage(ns):
    """Stage from namespace name per convention pid-<id>-<app>-<STAGE>-<num>-...

    Checks dash-segment index 3 first (canonical position), then any segment,
    matching the bash loops' detect_stage. Returns 'other' if no keyword found.
    """
    parts = ns.lower().split("-")
    if len(parts) > 3 and parts[3] in STAGE_KEYWORDS:
        return parts[3]
    for p in parts:
        if p in STAGE_KEYWORDS:
            return p
    return "other"


# --------------------------------------------------- relationship resolution

def workload_for(pod, rs_index):
    """Logical workload (kind, name) for a pod. Follows pod -> ReplicaSet ->
    Deployment via ownerReferences so we report 'Deployment/web' rather than
    the ReplicaSet. rs_index maps (ns, name) -> ReplicaSet object. Returns
    ('', '') for pods with no controller owner."""
    ns = pod["metadata"]["namespace"]
    for owner in pod.get("metadata", {}).get("ownerReferences", []):
        if not owner.get("controller"):
            continue
        kind, name = owner["kind"], owner["name"]
        if kind == "ReplicaSet":
            rs = rs_index.get((ns, name))
            if rs:
                for o2 in rs.get("metadata", {}).get("ownerReferences", []):
                    if o2.get("controller"):
                        return o2.get("kind", "ReplicaSet"), o2.get("name", name)
            return kind, name
        return kind, name
    return "", ""


# ------------------------------------------------------------- rollup math

def sum_limit(values):
    """Sum configured requests/limits. Any unset (None) contributor means the
    aggregate is unbounded -> None. Empty -> None."""
    values = list(values)
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def sum_usage(values):
    """Sum observed usage. None means 'no data' for that contributor and is
    skipped. Returns None only when every contributor is missing."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def util_pct(usage, limit):
    """usage/limit as a percentage, or None when usage missing or limit
    unknown/zero."""
    if usage is None or not limit:
        return None
    return usage / limit * 100.0


# Canonical numeric fields every level carries.
LIMIT_FIELDS = ("cpu_request", "cpu_limit", "mem_request", "mem_limit")
USAGE_FIELDS = ("cpu_now", "cpu_peak", "cpu_avg", "mem_now", "mem_peak")
# Namespace ResourceQuota "Used" counters (requests/limits booked vs. the Hard
# cap). Only meaningful at namespace level and above (a quota is namespace-scoped
# — there is no per-pod/container quota); stage/cluster sum the namespaces, and
# workload/pod/container rows leave these None. Filled by collect_namespace().
QUOTA_USED_FIELDS = ("cpu_request_used", "cpu_limit_used",
                     "mem_request_used", "mem_limit_used")


def rollup(leaves):
    """Aggregate a list of leaf records into one level dict.

    Requests/limits use sum_limit (None if any contributor unset); usage uses
    sum_usage (None only if all missing). Adds cpu/mem peak-util%, oom_count,
    and distinct pod/container counts.
    """
    agg = {}
    for f in LIMIT_FIELDS:
        agg[f] = sum_limit([leaf.get(f) for leaf in leaves])
    for f in USAGE_FIELDS:
        agg[f] = sum_usage([leaf.get(f) for leaf in leaves])
    # Quota "Used" counters don't come from pod specs; they're set on the
    # namespace totals from the ResourceQuota (collect_namespace). None here.
    for f in QUOTA_USED_FIELDS:
        agg[f] = None
    agg["cpu_peak_util_pct"] = util_pct(agg["cpu_peak"], agg["cpu_limit"])
    agg["mem_peak_util_pct"] = util_pct(agg["mem_peak"], agg["mem_limit"])
    agg["oom_count"] = sum(leaf.get("oom_count", 0) for leaf in leaves)
    agg["pod_count"] = len({(leaf["namespace"], leaf["pod"]) for leaf in leaves})
    agg["container_count"] = len(leaves)
    return agg


def _qualifies(peak, current_limit, frac):
    """A resource qualifies for a recommendation when it has no current limit
    (unbounded -> always) or its peak exceeds `frac` of the current limit.
    `frac` is the utilisation threshold as a fraction in (0, 1]."""
    if current_limit is None:
        return True
    return peak > frac * current_limit


def compute_recommendation(totals, target_util=80.0):
    """Recommended request/limit for one workload's `totals`, restricted to
    'hot' resources. request = round_up(peak); limit = round_up(peak / frac)
    where frac = target_util/100. Returns a dict with keys cpu_request_rec,
    cpu_limit_rec, mem_request_rec, mem_limit_rec — each None when that resource
    has no peak or is not hot."""
    if not 0.0 < target_util <= 100.0:
        raise ValueError(
            f"target_util must be in (0, 100], got {target_util!r}")
    frac = target_util / 100.0
    rec = {"cpu_request_rec": None, "cpu_limit_rec": None,
           "mem_request_rec": None, "mem_limit_rec": None}

    cpu_peak = totals.get("cpu_peak")
    if cpu_peak is not None and _qualifies(cpu_peak, totals.get("cpu_limit"), frac):
        rec["cpu_request_rec"] = round_up_cpu_10m(cpu_peak)
        rec["cpu_limit_rec"] = round_up_cpu_10m(cpu_peak / frac)

    mem_peak = totals.get("mem_peak")
    if mem_peak is not None and _qualifies(mem_peak, totals.get("mem_limit"), frac):
        rec["mem_request_rec"] = round_up_mem_mi(mem_peak)
        rec["mem_limit_rec"] = round_up_mem_mi(mem_peak / frac)
    return rec


# base field -> the matching recommendation key in compute_recommendation output.
_REC_BASES = {"cpu_request": "cpu_request_rec", "cpu_limit": "cpu_limit_rec",
              "mem_request": "mem_request_rec", "mem_limit": "mem_limit_rec"}


def _quota_status(total, quota):
    """no-quota when the namespace has no cap; EXCEEDS when the summed
    recommendation is above the cap; OK otherwise (including nothing to place)."""
    if quota is None:
        return "no-quota"
    if total is None:
        return "OK"
    return "EXCEEDS" if total > quota else "OK"


def namespace_recommendation_summary(node, target_util=80.0):
    """Per-namespace gate: for each workload use the recommended value where the
    resource is hot, else its current configured value; sum per dimension
    (unset contributors skipped) and compare to the namespace quota hard cap.
    Returns stage/namespace, per-dimension *_rec_sum / *_quota / *_status, and
    an overall quota_action (INCREASE_QUOTA if any dimension EXCEEDS, else OK)."""
    collected = {base: [] for base in _REC_BASES}
    for wl in node["workloads"]:
        rec = compute_recommendation(wl["totals"], target_util)
        for base, rkey in _REC_BASES.items():
            val = rec[rkey]
            if val is None:
                val = wl["totals"].get(base)   # cold/no-peak -> keep current
            collected[base].append(val)

    result = {"stage": node["stage"], "namespace": node["namespace"]}
    exceeds = False
    for base in _REC_BASES:
        total = sum_usage(collected[base])     # skip None contributors
        quota = node["totals"].get(base)
        status = _quota_status(total, quota)
        result[base + "_rec_sum"] = total
        result[base + "_quota"] = quota
        result[base + "_status"] = status
        exceeds = exceeds or status == "EXCEEDS"
    result["quota_action"] = "INCREASE_QUOTA" if exceeds else "OK"
    return result


# ----------------------------------------------------------------- OOM merge

def _oom_key(o):
    return (o["namespace"], o["pod"], o["container"])


def merge_ooms(live, thanos):
    """Merge live (pod lastState) and Thanos (historical) OOM records, keyed by
    (namespace, pod, container). Live fields win on overlap; Thanos contributes
    oom_events. source is 'live', 'thanos', or 'both'. Sorted by key."""
    by_key = {}
    for o in live:
        by_key[_oom_key(o)] = {**o, "source": "live"}
    for o in thanos:
        k = _oom_key(o)
        if k in by_key:
            existing = by_key[k]
            existing["source"] = "both"
            if "oom_events" in o:
                existing["oom_events"] = o["oom_events"]
        else:
            by_key[k] = {**o, "source": "thanos"}
    return [by_key[k] for k in sorted(by_key)]


# --------------------------------------------------- namespace ResourceQuota

# Logical pair -> (ResourceQuota resource names, parser). A ResourceQuota counts
# requests.cpu / limits.cpu / requests.memory / limits.memory; bare `cpu` and
# `memory` are the legacy aliases for requests.cpu / requests.memory, so accept
# either spelling.
_QUOTA_RESOURCES = {
    "cpu_request": (("requests.cpu", "cpu"), parse_cpu),
    "cpu_limit":   (("limits.cpu",), parse_cpu),
    "mem_request": (("requests.memory", "memory"), parse_mem),
    "mem_limit":   (("limits.memory",), parse_mem),
}


def _quota_pick(qmap, keys, parse):
    """First present-and-parseable value among keys in a quota hard/used map."""
    for k in keys:
        if k in qmap:
            v = parse(qmap[k])
            if v is not None:
                return v
    return None


def parse_quota(items):
    """Reduce a namespace's ResourceQuota objects to one dict of configured Hard
    caps + booked Used amounts for cpu/mem requests & limits.

    Keys: cpu_request_hard / cpu_limit_hard / mem_request_hard / mem_limit_hard
    plus the matching *_used (None where the quota doesn't track that resource).
    Across multiple quotas the binding Hard is the smallest cap and Used the
    largest booked value (they agree in practice — Used is the namespace's
    consumption). Empty/None input -> all None.
    """
    hard = {f"{name}_hard": None for name in _QUOTA_RESOURCES}
    used = {f"{name}_used": None for name in _QUOTA_RESOURCES}
    for item in items or []:
        status = item.get("status", {}) or {}
        h = status.get("hard", {}) or {}
        u = status.get("used", {}) or {}
        for name, (keys, parse) in _QUOTA_RESOURCES.items():
            hv = _quota_pick(h, keys, parse)
            if hv is not None:
                cur = hard[f"{name}_hard"]
                hard[f"{name}_hard"] = hv if cur is None else min(cur, hv)
            uv = _quota_pick(u, keys, parse)
            if uv is not None:
                cur = used[f"{name}_used"]
                used[f"{name}_used"] = uv if cur is None else max(cur, uv)
    return {**hard, **used}


def apply_quota_to_totals(totals, quota):
    """Overlay a namespace's ResourceQuota onto its rollup totals in place: the
    configured request/limit columns become the quota Hard caps and the Used
    counters are filled. Peak-util% is recomputed against the new Hard limits;
    measured usage/peak/avg and the pod/container/oom counts are left as-is."""
    totals["cpu_request"] = quota["cpu_request_hard"]
    totals["cpu_limit"] = quota["cpu_limit_hard"]
    totals["mem_request"] = quota["mem_request_hard"]
    totals["mem_limit"] = quota["mem_limit_hard"]
    for f in QUOTA_USED_FIELDS:
        totals[f] = quota[f]
    totals["cpu_peak_util_pct"] = util_pct(totals.get("cpu_peak"),
                                           totals["cpu_limit"])
    totals["mem_peak_util_pct"] = util_pct(totals.get("mem_peak"),
                                           totals["mem_limit"])
    return totals


# ------------------------------------------------------- pod -> leaf records

def pods_to_leaves(pods, rs_index):
    """One leaf per container with configured requests/limits + workload labels.
    Usage fields (cpu_now/peak/avg, mem_now/peak) start as None and are filled
    by attach_usage(). oom_count starts at 0 and is bumped by attach_ooms()."""
    leaves = []
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        node = pod.get("spec", {}).get("nodeName", "-")
        wl_kind, wl_name = workload_for(pod, rs_index)
        for c in pod.get("spec", {}).get("containers", []) or []:
            res = c.get("resources", {}) or {}
            req = res.get("requests", {}) or {}
            lim = res.get("limits", {}) or {}
            leaves.append({
                "namespace": ns, "pod": name, "container": c["name"],
                "node": node, "workload_kind": wl_kind, "workload": wl_name,
                "cpu_request": parse_cpu(req.get("cpu")),
                "cpu_limit": parse_cpu(lim.get("cpu")),
                "mem_request": parse_mem(req.get("memory")),
                "mem_limit": parse_mem(lim.get("memory")),
                "cpu_now": None, "cpu_peak": None, "cpu_avg": None,
                "mem_now": None, "mem_peak": None,
                "oom_count": 0,
            })
    return leaves


def live_ooms_from_pods(pods):
    """Live OOM records from pod lastState.terminated.reason == OOMKilled."""
    out = []
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            last = (cs.get("lastState") or {}).get("terminated")
            if not last or last.get("reason") != "OOMKilled":
                continue
            out.append({
                "namespace": ns, "pod": name, "container": cs["name"],
                "restart_count": cs.get("restartCount", 0),
                "exit_code": last.get("exitCode"),
                "finished_at": last.get("finishedAt"),
            })
    return out


# ----------------------------------------------------- nested tree assembly

def _workload_key(leaf):
    return (leaf.get("workload_kind") or "", leaf.get("workload") or leaf["pod"])


def build_namespace_tree(namespace, leaves, ooms):
    """namespace -> workloads -> pods -> containers, with a rollup at each level.

    leaves are this namespace's container records (usage already attached).
    ooms is the merged OOM list for this namespace.
    """
    # Container leaves need their own peak-util% so the renderers show it at the
    # container level too (rollup() only computes util% for aggregate levels).
    for leaf in leaves:
        leaf["cpu_peak_util_pct"] = util_pct(leaf.get("cpu_peak"),
                                             leaf.get("cpu_limit"))
        leaf["mem_peak_util_pct"] = util_pct(leaf.get("mem_peak"),
                                             leaf.get("mem_limit"))

    workloads = []
    wl_groups = {}
    for leaf in leaves:
        wl_groups.setdefault(_workload_key(leaf), []).append(leaf)

    for (wl_kind, wl_name), wl_leaves in sorted(wl_groups.items()):
        pods = []
        pod_groups = {}
        for leaf in wl_leaves:
            pod_groups.setdefault(leaf["pod"], []).append(leaf)
        for pod_name, pod_leaves in sorted(pod_groups.items()):
            pods.append({
                "name": pod_name,
                "totals": rollup(pod_leaves),
                "containers": sorted(pod_leaves, key=lambda x: x["container"]),
            })
        workloads.append({
            "kind": wl_kind, "name": wl_name,
            "totals": rollup(wl_leaves), "pods": pods,
        })

    return {
        "namespace": namespace,
        "stage": detect_stage(namespace),
        "totals": rollup(leaves),
        "workloads": workloads,
        "ooms": ooms,
    }


def _template_totals(containers):
    """Totals dict for a workload's pod template (one replica's worth of
    configured requests/limits). Usage is unknown (None), pod_count is 0 — used
    for declared-but-idle workloads that have no running pods."""
    cpu_req, cpu_lim, mem_req, mem_lim = [], [], [], []
    for c in containers:
        res = c.get("resources", {}) or {}
        req = res.get("requests", {}) or {}
        lim = res.get("limits", {}) or {}
        cpu_req.append(parse_cpu(req.get("cpu")))
        cpu_lim.append(parse_cpu(lim.get("cpu")))
        mem_req.append(parse_mem(req.get("memory")))
        mem_lim.append(parse_mem(lim.get("memory")))
    return {
        "cpu_request": sum_limit(cpu_req), "cpu_limit": sum_limit(cpu_lim),
        "mem_request": sum_limit(mem_req), "mem_limit": sum_limit(mem_lim),
        "cpu_now": None, "cpu_peak": None, "cpu_avg": None,
        "mem_now": None, "mem_peak": None,
        "cpu_request_used": None, "cpu_limit_used": None,
        "mem_request_used": None, "mem_limit_used": None,
        "cpu_peak_util_pct": None, "mem_peak_util_pct": None,
        "oom_count": 0, "pod_count": 0, "container_count": len(containers),
    }


def idle_workload_entries(declared, present_keys):
    """Workload entries for declared workloads that have no running pods.

    declared is a list of (kind, workload_obj). Any (kind, name) already in
    present_keys (i.e. a workload with running pods, derived from pod owners) is
    skipped. Returns workload dicts shaped like build_namespace_tree's, with
    empty pods, template-derived totals, and idle=True. Sorted by (kind, name).
    """
    out = []
    for kind, obj in declared:
        name = obj.get("metadata", {}).get("name")
        if not name or (kind, name) in present_keys:
            continue
        containers = (obj.get("spec", {}).get("template", {})
                      .get("spec", {}).get("containers", []) or [])
        out.append({
            "kind": kind, "name": name, "idle": True,
            "totals": _template_totals(containers), "pods": [],
        })
    return sorted(out, key=lambda w: (w["kind"], w["name"]))


# ----------------------------------------------------------- thanos queries

def usage_queries(namespace, window, step):
    """PromQL for the five usage series, aggregated by (pod, container).

    cpu_* are cores (rate of the CPU seconds counter); mem_* are working-set
    bytes. peak/avg use *_over_time across the lookback window.
    """
    sel = f'namespace="{namespace}",container!=""'
    cpu_rate = f"rate(container_cpu_usage_seconds_total{{{sel}}}[5m])"
    wss = f"container_memory_working_set_bytes{{{sel}}}"
    by = "sum by (pod, container)"
    return {
        "cpu_now": f"{by} ({cpu_rate})",
        "cpu_peak": f"{by} (max_over_time({cpu_rate}[{window}:{step}]))",
        "cpu_avg": f"{by} (avg_over_time({cpu_rate}[{window}:{step}]))",
        "mem_now": f"{by} ({wss})",
        "mem_peak": f"{by} (max_over_time({wss}[{window}]))",
    }


def oom_queries(namespace, window):
    """PromQL for historical OOM signal, aggregated by (pod, container)."""
    sel = f'namespace="{namespace}",container!=""'
    return {
        "events": f'sum by (pod, container) '
                  f'(increase(container_oom_events_total{{{sel}}}[{window}]))',
        "terminated": f'max by (pod, container) '
                      f'(kube_pod_container_status_last_terminated_reason'
                      f'{{namespace="{namespace}",reason="OOMKilled"}})',
    }


def parse_vector_by_pod_container(payload):
    """Thanos instant-vector payload -> {(pod, container): float}. Series
    missing pod/container labels are skipped."""
    out = {}
    for s in payload.get("data", {}).get("result", []):
        metric = s.get("metric", {})
        pod = metric.get("pod")
        container = metric.get("container")
        if not pod or not container:
            continue
        try:
            out[(pod, container)] = float(s.get("value", [None, None])[1])
        except (TypeError, ValueError, IndexError):
            continue
    return out


# --------------------------------------------------- attach observed signals

def attach_usage(leaves, usage_maps):
    """Fill cpu_now/peak/avg + mem_now/peak on each leaf from the per-series
    maps produced by parse_vector_by_pod_container. Missing series stay None."""
    for leaf in leaves:
        key = (leaf["pod"], leaf["container"])
        for field, m in usage_maps.items():
            if key in m:
                leaf[field] = m[key]


def thanos_ooms(namespace, events_map):
    """Historical OOM records (oom_events > 0) from the increase() vector."""
    out = []
    for (pod, container), v in events_map.items():
        if v and v > 0:
            out.append({"namespace": namespace, "pod": pod,
                        "container": container, "oom_events": int(round(v))})
    return out


def attach_oom_counts(leaves, merged_ooms):
    """Set each leaf's oom_count to 1 if its (ns,pod,container) is in the merged
    OOM set, else 0 (used so rollups surface an oom_count per level)."""
    keys = {(o["namespace"], o["pod"], o["container"]) for o in merged_ooms}
    for leaf in leaves:
        leaf["oom_count"] = 1 if (
            leaf["namespace"], leaf["pod"], leaf["container"]) in keys else 0


# ------------------------------------------------------------- k8s backends

# logical -> (cli_plural, api_group_path, is_namespaced)
_RESOURCES = {
    "pods":         ("pods",         "/api/v1",        True),
    "resourcequotas": ("resourcequotas", "/api/v1",    True),
    "replicasets":  ("replicasets",  "/apis/apps/v1",  True),
    "deployments":  ("deployments",  "/apis/apps/v1",  True),
    "statefulsets": ("statefulsets", "/apis/apps/v1",  True),
    "daemonsets":   ("daemonsets",   "/apis/apps/v1",  True),
    "namespaces":   ("namespaces",   "/api/v1",        False),
    # OpenShift: listing projects returns what the caller can see and doesn't
    # need cluster-wide namespace list permission (matches
    # fetch-cluster-state-loop.sh). Only used by the oc CLI backend.
    "projects":     ("projects",     "/apis/project.openshift.io/v1", False),
}


class CliK8sClient:
    """Backend that shells out to oc/kubectl. `run(args)` returns stdout text;
    injectable for tests."""

    def __init__(self, binary="oc", run=None):
        self.binary = binary
        self._run = run or self._default_run

    def _default_run(self, args):
        proc = subprocess.run([self.binary, *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        if proc.returncode != 0:
            sys.stderr.write(f"command failed: {self.binary} {' '.join(args)}\n"
                             f"{proc.stderr}\n")
            return ""
        return proc.stdout

    def _list(self, resource, namespace):
        plural, _path, namespaced = _RESOURCES[resource]
        args = ["get", plural]
        if namespaced:
            args += (["-n", namespace] if namespace else ["-A"])
        args += ["-o", "json"]
        raw = self._run(args)
        if not raw.strip():
            return []
        try:
            return json.loads(raw).get("items", [])
        except json.JSONDecodeError:
            return []

    def list_namespaces(self):
        # On OpenShift use `oc get projects` (visible to the caller, no cluster
        # namespace-list RBAC needed); kubectl has no projects API.
        resource = "projects" if self.binary == "oc" else "namespaces"
        return self._list(resource, None)

    def list_pods(self, namespace=None):
        return self._list("pods", namespace)

    def list_resourcequotas(self, namespace=None):
        return self._list("resourcequotas", namespace)

    def list_replicasets(self, namespace=None):
        return self._list("replicasets", namespace)

    def list_deployments(self, namespace=None):
        return self._list("deployments", namespace)

    def list_statefulsets(self, namespace=None):
        return self._list("statefulsets", namespace)

    def list_daemonsets(self, namespace=None):
        return self._list("daemonsets", namespace)


class RestK8sClient:
    """In-cluster backend talking to the API server over HTTPS. `get_json(url)`
    is injectable for tests; the default uses urllib with the SA token + CA."""

    def __init__(self, host, token=None, ca_cert=None, get_json=None,
                 insecure=False):
        self.host = host.rstrip("/")
        self.token = token
        self.ctx = ssl.create_default_context(cafile=ca_cert) if ca_cert \
            else ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self._get = get_json or self._default_get

    def _default_get(self, url):
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
            return json.load(r)

    def _url(self, resource, namespace):
        plural, path, namespaced = _RESOURCES[resource]
        if namespaced and namespace:
            return f"{self.host}{path}/namespaces/{namespace}/{plural}"
        return f"{self.host}{path}/{plural}"

    def _list(self, resource, namespace):
        payload = self._get(self._url(resource, namespace))
        return payload.get("items", []) if isinstance(payload, dict) else []

    def list_namespaces(self):
        return self._list("namespaces", None)

    def list_pods(self, namespace=None):
        return self._list("pods", namespace)

    def list_resourcequotas(self, namespace=None):
        return self._list("resourcequotas", namespace)

    def list_replicasets(self, namespace=None):
        return self._list("replicasets", namespace)

    def list_deployments(self, namespace=None):
        return self._list("deployments", namespace)

    def list_statefulsets(self, namespace=None):
        return self._list("statefulsets", namespace)

    def list_daemonsets(self, namespace=None):
        return self._list("daemonsets", namespace)


# ----------------------------------------------------------- thanos client

# (namespace, service, port) — first match wins. Mirrors oom-usage.py.
QUERIER_CANDIDATES = [
    ("openshift-monitoring", "thanos-querier", "9091"),
    ("monitoring", "thanos-query", "9090"),
    ("monitoring", "thanos-query-frontend", "9090"),
    ("monitoring", "kps-kube-prometheus-stack-thanos-discovery", "10902"),
    ("monitoring", "kps-kube-prometheus-stack-prometheus", "9090"),
    ("monitoring", "kps-prometheus", "9090"),
    ("monitoring", "prometheus-operated", "9090"),
]


def discover_querier():
    for ns, name, port in QUERIER_CANDIDATES:
        try:
            rc = subprocess.run([CLI, "get", "svc", name, "-n", ns],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode
        except FileNotFoundError:
            # No oc/kubectl on PATH (e.g. the python:3.12-slim CronJob image).
            # Can't auto-discover -> caller degrades to no usage metrics.
            return None
        if rc == 0:
            return ns, name, port
    return None


def start_port_forward(svc, local_port, timeout=15):
    ns, name, remote_port = svc
    proc = subprocess.Popen(
        [CLI, "port-forward", f"svc/{name}", f"{local_port}:{remote_port}", "-n", ns],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    atexit.register(
        lambda: (proc.terminate(), proc.wait(timeout=5)) if proc.poll() is None else None
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = (proc.stdout.read() or "").strip() if proc.stdout else ""
            raise RuntimeError(f"{CLI} port-forward exited: {out or 'no output'}")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.3)
    # Timed out. Kill it, then drain whatever kubectl wrote so the user sees why.
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    out = (proc.stdout.read() or "").strip() if proc.stdout else ""
    raise RuntimeError(
        f"timed out waiting for port-forward to {ns}/{name}:{remote_port} "
        f"on local :{local_port}.\n--- {CLI} output ---\n{out or '(empty)'}"
    )


def auto_token():
    if CLI != "oc" and not shutil.which("oc"):
        return None
    try:
        out = subprocess.run(["oc", "whoami", "-t"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True).stdout.strip()
        return out or None
    except FileNotFoundError:
        return None


class Thanos:
    def __init__(self, base_url, *, token=None, insecure=False, timeout=30,
                 dedup=True, partial_response=False, max_source_resolution=None,
                 engine=None, store_matchers=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.dedup = dedup
        self.partial_response = partial_response
        self.max_source_resolution = max_source_resolution
        self.engine = engine
        self.store_matchers = list(store_matchers or [])
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _thanos_params(self):
        p = []
        # Thanos accepts these as repeated form fields. Omitting them lets
        # the server keep its defaults (which is also fine).
        p.append(("dedup", "true" if self.dedup else "false"))
        if self.partial_response:
            p.append(("partial_response", "true"))
        if self.max_source_resolution:
            p.append(("max_source_resolution", self.max_source_resolution))
        if self.engine:
            p.append(("engine", self.engine))
        for m in self.store_matchers:
            p.append(("storeMatch[]", m))
        return p

    def _get(self, path, params):
        qs = urllib.parse.urlencode(params, doseq=True)
        url = f"{self.base_url}{path}?{qs}"
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as r:
            payload = json.load(r)
        if payload.get("status") != "success":
            raise RuntimeError(
                f"API error: {payload.get('errorType')}: {payload.get('error')}"
            )
        return payload

    def probe(self):
        try:
            self._get("/api/v1/query", [("query", "1"), ("time", str(int(time.time())))])
            return True, None
        except Exception as e:
            return False, str(e)

    def query(self, expr, ts=None):
        params = [("query", expr)]
        if ts is not None:
            params.append(("time", f"{ts:.3f}"))
        params += self._thanos_params()
        return self._get("/api/v1/query", params)

    def query_range(self, expr, start, end, step):
        params = [
            ("query", expr),
            ("start", f"{start:.3f}"),
            ("end", f"{end:.3f}"),
            ("step", str(step)),
        ]
        params += self._thanos_params()
        return self._get("/api/v1/query_range", params)

    def labels(self, start=None, end=None):
        params = []
        if start is not None:
            params.append(("start", f"{start:.3f}"))
        if end is not None:
            params.append(("end", f"{end:.3f}"))
        return self._get("/api/v1/labels", params)

    def label_values(self, name, start=None, end=None):
        params = []
        if start is not None:
            params.append(("start", f"{start:.3f}"))
        if end is not None:
            params.append(("end", f"{end:.3f}"))
        return self._get(f"/api/v1/label/{urllib.parse.quote(name)}/values", params)

    def series(self, matches, start=None, end=None):
        params = [("match[]", m) for m in matches]
        if start is not None:
            params.append(("start", f"{start:.3f}"))
        if end is not None:
            params.append(("end", f"{end:.3f}"))
        return self._get("/api/v1/series", params)


# --------------------------------------------------------- env autodetection

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def choose_backend_kind(force_cli, force_rest, token_path=SA_TOKEN_PATH):
    """'rest' when in-cluster (SA token present + KUBERNETES_SERVICE_HOST set),
    else 'cli'. Explicit flags win."""
    if force_cli:
        return "cli"
    if force_rest:
        return "rest"
    if os.environ.get("KUBERNETES_SERVICE_HOST") and os.path.exists(token_path):
        return "rest"
    return "cli"


def pick_cli_binary(prefer_kubectl=False):
    """oc by default (falls back to kubectl); prefer_kubectl flips the order.
    Honors $OC_BIN / $KUBECTL_BIN."""
    if prefer_kubectl:
        return os.environ.get("KUBECTL_BIN") or (
            "kubectl" if shutil.which("kubectl") else "oc")
    return os.environ.get("OC_BIN") or (
        "oc" if shutil.which("oc") else "kubectl")


def read_sa_token(token_path=SA_TOKEN_PATH):
    try:
        with open(token_path) as f:
            return f.read().strip() or None
    except OSError:
        return None


# --------------------------------------------------------------- orchestrator

def _run_usage_queries(thanos, namespace, window, step):
    """Execute the five usage queries; return {field: {(pod,container): val}}."""
    qs = usage_queries(namespace, window, step)
    return {field: parse_vector_by_pod_container(thanos.query(expr))
            for field, expr in qs.items()}


def _run_oom_queries(thanos, namespace, window):
    qs = oom_queries(namespace, window)
    events = parse_vector_by_pod_container(thanos.query(qs["events"]))
    return events


def _declared_workloads(fcu_k8s, namespace):
    """(kind, obj) for every Deployment/StatefulSet/DaemonSet in the namespace.
    Tolerates clients that don't implement a lister (returns nothing for it)."""
    declared = []
    for kind, attr in (("Deployment", "list_deployments"),
                       ("StatefulSet", "list_statefulsets"),
                       ("DaemonSet", "list_daemonsets")):
        lister = getattr(fcu_k8s, attr, None)
        if lister is None:
            continue
        for obj in lister(namespace) or []:
            declared.append((kind, obj))
    return declared


def collect_namespace(fcu_k8s, namespace, thanos, window, step,
                      include_idle=True):
    """Full per-namespace pipeline -> nested namespace tree dict.

    When include_idle is set, declared Deployments/StatefulSets/DaemonSets with
    no running pods are appended as idle workload rows (configured template
    limits, zero usage) so the report lists them too. They do not affect the
    namespace totals (no running pods reserve nothing)."""
    pods = fcu_k8s.list_pods(namespace)
    rs = fcu_k8s.list_replicasets(namespace)
    rs_index = {(r["metadata"]["namespace"], r["metadata"]["name"]): r
                for r in rs}
    leaves = pods_to_leaves(pods, rs_index)
    live = live_ooms_from_pods(pods)

    thanos_oom = []
    if thanos is not None and getattr(thanos, "available", True):
        usage_maps = _run_usage_queries(thanos, namespace, window, step)
        attach_usage(leaves, usage_maps)
        events = _run_oom_queries(thanos, namespace, window)
        thanos_oom = thanos_ooms(namespace, events)

    merged = merge_ooms(live, thanos_oom)
    attach_oom_counts(leaves, merged)
    node = build_namespace_tree(namespace, leaves, merged)

    # Namespace request/limit columns come from the ResourceQuota Hard caps (and
    # the new *_used columns from its Used counters) rather than the summed pod
    # specs. Only when a quota exists; otherwise the totals keep the pod-spec
    # rollup so namespaces without a quota still show their configured totals.
    lister = getattr(fcu_k8s, "list_resourcequotas", None)
    quotas = lister(namespace) if lister else []
    if quotas:
        apply_quota_to_totals(node["totals"], parse_quota(quotas))

    if include_idle:
        present = {(w["kind"], w["name"]) for w in node["workloads"]}
        idle = idle_workload_entries(_declared_workloads(fcu_k8s, namespace),
                                     present)
        if idle:
            node["workloads"] = sorted(node["workloads"] + idle,
                                       key=lambda w: (w["kind"], w["name"]))
    return node


# ------------------------------------------------------------- flat rows / csv

# (csv header, source key). Metric headers carry their unit so the CSV is
# self-describing — CPU in cores, memory in bytes, util as a percent. The source
# keys are the internal field names used everywhere else (rollup, JSON, leaves).
CSV_FIELDS = [
    ("level", "level"), ("stage", "stage"), ("namespace", "namespace"),
    ("workload_kind", "workload_kind"), ("workload", "workload"),
    ("pod", "pod"), ("container", "container"),
    ("cpu_request_cores", "cpu_request"), ("cpu_limit_cores", "cpu_limit"),
    ("cpu_now_cores", "cpu_now"),
    ("cpu_request_used_cores", "cpu_request_used"),
    ("cpu_limit_used_cores", "cpu_limit_used"),
    ("cpu_peak_cores", "cpu_peak"),
    ("cpu_avg_cores", "cpu_avg"), ("cpu_peak_util_pct", "cpu_peak_util_pct"),
    ("mem_request_bytes", "mem_request"), ("mem_limit_bytes", "mem_limit"),
    ("mem_now_bytes", "mem_now"),
    ("mem_request_used_bytes", "mem_request_used"),
    ("mem_limit_used_bytes", "mem_limit_used"),
    ("mem_peak_bytes", "mem_peak"),
    ("mem_peak_util_pct", "mem_peak_util_pct"),
    ("oom_count", "oom_count"), ("pod_count", "pod_count"),
    ("container_count", "container_count"),
]
CSV_COLUMNS = [header for header, _ in CSV_FIELDS]


def _row_from_totals(level, stage, namespace, totals, **ids):
    """Build a CSV row keyed by the (unit-bearing) headers in CSV_FIELDS, drawing
    identity from level/stage/namespace/ids and metrics from the totals dict."""
    identity = {"level": level, "stage": stage, "namespace": namespace}
    identity.update(ids)
    row = {}
    for header, key in CSV_FIELDS:
        if key in identity:
            row[header] = identity[key]
        else:
            row[header] = totals.get(key, "")
    return row


# Metric (non-identity) columns, reused for the concise per-namespace summary.
_IDENTITY_KEYS = {"level", "stage", "namespace", "workload_kind", "workload",
                  "pod", "container"}
METRIC_FIELDS = [(h, k) for h, k in CSV_FIELDS if k not in _IDENTITY_KEYS]
NS_CSV_FIELDS = [("stage", "stage"), ("namespace", "namespace")] + METRIC_FIELDS
NS_CSV_COLUMNS = [h for h, _ in NS_CSV_FIELDS]

# Human-readable twin of resources.csv: same rows/columns, but each metric is
# rendered with its unit inline (200m, 6.3Mi, 4.9%) instead of a raw float. The
# unit-bearing header suffix (_cores/_bytes) is dropped because the value now
# carries the unit itself; counts and identity columns pass through unchanged.
_CPU_METRIC_KEYS = {"cpu_request", "cpu_limit", "cpu_now", "cpu_peak", "cpu_avg",
                    "cpu_request_used", "cpu_limit_used"}
_MEM_METRIC_KEYS = {"mem_request", "mem_limit", "mem_now", "mem_peak",
                    "mem_request_used", "mem_limit_used"}
_PCT_METRIC_KEYS = {"cpu_peak_util_pct", "mem_peak_util_pct"}
_HEADER_TO_KEY = dict(CSV_FIELDS)


def _human_header(header):
    """Drop the raw-unit suffix from a CSV header ('cpu_peak_cores' -> 'cpu_peak')."""
    for suf in ("_cores", "_bytes"):
        if header.endswith(suf):
            return header[: -len(suf)]
    return header


HUMAN_CSV_COLUMNS = [_human_header(h) for h, _ in CSV_FIELDS]
NS_HUMAN_CSV_COLUMNS = [_human_header(h) for h, _ in NS_CSV_FIELDS]


def _human_metric(key, value):
    """Format one raw cell for the human CSV, chosen by its source key."""
    v = None if value == "" else value
    if key in _CPU_METRIC_KEYS:
        return fmt_cores(v)
    if key in _MEM_METRIC_KEYS:
        return fmt_bytes(v)
    if key in _PCT_METRIC_KEYS:
        return fmt_pct(v)
    return "" if v is None else v  # identity strings + counts pass through


def aggregate_totals(totals_list):
    """Combine a list of level-`totals` dicts into one. Same None-semantics as
    rollup(): requests/limits are None if any contributor is unset; usage is
    None only if all are missing. Counts (oom/pod/container) are plain sums."""
    agg = {}
    for f in LIMIT_FIELDS:
        agg[f] = sum_limit([t.get(f) for t in totals_list])
    for f in USAGE_FIELDS:
        agg[f] = sum_usage([t.get(f) for t in totals_list])
    # Roll up each namespace's quota "Used" counters (sum; None only if every
    # contributing namespace lacks a quota).
    for f in QUOTA_USED_FIELDS:
        agg[f] = sum_usage([t.get(f) for t in totals_list])
    agg["cpu_peak_util_pct"] = util_pct(agg["cpu_peak"], agg["cpu_limit"])
    agg["mem_peak_util_pct"] = util_pct(agg["mem_peak"], agg["mem_limit"])
    for f in ("oom_count", "pod_count", "container_count"):
        agg[f] = sum(t.get(f, 0) for t in totals_list)
    return agg


def group_by_stage(trees):
    """{stage: [namespace node, ...]} for the given namespace trees."""
    groups = {}
    for node in trees:
        groups.setdefault(node["stage"], []).append(node)
    return groups


def stage_summaries(trees):
    """[(stage, totals), ...] sorted by stage — each stage's namespaces summed."""
    groups = group_by_stage(trees)
    return [(stage, aggregate_totals([n["totals"] for n in groups[stage]]))
            for stage in sorted(groups)]


def cluster_summary(trees):
    """Grand total across every namespace in the report."""
    return aggregate_totals([n["totals"] for n in trees])


def summary_rows(trees, kinds=("cluster", "stage")):
    """Rollup rows (level=cluster and/or level=stage) for the flat CSV."""
    rows = []
    if "cluster" in kinds:
        rows.append(_row_from_totals("cluster", "", "", cluster_summary(trees)))
    if "stage" in kinds:
        for stage, totals in stage_summaries(trees):
            rows.append(_row_from_totals("stage", stage, "", totals))
    return rows


def flatten_rows(trees):
    """One row per level (namespace/workload/pod/container) across all trees."""
    rows = []
    for node in trees:
        ns, stage = node["namespace"], node["stage"]
        rows.append(_row_from_totals("namespace", stage, ns, node["totals"]))
        for wl in node["workloads"]:
            rows.append(_row_from_totals(
                "workload", stage, ns, wl["totals"],
                workload_kind=wl["kind"], workload=wl["name"]))
            for pod in wl["pods"]:
                rows.append(_row_from_totals(
                    "pod", stage, ns, pod["totals"],
                    workload_kind=wl["kind"], workload=wl["name"],
                    pod=pod["name"]))
                for c in pod["containers"]:
                    rows.append(_row_from_totals(
                        "container", stage, ns, c,
                        workload_kind=wl["kind"], workload=wl["name"],
                        pod=pod["name"], container=c["container"]))
    return rows


def render_resources_csv(trees, stream, summary_kinds=("cluster", "stage")):
    """Write the flat per-level CSV. summary_kinds controls which rollup rows
    are prepended (cluster grand total and/or per-stage totals)."""
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in summary_rows(trees, summary_kinds) + flatten_rows(trees):
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


def render_resources_human_csv(trees, stream, summary_kinds=("cluster", "stage")):
    """Human-readable twin of render_resources_csv: identical rows, but every
    metric is formatted with its unit inline (200m, 6.3Mi, 4.9%). Same
    summary_kinds semantics."""
    writer = csv.DictWriter(stream, fieldnames=HUMAN_CSV_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for row in summary_rows(trees, summary_kinds) + flatten_rows(trees):
        writer.writerow({_human_header(h): _human_metric(_HEADER_TO_KEY[h], v)
                         for h, v in row.items()})


def render_namespaces_csv(trees, stream):
    """Concise one-row-per-namespace summary: total configured vs. real CPU/mem
    (no workload/pod/container detail). Sorted by stage then namespace."""
    writer = csv.DictWriter(stream, fieldnames=NS_CSV_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        row = {"stage": node["stage"], "namespace": node["namespace"]}
        for header, key in METRIC_FIELDS:
            v = node["totals"].get(key)
            row[header] = "" if v is None else v
        writer.writerow(row)


def render_namespaces_human_csv(trees, stream):
    """Human-readable twin of render_namespaces_csv: one row per namespace, each
    metric formatted with its unit inline (200m, 6.3Mi, 4.9%)."""
    writer = csv.DictWriter(stream, fieldnames=NS_HUMAN_CSV_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        row = {"stage": node["stage"], "namespace": node["namespace"]}
        for header, key in METRIC_FIELDS:
            row[_human_header(header)] = _human_metric(key, node["totals"].get(key))
        writer.writerow(row)


def render_ooms_csv(trees, stream):
    cols = ["stage", "namespace", "pod", "container", "source", "oom_events",
            "restart_count", "exit_code", "finished_at"]
    writer = csv.DictWriter(stream, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for node in trees:
        for o in node["ooms"]:
            writer.writerow({"stage": node["stage"], **o})


def render_json(trees, stream, window, cluster, summaries=True):
    obj = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "cluster": cluster,
        "window": window,
        "namespaces": trees,
    }
    if summaries:
        obj["cluster_totals"] = cluster_summary(trees)
        obj["stage_summaries"] = [{"stage": s, "totals": t}
                                  for s, t in stage_summaries(trees)]
    json.dump(obj, stream, indent=2)
    stream.write("\n")


def render_stdout_formats(trees, formats, stream, window, cluster, levels):
    """Write each requested --format to a single stream (stdout). Lets every
    on-disk view also be produced without --output-dir, e.g. piped:
    `... --format resources-human | column -s, -t`. 'none' suppresses stdout
    entirely (pair with --output-dir to only write files)."""
    if "none" in formats:
        return
    if "text" in formats:
        render_text(trees, stream, levels=levels)
    if "json" in formats:
        render_json(trees, stream, window=window, cluster=cluster)
    if "csv" in formats:
        render_resources_csv(trees, stream)
    if "resources-human" in formats:
        render_resources_human_csv(trees, stream)
    if "namespaces-human" in formats:
        render_namespaces_human_csv(trees, stream)


# ----------------------------------------------------- persisting to disk/PVC

REPORT_DATE_FMT = "%Y-%m-%d"
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def dated_output_dir(base, now):
    """`<base>/<YYYY-MM-DD>` for `now` — one folder per daily run so a CronJob
    keeps history instead of overwriting yesterday's report."""
    return os.path.join(base, now.strftime(REPORT_DATE_FMT))


LEGEND_TEXT = """# Cluster-Nutzungsbericht — Legende

Erzeugt von scripts/python/fetch-cluster-usage.py.

## Dateien in diesem Ordner
- `resources.csv`  — CPU-/Speicher-Konfiguration vs. tatsächliche Nutzung, eine Zeile pro Ebene (siehe `level`).
- `resources-human.csv` — dieselben Zeilen wie `resources.csv`, aber jeder Wert mit Einheit (z. B. `200m`, `6.3Mi`, `4.9%`) statt Rohzahl; CPU-Mikrowerte als `µ` (z. B. `0.525µ`).
- `namespaces.csv` — kompakte Übersicht: genau eine Zeile pro Namespace (Summe: verbraucht vs. limitiert), ohne Workload-/Pod-/Container-Details.
- `namespaces-human.csv` — dieselben Zeilen wie `namespaces.csv`, aber jeder Wert mit Einheit (z. B. `200m`, `6.3Mi`, `4.9%`) statt Rohzahl.
- `ooms.csv`       — eine Zeile pro OOM-getötetem Container.
- `summary.txt`    — menschenlesbare Tabelle (CPU in Cores/Milli, Speicher in Ki/Mi/Gi, % und OOM-Anzahl) — dieselben Zahlen wie die CSVs, nur kompakt formatiert.
- `report.json`   — dieselben Daten verschachtelt (Namespace → Workload → Pod → Container) plus Aggregationen.
- `by-stage/<stage>/` — (im obersten Ordner) dieselben Dateien, beschränkt auf eine Stage.

## resources.csv — die Spalte `level` sagt, was jede Zeile aggregiert
- `cluster`   — Gesamtsumme über alle Namespaces im Bericht.
- `stage`     — Summe über alle Namespaces einer Stage (ref/test/prod/phase/pnext/other);
                `stage` ist gesetzt, `namespace` ist leer.
- `namespace` — Summe für einen Namespace.
- `workload`  — ein Deployment / StatefulSet / DaemonSet (Pods gruppiert über ownerReferences).
- `pod`       — ein Pod (Summe seiner Container).
- `container` — ein Container (die feinste Ebene).

Die Aggregationszeilen (`cluster`, `stage`) stehen zuerst, danach die Detailzeilen pro Namespace.
Eine `workload`-Zeile mit `pod_count` = 0 ist *deklariert, aber inaktiv* (z. B. ein auf 0
skaliertes StatefulSet): die Limits stammen aus dem Pod-Template, die Nutzungsspalten sind leer.

## resources.csv Spalten
Die Einheit steht im Spaltennamen: `_cores` = CPU in **Cores**, `_bytes` =
Speicher in **Bytes**, `_pct` = Prozent. Beispiel: `cpu_now_cores=24.5` sind
24,5 CPU-Cores; `mem_now_bytes=23092903` sind 23.092.903 Bytes (≈ 22 MiB).
- `level`, `stage`, `namespace`, `workload_kind`, `workload`, `pod`, `container`
    — Identität der Zeile (leer, wo für die Ebene nicht zutreffend).
- `cpu_request_cores` / `cpu_limit_cores` / `mem_request_bytes` / `mem_limit_bytes`
    — konfigurierte Requests/Limits. Auf Ebene `namespace` (und summiert in `stage`/`cluster`)
      stammen diese aus der **ResourceQuota** des Namespace (Spalte *Hard* von
      `requests.cpu`/`limits.cpu`/`requests.memory`/`limits.memory`); auf Ebene
      `workload`/`pod`/`container` aus den Pod-Specs. Leer = keine Quota bzw. irgendwo
      nicht gesetzt/unbegrenzt. (Hat ein Namespace keine Quota, bleibt die Summe der Pod-Specs.)
- `cpu_now_cores` / `mem_now_bytes`   — Nutzung zum Berichtszeitpunkt (CPU = Rate über 5m; Speicher = Working Set).
- `cpu_request_used_cores` / `cpu_limit_used_cores` / `mem_request_used_bytes` / `mem_limit_used_bytes`
    — gebuchte Nutzung laut ResourceQuota (Spalte *Used*: was Requests/Limits der laufenden
      Pods vom Hard-Kontingent belegen). Nur auf Ebene `namespace` gesetzt (in `stage`/`cluster`
      summiert); auf `workload`/`pod`/`container` leer, da Quotas namespace-weit gelten.
- `cpu_peak_cores` / `mem_peak_bytes` — Spitzenwert über das Rückblickfenster (--window).
- `cpu_avg_cores`                     — durchschnittliche CPU über das Fenster.
- `cpu_peak_util_pct` / `mem_peak_util_pct` — Spitze ÷ Limit, in Prozent (leer, wenn kein Limit).
- `oom_count`             — Anzahl OOM-getöteter Container im Bereich.
- `pod_count` / `container_count` — wie viele Pods/Container die Zeile aggregiert.
Leere Nutzungszellen bedeuten, dass Thanos keine Daten hatte (oder --no-thanos / Thanos nicht erreichbar).

Hinweis: `report.json` verwendet die kurzen Schlüssel (`cpu_limit`, `mem_now`, …);
die Einheiten sind dort dieselben (CPU in Cores, Speicher in Bytes).

## Einheiten in `resources-human.csv`, `namespaces-human.csv` und `summary.txt`
Hier steht die Einheit **direkt am Wert** (statt im Spaltennamen), damit kleine
Zahlen lesbar bleiben und nicht als `0.000` oder in wissenschaftlicher Notation
(`5.25e-07`) erscheinen. Außerdem trägt **jeder** CPU-Wert eine Einheit (`c`/`m`/`µ`),
damit Excel (deutsche Locale) z. B. `18.5` nicht als Datum (18. Mai) oder als
tausender-gruppierte Zahl interpretiert — `18.5c` bleibt Text. Für Excel daher
bevorzugt die `*-human.csv` öffnen (die Roh-`resources.csv` enthält bewusst nackte
Zahlen für die maschinelle Weiterverarbeitung).

### CPU — Cores mit metrischem Präfix
Das Präfix verschiebt das Komma; die Buchstaben unterscheiden sich um den Faktor
1000:
- `c` = **Cores**, z. B. `24.5c` = 24,5 Cores; `1c` = 1 Core.
- `m` = **Milli** = Tausendstel Core (×10⁻³). Beispiel: `200m` = 0,2 Cores; `1m` = 0,001 Cores.
- `µ` = **Mikro** = Millionstel Core (×10⁻⁶). Beispiel: `0.525µ` = 0,000000525 Cores; `148µ` = 0,000148 Cores.

Umrechnung: `1c` = `1000m` = `1 000 000µ`, also **`1m` = `1000µ`**.
- Cores → Milli: ×1000   (0,2 → `200m`)
- Cores → Mikro: ×1 000 000   (0,000148 → `148µ`)

Achtung Groß-/Kleinschreibung: `m` (klein) = Milli; `M` (groß) wäre Mega (×10⁶).
`µ` ist das SI-Zeichen für Mikro. (Kubernetes selbst schreibt Mikro als `u`; diese
menschenlesbaren Dateien verwenden bewusst das Zeichen `µ`.) Das `c` steht hier für
**Cores** (die Basiseinheit), nicht für das SI-Zenti — Zenti-Cores kommen im Bericht
nicht vor.

Faustregel im Bericht: konfigurierte Limits/Requests liegen meist im Milli-Bereich
(`100m`, `500m`), die gemessenen Spitzen auf einem fast leeren Cluster im
Mikro-Bereich (`76.8µ`).

### Speicher — binäre Präfixe (Faktor 1024)
`Ki`/`Mi`/`Gi`/`Ti` (Kibi/Mebi/Gibi/Tebi), je Stufe ×1024:
- `1Ki` = 1024 Bytes; `1Mi` = 1024 Ki = 1 048 576 Bytes; `1Gi` = 1024 Mi.
- Beispiel: `6.3Mi` ≈ 6 606 029 Bytes; `344.0Ki` = 352 256 Bytes.

### Prozent
`_pct`-Werte (z. B. `4.9%`) sind Spitze ÷ Limit × 100 — unverändert gegenüber den
Roh-CSVs.

## ooms.csv Spalten
- `stage`, `namespace`, `pod`, `container` — welcher Container OOM erlitten hat.
- `source`        — `live` (aktueller Pod-Zustand), `thanos` (historisch) oder `both`.
- `oom_events`    — Anzahl der OOM-Kills über das Fenster (aus Thanos).
- `restart_count`, `exit_code` (137 = OOMKilled), `finished_at` — aus dem Live-Pod-Zustand.
"""


def write_legend(out_dir):
    """Write the German LEGEND.md (column/row descriptions) into out_dir, as
    UTF-8 (it contains non-ASCII glyphs and umlauts, so the encoding is pinned
    to survive a LANG=C container). Returns its path."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "LEGEND.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(LEGEND_TEXT)
    return path


def write_report_files(trees, out_dir, window, cluster,
                       summary_kinds=("cluster", "stage")):
    """Write resources.csv, ooms.csv, report.json and LEGEND.md into out_dir
    (created if needed). summary_kinds selects which rollup rows the CSV/JSON
    carry. Returns out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    # Explicit UTF-8 everywhere: the *-human.csv / summary.txt carry the µ sign
    # (and summary.txt an em dash), which would raise UnicodeEncodeError under a
    # C/POSIX-locale CronJob if left to the platform default encoding.
    with open(os.path.join(out_dir, "resources.csv"), "w", encoding="utf-8") as f:
        render_resources_csv(trees, f, summary_kinds=summary_kinds)
    with open(os.path.join(out_dir, "resources-human.csv"), "w",
              encoding="utf-8") as f:
        render_resources_human_csv(trees, f, summary_kinds=summary_kinds)
    with open(os.path.join(out_dir, "namespaces.csv"), "w", encoding="utf-8") as f:
        render_namespaces_csv(trees, f)
    with open(os.path.join(out_dir, "namespaces-human.csv"), "w",
              encoding="utf-8") as f:
        render_namespaces_human_csv(trees, f)
    with open(os.path.join(out_dir, "ooms.csv"), "w", encoding="utf-8") as f:
        render_ooms_csv(trees, f)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        render_json(trees, f, window=window, cluster=cluster,
                    summaries=bool(summary_kinds))
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Cluster usage — window {window}, cluster {cluster}\n")
        render_text(trees, f)
    write_legend(out_dir)
    return out_dir


def write_all_reports(trees, out_dir, window, cluster):
    """Combined report (cluster + per-stage rollups) at out_dir, plus a
    self-contained per-stage report under out_dir/by-stage/<stage>/."""
    write_report_files(trees, out_dir, window, cluster,
                       summary_kinds=("cluster", "stage"))
    for stage, stage_nodes in sorted(group_by_stage(trees).items()):
        write_report_files(stage_nodes,
                           os.path.join(out_dir, "by-stage", stage),
                           window, cluster, summary_kinds=("stage",))
    return out_dir


def prune_old_reports(base, retention_days, now):
    """Remove `<base>/<YYYY-MM-DD>` folders older than retention_days. Only
    touches date-named directories (never other files in base). retention_days
    <= 0 is a no-op. Returns the list of removed folder names."""
    if retention_days <= 0:
        return []
    cutoff = (now - timedelta(days=retention_days)).date()
    removed = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    for name in entries:
        if not _DATE_DIR_RE.match(name):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        try:
            folder_date = datetime.strptime(name, REPORT_DATE_FMT).date()
        except ValueError:
            continue
        if folder_date < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    return removed


# --------------------------------------------------------------- text render

def _print_table(headers, rows, stream, indent="  "):
    if not rows:
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = indent + "  ".join(f"{{:<{w}}}" for w in widths)
    stream.write(fmt.format(*headers) + "\n")
    for r in rows:
        stream.write(fmt.format(*[str(c) for c in r]) + "\n")


def _usage_cells(t):
    """Common CPU/mem columns for a totals dict."""
    return [
        fmt_cores(t.get("cpu_request")), fmt_cores(t.get("cpu_limit")),
        fmt_cores(t.get("cpu_now")),
        fmt_cores(t.get("cpu_request_used")), fmt_cores(t.get("cpu_limit_used")),
        fmt_cores(t.get("cpu_peak")),
        fmt_pct(t.get("cpu_peak_util_pct")),
        fmt_bytes(t.get("mem_request")), fmt_bytes(t.get("mem_limit")),
        fmt_bytes(t.get("mem_now")),
        fmt_bytes(t.get("mem_request_used")), fmt_bytes(t.get("mem_limit_used")),
        fmt_bytes(t.get("mem_peak")),
        fmt_pct(t.get("mem_peak_util_pct")),
        t.get("oom_count", 0),
    ]


_USAGE_HEADERS = ["CPU req", "CPU lim", "CPU now",
                  "CPU req-used", "CPU lim-used", "CPU peak", "CPU %",
                  "MEM req", "MEM lim", "MEM now",
                  "MEM req-used", "MEM lim-used", "MEM peak", "MEM %", "OOM"]


def render_text(trees, stream, levels=("namespace", "workload", "pod",
                                       "container"), summary=True):
    if summary and trees:
        stream.write("\n" + "=" * 100 + "\n")
        stream.write("SUMMARY — cluster total + per-stage rollups\n")
        stream.write("=" * 100 + "\n")
        rows = [["CLUSTER", *_usage_cells(cluster_summary(trees))]]
        for stage, totals in stage_summaries(trees):
            rows.append([f"stage/{stage}", *_usage_cells(totals)])
        _print_table(["SCOPE", *_USAGE_HEADERS], rows, stream)

        stream.write("\nBY NAMESPACE — total used vs. limited per namespace\n")
        ns_rows = [[f"{n['stage']}/{n['namespace']}", *_usage_cells(n["totals"])]
                   for n in sorted(trees, key=lambda n: (n["stage"],
                                                         n["namespace"]))]
        _print_table(["NAMESPACE", *_USAGE_HEADERS], ns_rows, stream)

    for node in trees:
        stream.write("\n" + "=" * 100 + "\n")
        stream.write(f"NAMESPACE  {node['namespace']}   [stage={node['stage']}]"
                     f"   pods={node['totals']['pod_count']} "
                     f"containers={node['totals']['container_count']} "
                     f"ooms={node['totals']['oom_count']}\n")
        stream.write("=" * 100 + "\n")

        if "namespace" in levels:
            _print_table(["", *_USAGE_HEADERS],
                         [["TOTAL", *_usage_cells(node["totals"])]], stream)

        if "workload" in levels:
            stream.write("\nWORKLOADS\n")
            rows = [[f"{wl['kind']}/{wl['name']}"
                     + ("  (idle: 0 pods)" if wl.get("idle") else ""),
                     *_usage_cells(wl["totals"])]
                    for wl in node["workloads"]]
            _print_table(["WORKLOAD", *_USAGE_HEADERS], rows, stream)

        if "pod" in levels:
            stream.write("\nPODS\n")
            rows = []
            for wl in node["workloads"]:
                for pod in wl["pods"]:
                    rows.append([pod["name"], *_usage_cells(pod["totals"])])
            _print_table(["POD", *_USAGE_HEADERS], rows, stream)

        if "container" in levels:
            stream.write("\nCONTAINERS\n")
            rows = []
            for wl in node["workloads"]:
                for pod in wl["pods"]:
                    for c in pod["containers"]:
                        rows.append([f"{pod['name']}/{c['container']}",
                                     *_usage_cells(c)])
            _print_table(["POD/CONTAINER", *_USAGE_HEADERS], rows, stream)

        if node["ooms"]:
            stream.write("\nOOM-KILLED\n")
            rows = [[o["pod"], o["container"], o.get("source", "-"),
                     o.get("oom_events", "-"), o.get("restart_count", "-"),
                     o.get("finished_at", "-")] for o in node["ooms"]]
            _print_table(["POD", "CONTAINER", "SRC", "EVENTS", "RESTARTS",
                          "LAST OOM"], rows, stream)


# ----------------------------------------------------------------------- CLI

# Binary used by the copied Thanos helpers (discover_querier / port-forward /
# auto_token). main() overrides this from --kubectl / autodetect.
CLI = "oc"


def build_parser():
    p = argparse.ArgumentParser(
        description="Cluster usage report: configured limits/requests vs real "
                    "Thanos usage per namespace/workload/pod/container, plus "
                    "OOM-killed list (live + Thanos).",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    scope = p.add_argument_group("scope")
    scope.add_argument("--pattern", default="^pid-",
                       help="Namespace name filter (default: ^pid-).")
    scope.add_argument("--namespace", action="append",
                       help="Restrict to this namespace (repeatable).")
    scope.add_argument("--all-namespaces", action="store_true",
                       help="Ignore --pattern; scan every namespace.")

    be = p.add_argument_group("backend")
    g = be.add_mutually_exclusive_group()
    g.add_argument("--in-cluster", action="store_true",
                   help="Force the in-cluster REST backend.")
    g.add_argument("--cli", action="store_true",
                   help="Force the oc/kubectl CLI backend.")
    be.add_argument("--kubectl", action="store_true",
                    help="Prefer kubectl over oc for the CLI backend.")

    th = p.add_argument_group("thanos")
    th.add_argument("--thanos-url", help="Thanos Querier base URL.")
    th.add_argument("--token", help="Bearer token for Thanos.")
    th.add_argument("--token-file", help="Read bearer token from file.")
    th.add_argument("--insecure", action="store_true",
                    help="Skip TLS verification for Thanos.")
    th.add_argument("--no-thanos", action="store_true",
                    help="Skip usage queries; configured + live OOM only.")
    th.add_argument("--window", default="24h",
                    help="Lookback window for peak/avg (default: 24h).")
    th.add_argument("--step", default="5m",
                    help="Sub-query step for *_over_time (default: 5m).")
    th.add_argument("--local-port", type=int, default=19090,
                    help="Local port for the port-forward fallback.")

    out = p.add_argument_group("output")
    out.add_argument("--level", default="namespace,workload,pod,container",
                     help="Comma list of text levels (default: all).")
    out.add_argument("--format", action="append",
                     choices=["text", "json", "csv", "resources-human",
                              "namespaces-human", "none"],
                     help="stdout format(s), repeatable (default: text). "
                          "'csv' = raw resources.csv; 'resources-human' / "
                          "'namespaces-human' = the unit-formatted CSVs; "
                          "'none' = no stdout (pair with --output-dir to only "
                          "write files).")
    out.add_argument("--output-dir",
                     help="Also write resources.csv, ooms.csv, report.json.")
    out.add_argument("--date-subdir", action="store_true",
                     help="Write into <output-dir>/<YYYY-MM-DD>/ so a daily run "
                          "keeps history instead of overwriting.")
    out.add_argument("--retention-days", type=int, default=0,
                     help="With --date-subdir, prune dated folders older than N "
                          "days (0 = keep everything).")
    out.add_argument("--no-idle-workloads", action="store_true",
                     help="Don't list declared Deployments/StatefulSets/"
                          "DaemonSets that have no running pods.")
    return p


def select_namespaces(k8s, pattern, explicit, all_namespaces):
    """Resolve the namespace list. explicit wins; else list + filter."""
    if explicit:
        return list(explicit)
    names = [n["metadata"]["name"] for n in k8s.list_namespaces()]
    if all_namespaces:
        return names
    rx = re.compile(pattern)
    return [n for n in names if rx.search(n)]


def _make_k8s(args):
    kind = choose_backend_kind(force_cli=args.cli, force_rest=args.in_cluster)
    if kind == "rest":
        api_host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if not api_host:
            sys.stderr.write(
                "error: --in-cluster requires KUBERNETES_SERVICE_HOST to be set "
                "(only available inside a pod). Drop --in-cluster to use oc/kubectl.\n")
            sys.exit(2)
        host = (f"https://{api_host}:"
                f"{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}")
        return RestK8sClient(host, token=read_sa_token(),
                             ca_cert=SA_CA_PATH if os.path.exists(SA_CA_PATH)
                             else None, insecure=args.insecure)
    return CliK8sClient(binary=pick_cli_binary(prefer_kubectl=args.kubectl))


def _resolve_token(args):
    if args.token:
        return args.token
    if args.token_file:
        with open(args.token_file) as f:
            return f.read().strip()
    return read_sa_token() or auto_token()


def _make_thanos(args):
    """Returns a Thanos client or None (when --no-thanos or unreachable)."""
    if args.no_thanos:
        return None
    base = args.thanos_url or os.environ.get("THANOS_URL")
    if not base:
        svc = discover_querier()
        if not svc:
            sys.stderr.write("warn: no Thanos URL and no known Querier Service; "
                             "continuing without usage metrics.\n")
            return None
        ns, name, port = svc
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            base = f"http://{name}.{ns}:{port}"
        else:
            sys.stderr.write(f"info: port-forwarding {ns}/{name}:{port}\n")
            start_port_forward(svc, args.local_port)
            base = f"http://127.0.0.1:{args.local_port}"
    client = Thanos(base, token=_resolve_token(args), insecure=args.insecure)
    ok, err = client.probe()
    if not ok:
        sys.stderr.write(f"warn: Thanos unreachable ({err}); continuing without "
                         "usage metrics.\n")
        return None
    return client


def main(argv=None):
    args = build_parser().parse_args(argv)
    global CLI
    CLI = pick_cli_binary(prefer_kubectl=args.kubectl)
    k8s = _make_k8s(args)
    thanos = _make_thanos(args)
    namespaces = select_namespaces(k8s, args.pattern, args.namespace,
                                   args.all_namespaces)
    if not namespaces:
        sys.stderr.write("no matching namespaces.\n")
        return 0

    trees = [collect_namespace(k8s, ns, thanos, args.window, args.step,
                               include_idle=not args.no_idle_workloads)
             for ns in namespaces]

    formats = args.format or ["text"]
    levels = tuple(s.strip() for s in args.level.split(",") if s.strip())
    cluster = os.environ.get("KUBERNETES_SERVICE_HOST", "local")
    render_stdout_formats(trees, formats, sys.stdout, window=args.window,
                          cluster=cluster, levels=levels)

    if args.output_dir:
        now = datetime.now(timezone.utc)
        target = (dated_output_dir(args.output_dir, now)
                  if args.date_subdir else args.output_dir)
        write_all_reports(trees, target, args.window, cluster)
        sys.stderr.write(f"wrote reports to {target} "
                         f"(combined + by-stage/, see LEGEND.md)\n")
        if args.date_subdir and args.retention_days > 0:
            removed = prune_old_reports(args.output_dir, args.retention_days, now)
            if removed:
                sys.stderr.write(
                    f"pruned {len(removed)} report folder(s) older than "
                    f"{args.retention_days}d: {', '.join(removed)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
