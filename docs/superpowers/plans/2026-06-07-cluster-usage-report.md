# Cluster Usage Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/python/fetch-cluster-usage.py` — a self-contained, stdlib-only Python 3 script that reports configured CPU/memory limits & requests vs. real Thanos usage rolled up at namespace/workload/pod/container, plus an OOM-killed list from live pod state + Thanos history, runnable both locally (oc/kubectl) and as an in-cluster CronJob.

**Architecture:** One script with pure, independently-testable functions (parsing, stage detection, owner resolution, rollup math, OOM merge), a `K8sClient` abstraction with REST (in-cluster) and CLI (oc/kubectl local) backends, a `Thanos` HTTP client reused from `fetch-thanos-metrics.py`, an orchestrator that builds a nested report, and three renderers (text/CSV/JSON). Tests load the hyphenated script via `importlib` through a `conftest.py`.

**Tech Stack:** Python 3 standard library only (`urllib`, `json`, `ssl`, `subprocess`, `argparse`, `csv`). Pytest for tests. Kubernetes manifests (YAML) for the CronJob.

---

## Reference material

Read these before starting — the new script reuses their patterns:

- `scripts/python/fetch-thanos-metrics.py` — `Thanos` class, `parse_time`, `start_port_forward`, `discover_querier`, `QUERIER_CANDIDATES`, `auto_token`, `CLI` selection (`oc` then `kubectl`).
- `scripts/python/fetch-cluster-oom.py` — `parse_mem`, `fmt_bytes`, `parse_iso`, `workload_for`, `find_oom_targets`, the `metric_first` fallback idea, and the `-` degrade-when-Prometheus-absent philosophy.
- Spec: `docs/superpowers/specs/2026-06-07-cluster-usage-report-design.md`.

## File structure

| File | Responsibility |
|---|---|
| `scripts/python/fetch-cluster-usage.py` | The whole script (helpers, clients, orchestration, renderers, CLI). |
| `scripts/python/tests/conftest.py` | Loads the hyphenated module as `fcu` and exposes it as a fixture + provides fake clients. |
| `scripts/python/tests/test_fetch_cluster_usage.py` | Unit tests for the pure logic. |
| `manifests/cluster-usage-cronjob.yaml` | ConfigMap (script) + ServiceAccount + ClusterRole/Binding + CronJob. |
| `scripts/README.md` | Doc entry for the new script. |

All Python code lands in the single script file; tasks add functions to it top-to-bottom. The script ends with a `main()` guarded by `if __name__ == "__main__":` so importing it for tests has no side effects.

---

## Task 0: Test harness scaffolding

**Files:**
- Create: `scripts/python/fetch-cluster-usage.py`
- Create: `scripts/python/tests/conftest.py`
- Create: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Create the script with a module docstring and main guard only**

`scripts/python/fetch-cluster-usage.py`:

```python
#!/usr/bin/env python3
"""Cluster usage report: configured CPU/mem limits & requests vs. real Thanos
usage, rolled up per namespace/workload/pod/container, plus an OOM-killed list
from live pod state + Thanos history. Self-contained (stdlib only); runs locally
via oc/kubectl and in-cluster as a CronJob. See scripts/README.md."""

import argparse
import csv
import json
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
from datetime import datetime, timezone


def main(argv=None):
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Create `conftest.py` that loads the hyphenated module**

`scripts/python/tests/conftest.py`:

```python
import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "fetch-cluster-usage.py"
_spec = importlib.util.spec_from_file_location("fetch_cluster_usage", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.fixture
def fcu():
    """The loaded fetch-cluster-usage module under test."""
    return _mod
```

- [ ] **Step 3: Create the test file with one smoke test**

`scripts/python/tests/test_fetch_cluster_usage.py`:

```python
def test_module_loads(fcu):
    assert hasattr(fcu, "main")
```

- [ ] **Step 4: Run the smoke test**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
chmod +x scripts/python/fetch-cluster-usage.py
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/conftest.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): scaffold fetch-cluster-usage.py + test harness"
```

---

## Task 1: Quantity parsing & formatting helpers

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (add after imports)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write failing tests**

Append to `test_fetch_cluster_usage.py`:

```python
import pytest


@pytest.mark.parametrize("s,expected", [
    ("100m", 0.1),
    ("1500m", 1.5),
    ("1", 1.0),
    ("2", 2.0),
    ("250000u", 0.25),
    ("500000000n", 0.5),
    (None, None),
    (2, 2.0),
])
def test_parse_cpu(fcu, s, expected):
    assert fcu.parse_cpu(s) == expected


@pytest.mark.parametrize("s,expected", [
    ("128Mi", 128 * 1024**2),
    ("1Gi", 1024**3),
    ("512Ki", 512 * 1024),
    ("64M", 64 * 10**6),
    (None, None),
    (1024, 1024),
])
def test_parse_mem(fcu, s, expected):
    assert fcu.parse_mem(s) == expected


def test_fmt_cores(fcu):
    assert fcu.fmt_cores(0.1) == "0.100"
    assert fcu.fmt_cores(None) == "-"


def test_fmt_bytes(fcu):
    assert fcu.fmt_bytes(1536) == "1.5Ki"
    assert fcu.fmt_bytes(None) == "-"


def test_fmt_pct(fcu):
    assert fcu.fmt_pct(95.0) == "95.0%"
    assert fcu.fmt_pct(None) == "-"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'parse_cpu'`.

- [ ] **Step 3: Implement the helpers**

Add to `fetch-cluster-usage.py` after the imports (before `main`):

```python
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
    for suf, mul in units.items():
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
    """Cores -> '0.100' style string. None -> '-'."""
    return "-" if v is None else f"{v:.3f}"


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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`
Expected: PASS (all parametrized cases green).

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): cpu/mem quantity parsing + formatting helpers"
```

---

## Task 2: Stage detection

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("ns,stage", [
    ("pid-001-shop-ref-01-blue", "ref"),
    ("pid-002-api-test-01-blue", "test"),
    ("pid-003-web-prod-01-blue", "prod"),
    ("pid-004-batch-phase-01-blue", "phase"),
    ("pid-005-cache-pnext-01-blue", "pnext"),
    ("pid-x-prod", "prod"),          # fallback: keyword in any segment
    ("kube-system", "other"),
    ("pid-007-noStage-here-99", "other"),
])
def test_detect_stage(fcu, ns, stage):
    assert fcu.detect_stage(ns) == stage
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k detect_stage -v`
Expected: FAIL (`no attribute 'detect_stage'`).

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k detect_stage -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): namespace stage detection"
```

---

## Task 3: Workload (owner) resolution

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write failing tests**

```python
def _pod(name, ns="ns1", owner=None, labels=None):
    md = {"name": name, "namespace": ns, "labels": labels or {}}
    if owner:
        md["ownerReferences"] = [owner]
    return {"metadata": md, "spec": {}, "status": {}}


