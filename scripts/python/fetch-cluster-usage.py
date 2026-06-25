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
        # sum_usage (not sum_limit): a workload with neither a recommendation
        # nor a configured value contributes None and is skipped, rather than
        # blanking the whole namespace total. This is deliberately optimistic —
        # a single fully-unspecified workload should not hide the rest of the
        # namespace's footprint from the quota check.
        total = sum_usage(collected[base])
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


# --------------------------------------------- namespace per-storageclass quota

# A namespace's per-storageclass storage quota is a ResourceQuota key of the form
# `<class>.storageclass.storage.k8s.io/requests.storage`. parse_storage_quota
# strips this suffix to recover the class name; values use the same Ki/Mi/Gi
# units as memory, so parse_mem parses them to bytes.
STORAGE_QUOTA_SUFFIX = ".storageclass.storage.k8s.io/requests.storage"

# Storage classes we always emit a column for (even where a namespace has no
# quota entry — those cells become 0), in the report's fixed left-to-right order.
# Any class found in the data but missing here is appended (storage_classes_in)
# so a renamed/unexpected class is surfaced, never silently dropped.
STORAGE_CLASSES = [
    "block-silver",
    "file-bronce",
    "file-gold",
    "file-intern-logging",
    "file-intern-monitoring-alertmgr",
    "file-intern-monitoring-prometheus",
    "file-intern-monitoring-user-prometheus",
    "file-intern-registry",
    "file-network",
    "file-silver",
    "object-silver",
    "ocs-storagecluster-ceph-rbd",
    "ocs-storagecluster-ceph-rgw",
    "ocs-storagecluster-cephfs",
    "openshift-storage.noobaa.io",
]


def parse_storage_quota(items):
    """From a namespace's ResourceQuota objects, pull every per-storageclass
    `requests.storage` entry into {storageclass: {"used": bytes, "hard": bytes}}.

    The class name is the quota key with STORAGE_QUOTA_SUFFIX stripped; values
    parse via parse_mem (Ki/Mi/Gi -> bytes). Across multiple quotas the binding
    Hard is the smallest cap and Used the largest booked value (matching
    parse_quota). Non-storage keys are ignored. Empty/None input -> {}."""
    out = {}
    for item in items or []:
        status = item.get("status", {}) or {}
        for field in ("hard", "used"):
            for key, raw in (status.get(field, {}) or {}).items():
                if not key.endswith(STORAGE_QUOTA_SUFFIX):
                    continue
                cls = key[: -len(STORAGE_QUOTA_SUFFIX)]
                v = parse_mem(raw)
                if v is None:
                    continue
                entry = out.setdefault(cls, {"used": None, "hard": None})
                if field == "hard":
                    entry["hard"] = v if entry["hard"] is None \
                        else min(entry["hard"], v)
                else:
                    entry["used"] = v if entry["used"] is None \
                        else max(entry["used"], v)
    return out


def storage_totals(storage):
    """Sum a namespace's per-storageclass quota into (used, hard) bytes across
    all classes. Each side is None when no class tracks it. Feeds the
    storage_used / storage_hard / storage_used_pct namespace columns."""
    used = [v.get("used") for v in (storage or {}).values() if v.get("used") is not None]
    hard = [v.get("hard") for v in (storage or {}).values() if v.get("hard") is not None]
    return (sum(used) if used else None, sum(hard) if hard else None)


def pvc_records(items):
    """PersistentVolumeClaim list -> [{name, storageclass, capacity}] sorted by
    name. Capacity is the provisioned status.capacity.storage (Bound PVCs) or,
    failing that, the requested spec.resources.requests.storage, parsed to bytes.
    storageClassName missing -> '-'."""
    out = []
    for it in items or []:
        meta = it.get("metadata", {}) or {}
        spec = it.get("spec", {}) or {}
        status = it.get("status", {}) or {}
        cap = (status.get("capacity", {}) or {}).get("storage")
        if cap is None:
            cap = ((spec.get("resources", {}) or {}).get("requests", {})
                   or {}).get("storage")
        out.append({
            "name": meta.get("name", ""),
            "storageclass": spec.get("storageClassName") or "-",
            "capacity": parse_mem(cap),
        })
    return sorted(out, key=lambda p: p["name"])


def _storageclass_description(sc):
    """Pull the human description from a StorageClass's annotations: the exact
    `description` key, else the first annotation ending in `/description`
    (e.g. `kubernetes.io/description`). '' when none is set."""
    ann = (sc.get("metadata", {}) or {}).get("annotations", {}) or {}
    if ann.get("description"):
        return ann["description"]
    for key, val in ann.items():
        if key.endswith("/description") and val:
            return val
    return ""


