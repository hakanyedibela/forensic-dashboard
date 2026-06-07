#!/usr/bin/env python3
"""Cluster usage report: configured CPU/mem limits & requests vs. real Thanos
usage, rolled up per namespace/workload/pod/container, plus an OOM-killed list
from live pod state + Thanos history. Self-contained (stdlib only); runs locally
via oc/kubectl and in-cluster as a CronJob. See scripts/README.md."""

import argparse
import atexit
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


_DUR_RE = re.compile(r"^\s*(-?\d+)\s*([smhdw])\s*$")


def parse_time(value, *, now=None):
    """Accept 'now', RFC3339, unix seconds, or a relative offset like '-1h'."""
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "now"):
        return (now or time.time())
    m = _DUR_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return (now or time.time()) + n * mult
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        raise SystemExit(f"error: cannot parse time: {value!r}")


def parse_step(value):
    """Step as Prometheus duration string ('30s', '1m', '5m') or seconds."""
    s = str(value).strip()
    if _DUR_RE.match(s):
        return s
    try:
        return str(int(float(s)))
    except ValueError:
        raise SystemExit(f"error: cannot parse step: {value!r}")


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


if __name__ == "__main__":
    sys.exit(main())