def test_workload_for_deployment_via_rs(fcu):
    rs = {"metadata": {"name": "web-abc", "namespace": "ns1",
                       "ownerReferences": [{"kind": "Deployment", "name": "web",
                                            "controller": True}]}}
    rs_index = {("ns1", "web-abc"): rs}
    pod = _pod("web-abc-123", owner={"kind": "ReplicaSet", "name": "web-abc",
                                     "controller": True})
    assert fcu.workload_for(pod, rs_index) == ("Deployment", "web")


def test_workload_for_statefulset(fcu):
    pod = _pod("db-0", owner={"kind": "StatefulSet", "name": "db",
                              "controller": True})
    assert fcu.workload_for(pod, {}) == ("StatefulSet", "db")


def test_workload_for_orphan(fcu):
    assert fcu.workload_for(_pod("loose"), {}) == ("", "")
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k workload_for -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k workload_for -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): pod->workload owner resolution"
```

---

## Task 4: Rollup math (sums + util%)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

These are the core aggregation primitives. `sum_limit` returns `None` if **any**
contributor is unset (an unset limit = unlimited, so the total is unbounded).
`sum_usage` returns `None` only if **all** contributors are missing (no Thanos
data), otherwise the sum of present values.

- [ ] **Step 1: Write failing tests**

```python
def test_sum_limit_all_present(fcu):
    assert fcu.sum_limit([0.1, 0.2, 0.3]) == pytest.approx(0.6)


def test_sum_limit_any_missing_is_none(fcu):
    assert fcu.sum_limit([0.1, None, 0.3]) is None


def test_sum_limit_empty_is_none(fcu):
    assert fcu.sum_limit([]) is None


def test_sum_usage_partial(fcu):
    assert fcu.sum_usage([1.0, None, 2.0]) == pytest.approx(3.0)


def test_sum_usage_all_none(fcu):
    assert fcu.sum_usage([None, None]) is None


def test_util_pct(fcu):
    assert fcu.util_pct(0.95, 1.0) == pytest.approx(95.0)


def test_util_pct_no_limit_or_usage(fcu):
    assert fcu.util_pct(0.5, None) is None
    assert fcu.util_pct(None, 1.0) is None
    assert fcu.util_pct(0.5, 0) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "sum_ or util_pct" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "sum_ or util_pct" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): rollup sum + util% primitives"
```

---

## Task 5: Leaf record + rollup over a level

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

A "leaf" is one `(namespace, pod, container)` record. `rollup(leaves)` produces
the aggregate dict used at pod/workload/namespace levels. The field set is the
canonical schema used by every renderer.

- [ ] **Step 1: Write failing tests**

```python
def _leaf(**kw):
    base = dict(
        namespace="ns1", pod="p1", container="c1",
        cpu_request=0.05, cpu_limit=0.2, cpu_now=0.1, cpu_peak=0.15, cpu_avg=0.1,
        mem_request=64 * 1024**2, mem_limit=128 * 1024**2,
        mem_now=100 * 1024**2, mem_peak=120 * 1024**2,
        oom_count=0,
    )
    base.update(kw)
    return base


def test_rollup_sums_and_counts(fcu):
    leaves = [_leaf(container="c1"), _leaf(container="c2", cpu_limit=0.3,
                                           mem_limit=256 * 1024**2)]
    agg = fcu.rollup(leaves)
    assert agg["cpu_limit"] == pytest.approx(0.5)
    assert agg["mem_limit"] == 384 * 1024**2
    assert agg["cpu_now"] == pytest.approx(0.2)
    assert agg["container_count"] == 2
    # peak util uses summed peak / summed limit
    assert agg["cpu_peak_util_pct"] == pytest.approx(0.3 / 0.5 * 100)


def test_rollup_unset_limit_blocks_util(fcu):
    leaves = [_leaf(cpu_limit=None)]
    agg = fcu.rollup(leaves)
    assert agg["cpu_limit"] is None
    assert agg["cpu_peak_util_pct"] is None


def test_rollup_pod_count_distinct(fcu):
    leaves = [_leaf(pod="p1"), _leaf(pod="p1", container="c2"), _leaf(pod="p2")]
    agg = fcu.rollup(leaves)
    assert agg["pod_count"] == 2
    assert agg["container_count"] == 3
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k rollup -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
# Canonical numeric fields every level carries.
LIMIT_FIELDS = ("cpu_request", "cpu_limit", "mem_request", "mem_limit")
USAGE_FIELDS = ("cpu_now", "cpu_peak", "cpu_avg", "mem_now", "mem_peak")


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
    agg["cpu_peak_util_pct"] = util_pct(agg["cpu_peak"], agg["cpu_limit"])
    agg["mem_peak_util_pct"] = util_pct(agg["mem_peak"], agg["mem_limit"])
    agg["oom_count"] = sum(leaf.get("oom_count", 0) for leaf in leaves)
    agg["pod_count"] = len({(leaf["namespace"], leaf["pod"]) for leaf in leaves})
    agg["container_count"] = len(leaves)
    return agg
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k rollup -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): leaf rollup into level aggregate"
```

---

## Task 6: OOM merge & source tagging

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write failing tests**

```python
def test_merge_ooms_dedup_and_source(fcu):
    live = [{"namespace": "ns1", "pod": "p1", "container": "c1",
             "restart_count": 3, "finished_at": "2026-06-07T10:00:00Z",
             "exit_code": 137}]
    thanos = [
        {"namespace": "ns1", "pod": "p1", "container": "c1", "oom_events": 5},
        {"namespace": "ns1", "pod": "p2", "container": "c1", "oom_events": 1},
    ]
    merged = fcu.merge_ooms(live, thanos)
    by_key = {(o["namespace"], o["pod"], o["container"]): o for o in merged}
    assert by_key[("ns1", "p1", "c1")]["source"] == "both"
    assert by_key[("ns1", "p1", "c1")]["oom_events"] == 5
    assert by_key[("ns1", "p1", "c1")]["restart_count"] == 3
    assert by_key[("ns1", "p2", "c1")]["source"] == "thanos"