def storageclass_descriptions(items):
    """{storageclass name: description} from a list of StorageClass objects.
    Classes without a description annotation are omitted. Empty/None -> {}."""
    out = {}
    for sc in items or []:
        name = (sc.get("metadata", {}) or {}).get("name")
        if not name:
            continue
        desc = _storageclass_description(sc)
        if desc:
            out[name] = desc
    return out


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
        "storage": {},
        "pvcs": [],
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
    "persistentvolumeclaims": ("persistentvolumeclaims", "/api/v1", True),
    "replicasets":  ("replicasets",  "/apis/apps/v1",  True),
    "deployments":  ("deployments",  "/apis/apps/v1",  True),
    "statefulsets": ("statefulsets", "/apis/apps/v1",  True),
    "daemonsets":   ("daemonsets",   "/apis/apps/v1",  True),
    "storageclasses": ("storageclasses", "/apis/storage.k8s.io/v1", False),
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

    def list_persistentvolumeclaims(self, namespace=None):
        return self._list("persistentvolumeclaims", namespace)

    def list_storageclasses(self):
        return self._list("storageclasses", None)

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

    def list_persistentvolumeclaims(self, namespace=None):
        return self._list("persistentvolumeclaims", namespace)

    def list_storageclasses(self):
        return self._list("storageclasses", None)

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
                      include_idle=True, sc_desc=None):
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
    # Per-storageclass requests.storage Hard/Used from the ResourceQuota, plus
    # the summed totals on the namespace rollup (-> namespaces.csv storage cols).
    node["storage"] = parse_storage_quota(quotas)
    s_used, s_hard = storage_totals(node["storage"])
    node["totals"]["storage_used"] = s_used
    node["totals"]["storage_hard"] = s_hard
    node["totals"]["storage_used_pct"] = util_pct(s_used, s_hard)
    # PersistentVolumeClaims in the namespace (-> resources.csv pvc rows), each
    # tagged with its StorageClass's description annotation (cluster-scoped map
    # fetched once, passed in as sc_desc).
    pvc_lister = getattr(fcu_k8s, "list_persistentvolumeclaims", None)
    node["pvcs"] = pvc_records(pvc_lister(namespace)) if pvc_lister else []
    sc_desc = sc_desc or {}
    for p in node["pvcs"]:
        p["description"] = sc_desc.get(p["storageclass"], "")

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
    ("pod", "pod"), ("container", "container"), ("pvc", "pvc"),
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
    # Storage: namespace-level ResourceQuota requests.storage Used/Hard (+%),
    # summed at stage/cluster; storageclass + capacity carry the per-PVC rows.
    ("storage_used_bytes", "storage_used"),
    ("storage_hard_bytes", "storage_hard"),
    ("storage_used_pct", "storage_used_pct"),
    ("storageclass", "storageclass"),
    ("storageclass_description", "storageclass_description"),
    ("storage_capacity_bytes", "storage_capacity"),
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
                  "pod", "container", "pvc", "storageclass",
                  "storageclass_description"}
# PVC-only metric columns: meaningful on level=pvc rows in resources.csv but not
# at the namespace level, so they're excluded from the namespaces.csv summary.
_PVC_ONLY_KEYS = {"storage_capacity"}
METRIC_FIELDS = [(h, k) for h, k in CSV_FIELDS
                 if k not in _IDENTITY_KEYS and k not in _PVC_ONLY_KEYS]
NS_CSV_FIELDS = [("stage", "stage"), ("namespace", "namespace")] + METRIC_FIELDS
# Per-storageclass quota columns appended only to namespaces.csv (the totals
# storage_used/hard/used_pct already come in via METRIC_FIELDS). Dense over the
# canonical class set so every namespace has the same columns.
NS_STORAGE_COLUMNS = []
for _cls in STORAGE_CLASSES:
    NS_STORAGE_COLUMNS += [f"{_cls}_used_bytes", f"{_cls}_hard_bytes",
                           f"{_cls}_used_pct"]
# Per-storageclass PVC usage in the namespace (aggregated from the PVCs, not the
# quota): <class>_pvc_bytes = sum of that class's PVC capacities, <class>_pvc_pct
# = that class's share of the namespace's total PVC capacity.
NS_PVC_COLUMNS = []
for _cls in STORAGE_CLASSES:
    NS_PVC_COLUMNS += [f"{_cls}_pvc_bytes", f"{_cls}_pvc_pct"]
NS_CSV_COLUMNS = ([h for h, _ in NS_CSV_FIELDS]
                  + NS_STORAGE_COLUMNS + NS_PVC_COLUMNS)

# Human-readable twin of resources.csv: same rows/columns, but each metric is
# rendered with its unit inline (200m, 6.3Mi, 4.9%) instead of a raw float. The
# unit-bearing header suffix (_cores/_bytes) is dropped because the value now
# carries the unit itself; counts and identity columns pass through unchanged.
_CPU_METRIC_KEYS = {"cpu_request", "cpu_limit", "cpu_now", "cpu_peak", "cpu_avg",
                    "cpu_request_used", "cpu_limit_used"}
_MEM_METRIC_KEYS = {"mem_request", "mem_limit", "mem_now", "mem_peak",
                    "mem_request_used", "mem_limit_used"}
_STORAGE_BYTES_KEYS = {"storage_used", "storage_hard", "storage_capacity"}
_PCT_METRIC_KEYS = {"cpu_peak_util_pct", "mem_peak_util_pct", "storage_used_pct"}
_HEADER_TO_KEY = dict(CSV_FIELDS)


def _human_header(header):
    """Drop the raw-unit suffix from a CSV header ('cpu_peak_cores' -> 'cpu_peak')."""
    for suf in ("_cores", "_bytes"):
        if header.endswith(suf):
            return header[: -len(suf)]
    return header


