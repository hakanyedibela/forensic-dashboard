#!/usr/bin/env python3
"""Per-pod OOM root-cause report: spec, workload, node, neighbors, events, PVCs, HPA/VPA,
network/storage signal, pre-OOM logs, and a verdict (A/B/C/D/E pattern). See scripts/README.md."""

import argparse
import json
import re
import shutil
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CLI = "oc"


# ---------------------------------------------------------------- subprocess

def _exec(args, check):
    proc = subprocess.run([CLI, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    if check and proc.returncode != 0:
        sys.stderr.write(f"command failed: {CLI} {' '.join(args)}\n{proc.stderr}\n")
        sys.exit(proc.returncode)
    return proc


def run(*args, check=True):
    return _exec(list(args), check).stdout


def get_json(*args):
    raw = run(*args, check=False)
    if not raw.strip():
        return {"items": []}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"items": []}


def current_namespace():
    if CLI == "oc":
        out = subprocess.run([CLI, "project", "-q"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True).stdout.strip()
        if out:
            return out
    out = subprocess.run([CLI, "config", "view", "--minify",
                          "-o", "jsonpath={..namespace}"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True).stdout.strip()
    return out or "default"


# ------------------------------------------------------------ misc helpers

def parse_iso(ts):
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1]
    elif len(s) >= 6 and s[-3] == ":" and s[-6] in "+-":
        s = s[:-6]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fmt_age(ts):
    when = parse_iso(ts)
    if not when:
        return "?"
    s = int((datetime.now(timezone.utc) - when).total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def parse_mem(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
             "K": 1000, "M": 10**6, "G": 10**9, "T": 10**12}
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


def fmt_bytes(n):
    if n is None:
        return "-"
    v = float(n)
    for unit in ["B", "Ki", "Mi", "Gi", "Ti"]:
        if v < 1024:
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}Pi"


def fmt_rate_bps(n):
    return "-" if n is None else f"{fmt_bytes(n)}/s"


# ------------------------------------------------------------ Prometheus

class Prom:
    def __init__(self, base_url, token=None, insecure=False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.available = False

    def _request(self, path):
        req = urllib.request.Request(f"{self.base_url}{path}")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, context=self.ctx, timeout=15) as r:
            return json.load(r)

    def query(self, expr, ts):
        try:
            payload = self._request(
                "/api/v1/query?" +
                urllib.parse.urlencode({"query": expr, "time": str(int(ts))}))
        except Exception:
            return None
        if payload.get("status") != "success":
            return None
        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return None

    def probe(self):
        try:
            payload = self._request(
                "/api/v1/query?" + urllib.parse.urlencode({"query": "1"}))
            self.available = (payload.get("status") == "success")
        except Exception:
            self.available = False
        return self.available


# Each entry probes a PromQL expression and attributes it to a scrape source.
# Optional fields:
#   bare     — bare metric name to probe if the selector returns nothing
#              (lets us distinguish "metric family exists, 0 series with this label
#              filter" from "metric truly missing"). Useful for {reason="OOMKilled"}
#              probes that are empty when no container has actually OOMed recently.
#   dropped_by_default_kps — kube-prometheus-stack's stock cAdvisor ServiceMonitor
#              has metric_relabel_configs that drop these specific names. Showing
#              them as "missing" misleads the user into thinking the scrape is
#              broken; mark them so the verdict is "dropped (kps relabel) — OK,
#              fallback handles it" instead.
DIAGNOSTIC_METRICS = [
    {"label": "Memory working set (cAdvisor)",
     "query": "container_memory_working_set_bytes",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "Memory working set (recording rule)",
     "query": "node_namespace_pod_container:container_memory_working_set_bytes",
     "source": "kube-prometheus-stack recording rules"},
    {"label": "Memory RSS (cAdvisor)",
     "query": "container_memory_rss",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "Memory limit (kube-state-metrics)",
     "query": 'kube_pod_container_resource_limits{resource="memory"}',
     "bare":  "kube_pod_container_resource_limits",
     "source": "kube-state-metrics deployment"},
    {"label": "Memory limit (cAdvisor fallback)",
     "query": "container_spec_memory_limit_bytes",
     "source": "kubelet /metrics/cadvisor scrape",
     "dropped_by_default_kps": True},
    {"label": "CPU usage counter (cAdvisor)",
     "query": "container_cpu_usage_seconds_total",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "CPU throttle counter (cAdvisor)",
     "query": "container_cpu_cfs_throttled_seconds_total",
     "source": "kubelet /metrics/cadvisor scrape",
     "dropped_by_default_kps": True},
    {"label": "Network rx (cAdvisor)",
     "query": "container_network_receive_bytes_total",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "Network tx (cAdvisor)",
     "query": "container_network_transmit_bytes_total",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "FS reads (cAdvisor)",
     "query": "container_fs_reads_bytes_total",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "FS writes (cAdvisor)",
     "query": "container_fs_writes_bytes_total",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "OOM events counter (cAdvisor)",
     "query": "container_oom_events_total",
     "source": "kubelet /metrics/cadvisor scrape"},
    {"label": "Pod terminated reason (KSM)",
     "query": 'kube_pod_container_status_terminated_reason{reason="OOMKilled"}',
     "bare":  "kube_pod_container_status_terminated_reason",
     "source": "kube-state-metrics deployment"},
    {"label": "Pod last terminated reason (KSM)",
     "query": 'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}',
     "bare":  "kube_pod_container_status_last_terminated_reason",
     "source": "kube-state-metrics deployment"},
]


def diagnose_metrics(prom):
    """Probe Prometheus for every metric this script depends on. Distinguishes:
       - OK                                    — selector returns N series
       - family present, 0 with selector       — bare metric exists but no series match the filter
       - dropped (kps relabel) — OK            — kps' default cAdvisor relabel drops this name
       - missing                               — neither selector nor bare returns anything
    A scrape source is reported as 'truly missing' only when every metric it
    provides is in the 'missing' state — a single OK or 'family present' entry
    proves the source is up."""
    import time as _time
    print(f"# Probing Prometheus at {prom.base_url} for OOM-relevant metrics.\n")

    width = max(len(m["query"]) for m in DIAGNOSTIC_METRICS)
    now = _time.time()
    rows = []
    source_state = {}  # source -> {"any_ok": bool, "any_missing": bool}

    for entry in DIAGNOSTIC_METRICS:
        v = prom.query(f"count({entry['query']})", now)
        bare_v = None
        if v is None and entry.get("bare"):
            bare_v = prom.query(f"count({entry['bare']})", now)

        if v is not None and v > 0:
            kind, status = "ok", f"OK ({int(v)} series)"
        elif bare_v is not None and bare_v > 0:
            kind, status = "family", f"family present ({int(bare_v)} series), 0 with selector"
        elif entry.get("dropped_by_default_kps"):
            kind, status = "dropped", "dropped (kps default relabel) — OK, fallback handles it"
        else:
            kind, status = "missing", "missing"

        rows.append((entry, kind, status))
        src = entry["source"]
        st = source_state.setdefault(src, {"any_ok": False, "any_missing": False})
        if kind in ("ok", "family"):
            st["any_ok"] = True
        if kind == "missing":
            st["any_missing"] = True

    for entry, _kind, status in rows:
        print(f"  {entry['query']:<{width}}  {status:<58}  # {entry['label']}")
    print()

    truly_missing = sorted(
        src for src, st in source_state.items()
        if st["any_missing"] and not st["any_ok"]
    )
    if not truly_missing:
        print("All required scrape sources are healthy.")
        print()
        print("If the rootcause report still shows '-' for some columns, the cause is one of:")
        print("  - no current series match {reason=\"OOMKilled\"} (nothing has OOMed recently — normal)")
        print("  - a couple of cAdvisor metrics dropped by kube-prometheus-stack's default relabel")
        print("    rules (container_spec_*, container_cpu_cfs_throttled_seconds_total) — the script's")
        print("    fallback chain already routes around these")
        print("  - label mismatch — your scrape may rename `pod` → `pod_name` or drop `container`.")
        print(f"    Verify with: curl -sG '{prom.base_url}/api/v1/series' \\")
        print("      --data-urlencode 'match[]=container_memory_working_set_bytes{namespace=\"<ns>\"}'")
        return

    print("Truly missing scrape sources:")
    for src in truly_missing:
        print(f"  - {src}")
    print()
    print("Install hints (see scripts/README.md → oom-rootcause.py for more):")
    if "kube-state-metrics deployment" in truly_missing:
        print("  helm upgrade --install kube-state-metrics \\")
        print("    prometheus-community/kube-state-metrics -n monitoring \\")
        print("    --set prometheus.monitor.enabled=true")
    if "kubelet /metrics/cadvisor scrape" in truly_missing:
        print("  Add a ServiceMonitor scraping kubelet :10250/metrics/cadvisor — easiest")
        print("  is to install the full kube-prometheus-stack from this repo:")
        print("    ./install.sh")
    if "kube-prometheus-stack recording rules" in truly_missing:
        print("  (only an issue if the cAdvisor scrape is also missing — recording rules")
        print("   are derived from cAdvisor metrics; the script's fallback chain means")
        print("   either of the two is enough)")


def auto_token():
    if CLI != "oc":
        return None
    try:
        proc = subprocess.run(["oc", "whoami", "-t"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        return proc.stdout.strip() or None
    except FileNotFoundError:
        return None


def metric_first(prom, candidates, ts):
    """Try expressions in order, return first non-None value."""
    for expr in candidates:
        v = prom.query(expr, ts)
        if v is not None:
            return v
    return None


# ------------------------------------------------------------ bulk fetch

def fetch_cluster_data(ns_args):
    """One-shot bulk fetch. Cluster-scoped resources (`nodes`) are always all-namespaces."""
    return {
        "pods": get_json("get", "pods", *ns_args, "-o", "json").get("items", []),
        "rs": get_json("get", "rs", *ns_args, "-o", "json").get("items", []),
        "deploys": get_json("get", "deploy", *ns_args, "-o", "json").get("items", []),
        "statefulsets": get_json("get", "statefulset", *ns_args, "-o", "json").get("items", []),
        "daemonsets": get_json("get", "daemonset", *ns_args, "-o", "json").get("items", []),
        "events": get_json("get", "events", *ns_args, "-o", "json").get("items", []),
        "services": get_json("get", "svc", *ns_args, "-o", "json").get("items", []),
        "pvcs": get_json("get", "pvc", *ns_args, "-o", "json").get("items", []),
        "hpas": get_json("get", "hpa", *ns_args, "-o", "json").get("items", []),
        "vpas": get_json("get", "vpa", *ns_args, "-o", "json").get("items", []),
        "limitranges": get_json("get", "limitrange", *ns_args, "-o", "json").get("items", []),
        "resourcequotas": get_json("get", "resourcequota", *ns_args, "-o", "json").get("items", []),
        "networkpolicies": get_json("get", "networkpolicy", *ns_args, "-o", "json").get("items", []),
        "nodes": get_json("get", "nodes", "-o", "json").get("items", []),
    }


# --------------------------------------------------- relationship resolution

def workload_for(pod, rs_index):
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
                        return o2.get("kind", "ReplicaSet"), o2.get("name", name), rs
            return kind, name, rs
        return kind, name, None
    return "", "", None


def services_targeting(pod, all_services):
    pod_labels = pod.get("metadata", {}).get("labels", {}) or {}
    matched = []
    for svc in all_services:
        if svc["metadata"]["namespace"] != pod["metadata"]["namespace"]:
            continue
        sel = svc.get("spec", {}).get("selector") or {}
        if not sel:
            continue
        if all(pod_labels.get(k) == v for k, v in sel.items()):
            matched.append(svc)
    return matched


def pvcs_for_pod(pod, all_pvcs):
    refs = []
    for vol in pod.get("spec", {}).get("volumes", []) or []:
        claim = vol.get("persistentVolumeClaim") or {}
        if claim.get("claimName"):
            refs.append(claim["claimName"])
    pvc_index = {(p["metadata"]["namespace"], p["metadata"]["name"]): p for p in all_pvcs}
    return [pvc_index[(pod["metadata"]["namespace"], n)]
            for n in refs if (pod["metadata"]["namespace"], n) in pvc_index]


def hpa_for_workload(kind, name, ns, all_hpas):
    for h in all_hpas:
        if h["metadata"]["namespace"] != ns:
            continue
        target = h.get("spec", {}).get("scaleTargetRef") or {}
        if target.get("kind") == kind and target.get("name") == name:
            return h
    return None


def vpa_for_workload(kind, name, ns, all_vpas):
    for v in all_vpas:
        if v["metadata"]["namespace"] != ns:
            continue
        target = v.get("spec", {}).get("targetRef") or {}
        if target.get("kind") == kind and target.get("name") == name:
            return v
    return None


def network_policies_for_pod(pod, all_nps):
    """NetworkPolicies whose podSelector matches this pod's labels."""
    pod_labels = pod.get("metadata", {}).get("labels", {}) or {}
    matched = []
    for np in all_nps:
        if np["metadata"]["namespace"] != pod["metadata"]["namespace"]:
            continue
        sel = (np.get("spec", {}).get("podSelector") or {}).get("matchLabels") or {}
        if sel and all(pod_labels.get(k) == v for k, v in sel.items()):
            matched.append(np)
        elif not sel:
            matched.append(np)  # empty selector targets all pods
    return matched


def neighbor_ooms_on_node(node_name, target_pod, target_finished_at, all_pods, window_s=3600):
    when = parse_iso(target_finished_at)
    if not when:
        return []
    out = []
    for pod in all_pods:
        if pod["metadata"]["name"] == target_pod:
            continue
        if pod.get("spec", {}).get("nodeName") != node_name:
            continue
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            last = (cs.get("lastState") or {}).get("terminated")
            if not last or last.get("reason") != "OOMKilled":
                continue
            ts = parse_iso(last.get("finishedAt"))
            if ts and abs((when - ts).total_seconds()) <= window_s:
                out.append({
                    "pod": pod["metadata"]["name"],
                    "container": cs["name"],
                    "finished_at": last.get("finishedAt"),
                })
    return out


def events_for_pod(pod_name, ns, all_events, since_seconds=3600):
    cutoff = datetime.now(timezone.utc).timestamp() - since_seconds
    out = []
    for ev in all_events:
        if ev.get("metadata", {}).get("namespace") != ns:
            continue
        obj = ev.get("involvedObject") or {}
        if obj.get("kind") != "Pod" or obj.get("name") != pod_name:
            continue
        ts = parse_iso(ev.get("lastTimestamp") or ev.get("eventTime"))
        if ts and ts.timestamp() < cutoff:
            continue
        out.append({
            "type": ev.get("type", ""),
            "reason": ev.get("reason", ""),
            "message": (ev.get("message") or "").strip(),
            "ts": ev.get("lastTimestamp") or ev.get("eventTime"),
            "count": ev.get("count", 1),
        })
    out.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return out


def fetch_logs(ns, pod, container, tail=30, grep_re=None):
    proc = subprocess.run(
        [CLI, "logs", pod, "-c", container, "-n", ns, "--previous", f"--tail={tail}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True)
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if grep_re:
        out = "\n".join(ln for ln in out.splitlines() if grep_re.search(ln))
    return out


def find_oom_targets(cluster):
    """Pods whose lastState.terminated.reason == OOMKilled."""
    rs_index = {(rs["metadata"]["namespace"], rs["metadata"]["name"]): rs
                for rs in cluster["rs"]}
    targets = []
    for pod in cluster["pods"]:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        wl_kind, wl_name, rs = workload_for(pod, rs_index)
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            last = (cs.get("lastState") or {}).get("terminated")
            if not last or last.get("reason") != "OOMKilled":
                continue
            targets.append({
                "namespace": ns,
                "pod": name,
                "container": cs["name"],
                "node": pod.get("spec", {}).get("nodeName", "-"),
                "workload_kind": wl_kind,
                "workload_name": wl_name,
                "replicaset": rs["metadata"]["name"] if rs else None,
                "rs_revision": (rs["metadata"].get("annotations", {}) or {}).get(
                    "deployment.kubernetes.io/revision") if rs else None,
                "pod_obj": pod,
                "container_status": cs,
                "exit_code": last.get("exitCode"),
                "finished_at": last.get("finishedAt"),
                "started_at": last.get("startedAt"),
                "restart_count": cs.get("restartCount", 0),
            })
    targets.sort(key=lambda t: t["finished_at"] or "", reverse=True)
    return targets


# ------------------------------------------------------- per-target enrich

def container_spec(pod, container_name):
    for c in pod.get("spec", {}).get("containers", []) or []:
        if c["name"] == container_name:
            return c
    return {}


def metric_summary(prom, ns, pod, container, finished_at):
    if not prom or not prom.available:
        return None
    when = parse_iso(finished_at)
    if not when:
        return None
    ts = when.timestamp()
    sel = f'namespace="{ns}",pod="{pod}",container="{container}"'
    sel_lim = sel + ',resource="memory"'
    sel_pod = f'namespace="{ns}",pod="{pod}"'
    return {
        "wss_at": metric_first(prom, [
            f'max by (namespace,pod,container) (container_memory_working_set_bytes{{{sel}}})',
            f'max by (namespace,pod,container) (node_namespace_pod_container:container_memory_working_set_bytes{{{sel}}})',
        ], ts),
        "wss_peak_5m": metric_first(prom, [
            f'max by (namespace,pod,container) (max_over_time(container_memory_working_set_bytes{{{sel}}}[5m]))',
            f'max by (namespace,pod,container) (max_over_time(node_namespace_pod_container:container_memory_working_set_bytes{{{sel}}}[5m]))',
        ], ts),
        "wss_peak_1h": metric_first(prom, [
            f'max by (namespace,pod,container) (max_over_time(container_memory_working_set_bytes{{{sel}}}[1h]))',
        ], ts),
        "deriv_1h": metric_first(prom, [
            f'max by (namespace,pod,container) (deriv(container_memory_working_set_bytes{{{sel}}}[1h]))',
        ], ts),
        "limit": metric_first(prom, [
            f'max by (namespace,pod,container) (kube_pod_container_resource_limits{{{sel_lim}}})',
            f'max by (namespace,pod,container) (container_spec_memory_limit_bytes{{{sel}}})',
        ], ts),
        "cpu_rate_5m": metric_first(prom, [
            f'max by (namespace,pod,container) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))',
        ], ts),
        "cpu_throttle_5m": metric_first(prom, [
            f'sum by (namespace,pod,container) (rate(container_cpu_cfs_throttled_seconds_total{{{sel}}}[5m]))',
        ], ts),
        "rx_5m": metric_first(prom, [
            f'sum by (pod) (max without (prometheus_replica) (rate(container_network_receive_bytes_total{{{sel_pod}}}[5m])))',
        ], ts),
        "tx_5m": metric_first(prom, [
            f'sum by (pod) (max without (prometheus_replica) (rate(container_network_transmit_bytes_total{{{sel_pod}}}[5m])))',
        ], ts),
        "rx_1m": metric_first(prom, [
            f'sum by (pod) (max without (prometheus_replica) (rate(container_network_receive_bytes_total{{{sel_pod}}}[1m])))',
        ], ts),
        "rx_baseline_30m": metric_first(prom, [
            f'sum by (pod) (max without (prometheus_replica) (avg_over_time(rate(container_network_receive_bytes_total{{{sel_pod}}}[5m])[30m:5m])))',
        ], ts),
        "fs_writes_5m": metric_first(prom, [
            f'sum by (pod) (max without (prometheus_replica) (rate(container_fs_writes_bytes_total{{{sel_pod}}}[5m])))',
        ], ts),
        "fs_reads_5m": metric_first(prom, [
            f'sum by (pod) (max without (prometheus_replica) (rate(container_fs_reads_bytes_total{{{sel_pod}}}[5m])))',
        ], ts),
    }


# -------------------------------------------------------------- diagnosis

LEAK_THRESHOLD_BPS = 1 * 1024 * 1024 / 60  # ~17.5 KiB/s ≈ +1 MiB/min


def diagnose(target, metrics, neighbors_on_node):
    """Apply A/B/C/D/E heuristics in priority order. Returns (label, evidence, fixes)."""
    started = parse_iso(target.get("started_at"))
    finished = parse_iso(target.get("finished_at"))
    lifetime_s = int((finished - started).total_seconds()) if (started and finished) else None

    if metrics:
        wss_peak = metrics.get("wss_peak_1h") or metrics.get("wss_peak_5m") or metrics.get("wss_at")
        limit = metrics.get("limit")
        deriv = metrics.get("deriv_1h")
        rx_1m = metrics.get("rx_1m")
        rx_baseline = metrics.get("rx_baseline_30m")
        peak_ratio = (wss_peak / limit) if (wss_peak and limit) else None
    else:
        wss_peak = limit = deriv = rx_1m = rx_baseline = peak_ratio = None

    evidence = []

    # E - startup overrun: short-lived + many restarts
    if lifetime_s is not None and lifetime_s < 60 and target["restart_count"] >= 2:
        evidence.append(f"pod lifetime before OOM: {lifetime_s}s (very short)")
        evidence.append(f"restart count: {target['restart_count']} (rapidly looping)")
        return ("E - STARTUP OVERRUN", evidence, [
            "App can't initialize within the memory limit. Common causes:",
            "  - JVM -Xmx larger than the container limit",
            "  - Loading a large ML model on startup",
            "  - Pre-warming a big in-memory cache",
            f"Fix: {CLI} logs {target['pod']} -c {target['container']} --previous "
            "→ look for OutOfMemoryError on startup.",
            "Set -XX:MaxRAMPercentage=75 (JVM) or raise the limit ≥ startup peak.",
        ])

    # D - node pressure / noisy neighbor
    if neighbors_on_node and len(neighbors_on_node) >= 2:
        evidence.append(f"{len(neighbors_on_node)} other pods OOMed on node "
                        f"{target['node']} within 1h:")
        for n in neighbors_on_node[:3]:
            evidence.append(f"  - {n['pod']}/{n['container']} at {n['finished_at']}")
        return ("D - NODE PRESSURE / NOISY NEIGHBOR", evidence, [
            "Multiple pods OOMing on the same node — investigate node memory commitment.",
            f"  {CLI} describe node {target['node']} | grep -A5 'Allocated resources'",
            f"  {CLI} top pods -A --sort-by=memory --field-selector=spec.nodeName={target['node']}",
            "Likely a missing limit on a neighbor, or kernel/system OOM:",
            f"  ssh {target['node']} sudo dmesg -T | grep -i 'killed process'",
        ])

    # A - leak
    if (deriv is not None and deriv > LEAK_THRESHOLD_BPS
            and peak_ratio is not None and peak_ratio >= 0.85):
        evidence.append(f"memory deriv (1h): +{deriv * 60 / (1024*1024):.1f} MiB/min sustained "
                        "→ leak signature")
        evidence.append(f"peak/limit: {peak_ratio*100:.1f}% — limit is the ceiling, not the cause")
        return ("A - MEMORY LEAK", evidence, [
            "Memory grows steadily without releasing — limit reached → OOM → restart → repeat.",
            "Capture a heap dump while memory is high (BEFORE the OOM):",
            f"  {CLI} exec {target['pod']} -c {target['container']} -- "
            "jcmd 1 GC.heap_dump /tmp/heap.hprof   # JVM",
            f"  {CLI} exec {target['pod']} -c {target['container']} -- "
            "curl localhost:6060/debug/pprof/heap > heap.out   # Go",
            "Don't just raise the limit — the leak will eat any new ceiling.",
        ])

    # B - request burst / spike
    if (rx_1m is not None and rx_baseline is not None and rx_baseline > 0
            and rx_1m / rx_baseline >= 3.0):
        evidence.append(
            f"network rx burst: {fmt_rate_bps(rx_1m)} in last 1m vs "
            f"{fmt_rate_bps(rx_baseline)} baseline → {rx_1m/rx_baseline:.1f}× spike")
        if peak_ratio is not None:
            evidence.append(f"peak/limit at OOM: {peak_ratio*100:.1f}%")
        return ("B - SPIKE / LARGE REQUEST", evidence, [
            "A traffic burst preceded the OOM — likely a large request body buffered in memory.",
            "Common culprits: file upload, paginate-without-limit, N+1 DB query, retry storms.",
            f"Correlate with logs at {target['finished_at']}:",
            f"  {CLI} logs {target['pod']} -c {target['container']} --previous "
            "| grep -iE 'POST|PUT|upload|size'",
            "Fixes: cap request body size, paginate, stream I/O, add memory-based HPA.",
        ])

    # C - under-provisioned
    if (peak_ratio is not None and peak_ratio >= 0.95
            and (deriv is None or deriv < LEAK_THRESHOLD_BPS)):
        evidence.append(f"peak/limit: {peak_ratio*100:.1f}% — at the ceiling")
        evidence.append("memory deriv flat (not climbing) — not a leak")
        return ("C - UNDER-PROVISIONED LIMIT", evidence, [
            "Workload's normal peak is ≥ its memory limit. Each peak triggers OOM.",
            "Recommendation: install VPA in recommender mode, set limit to P99 × 1.3.",
            "  kubectl get vpa <name> -o jsonpath='{.status.recommendation}'",
            "Verify request matches typical usage so the scheduler reserves enough headroom.",
        ])

    # Indeterminate
    evidence.append("Insufficient signal to match a known pattern with confidence.")
    if peak_ratio is not None:
        evidence.append(f"peak/limit: {peak_ratio*100:.1f}%")
    if deriv is not None:
        evidence.append(f"memory deriv (1h): {deriv * 60 / (1024*1024):+.1f} MiB/min")
    if not metrics or not metrics.get("limit"):
        evidence.append("(Prometheus not reachable — pattern detection is limited; "
                        "rerun with --prometheus-url for full analysis.)")
    return ("? - INDETERMINATE", evidence, [
        "Manual inspection recommended. Useful starting points:",
        f"  ./oom-history.py --deployment {target['workload_name']} --logs",
        f"  ./oom-usage.py -n {target['namespace']}",
        "  Grafana dashboard oom-7d-detail (full timeline + Loki logs)",
    ])


# ------------------------------------------------------------- rendering

def hr(char="=", width=80):
    return char * width


def render_text(report, with_logs):
    print()
    print(hr("="))
    print(f"OOM ROOT-CAUSE ANALYSIS — {report['namespace']}/{report['pod']}/{report['container']}")
    print(hr("="))

    t = report["target"]
    print()
    print("CONTEXT")
    print(f"  Workload:        {t['workload_kind']}/{t['workload_name']}"
          + (f"  (rev {t['rs_revision']}, ReplicaSet {t['replicaset']})"
             if t['replicaset'] else ""))
    print(f"  Node:            {t['node']}")
    print(f"  OOM at:          {t['finished_at']}  ({fmt_age(t['finished_at'])} ago)")
    print(f"  Started:         {t['started_at']}")
    started = parse_iso(t["started_at"])
    finished = parse_iso(t["finished_at"])
    if started and finished:
        lifetime = int((finished - started).total_seconds())
        print(f"  Lifetime:        {lifetime}s before OOM")
    print(f"  Exit code:       {t['exit_code']}   (137 = OOMKilled)")
    print(f"  Restart count:   {t['restart_count']}")

    spec = report["container_spec"]
    res = (spec.get("resources") or {})
    print()
    print("CONFIGURATION")
    print(f"  Image:           {spec.get('image', '?')}")
    print(f"  Memory limit:    {(res.get('limits') or {}).get('memory', '-')}"
          f"   request: {(res.get('requests') or {}).get('memory', '-')}")
    print(f"  CPU limit:       {(res.get('limits') or {}).get('cpu', '-')}"
          f"   request: {(res.get('requests') or {}).get('cpu', '-')}")
    print(f"  QoS class:       {report['pod_obj'].get('status', {}).get('qosClass', '-')}")
    probes = []
    for kind in ("livenessProbe", "readinessProbe", "startupProbe"):
        if spec.get(kind):
            probes.append(kind)
    print(f"  Probes:          {', '.join(probes) if probes else '-'}")
    print(f"  Args:            {' '.join(spec.get('args', []) or []) or '-'}")

    m = report["metrics"]
    print()
    print("MEMORY (Prometheus)")
    if m:
        print(f"  WSS at OOM:      {fmt_bytes(m.get('wss_at'))}")
        print(f"  Peak (last 5m):  {fmt_bytes(m.get('wss_peak_5m'))}")
        print(f"  Peak (last 1h):  {fmt_bytes(m.get('wss_peak_1h'))}")
        if m.get("deriv_1h") is not None:
            print(f"  Slope (deriv 1h):{m['deriv_1h'] * 60 / (1024*1024):+.2f} MiB/min")
        print(f"  Memory limit:    {fmt_bytes(m.get('limit'))}")
    else:
        print("  (Prometheus not reachable — skipping)")

    print()
    print("CPU (Prometheus)")
    if m:
        print(f"  Usage (5m avg):  {('%.3f cores' % m['cpu_rate_5m']) if m.get('cpu_rate_5m') is not None else '-'}")
        print(f"  Throttle (5m):   {('%.3f cores-equivalent' % m['cpu_throttle_5m']) if m.get('cpu_throttle_5m') is not None else '-'}")
    else:
        print("  (Prometheus not reachable — skipping)")

    print()
    print("NETWORK (Prometheus)")
    if m:
        print(f"  Rx 5m avg:       {fmt_rate_bps(m.get('rx_5m'))}")
        print(f"  Tx 5m avg:       {fmt_rate_bps(m.get('tx_5m'))}")
        print(f"  Rx 1m (recent):  {fmt_rate_bps(m.get('rx_1m'))}")
        print(f"  Rx baseline 30m: {fmt_rate_bps(m.get('rx_baseline_30m'))}")
    else:
        print("  (Prometheus not reachable — skipping)")

    print()
    print("STORAGE")
    if report["pvcs"]:
        for pvc in report["pvcs"]:
            spec = pvc.get("spec", {})
            status = pvc.get("status", {})
            cap = (status.get("capacity") or {}).get("storage", "?")
            print(f"  PVC {pvc['metadata']['name']}: "
                  f"status={status.get('phase', '?')}, capacity={cap}, "
                  f"sc={spec.get('storageClassName', '?')}, "
                  f"access={','.join(spec.get('accessModes', []) or [])}")
    else:
        print("  (no PVCs mounted)")
    if m and (m.get("fs_reads_5m") is not None or m.get("fs_writes_5m") is not None):
        print(f"  fs read 5m:      {fmt_rate_bps(m.get('fs_reads_5m'))}")
        print(f"  fs write 5m:     {fmt_rate_bps(m.get('fs_writes_5m'))}")

    print()
    print("NODE")
    n = report["node"]
    if n:
        print(f"  Name:            {n['metadata']['name']}")
        cap = (n.get("status", {}).get("capacity") or {})
        alloc = (n.get("status", {}).get("allocatable") or {})
        print(f"  Capacity:        memory={cap.get('memory', '?')}, cpu={cap.get('cpu', '?')}")
        print(f"  Allocatable:     memory={alloc.get('memory', '?')}, cpu={alloc.get('cpu', '?')}")
        for cond in n.get("status", {}).get("conditions", []) or []:
            if cond.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure", "Ready"):
                print(f"  {cond['type']:16}{cond.get('status', '?')}"
                      f"  ({cond.get('reason', '-')})")
    else:
        print(f"  (node {t['node']} not in API response — cluster-scoped fetch may have failed)")

    print()
    print(f"NEIGHBOR PODS — OOMs on the same node ({t['node']}) within 1h")
    if report["neighbors"]:
        for nb in report["neighbors"]:
            print(f"  - {nb['pod']}/{nb['container']} at {nb['finished_at']}")
    else:
        print("  (none — no node-pressure pattern at this timestamp)")

    print()
    print("AUTOSCALING")
    if report["hpa"]:
        spec = report["hpa"]["spec"]
        print(f"  HPA {report['hpa']['metadata']['name']}: "
              f"min={spec.get('minReplicas', '?')}, max={spec.get('maxReplicas', '?')}")
        for met in spec.get("metrics", []) or []:
            print(f"    metric: {met}")
    else:
        print("  (no HPA targets this workload)")
    if report["vpa"]:
        rec = (report["vpa"].get("status") or {}).get("recommendation") or {}
        print(f"  VPA {report['vpa']['metadata']['name']} recommendation:")
        for cr in rec.get("containerRecommendations", []) or []:
            print(f"    {cr.get('containerName', '?')}: "
                  f"target={cr.get('target', {})}, "
                  f"upperBound={cr.get('upperBound', {})}")
    else:
        print("  (no VPA targets this workload — install in recommender mode for "
              "right-sizing hints)")

    print()
    print("SERVICES & NETWORK POLICY")
    if report["services"]:
        for svc in report["services"]:
            ports = ",".join(str(p.get("port", "?")) for p in
                             svc.get("spec", {}).get("ports", []) or [])
            print(f"  Service {svc['metadata']['name']}: "
                  f"type={svc.get('spec', {}).get('type', '?')}, ports={ports}")
    else:
        print("  (no Service selects this pod)")
    if report["networkpolicies"]:
        for np in report["networkpolicies"]:
            types = ",".join(np.get("spec", {}).get("policyTypes", []) or [])
            print(f"  NetworkPolicy {np['metadata']['name']}: types={types}")

    print()
    print("NAMESPACE CONSTRAINTS")
    if report["limitranges"]:
        for lr in report["limitranges"]:
            print(f"  LimitRange {lr['metadata']['name']}:")
            for limit in (lr.get("spec", {}).get("limits") or []):
                print(f"    {limit.get('type')}: default={limit.get('default')}, "
                      f"defaultRequest={limit.get('defaultRequest')}, "
                      f"max={limit.get('max')}, min={limit.get('min')}")
    if report["resourcequotas"]:
        for rq in report["resourcequotas"]:
            hard = rq.get("status", {}).get("hard") or {}
            used = rq.get("status", {}).get("used") or {}
            print(f"  ResourceQuota {rq['metadata']['name']}:")
            for key in sorted(hard.keys()):
                print(f"    {key}: used={used.get(key, '?')} / hard={hard[key]}")
    if not report["limitranges"] and not report["resourcequotas"]:
        print("  (no LimitRange / ResourceQuota in this namespace)")

    print()
    print(f"RECENT EVENTS — pod, last 1h ({len(report['events'])} entries)")
    if report["events"]:
        for ev in report["events"][:10]:
            print(f"  [{ev['ts']}] {ev['type']}/{ev['reason']} (x{ev['count']}): {ev['message']}")
        if len(report["events"]) > 10:
            print(f"  ... and {len(report['events']) - 10} more")
    else:
        print("  (no recent events — kube event retention is typically ~1h)")

    if with_logs:
        print()
        print("PRE-OOM LOGS (last 30 lines, error/oom filtered)")
        if report["logs"] is None:
            print("  (no previous logs available)")
        elif not report["logs"].strip():
            print("  (no matching log lines)")
        else:
            for line in report["logs"].splitlines():
                print(f"  | {line}")

    print()
    print("VERDICT")
    pattern, evidence, fixes = report["verdict"]
    print(f"  Most likely cause:  {pattern}")
    print("  Evidence:")
    for e in evidence:
        print(f"    - {e}")
    print("  Recommended action:")
    for f in fixes:
        print(f"    {f}")
    print(hr("="))


def render_summary(reports):
    """One-line per OOM, for quick triage."""
    if not reports:
        print("# no OOMKilled containers found")
        return
    cols = [
        ("NAMESPACE", lambda r: r["namespace"]),
        ("POD", lambda r: r["pod"]),
        ("CONTAINER", lambda r: r["container"]),
        ("PATTERN", lambda r: r["verdict"][0]),
        ("OOM AGE", lambda r: fmt_age(r["target"]["finished_at"]) + " ago"),
    ]
    cells = [[fn(r) for r in reports] for _, fn in cols]
    widths = [max(len(h), max(len(c) for c in col))
              for (h, _), col in zip(cols, cells)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*[h for h, _ in cols]))
    for i in range(len(reports)):
        print(fmt.format(*[c[i] for c in cells]))


# ----------------------------------------------------------------- main

def build_report(target, cluster, prom, with_logs, log_grep):
    pod = target["pod_obj"]
    spec = container_spec(pod, target["container"])
    metrics = metric_summary(prom, target["namespace"], target["pod"],
                             target["container"], target["finished_at"])
    pvcs = pvcs_for_pod(pod, cluster["pvcs"])
    services = services_targeting(pod, cluster["services"])
    nps = network_policies_for_pod(pod, cluster["networkpolicies"])
    hpa = hpa_for_workload(target["workload_kind"], target["workload_name"],
                           target["namespace"], cluster["hpas"])
    vpa = vpa_for_workload(target["workload_kind"], target["workload_name"],
                           target["namespace"], cluster["vpas"])
    node = next((n for n in cluster["nodes"]
                 if n["metadata"]["name"] == target["node"]), None)
    neighbors = neighbor_ooms_on_node(target["node"], target["pod"],
                                      target["finished_at"], cluster["pods"])
    events = events_for_pod(target["pod"], target["namespace"], cluster["events"])

    logs = None
    if with_logs:
        logs = fetch_logs(target["namespace"], target["pod"], target["container"],
                          tail=30, grep_re=log_grep)

    # Namespace-scoped constraints (filtered to this pod's namespace)
    ns = target["namespace"]
    limitranges = [lr for lr in cluster["limitranges"]
                   if lr["metadata"]["namespace"] == ns]
    resourcequotas = [rq for rq in cluster["resourcequotas"]
                      if rq["metadata"]["namespace"] == ns]

    verdict = diagnose(target, metrics, neighbors)

    return {
        "namespace": target["namespace"],
        "pod": target["pod"],
        "container": target["container"],
        "target": target,
        "pod_obj": pod,
        "container_spec": spec,
        "metrics": metrics,
        "pvcs": pvcs,
        "services": services,
        "networkpolicies": nps,
        "hpa": hpa,
        "vpa": vpa,
        "node": node,
        "neighbors": neighbors,
        "events": events,
        "limitranges": limitranges,
        "resourcequotas": resourcequotas,
        "logs": logs,
        "verdict": verdict,
    }


def report_for_json(r):
    """Strip non-serialisable fields (Pod / RS objects) for --json output."""
    pruned = dict(r)
    target = dict(r["target"])
    target.pop("pod_obj", None)
    target.pop("container_status", None)
    pruned["target"] = target
    pruned.pop("pod_obj", None)
    pruned.pop("container_spec", None)
    pruned.pop("node", None)
    pruned.pop("services", None)
    pruned.pop("networkpolicies", None)
    pruned.pop("hpa", None)
    pruned.pop("vpa", None)
    pruned.pop("limitranges", None)
    pruned.pop("resourcequotas", None)
    pruned.pop("pvcs", None)
    pattern, evidence, fixes = r["verdict"]
    pruned["verdict"] = {"pattern": pattern, "evidence": evidence, "fixes": fixes}
    return pruned


def main():
    global CLI
    p = argparse.ArgumentParser(
        description="Per-pod OOM root-cause report. Pulls workload + node + neighbors + "
        "events + storage + autoscaling + Prometheus signal in one shot.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-n", "--namespace", help="namespace (default: current oc project)")
    g.add_argument("-A", "--all-namespaces", action="store_true", help="all namespaces")
    p.add_argument("--pod", help="filter to a single pod by name")
    p.add_argument("--summary", action="store_true",
                   help="one-line-per-OOM summary table instead of full report")
    p.add_argument("--logs", action="store_true",
                   help="include filtered pre-OOM logs at the end of each report")
    p.add_argument("--grep",
                   help="regex applied to log lines when --logs is set "
                        "(default: error|oom|killed|out ?of ?memory|fatal|exception)")
    p.add_argument("--prometheus-url", default="http://localhost:9090",
                   help="Prometheus / Thanos base URL (default: http://localhost:9090)")
    p.add_argument("--token", help="bearer token (default: try `oc whoami -t`)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.add_argument("--no-prometheus", action="store_true",
                   help="don't query Prometheus (verdict will be limited)")
    p.add_argument("--diagnose", action="store_true",
                   help="probe Prometheus for the metrics this script depends on, "
                        "print which scrape sources are missing, and exit")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("--kubectl", action="store_true", help="use kubectl instead of oc")
    args = p.parse_args()

    CLI = "kubectl" if args.kubectl else "oc"
    if not shutil.which(CLI):
        sys.stderr.write(f"{CLI} not found in PATH\n")
        sys.exit(2)

    if args.diagnose:
        prom = Prom(args.prometheus_url,
                    token=args.token or auto_token(),
                    insecure=args.insecure)
        if not prom.probe():
            sys.stderr.write(
                f"cannot reach Prometheus at {args.prometheus_url}\n"
                "Start a port-forward, or pass --prometheus-url / --token / --insecure.\n")
            sys.exit(3)
        diagnose_metrics(prom)
        return

    if args.all_namespaces:
        ns_args = ["-A"]
        scope = "all namespaces"
    else:
        ns = args.namespace or current_namespace()
        ns_args = ["-n", ns]
        scope = f"namespace {ns}"

    sys.stderr.write(f"# fetching cluster state for {scope}\n")
    cluster = fetch_cluster_data(ns_args)

    targets = find_oom_targets(cluster)
    if args.pod:
        targets = [t for t in targets if t["pod"] == args.pod]
    if not targets:
        sys.stderr.write(f"# no OOMKilled containers found in {scope}"
                         + (f" matching pod={args.pod!r}" if args.pod else "") + "\n")
        return

    prom = None
    if not args.no_prometheus:
        prom = Prom(args.prometheus_url,
                    token=args.token or auto_token(),
                    insecure=args.insecure)
        if not prom.probe():
            sys.stderr.write(
                f"# Prometheus not reachable at {args.prometheus_url} — "
                "verdict will be based on K8s data only.\n"
                "# (rerun with --prometheus-url <URL> [--insecure] [--token TOKEN], "
                "or --no-prometheus to silence this)\n")

    log_grep = re.compile(
        args.grep or r"error|oom|killed|out ?of ?memory|fatal|exception",
        re.IGNORECASE) if args.logs else None

    reports = [build_report(t, cluster, prom, args.logs, log_grep) for t in targets]

    if args.json:
        json.dump([report_for_json(r) for r in reports],
                  sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    if args.summary:
        render_summary(reports)
        return

    sys.stderr.write(f"# {len(reports)} OOMKilled container(s) in {scope}\n")
    for r in reports:
        render_text(r, with_logs=args.logs)


if __name__ == "__main__":
    main()