def test_merge_ooms_live_only(fcu):
    live = [{"namespace": "ns1", "pod": "p1", "container": "c1",
             "restart_count": 1}]
    merged = fcu.merge_ooms(live, [])
    assert merged[0]["source"] == "live"
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k merge_ooms -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k merge_ooms -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): merge live + thanos OOM records with source tags"
```

---

## Task 7: Extract leaves from pods (configured side)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Turns raw pod objects into leaf records carrying the **configured** fields and
workload labels. Usage fields are filled in later (Task 10) and default to None
here. Also extracts live OOM records.

- [ ] **Step 1: Write failing tests**

```python
def _pod_full(name, ns, containers, owner=None, node="n1", statuses=None):
    md = {"name": name, "namespace": ns, "labels": {}}
    if owner:
        md["ownerReferences"] = [owner]
    return {
        "metadata": md,
        "spec": {"nodeName": node, "containers": containers},
        "status": {"containerStatuses": statuses or []},
    }


def test_pods_to_leaves_configured(fcu):
    pods = [_pod_full(
        "web-abc-1", "ns1",
        containers=[{"name": "web", "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"}}}],
        owner={"kind": "ReplicaSet", "name": "web-abc", "controller": True},
    )]
    rs = {"metadata": {"name": "web-abc", "namespace": "ns1",
                       "ownerReferences": [{"kind": "Deployment", "name": "web",
                                            "controller": True}]}}
    leaves = fcu.pods_to_leaves(pods, {("ns1", "web-abc"): rs})
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf["workload_kind"] == "Deployment"
    assert leaf["workload"] == "web"
    assert leaf["cpu_limit"] == pytest.approx(0.2)
    assert leaf["mem_request"] == 64 * 1024**2
    assert leaf["cpu_now"] is None  # usage filled later
    assert leaf["oom_count"] == 0


def test_pods_to_leaves_no_resources(fcu):
    pods = [_pod_full("bare", "ns1", containers=[{"name": "c"}])]
    leaves = fcu.pods_to_leaves(pods, {})
    assert leaves[0]["cpu_limit"] is None
    assert leaves[0]["mem_request"] is None


def test_live_ooms_from_pods(fcu):
    statuses = [{"name": "c", "restartCount": 2, "lastState": {"terminated": {
        "reason": "OOMKilled", "exitCode": 137,
        "finishedAt": "2026-06-07T10:00:00Z"}}}]
    pods = [_pod_full("p1", "ns1", containers=[{"name": "c"}], statuses=statuses)]
    ooms = fcu.live_ooms_from_pods(pods)
    assert ooms == [{"namespace": "ns1", "pod": "p1", "container": "c",
                     "restart_count": 2, "exit_code": 137,
                     "finished_at": "2026-06-07T10:00:00Z"}]


def test_live_ooms_ignores_non_oom(fcu):
    statuses = [{"name": "c", "lastState": {"terminated": {"reason": "Error"}}}]
    pods = [_pod_full("p1", "ns1", containers=[{"name": "c"}], statuses=statuses)]
    assert fcu.live_ooms_from_pods(pods) == []
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "to_leaves or live_ooms" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "to_leaves or live_ooms" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): build configured leaves + live OOM records from pods"
```

---

## Task 8: Build the nested namespace tree

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Groups leaves into the namespace → workload → pod → container tree, computing a
rollup at each level. Pure function over leaves + merged OOM list.

- [ ] **Step 1: Write failing tests**

```python
def test_build_namespace_tree(fcu):
    leaves = [
        _leaf(namespace="ns1", pod="web-1", container="web"),
        _leaf(namespace="ns1", pod="web-2", container="web"),
    ]
    for leaf in leaves:
        leaf["workload_kind"] = "Deployment"
        leaf["workload"] = "web"
    ooms = [{"namespace": "ns1", "pod": "web-1", "container": "web",
             "source": "live"}]
    node = fcu.build_namespace_tree("ns1", leaves, ooms)
    assert node["namespace"] == "ns1"
    assert node["stage"] == "other"
    assert node["totals"]["container_count"] == 2
    assert len(node["workloads"]) == 1
    wl = node["workloads"][0]
    assert (wl["kind"], wl["name"]) == ("Deployment", "web")
    assert wl["totals"]["pod_count"] == 2
    assert len(wl["pods"]) == 2
    assert wl["pods"][0]["containers"][0]["container"] == "web"
    assert node["ooms"] == ooms
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k build_namespace_tree -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
# ----------------------------------------------------- nested tree assembly

def _workload_key(leaf):
    return (leaf.get("workload_kind") or "", leaf.get("workload") or leaf["pod"])


def build_namespace_tree(namespace, leaves, ooms):
    """namespace -> workloads -> pods -> containers, with a rollup at each level.

    leaves are this namespace's container records (usage already attached).
    ooms is the merged OOM list for this namespace.
    """
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k build_namespace_tree -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): assemble nested namespace->workload->pod->container tree"
```

---

## Task 9: Thanos query builders & usage parsing

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Build the PromQL strings and parse a Thanos vector response into a
`(pod, container) -> value` map. The HTTP client itself (Task 11) is thin; these
string/parse functions hold the logic worth testing.

- [ ] **Step 1: Write failing tests**

```python
def test_usage_queries_contain_expected_promql(fcu):
    q = fcu.usage_queries("ns1", "24h", "5m")
    assert q["cpu_now"] == (
        'sum by (pod, container) '
        '(rate(container_cpu_usage_seconds_total{namespace="ns1",container!=""}[5m]))'
    )
    assert "max_over_time" in q["cpu_peak"]
    assert "[24h:5m]" in q["cpu_peak"]
    assert q["mem_now"] == (
        'sum by (pod, container) '
        '(container_memory_working_set_bytes{namespace="ns1",container!=""})'
    )
    assert "max_over_time" in q["mem_peak"] and "[24h]" in q["mem_peak"]