HUMAN_CSV_COLUMNS = [_human_header(h) for h, _ in CSV_FIELDS]
NS_HUMAN_STORAGE_COLUMNS = [_human_header(c) for c in NS_STORAGE_COLUMNS]
NS_HUMAN_PVC_COLUMNS = [_human_header(c) for c in NS_PVC_COLUMNS]
NS_HUMAN_CSV_COLUMNS = ([_human_header(h) for h, _ in NS_CSV_FIELDS]
                        + NS_HUMAN_STORAGE_COLUMNS + NS_HUMAN_PVC_COLUMNS)


def _ns_pvc_cells(node, human):
    """Per-storageclass PVC usage for namespaces.csv: <class>_pvc_bytes (sum of
    the namespace's PVC capacities of that class) and <class>_pvc_pct (that
    class's share of the namespace's total PVC capacity). Dense over the
    canonical class set; share is blank when the namespace has no PVCs."""
    by_class = {}
    total = 0
    for p in node.get("pvcs", []):
        cap = p.get("capacity")
        if cap is None:
            continue
        cls = p.get("storageclass")
        by_class[cls] = by_class.get(cls, 0) + cap
        total += cap
    cells = {}
    for cls in STORAGE_CLASSES:
        b = by_class.get(cls, 0)
        pct = util_pct(b, total) if total else None
        if human:
            cells[f"{cls}_pvc"] = fmt_bytes(b)
            cells[f"{cls}_pvc_pct"] = fmt_pct(pct)
        else:
            cells[f"{cls}_pvc_bytes"] = b
            cells[f"{cls}_pvc_pct"] = "" if pct is None else pct
    return cells


def _ns_storage_cells(node, human):
    """Per-storageclass quota cells for namespaces.csv. A class absent from the
    namespace's quota shows 0 (dense matrix, matching the cpu/mem 'used'
    columns); %-used is Used/Hard (blank / '-' where Hard is 0)."""
    store = node.get("storage") or {}
    cells = {}
    for cls in STORAGE_CLASSES:
        entry = store.get(cls) or {}
        used = entry.get("used") or 0
        hard = entry.get("hard") or 0
        pct = util_pct(used, hard)
        if human:
            cells[f"{cls}_used"] = fmt_bytes(used)
            cells[f"{cls}_hard"] = fmt_bytes(hard)
            cells[f"{cls}_used_pct"] = fmt_pct(pct)
        else:
            cells[f"{cls}_used_bytes"] = used
            cells[f"{cls}_hard_bytes"] = hard
            cells[f"{cls}_used_pct"] = "" if pct is None else pct
    return cells


def _human_metric(key, value):
    """Format one raw cell for the human CSV, chosen by its source key."""
    v = None if value == "" else value
    if key in _CPU_METRIC_KEYS:
        return fmt_cores(v)
    if key in _MEM_METRIC_KEYS or key in _STORAGE_BYTES_KEYS:
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
    # Storage quota Used/Hard sum across namespaces; %-used recomputed.
    agg["storage_used"] = sum_usage([t.get("storage_used") for t in totals_list])
    agg["storage_hard"] = sum_usage([t.get("storage_hard") for t in totals_list])
    agg["storage_used_pct"] = util_pct(agg["storage_used"], agg["storage_hard"])
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
        # PVCs are namespace-scoped (parallel to workloads): one row each, with
        # capacity + storageclass and the cpu/mem columns left blank.
        for pvc in node.get("pvcs", []):
            rows.append(_row_from_totals(
                "pvc", stage, ns, {"storage_capacity": pvc["capacity"]},
                pvc=pvc["name"], storageclass=pvc["storageclass"],
                storageclass_description=pvc.get("description", "")))
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
        row.update(_ns_storage_cells(node, human=False))
        row.update(_ns_pvc_cells(node, human=False))
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
        row.update(_ns_storage_cells(node, human=True))
        row.update(_ns_pvc_cells(node, human=True))
        writer.writerow(row)


# (csv header, source key) for the per-workload recommendation CSV. Current
# (cur) values sit beside the recommended (rec) ones so old-vs-new is one row.
REC_FIELDS = [
    ("stage", "stage"), ("namespace", "namespace"),
    ("workload_kind", "workload_kind"), ("workload", "workload"),
    ("cpu_peak_cores", "cpu_peak"),
    ("cpu_request_cur_cores", "cpu_request"),
    ("cpu_limit_cur_cores", "cpu_limit"),
    ("cpu_request_rec_cores", "cpu_request_rec"),
    ("cpu_limit_rec_cores", "cpu_limit_rec"),
    ("mem_peak_bytes", "mem_peak"),
    ("mem_request_cur_bytes", "mem_request"),
    ("mem_limit_cur_bytes", "mem_limit"),
    ("mem_request_rec_bytes", "mem_request_rec"),
    ("mem_limit_rec_bytes", "mem_limit_rec"),
]
REC_COLUMNS = [h for h, _ in REC_FIELDS]
REC_HUMAN_COLUMNS = [_human_header(h) for h, _ in REC_FIELDS]

_REC_CPU_KEYS = {"cpu_peak", "cpu_request", "cpu_limit",
                 "cpu_request_rec", "cpu_limit_rec"}
_REC_MEM_KEYS = {"mem_peak", "mem_request", "mem_limit",
                 "mem_request_rec", "mem_limit_rec"}


def _rec_human(key, value):
    """Format one recommendation cell with its unit (cores/bytes); identity
    strings pass through."""
    if key in _REC_CPU_KEYS:
        return fmt_cores(value)
    if key in _REC_MEM_KEYS:
        return fmt_bytes(value)
    return "" if value is None else value