def test_parse_vector_by_pod_container(fcu):
    payload = {"data": {"resultType": "vector", "result": [
        {"metric": {"pod": "p1", "container": "c1"}, "value": [0, "0.25"]},
        {"metric": {"pod": "p2", "container": "c1"}, "value": [0, "0.5"]},
    ]}}
    out = fcu.parse_vector_by_pod_container(payload)
    assert out == {("p1", "c1"): 0.25, ("p2", "c1"): 0.5}


def test_parse_vector_empty(fcu):
    assert fcu.parse_vector_by_pod_container({"data": {"result": []}}) == {}
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "usage_queries or parse_vector" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "usage_queries or parse_vector" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): thanos query builders + vector parsing"
```

---

## Task 10: Attach usage & OOM events onto leaves

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Merges the parsed Thanos maps onto the configured leaves, and produces the
Thanos-side OOM records + per-container oom_count.

- [ ] **Step 1: Write failing tests**

```python
def test_attach_usage(fcu):
    leaves = [_leaf(pod="p1", container="c1", cpu_now=None, cpu_peak=None,
                    mem_now=None, mem_peak=None, cpu_avg=None)]
    usage = {
        "cpu_now": {("p1", "c1"): 0.1},
        "cpu_peak": {("p1", "c1"): 0.18},
        "cpu_avg": {("p1", "c1"): 0.12},
        "mem_now": {("p1", "c1"): 100 * 1024**2},
        "mem_peak": {("p1", "c1"): 120 * 1024**2},
    }
    fcu.attach_usage(leaves, usage)
    assert leaves[0]["cpu_now"] == pytest.approx(0.1)
    assert leaves[0]["cpu_peak"] == pytest.approx(0.18)
    assert leaves[0]["mem_peak"] == 120 * 1024**2


def test_attach_usage_missing_series_stays_none(fcu):
    leaves = [_leaf(pod="p9", container="c1", cpu_now=None)]
    fcu.attach_usage(leaves, {"cpu_now": {}, "cpu_peak": {}, "cpu_avg": {},
                              "mem_now": {}, "mem_peak": {}})
    assert leaves[0]["cpu_now"] is None


def test_thanos_ooms_and_counts(fcu):
    events = {("p1", "c1"): 5.0, ("p2", "c1"): 0.0}
    leaves = [_leaf(namespace="ns1", pod="p1", container="c1", oom_count=0)]
    ooms = fcu.thanos_ooms("ns1", events)
    assert ooms == [{"namespace": "ns1", "pod": "p1", "container": "c1",
                     "oom_events": 5}]
    fcu.attach_oom_counts(leaves, fcu.merge_ooms([], ooms))
    assert leaves[0]["oom_count"] == 1
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "attach_ or thanos_ooms" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "attach_ or thanos_ooms" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): attach thanos usage + oom counts onto leaves"
```

---

## Task 11: Kubernetes client backends (REST + CLI)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Two backends behind a common surface. The REST URL building and CLI argument
construction are unit-tested with a fake transport; live calls are exercised
manually in Task 16.

- [ ] **Step 1: Write failing tests**

```python
def test_cli_client_builds_get_args(fcu):
    calls = []

    def fake_run(args):
        calls.append(args)
        return '{"items": [{"metadata": {"name": "x"}}]}'

    client = fcu.CliK8sClient(binary="kubectl", run=fake_run)
    items = client.list_pods("ns1")
    assert calls[0] == ["get", "pods", "-n", "ns1", "-o", "json"]
    assert items == [{"metadata": {"name": "x"}}]


def test_cli_client_all_namespaces(fcu):
    client = fcu.CliK8sClient(binary="oc",
                              run=lambda a: '{"items": []}')
    client.list_pods(None)  # no namespace -> -A
    # rebuild to capture args
    seen = []
    client2 = fcu.CliK8sClient(binary="oc",
                               run=lambda a: (seen.append(a) or '{"items": []}'))
    client2.list_deployments(None)
    assert seen[0] == ["get", "deployments", "-A", "-o", "json"]


def test_rest_client_builds_url(fcu):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return {"items": [{"metadata": {"name": "p"}}]}

    client = fcu.RestK8sClient(host="https://k8s:6443", token="t",
                               get_json=fake_get)
    client.list_pods("ns1")
    assert seen["url"] == "https://k8s:6443/api/v1/namespaces/ns1/pods"
    client.list_deployments(None)
    assert seen["url"] == "https://k8s:6443/apis/apps/v1/deployments"
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "cli_client or rest_client" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script. The `_RESOURCES` table maps the logical resource to its CLI
plural and REST API path (core `/api/v1` vs `/apis/apps/v1`).

```python
# ------------------------------------------------------------- k8s backends

# logical -> (cli_plural, api_group_path, is_namespaced)
_RESOURCES = {
    "pods":         ("pods",         "/api/v1",        True),
    "replicasets":  ("replicasets",  "/apis/apps/v1",  True),
    "deployments":  ("deployments",  "/apis/apps/v1",  True),
    "statefulsets": ("statefulsets", "/apis/apps/v1",  True),
    "daemonsets":   ("daemonsets",   "/apis/apps/v1",  True),
    "namespaces":   ("namespaces",   "/api/v1",        False),
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
        return self._list("namespaces", None)

    def list_pods(self, namespace=None):
        return self._list("pods", namespace)

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

    def list_replicasets(self, namespace=None):
        return self._list("replicasets", namespace)

    def list_deployments(self, namespace=None):
        return self._list("deployments", namespace)

    def list_statefulsets(self, namespace=None):
        return self._list("statefulsets", namespace)

    def list_daemonsets(self, namespace=None):
        return self._list("daemonsets", namespace)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "cli_client or rest_client" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): REST + CLI kubernetes client backends"
```

---

## Task 12: Backend & Thanos endpoint auto-detection

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Environment glue: pick REST vs CLI, pick the CLI binary (`oc` then `kubectl`),
read the SA token. The `Thanos` HTTP class itself is copied verbatim from
`fetch-thanos-metrics.py` (lines for `class Thanos`, `discover_querier`,
`start_port_forward`, `QUERIER_CANDIDATES`, `parse_time`) in Step 3.

- [ ] **Step 1: Write failing tests**

```python
def test_choose_backend_prefers_rest_in_cluster(fcu, monkeypatch, tmp_path):
    tok = tmp_path / "token"
    tok.write_text("abc")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    kind = fcu.choose_backend_kind(force_cli=False, force_rest=False,
                                   token_path=str(tok))
    assert kind == "rest"


def test_choose_backend_falls_back_to_cli(fcu, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    kind = fcu.choose_backend_kind(force_cli=False, force_rest=False,
                                   token_path="/nonexistent")
    assert kind == "cli"


def test_choose_backend_force_cli(fcu, monkeypatch, tmp_path):
    tok = tmp_path / "token"
    tok.write_text("abc")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert fcu.choose_backend_kind(force_cli=True, force_rest=False,
                                   token_path=str(tok)) == "cli"


def test_pick_cli_binary(fcu, monkeypatch):
    monkeypatch.setattr(fcu.shutil, "which",
                        lambda b: "/usr/bin/oc" if b == "oc" else None)
    assert fcu.pick_cli_binary(prefer_kubectl=False) == "oc"
    monkeypatch.setattr(fcu.shutil, "which",
                        lambda b: "/usr/bin/kubectl" if b == "kubectl" else None)
    assert fcu.pick_cli_binary(prefer_kubectl=False) == "kubectl"
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "choose_backend or pick_cli" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

First, copy the Thanos HTTP plumbing from `fetch-thanos-metrics.py` into the
script (paste these symbols unchanged, placing them above `main`):
`QUERIER_CANDIDATES`, `parse_time`, `parse_step`, `discover_querier`,
`start_port_forward`, `auto_token`, and `class Thanos`. (They are stdlib-only and
already battle-tested; do not re-implement.)

Then add the detection helpers:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "choose_backend or pick_cli" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): backend + cli-binary autodetection; import Thanos client"
```

---

## Task 13: Orchestrator — collect one namespace

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Ties the per-namespace pipeline together: list pods/rs, build leaves, query
Thanos (optional), attach usage + OOMs, build the tree. Thanos is injected so it
can be `None` (degrade) or a fake.

- [ ] **Step 1: Write failing tests**

Add a fake Thanos to `conftest.py`:

```python
class FakeThanos:
    """Returns canned instant-vector payloads keyed by substring of the query."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.available = True

    def query(self, expr, ts=None):
        for needle, result in self.responses.items():
            if needle in expr:
                return {"data": {"resultType": "vector", "result": result}}
        return {"data": {"resultType": "vector", "result": []}}


@pytest.fixture
def fake_thanos():
    return FakeThanos
```

Then tests in the test file:

```python
class FakeK8s:
    def __init__(self, pods=None, rs=None):
        self._pods = pods or []
        self._rs = rs or []

    def list_pods(self, namespace=None):
        return self._pods

    def list_replicasets(self, namespace=None):
        return self._rs

    def list_namespaces(self):
        return []


def test_collect_namespace_no_thanos(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "pid-001-shop-ref-01-blue",
                     "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"}}}]},
        "status": {"containerStatuses": []},
    }]
    node = fcu.collect_namespace(
        fcu_k8s=FakeK8s(pods=pods),
        namespace="pid-001-shop-ref-01-blue",
        thanos=None, window="24h", step="5m")
    assert node["stage"] == "ref"
    assert node["totals"]["cpu_limit"] == pytest.approx(0.2)
    assert node["totals"]["cpu_now"] is None  # no thanos
    assert node["workloads"][0]["pods"][0]["name"] == "web-1"


def test_collect_namespace_with_thanos(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"}}}]},
        "status": {"containerStatuses": []},
    }]
    from conftest import FakeThanos
    thanos = FakeThanos({
        "rate(container_cpu_usage_seconds_total": [
            {"metric": {"pod": "web-1", "container": "web"},
             "value": [0, "0.05"]}],
        "container_memory_working_set_bytes": [
            {"metric": {"pod": "web-1", "container": "web"},
             "value": [0, str(80 * 1024**2)]}],
        "container_oom_events_total": [
            {"metric": {"pod": "web-1", "container": "web"},
             "value": [0, "2"]}],
    })
    node = fcu.collect_namespace(fcu_k8s=FakeK8s(pods=pods), namespace="ns1",
                                 thanos=thanos, window="24h", step="5m")
    leaf = node["workloads"][0]["pods"][0]["containers"][0]
    assert leaf["cpu_now"] == pytest.approx(0.05)
    assert leaf["mem_now"] == 80 * 1024**2
    assert node["totals"]["oom_count"] == 1
    assert any(o["source"] in ("thanos", "both") for o in node["ooms"])
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k collect_namespace -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
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


def collect_namespace(fcu_k8s, namespace, thanos, window, step):
    """Full per-namespace pipeline -> nested namespace tree dict."""
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
    return build_namespace_tree(namespace, leaves, merged)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k collect_namespace -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): per-namespace collection orchestrator"
```

---

## Task 14: Renderers — CSV and JSON

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

CSV first (it forces the flat-row schema that the text renderer also uses). JSON
is just the namespace-tree list wrapped with metadata.

- [ ] **Step 1: Write failing tests**

```python
import io


def _sample_tree(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"},
                "requests": {"cpu": "50m", "memory": "64Mi"}}}]},
        "status": {"containerStatuses": []},
    }]

    class K:
        list_pods = lambda self, ns=None: pods
        list_replicasets = lambda self, ns=None: []
    return fcu.collect_namespace(fcu_k8s=K(), namespace="ns1", thanos=None,
                                 window="24h", step="5m")


def test_flatten_rows_one_per_level(fcu):
    rows = fcu.flatten_rows([_sample_tree(fcu)])
    levels = {r["level"] for r in rows}
    assert levels == {"namespace", "workload", "pod", "container"}
    container_row = next(r for r in rows if r["level"] == "container")
    assert container_row["namespace"] == "ns1"
    assert container_row["cpu_limit"] == pytest.approx(0.2)


def test_render_csv_has_header_and_rows(fcu):
    buf = io.StringIO()
    fcu.render_resources_csv([_sample_tree(fcu)], buf)
    text = buf.getvalue()
    assert text.splitlines()[0].startswith("level,stage,namespace")
    assert "container" in text


def test_render_json_structure(fcu):
    buf = io.StringIO()
    fcu.render_json([_sample_tree(fcu)], buf, window="24h", cluster="c1")
    obj = json.loads(buf.getvalue())
    assert obj["window"] == "24h"
    assert obj["cluster"] == "c1"
    assert obj["namespaces"][0]["namespace"] == "ns1"
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "flatten_rows or render_csv or render_json" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script:

```python
# ------------------------------------------------------------- flat rows / csv