def recommendation_rows(trees, target_util=80.0):
    """One dict per qualifying (hot) workload, keyed by REC_FIELDS source keys.
    A workload is included only if it qualifies on at least one resource."""
    rows = []
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        for wl in node["workloads"]:
            rec = compute_recommendation(wl["totals"], target_util)
            if all(v is None for v in rec.values()):
                continue
            t = wl["totals"]
            rows.append({
                "stage": node["stage"], "namespace": node["namespace"],
                "workload_kind": wl["kind"], "workload": wl["name"],
                "cpu_peak": t.get("cpu_peak"),
                "cpu_request": t.get("cpu_request"),
                "cpu_limit": t.get("cpu_limit"),
                "cpu_request_rec": rec["cpu_request_rec"],
                "cpu_limit_rec": rec["cpu_limit_rec"],
                "mem_peak": t.get("mem_peak"),
                "mem_request": t.get("mem_request"),
                "mem_limit": t.get("mem_limit"),
                "mem_request_rec": rec["mem_request_rec"],
                "mem_limit_rec": rec["mem_limit_rec"],
            })
    return rows


def render_recommendations_csv(trees, stream, target_util=80.0):
    """Per-workload recommendation CSV (only hot workloads; raw numbers)."""
    writer = csv.DictWriter(stream, fieldnames=REC_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in recommendation_rows(trees, target_util):
        writer.writerow({h: ("" if row.get(k) is None else row.get(k))
                         for h, k in REC_FIELDS})


def render_recommendations_human_csv(trees, stream, target_util=80.0):
    """Human-readable twin: same rows, each metric formatted with its unit."""
    writer = csv.DictWriter(stream, fieldnames=REC_HUMAN_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for row in recommendation_rows(trees, target_util):
        writer.writerow({_human_header(h): _rec_human(k, row.get(k))
                         for h, k in REC_FIELDS})


# ----------------------------------- apply manifest (per-workload patch YAML)

def cpu_quantity(cores):
    """Cores -> a valid Kubernetes CPU quantity string in millicores ('320m').
    Unlike fmt_cores there is no 'c'/'µ' suffix, so it round-trips via parse_cpu
    and is accepted verbatim by kubectl. None -> None."""
    if cores is None:
        return None
    return f"{int(round(cores * 1000))}m"


def mem_quantity(nbytes):
    """Bytes -> a valid Kubernetes memory quantity string. Exact Gi multiples
    render as 'Gi', otherwise Mi (recommendations are rounded up to whole Mi, so
    Mi is always exact). Round-trips via parse_mem. None -> None."""
    if nbytes is None:
        return None
    gi, mi = 1024 ** 3, 1024 ** 2
    if nbytes % gi == 0:
        return f"{nbytes // gi}Gi"
    return f"{nbytes // mi}Mi"


def _opt_max(vals):
    xs = [v for v in vals if v is not None]
    return max(xs) if xs else None


def _opt_first(vals):
    for v in vals:
        if v is not None:
            return v
    return None


def workload_container_recommendations(wl, target_util=80.0):
    """Per-container recommendations for one workload node, keyed by container
    *name* (the strategic-merge patchMergeKey). For each container name the peak
    is the max across the workload's replicas and the limit/request the
    configured template value; compute_recommendation is then run on that
    synthetic per-container totals. Returns one dict per container with its
    current (`*_cur`) values and the compute_recommendation `*_rec` keys (None
    where that resource is not hot). Containers with no running pods produce no
    entry (idle workloads never qualify)."""
    by_name = {}
    for pod in wl.get("pods", []) or []:
        for c in pod.get("containers", []) or []:
            by_name.setdefault(c["container"], []).append(c)
    out = []
    for name in sorted(by_name):
        leaves = by_name[name]
        totals = {
            "cpu_peak": _opt_max(l.get("cpu_peak") for l in leaves),
            "mem_peak": _opt_max(l.get("mem_peak") for l in leaves),
            "cpu_limit": _opt_first(l.get("cpu_limit") for l in leaves),
            "mem_limit": _opt_first(l.get("mem_limit") for l in leaves),
        }
        rec = compute_recommendation(totals, target_util)
        out.append({
            "container": name,
            "cpu_request_cur": _opt_first(l.get("cpu_request") for l in leaves),
            "cpu_limit_cur": totals["cpu_limit"],
            "mem_request_cur": _opt_first(l.get("mem_request") for l in leaves),
            "mem_limit_cur": totals["mem_limit"],
            **rec,
        })
    return out


def _has_recommendation(c):
    return any(c[k] is not None for k in
               ("cpu_request_rec", "cpu_limit_rec",
                "mem_request_rec", "mem_limit_rec"))


def _container_patch_resources(c):
    """Strategic-merge `resources` body for one container: only the hot
    dimensions, each with both requests and limits (compute_recommendation
    always pairs them). Cold dimensions are omitted so the merge preserves the
    live object's untouched keys."""
    req, lim = {}, {}
    if c["cpu_limit_rec"] is not None:      # cpu hot
        req["cpu"] = cpu_quantity(c["cpu_request_rec"])
        lim["cpu"] = cpu_quantity(c["cpu_limit_rec"])
    if c["mem_limit_rec"] is not None:      # mem hot
        req["memory"] = mem_quantity(c["mem_request_rec"])
        lim["memory"] = mem_quantity(c["mem_limit_rec"])
    res = {}
    if req:
        res["requests"] = req
    if lim:
        res["limits"] = lim
    return res


def _summary_cell(kind, cur, rec):
    fmt = cpu_quantity if kind == "cpu" else mem_quantity
    cur_s = "-" if cur is None else fmt(cur)
    new = rec if rec is not None else cur
    new_s = "-" if new is None else fmt(new)
    return f"{cur_s}→{new_s}"


def _container_summary_line(c):
    """One human old->new line per container, e.g.
    `web: cpu req 500m->900m, lim 1000m->1130m | mem req 64Mi->64Mi, lim ...`."""
    cpu = (f"cpu req {_summary_cell('cpu', c['cpu_request_cur'], c['cpu_request_rec'])}"
           f", lim {_summary_cell('cpu', c['cpu_limit_cur'], c['cpu_limit_rec'])}")
    mem = (f"mem req {_summary_cell('mem', c['mem_request_cur'], c['mem_request_rec'])}"
           f", lim {_summary_cell('mem', c['mem_limit_cur'], c['mem_limit_rec'])}")
    return f"{c['container']}: {cpu} | {mem}"


_APPLY_DIMS = ("cpu_request", "cpu_limit", "mem_request", "mem_limit")


def _exceeds_reason(base, summary):
    fmt = cpu_quantity if base.startswith("cpu") else mem_quantity
    return (f"{base} sum {fmt(summary[base + '_rec_sum'])} "
            f"> quota {fmt(summary[base + '_quota'])}")


def build_apply_plan(trees, target_util=80.0):
    """Pure model behind both apply serializers. Returns
    {target_util, skipped: [...], patches: [...]}. A namespace whose
    namespace_recommendation_summary flags INCREASE_QUOTA contributes no patches
    and one `skipped` entry (with per-dimension reasons); every other namespace
    contributes one patch per workload that has at least one hot container."""
    plan = {"target_util": target_util, "skipped": [], "patches": []}
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        summary = namespace_recommendation_summary(node, target_util)
        if summary["quota_action"] == "INCREASE_QUOTA":
            reasons = [_exceeds_reason(b, summary) for b in _APPLY_DIMS
                       if summary[b + "_status"] == "EXCEEDS"]
            plan["skipped"].append({"namespace": node["namespace"],
                                    "stage": node["stage"], "reasons": reasons})
            continue
        for wl in node["workloads"]:
            hot = [c for c in workload_container_recommendations(wl, target_util)
                   if _has_recommendation(c)]
            if not hot:
                continue
            containers = [{"name": c["container"],
                           "resources": _container_patch_resources(c)}
                          for c in hot]
            plan["patches"].append({
                "stage": node["stage"], "namespace": node["namespace"],
                "kind": wl["kind"], "name": wl["name"],
                "summary_lines": [_container_summary_line(c) for c in hot],
                "patch": {"spec": {"template": {"spec":
                                                {"containers": containers}}}},
            })
    return plan


def _yaml_dq(s):
    """Double-quote a scalar so it round-trips through any YAML parser."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_inline_map(d):
    return "{" + ", ".join(f"{k}: {_yaml_dq(v)}" for k, v in d.items()) + "}"


def render_recommendations_apply_yaml(trees, stream, target_util=80.0):
    """Human-reviewable multi-doc patch YAML: one ResourcePatch per hot workload,
    a SKIPPED header block for INCREASE_QUOTA namespaces. Hand-emitted (stdlib
    only) with quoted string scalars."""
    plan = build_apply_plan(trees, target_util)
    w = stream.write
    w("# recommendations-apply.yaml — generated by fetch-cluster-usage.py\n")
    w(f"# target_util={target_util:g}\n")
    w("# SKIPPED (namespace quota must be raised first — "
      "quota_action=INCREASE_QUOTA):\n")
    if plan["skipped"]:
        for s in plan["skipped"]:
            reasons = "; ".join(s["reasons"]) if s["reasons"] else "quota exceeded"
            w(f"#   {s['namespace']}: {reasons}\n")
    else:
        w("#   (none)\n")
    for p in plan["patches"]:
        w("---\n")
        w(f"# {p['namespace']} / {p['kind']} {p['name']}\n")
        for line in p["summary_lines"]:
            w(f"#   {line}\n")
        w("apiVersion: forensic-dashboards/v1\n")
        w("kind: ResourcePatch\n")
        w("target:\n")
        w(f"  kind: {_yaml_dq(p['kind'])}\n")
        w(f"  namespace: {_yaml_dq(p['namespace'])}\n")
        w(f"  name: {_yaml_dq(p['name'])}\n")
        w("patch:\n")
        w("  spec:\n")
        w("    template:\n")
        w("      spec:\n")
        w("        containers:\n")
        for c in p["patch"]["spec"]["template"]["spec"]["containers"]:
            w(f"          - name: {_yaml_dq(c['name'])}\n")
            w("            resources:\n")
            res = c["resources"]
            if "requests" in res:
                w(f"              requests: {_yaml_inline_map(res['requests'])}\n")
            if "limits" in res:
                w(f"              limits: {_yaml_inline_map(res['limits'])}\n")


def render_recommendations_apply_json(trees, stream, target_util=80.0):
    """Machine-readable sidecar consumed by apply-recommendations.py (stdlib
    json, so the applier needs no YAML dependency). Same patches as the YAML."""
    plan = build_apply_plan(trees, target_util)
    obj = {
        "generated_by": "fetch-cluster-usage.py",
        "target_util": target_util,
        "skipped": plan["skipped"],
        "patches": [{"namespace": p["namespace"], "stage": p["stage"],
                     "kind": p["kind"], "name": p["name"],
                     "summary": p["summary_lines"], "patch": p["patch"]}
                    for p in plan["patches"]],
    }
    json.dump(obj, stream, indent=2)
    stream.write("\n")


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


def render_stdout_formats(trees, formats, stream, window, cluster, levels,
                          target_util=80.0):
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
    if "recommendations" in formats:
        render_recommendations_csv(trees, stream, target_util=target_util)
    if "recommendations-human" in formats:
        render_recommendations_human_csv(trees, stream, target_util=target_util)
    if "recommendations-apply" in formats:
        render_recommendations_apply_yaml(trees, stream, target_util=target_util)


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
- `recommendations.csv` — Empfehlung zum Right-Sizing: eine Zeile pro **heißem** Workload (Spitze nahe/über dem Limit), aktuelle Werte neben den empfohlenen. Rohzahlen.
- `recommendations-human.csv` — dieselben Zeilen wie `recommendations.csv`, aber jeder Wert mit Einheit (z. B. `200m`, `1.13c`, `6.3Mi`) statt Rohzahl.
- `recommendations-apply.yaml` — anwendbares Patch-Manifest: ein `ResourcePatch`-Dokument pro **heißem** Workload, das die empfohlenen `requests`/`limits` pro Container als Strategic-Merge-Patch setzt. Namespaces, deren Summe das ResourceQuota überschreiten würde (`quota_action=INCREASE_QUOTA`), werden **übersprungen** und im `SKIPPED`-Kopfblock genannt (Quota zuerst manuell erhöhen). Zum menschlichen Review gedacht.
- `recommendations-apply.json` — maschinenlesbares Pendant zu `recommendations-apply.yaml` (dieselben Patches), das `apply-recommendations.py` mit der Python-Standardbibliothek liest (kein YAML-Paket nötig).
- `ooms.csv`       — eine Zeile pro OOM-getötetem Container.
- `summary.txt`    — menschenlesbare Tabelle (CPU in Cores/Milli, Speicher in Ki/Mi/Gi, % und OOM-Anzahl) — dieselben Zahlen wie die CSVs, nur kompakt formatiert.
- `report.json`   — dieselben Daten verschachtelt (Namespace → Workload → Pod → Container) plus Aggregationen.
- `by-stage/<stage>/` — (im obersten Ordner) dieselben Dateien, beschränkt auf eine Stage.
- `apply/` — (im obersten Ordner) sammelt **nur** die Apply-Manifeste an einem Ort, damit `apply-recommendations.py` nicht jeden `by-stage/`-Ordner durchsuchen muss: `all.yaml`/`all.json` (alle Namespaces zusammen), je Stage ein `<stage>.yaml`/`<stage>.json` (für einen gestaffelten Rollout — erst test, dann prod) und je Namespace ein `<stage>/<namespace>.yaml`/`.json` (um einen einzelnen Namespace dediziert anzuwenden). Namespaces ohne Änderung (keine heißen Workloads und nicht Quota-übersprungen) erhalten keine Datei.

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

## recommendations.csv Spalten
Right-Sizing-Vorschlag pro Workload, abgeleitet aus dem Spitzenverbrauch über
das Fenster (--window). Enthält **nur heiße** Workloads: ein Workload erscheint,
wenn seine CPU- **oder** Speicher-Spitze kein Limit hat (unbegrenzt) oder die
Zielauslastung überschreitet — ruhige Workloads werden weggelassen. Die
Empfehlung pro Dimension: `request = aufgerundet(Spitze)`,
`limit = aufgerundet(Spitze ÷ target_util)`, mit `target_util` = `--target-util`/100
(Standard 80 %). CPU wird auf 10 Millicores, Speicher auf Mebibyte aufgerundet.
- `stage`, `namespace`, `workload_kind`, `workload` — Identität des Workloads.
- `cpu_peak_cores` / `mem_peak_bytes` — gemessene Spitze über das Fenster (Basis der Empfehlung).
- `cpu_request_cur_cores` / `cpu_limit_cur_cores` / `mem_request_cur_bytes` / `mem_limit_cur_bytes`
    — die **aktuell** konfigurierten Requests/Limits (`cur`), leer wenn nicht gesetzt.
- `cpu_request_rec_cores` / `cpu_limit_rec_cores` / `mem_request_rec_bytes` / `mem_limit_rec_bytes`
    — die **empfohlenen** Werte (`rec`). Leer, wenn diese Dimension nicht heiß ist
      (also keine Änderung nötig). `cur` und `rec` stehen nebeneinander, damit
      alt-vs-neu in einer Zeile vergleichbar ist.
In `recommendations-human.csv` tragen alle Werte ihre Einheit (CPU `c`/`m`/`µ`,
Speicher `Ki`/`Mi`/`Gi`) — siehe Einheiten-Abschnitt oben.

## recommendations-apply.yaml / .json — empfohlene Werte auf den Cluster anwenden
`recommendations-apply.yaml` macht aus den Empfehlungen ein anwendbares
Manifest. Es enthält **nur Workloads** (Container-`requests`/`limits`); das
ResourceQuota wird **nie** verändert.
- Pro heißem Workload ein `ResourcePatch`-Dokument (`apiVersion:
  forensic-dashboards/v1`) mit `target` (kind/namespace/name) und `patch`
  (Strategic-Merge-Body für `spec.template.spec.containers[].resources`).
- Je Container werden nur die **heißen** Dimensionen gesetzt (CPU **und/oder**
  Speicher, jeweils request **und** limit zusammen); kalte Dimensionen bleiben
  weg, damit der Merge die Live-Werte unberührt lässt. Mehrere Container werden
  per Name (Strategic-Merge-Key) getrennt aufgeführt.
- **Quota-Schutz:** Würde die Summe der Empfehlungen eines Namespace dessen
  ResourceQuota überschreiten (`quota_action=INCREASE_QUOTA`), wird der Namespace
  **komplett übersprungen** und im `SKIPPED`-Kopfblock mit der überschrittenen
  Dimension genannt. Erst Quota erhöhen, dann den Export neu erzeugen.
- Der alt→neu-Vergleich steht im Kommentarkopf jedes Dokuments.

`recommendations-apply.json` ist das maschinenlesbare Pendant (dieselben
Patches), das der Applier ohne YAML-Abhängigkeit liest.

### apply-recommendations.py — Manifest anwenden (Dry-Run-Standard)
Eigenständiges Skript, das `recommendations-apply.json` einliest und je Patch
`kubectl`/`oc patch ... --type=strategic` aufruft.
- **Ohne Flag: Server-seitiger Dry-Run** (`--dry-run=server`) — validiert gegen
  das Live-Objekt (Schema, Admission, Quota) und zeigt alt→neu, **ändert nichts**.
- **`--execute`** wendet die Änderungen wirklich an; schlägt ein Patch fehl,
  endet das Skript mit Exit-Code ≠ 0.
- Flags: `--manifest PFAD` (Default `recommendations-apply.json`), `--execute`,
  `--context NAME`, `--oc` (statt `kubectl`), `--kubectl PFAD`.
- Ein zwischenzeitlich gelöschter Workload (`NotFound`) wird übersprungen, nicht
  als Fehler gewertet. Der `SKIPPED`-Block wird beim Anwenden erneut ausgegeben.

## Speicher (Storage) — in resources.csv und namespaces.csv
Das Speicher-Kontingent (`requests.storage`) steckt direkt in den Haupt-CSVs
(`resources.csv` und `namespaces.csv`), nicht in einer eigenen Datei.

**Quota-Summen (in beiden CSVs, auf Namespace-Ebene aus der ResourceQuota; in
`stage`/`cluster` summiert):**
- `storage_used_bytes` — gebuchter Speicher über alle StorageClasses (Quota *Used*).
- `storage_hard_bytes` — Hard-Kontingent über alle StorageClasses (Quota *Hard*).
- `storage_used_pct` — `storage_used / storage_hard` in Prozent (leer, wenn kein Hard-Kontingent).

**Pro StorageClass — Quota (nur in `namespaces.csv`):** je Klasse drei Spalten
`<class>_used_bytes`, `<class>_hard_bytes`, `<class>_used_pct` (Used, Hard und
Used/Hard %). Die Klassen stehen in fester Reihenfolge; eine im Quota nicht
vorhandene Klasse erscheint als `0` (dichte Matrix).

**Pro StorageClass — tatsächliche PVC-Nutzung (nur in `namespaces.csv`):** je
Klasse zwei Spalten `<class>_pvc_bytes` (Summe der PVC-Kapazitäten dieser Klasse
im Namespace) und `<class>_pvc_pct` (Anteil dieser Klasse an der gesamten
PVC-Kapazität des Namespace). So sieht man z. B., wie viel Speicher `file-silver`
im Namespace belegt und welchen Anteil das ausmacht — unabhängig von der Quota.
Leer im `*_pct`, wenn der Namespace keine PVCs hat.

**PVCs (nur in `resources.csv`):** je PersistentVolumeClaim eine Zeile mit
`level=pvc`. Quelle ist `oc get pvc`.
- `pvc` — Name des PVC (in der Spalte `pvc`).
- `storageclass` — StorageClass des PVC (`spec.storageClassName`).
- `storageclass_description` — Beschreibung der StorageClass aus deren Annotation
  `description` (bzw. `…/description`); leer, wenn nicht gesetzt.
- `storage_capacity_bytes` — bereitgestellte Kapazität (`status.capacity.storage`,
  sonst `spec.resources.requests.storage`).
Die CPU-/Speicher-Spalten sind in `pvc`-Zeilen leer; umgekehrt sind
`pvc`/`storageclass`/`storage_capacity_bytes` in den übrigen Zeilen leer.
In den `*-human.csv` tragen die Byte-Werte ihre Einheit (`Ki`/`Mi`/`Gi`/`Ti`)
und `*_pct` das `%`-Zeichen; der `_bytes`-Suffix entfällt im Spaltennamen.

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
                       summary_kinds=("cluster", "stage"), target_util=80.0):
    """Write resources.csv, ooms.csv, report.json and LEGEND.md into out_dir
    (created if needed). summary_kinds selects which rollup rows the CSV/JSON
    carry. target_util drives the recommendation CSVs. Returns out_dir."""
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
    with open(os.path.join(out_dir, "recommendations.csv"), "w",
              encoding="utf-8") as f:
        render_recommendations_csv(trees, f, target_util=target_util)
    with open(os.path.join(out_dir, "recommendations-human.csv"), "w",
              encoding="utf-8") as f:
        render_recommendations_human_csv(trees, f, target_util=target_util)
    with open(os.path.join(out_dir, "recommendations-apply.yaml"), "w",
              encoding="utf-8") as f:
        render_recommendations_apply_yaml(trees, f, target_util=target_util)
    with open(os.path.join(out_dir, "recommendations-apply.json"), "w",
              encoding="utf-8") as f:
        render_recommendations_apply_json(trees, f, target_util=target_util)
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


def write_apply_manifest_pair(trees, path_noext, target_util=80.0):
    """Write `<path_noext>.yaml` + `<path_noext>.json` apply manifests for
    `trees` (the human-reviewable YAML and the applier's JSON sidecar)."""
    with open(path_noext + ".yaml", "w", encoding="utf-8") as f:
        render_recommendations_apply_yaml(trees, f, target_util=target_util)
    with open(path_noext + ".json", "w", encoding="utf-8") as f:
        render_recommendations_apply_json(trees, f, target_util=target_util)


def write_apply_manifests(trees, apply_dir, target_util=80.0):
    """Collect every recommendations-apply manifest into one folder so
    apply-recommendations.py never has to hunt through by-stage/. Writes, under
    apply_dir:
      - `all.{yaml,json}`        — the full set across all namespaces;
      - `<stage>.{yaml,json}`    — one per stage, for a staged rollout;
      - `<stage>/<namespace>.{yaml,json}` — one per namespace, for applying a
        single namespace on its own. A namespace with nothing to apply (no hot
        workloads and not quota-skipped) gets no file.
    Returns apply_dir."""
    os.makedirs(apply_dir, exist_ok=True)
    write_apply_manifest_pair(trees, os.path.join(apply_dir, "all"), target_util)
    for stage, stage_nodes in sorted(group_by_stage(trees).items()):
        write_apply_manifest_pair(stage_nodes,
                                  os.path.join(apply_dir, stage), target_util)
        stage_subdir = os.path.join(apply_dir, stage)
        for node in sorted(stage_nodes, key=lambda n: n["namespace"]):
            plan = build_apply_plan([node], target_util)
            if not plan["patches"] and not plan["skipped"]:
                continue   # nothing to apply for this namespace -> no file
            os.makedirs(stage_subdir, exist_ok=True)
            write_apply_manifest_pair(
                [node], os.path.join(stage_subdir, node["namespace"]),
                target_util)
    return apply_dir


def write_all_reports(trees, out_dir, window, cluster, target_util=80.0):
    """Combined report (cluster + per-stage rollups) at out_dir, plus a
    self-contained per-stage report under out_dir/by-stage/<stage>/ and an
    out_dir/apply/ folder gathering every recommendations-apply manifest."""
    write_report_files(trees, out_dir, window, cluster,
                       summary_kinds=("cluster", "stage"),
                       target_util=target_util)
    for stage, stage_nodes in sorted(group_by_stage(trees).items()):
        write_report_files(stage_nodes,
                           os.path.join(out_dir, "by-stage", stage),
                           window, cluster, summary_kinds=("stage",),
                           target_util=target_util)
    write_apply_manifests(trees, os.path.join(out_dir, "apply"), target_util)
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
                              "namespaces-human", "recommendations",
                              "recommendations-human", "recommendations-apply",
                              "none"],
                     help="stdout format(s), repeatable (default: text). "
                          "'csv' = raw resources.csv; 'resources-human' / "
                          "'namespaces-human' = the unit-formatted CSVs; "
                          "'recommendations' / 'recommendations-human' = the "
                          "per-workload right-sizing CSV (raw / unit-formatted); "
                          "'recommendations-apply' = the per-workload patch YAML "
                          "manifest (for apply-recommendations.py); "
                          "'none' = no stdout (pair with --output-dir to only "
                          "write files).")
    out.add_argument("--target-util", type=float, default=80.0,
                     metavar="PCT",
                     help="Target peak utilisation %% for recommendations: "
                          "limit = peak / (PCT/100) (default: 80).")
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

    # StorageClass descriptions are cluster-scoped — fetch once and share.
    sc_lister = getattr(k8s, "list_storageclasses", None)
    try:
        sc_desc = storageclass_descriptions(sc_lister()) if sc_lister else {}
    except Exception:                       # missing RBAC / API -> skip the column
        sc_desc = {}

    trees = [collect_namespace(k8s, ns, thanos, args.window, args.step,
                               include_idle=not args.no_idle_workloads,
                               sc_desc=sc_desc)
             for ns in namespaces]

    formats = args.format or ["text"]
    levels = tuple(s.strip() for s in args.level.split(",") if s.strip())
    cluster = os.environ.get("KUBERNETES_SERVICE_HOST", "local")
    render_stdout_formats(trees, formats, sys.stdout, window=args.window,
                          cluster=cluster, levels=levels,
                          target_util=args.target_util)

    if args.output_dir:
        now = datetime.now(timezone.utc)
        target = (dated_output_dir(args.output_dir, now)
                  if args.date_subdir else args.output_dir)
        write_all_reports(trees, target, args.window, cluster,
                          target_util=args.target_util)
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