CSV_COLUMNS = [
    "level", "stage", "namespace", "workload_kind", "workload", "pod",
    "container", "cpu_request", "cpu_limit", "cpu_now", "cpu_peak", "cpu_avg",
    "cpu_peak_util_pct", "mem_request", "mem_limit", "mem_now", "mem_peak",
    "mem_peak_util_pct", "oom_count", "pod_count", "container_count",
]


def _row_from_totals(level, stage, namespace, totals, **ids):
    row = {c: "" for c in CSV_COLUMNS}
    row.update({"level": level, "stage": stage, "namespace": namespace})
    row.update(ids)
    for k, v in totals.items():
        if k in row:
            row[k] = v
    return row


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


def render_resources_csv(trees, stream):
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in flatten_rows(trees):
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


def render_ooms_csv(trees, stream):
    cols = ["stage", "namespace", "pod", "container", "source", "oom_events",
            "restart_count", "exit_code", "finished_at"]
    writer = csv.DictWriter(stream, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for node in trees:
        for o in node["ooms"]:
            writer.writerow({"stage": node["stage"], **o})


def render_json(trees, stream, window, cluster):
    obj = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "cluster": cluster,
        "window": window,
        "namespaces": trees,
    }
    json.dump(obj, stream, indent=2)
    stream.write("\n")
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "flatten_rows or render_csv or render_json" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): CSV + JSON renderers and flat-row schema"
```

---

## Task 15: Renderer — text tables

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write failing tests**

```python
def test_render_text_contains_levels(fcu):
    buf = io.StringIO()
    fcu.render_text([_sample_tree(fcu)], buf, levels=("namespace", "workload",
                                                      "pod", "container"))
    out = buf.getvalue()
    assert "NAMESPACE  ns1" in out or "ns1" in out
    assert "CPU lim" in out
    assert "MEM lim" in out


def test_render_text_level_filter(fcu):
    buf = io.StringIO()
    fcu.render_text([_sample_tree(fcu)], buf, levels=("namespace",))
    out = buf.getvalue()
    # container sub-table header should not appear when only namespace requested
    assert "CONTAINERS" not in out
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k render_text -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add to the script. `_fmt_cell` turns a level dict into display strings; the
sub-table printers share `_print_table` with dynamic column widths.

```python
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
        fmt_cores(t.get("cpu_now")), fmt_cores(t.get("cpu_peak")),
        fmt_pct(t.get("cpu_peak_util_pct")),
        fmt_bytes(t.get("mem_request")), fmt_bytes(t.get("mem_limit")),
        fmt_bytes(t.get("mem_now")), fmt_bytes(t.get("mem_peak")),
        fmt_pct(t.get("mem_peak_util_pct")),
        t.get("oom_count", 0),
    ]


_USAGE_HEADERS = ["CPU req", "CPU lim", "CPU now", "CPU peak", "CPU %",
                  "MEM req", "MEM lim", "MEM now", "MEM peak", "MEM %", "OOM"]


def render_text(trees, stream, levels=("namespace", "workload", "pod",
                                       "container")):
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
            rows = [[f"{wl['kind']}/{wl['name']}", *_usage_cells(wl["totals"])]
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k render_text -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): text table renderer with per-level sub-tables"
```

---

## Task 16: CLI wiring & `main()`

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py`
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

Wire argparse, namespace selection, endpoint/token resolution, the collection
loop, and output dispatch. The argument parser and namespace-filter are unit
tested; the full `main()` is smoke-tested for `--help` and exercised live in
Task 18.

- [ ] **Step 1: Write failing tests**

```python
def test_select_namespaces_pattern(fcu):
    all_ns = [{"metadata": {"name": "pid-001-shop-ref-01-blue"}},
              {"metadata": {"name": "kube-system"}},
              {"metadata": {"name": "pid-002-api-test-01-blue"}}]

    class K:
        def list_namespaces(self):
            return all_ns
    names = fcu.select_namespaces(K(), pattern=r"^pid-", explicit=None,
                                  all_namespaces=False)
    assert names == ["pid-001-shop-ref-01-blue", "pid-002-api-test-01-blue"]


def test_select_namespaces_explicit(fcu):
    class K:
        def list_namespaces(self):
            raise AssertionError("must not be called when explicit given")
    assert fcu.select_namespaces(K(), pattern=r"^pid-", explicit=["a", "b"],
                                 all_namespaces=False) == ["a", "b"]


def test_build_parser_defaults(fcu):
    args = fcu.build_parser().parse_args([])
    assert args.pattern == "^pid-"
    assert args.window == "24h"
    assert args.step == "5m"
    assert args.level == "namespace,workload,pod,container"


def test_main_help_exits_zero(fcu):
    with pytest.raises(SystemExit) as e:
        fcu.main(["--help"])
    assert e.value.code == 0
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "select_namespaces or build_parser or main_help" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace the placeholder `main` with the parser, helpers, and real `main`:

```python
# ----------------------------------------------------------------------- CLI

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
                     choices=["text", "json", "csv"],
                     help="stdout format(s) (default: text).")
    out.add_argument("--output-dir",
                     help="Also write resources.csv, ooms.csv, report.json.")
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
        host = (f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:"
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
    k8s = _make_k8s(args)
    thanos = _make_thanos(args)
    namespaces = select_namespaces(k8s, args.pattern, args.namespace,
                                   args.all_namespaces)
    if not namespaces:
        sys.stderr.write("no matching namespaces.\n")
        return 0

    trees = [collect_namespace(k8s, ns, thanos, args.window, args.step)
             for ns in namespaces]

    formats = args.format or ["text"]
    levels = tuple(s.strip() for s in args.level.split(",") if s.strip())
    cluster = os.environ.get("KUBERNETES_SERVICE_HOST", "local")
    if "text" in formats:
        render_text(trees, sys.stdout, levels=levels)
    if "json" in formats:
        render_json(trees, sys.stdout, window=args.window, cluster=cluster)
    if "csv" in formats:
        render_resources_csv(trees, sys.stdout)

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        with open(os.path.join(args.output_dir, "resources.csv"), "w") as f:
            render_resources_csv(trees, f)
        with open(os.path.join(args.output_dir, "ooms.csv"), "w") as f:
            render_ooms_csv(trees, f)
        with open(os.path.join(args.output_dir, "report.json"), "w") as f:
            render_json(trees, f, window=args.window, cluster=cluster)
        sys.stderr.write(f"wrote reports to {args.output_dir}\n")
    return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "select_namespaces or build_parser or main_help" -v`
Expected: PASS.

- [ ] **Step 5: Run the full test suite**

Run: `python3 -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`
Expected: PASS (all tasks' tests green).

- [ ] **Step 6: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat(usage): CLI parser, endpoint resolution, main()"
```

---

## Task 17: CronJob + RBAC manifest

**Files:**
- Create: `manifests/cluster-usage-cronjob.yaml`

No automated test (declarative YAML); validated with `--dry-run` in Task 18.

- [ ] **Step 1: Write the manifest**

`manifests/cluster-usage-cronjob.yaml`:

```yaml
# Cluster-wide CronJob that runs fetch-cluster-usage.py against all pid-*
# namespaces and logs the report. The script is shipped via a ConfigMap so no
# image build / registry is needed -- the job runs stock python:3.12-slim.
#
# OpenShift (production) note: thanos-querier requires the ServiceAccount to be
# allowed to read metrics. Uncomment the cluster-monitoring-view binding below
# and set THANOS_URL to the querier route/service.
#
# RKE2 (local test): the kube-prometheus-stack Thanos/Prometheus service in the
# monitoring namespace typically needs no auth; set THANOS_URL accordingly or
# let auto-discovery handle it.
#
# Regenerate the ConfigMap after editing the script:
#   oc create configmap cluster-usage-script \
#     --from-file=fetch-cluster-usage.py=scripts/python/fetch-cluster-usage.py \
#     -n forensic-usage --dry-run=client -o yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: forensic-usage
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cluster-usage
  namespace: forensic-usage
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-usage-read
rules:
  - apiGroups: [""]
    resources: ["namespaces", "pods"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["replicasets", "deployments", "statefulsets", "daemonsets"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cluster-usage-read
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-usage-read
subjects:
  - kind: ServiceAccount
    name: cluster-usage
    namespace: forensic-usage
---
# OpenShift only: lets the SA token query thanos-querier. Harmless to omit on
# clusters where Thanos needs no auth (RKE2/KPS).
# apiVersion: rbac.authorization.k8s.io/v1
# kind: ClusterRoleBinding
# metadata:
#   name: cluster-usage-monitoring
# roleRef:
#   apiGroup: rbac.authorization.k8s.io
#   kind: ClusterRole
#   name: cluster-monitoring-view
# subjects:
#   - kind: ServiceAccount
#     name: cluster-usage
#     namespace: forensic-usage
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-usage-script
  namespace: forensic-usage
data:
  fetch-cluster-usage.py: |
    # PLACEHOLDER -- replace with the real script via:
    #   oc create configmap cluster-usage-script \
    #     --from-file=fetch-cluster-usage.py=scripts/python/fetch-cluster-usage.py \
    #     -n forensic-usage --dry-run=client -o yaml | oc apply -f -
    raise SystemExit("ConfigMap not populated; see manifest header")
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cluster-usage
  namespace: forensic-usage
spec:
  schedule: "0 6 * * *"   # daily 06:00; matches the default 24h window
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          serviceAccountName: cluster-usage
          restartPolicy: Never
          containers:
            - name: cluster-usage
              image: python:3.12-slim
              command: ["python3", "/app/fetch-cluster-usage.py"]
              args: ["--in-cluster", "--window", "24h", "--format", "text"]
              env:
                - name: THANOS_URL
                  value: ""   # set to your querier, e.g.
                              # http://thanos-querier.openshift-monitoring:9091
                              # (OpenShift) — add --insecure in args if a route
              volumeMounts:
                - name: script
                  mountPath: /app
              resources:
                requests: { cpu: 50m, memory: 128Mi }
                limits:   { cpu: 500m, memory: 256Mi }
          volumes:
            - name: script
              configMap:
                name: cluster-usage-script
```

- [ ] **Step 2: Validate YAML parses**

Run: `python3 -c "import sys; print('ok')" && python3 - <<'PY'
import pathlib
text = pathlib.Path("manifests/cluster-usage-cronjob.yaml").read_text()
assert "kind: CronJob" in text and "kind: ClusterRole" in text
assert text.count("---") >= 6
print("manifest structure ok")
PY`
Expected: `manifest structure ok`.

- [ ] **Step 3: Commit**

```bash
git add manifests/cluster-usage-cronjob.yaml
git commit -m "feat(usage): cluster-wide CronJob + RBAC manifest"
```

---

## Task 18: Live smoke test against the local RKE2 cluster

**Files:** none (manual verification). Skip if no cluster is reachable; note it
in the final report per the verification-before-completion skill.

- [ ] **Step 1: Confirm a cluster context**

Run: `kubectl config current-context`
Expected: your RKE2 context name (no error).

- [ ] **Step 2: Run against all pid-* namespaces, no Thanos (fast path)**

Run: `python3 scripts/python/fetch-cluster-usage.py --kubectl --no-thanos`
Expected: text tables per `pid-*` namespace with CPU/MEM req/lim columns
populated and usage columns `-`. No traceback.

- [ ] **Step 3: Run with Thanos auto-discovery**

Run: `python3 scripts/python/fetch-cluster-usage.py --kubectl --window 6h`
Expected: usage (`CPU now/peak`, `MEM now/peak`) populated where series exist;
`warn: Thanos unreachable ...` (and `-` usage) is acceptable if the local
querier isn't up.

- [ ] **Step 4: Write all formats to a dir**

Run: `python3 scripts/python/fetch-cluster-usage.py --kubectl --no-thanos --output-dir /tmp/usage && head -3 /tmp/usage/resources.csv && python3 -c "import json;json.load(open('/tmp/usage/report.json'));print('json ok')"`
Expected: CSV header + rows; `json ok`.

- [ ] **Step 5: Validate the CronJob manifest server-side (dry-run)**

Run: `kubectl apply --dry-run=server -f manifests/cluster-usage-cronjob.yaml`
Expected: each object reports `(server dry run)` with no schema errors. (If the
`forensic-usage` namespace doesn't exist yet, server dry-run of namespaced
objects may warn — client dry-run `--dry-run=client` is an acceptable fallback.)

- [ ] **Step 6: Record results**

No commit. Capture the actual command output in the completion summary. If any
step fails, fix the script (add a regression test first) before claiming done.

---

## Task 19: README documentation

**Files:**
- Modify: `scripts/README.md`

- [ ] **Step 1: Add the index row**

In `scripts/README.md`, add to the index table (after the `oom-rootcause.py`
row):

```markdown
| [`fetch-cluster-usage.py`](#fetch-cluster-usagepy) | Report configured CPU/memory **limits & requests vs. real Thanos usage** (current + peak over a window) rolled up at **namespace / workload / pod / container**, plus an **OOM-killed** list from live pod state + Thanos history. Runs locally (oc/kubectl) and in-cluster as a CronJob. |
```

- [ ] **Step 2: Add the section**

Append to `scripts/README.md`:

```markdown
---

## `fetch-cluster-usage.py`

Self-contained (stdlib-only) report of **limits vs. real usage** across every
`pid-*` namespace, with OOM-killed containers from both live pod state and
Thanos history. Auto-detects its environment: in-cluster it uses the Kubernetes
REST API with the pod ServiceAccount token; locally it shells out to `oc`/`kubectl`.

### What it reports

For each level — namespace, workload (Deployment/StatefulSet/DaemonSet), pod,
container — it shows CPU request/limit/now/peak (+peak-util%) and memory
request/limit/now/peak (+peak-util%), plus OOM counts. Usage comes from Thanos
(`container_cpu_usage_seconds_total` rate, `container_memory_working_set_bytes`,
current + `*_over_time` peak/avg over `--window`).

### Usage

```bash
# Local (RKE2, kubectl), all pid-* namespaces, last 24h
python3 scripts/python/fetch-cluster-usage.py --kubectl

# Local without Thanos (configured limits + live OOM only)
python3 scripts/python/fetch-cluster-usage.py --kubectl --no-thanos

# Explicit Thanos URL + token (e.g. OpenShift querier)
python3 scripts/python/fetch-cluster-usage.py \
  --thanos-url https://thanos-querier-openshift-monitoring.apps.example.com \
  --insecure --window 7d

# One namespace, write CSV/JSON to a directory
python3 scripts/python/fetch-cluster-usage.py --namespace pid-002-api-test-01-blue \
  --output-dir ./reports/usage

# Trim text verbosity to just rollups
python3 scripts/python/fetch-cluster-usage.py --level namespace,workload
```

### In-cluster (CronJob)

`manifests/cluster-usage-cronjob.yaml` ships a cluster-wide CronJob (ServiceAccount
+ ClusterRole/Binding + ConfigMap-delivered script on `python:3.12-slim`).
Populate the ConfigMap from the real script and set `THANOS_URL`:

```bash
oc create configmap cluster-usage-script \
  --from-file=fetch-cluster-usage.py=scripts/python/fetch-cluster-usage.py \
  -n forensic-usage --dry-run=client -o yaml | oc apply -f -
oc apply -f manifests/cluster-usage-cronjob.yaml
```

On OpenShift, uncomment the `cluster-monitoring-view` binding so the SA token is
accepted by `thanos-querier`. On RKE2/kube-prometheus-stack, Thanos in-cluster
usually needs no auth.

### Requirements

- Python 3.6+ (stdlib only)
- Local: `oc` or `kubectl` (`--kubectl`) with an active session; RBAC `get`/`list`
  on pods, replicasets, deployments, statefulsets, daemonsets, namespaces.
- In-cluster: the ServiceAccount RBAC from the manifest.
- A reachable Thanos/Prometheus query API for usage columns (optional; degrades
  to `-` when absent).
```

- [ ] **Step 2 check: parse the README still has the table**

Run: `grep -c "fetch-cluster-usage.py" scripts/README.md`
Expected: `>= 2` (index row + section).

- [ ] **Step 3: Commit**

```bash
git add scripts/README.md
git commit -m "docs(usage): document fetch-cluster-usage.py"
```

---

## Final verification

- [ ] Run the whole suite: `python3 -m pytest scripts/python/tests/ -v` → all PASS.
- [ ] `python3 scripts/python/fetch-cluster-usage.py --help` prints usage, exit 0.
- [ ] Confirm the live smoke results from Task 18 are recorded (or explicitly noted as skipped because no cluster was reachable).
- [ ] `git log --oneline` shows one commit per task.

---

## Self-review notes (author)

- **Spec coverage:** dual backend (T11–12), namespace discovery/scope (T16),
  configured limits (T7), Thanos usage current+peak+avg (T9–10), four-level
  rollups (T5,T8), OOM both-source merge (T6,T10,T13), text/CSV/JSON (T14–15),
  CronJob+RBAC manifest (T17), prod-OpenShift/local-RKE2 split (T12,T16,T17,T19),
  tests (every task). All spec sections map to a task.
- **No placeholders:** every code step has complete, runnable code; the only
  literal "PLACEHOLDER" is the ConfigMap script body, intentionally replaced by
  the documented `oc create configmap --from-file` command.
- **Type/name consistency:** leaf field names (`cpu_now`, `mem_peak`,
  `oom_count`, `workload_kind`/`workload`), `rollup()` output keys, `CSV_COLUMNS`,
  and the renderer accessors all use the same identifiers across tasks.
```
