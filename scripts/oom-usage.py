#!/usr/bin/env python3
"""Recover memory/CPU usage at OOM time by querying Prometheus per OOMKilled container. See scripts/README.md."""

import argparse
import atexit
import json
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

CLI = "oc"


def _exec(args, check):
    proc = subprocess.run([CLI, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.stderr.write(f"command failed: {CLI} {' '.join(args)}\n{proc.stderr}\n")
        sys.exit(proc.returncode)
    return proc


def run(*args, check=True):
    return _exec(list(args), check).stdout


def current_namespace():
    if CLI == "oc":
        out = subprocess.run([CLI, "project", "-q"], capture_output=True, text=True).stdout.strip()
        if out:
            return out
    out = subprocess.run([CLI, "config", "view", "--minify",
                          "-o", "jsonpath={..namespace}"], capture_output=True, text=True).stdout.strip()
    return out or "default"


def get_json(*args):
    raw = run(*args, check=False)
    if not raw.strip():
        return {"items": []}
    return json.loads(raw)


def build_rs_index(ns_args):
    items = get_json("get", "rs", *ns_args, "-o", "json").get("items", [])
    idx = {}
    for rs in items:
        ns = rs["metadata"]["namespace"]
        name = rs["metadata"]["name"]
        owner = next((o for o in rs.get("metadata", {}).get("ownerReferences", [])
                      if o.get("controller")), None)
        idx[(ns, name)] = (owner["kind"], owner["name"]) if owner else ("ReplicaSet", name)
    return idx


def workload_for(pod, rs_idx):
    ns = pod["metadata"]["namespace"]
    for owner in pod.get("metadata", {}).get("ownerReferences", []):
        if not owner.get("controller"):
            continue
        kind, name = owner["kind"], owner["name"]
        if kind == "ReplicaSet":
            return rs_idx.get((ns, name), (kind, name))
        return kind, name
    return "", ""


def find_oom_targets(ns_args):
    pods = get_json("get", "pods", *ns_args, "-o", "json").get("items", [])
    rs_idx = build_rs_index(ns_args)
    targets = []
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        kind, workload = workload_for(pod, rs_idx)
        for cs in pod.get("status", {}).get("containerStatuses", []):
            last = (cs.get("lastState") or {}).get("terminated")
            if not last or last.get("reason") != "OOMKilled":
                continue
            targets.append({
                "namespace": ns,
                "pod": name,
                "container": cs["name"],
                "workload_kind": kind,
                "workload": workload,
                "finished_at": last.get("finishedAt"),
                "exit_code": last.get("exitCode"),
                "restart_count": cs.get("restartCount", 0),
            })
    targets.sort(key=lambda t: t["finished_at"] or "", reverse=True)
    return targets


def parse_iso(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fmt_bytes(n):
    if n is None:
        return "-"
    v = float(n)
    for unit in ["B", "Ki", "Mi", "Gi", "Ti"]:
        if v < 1024:
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}Pi"


def fmt_pct(num, den):
    if num is None or not den:
        return "-"
    return f"{(num / den) * 100:.1f}%"


def fmt_cores(v):
    return "-" if v is None else f"{v:.3f}"


class Prom:
    def __init__(self, base_url, token=None, insecure=False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def query(self, expr, ts):
        params = urllib.parse.urlencode({"query": expr, "time": str(int(ts))})
        url = f"{self.base_url}/api/v1/query?{params}"
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=15) as r:
                payload = json.load(r)
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
        """Cheap reachability check. Returns (ok, error_message)."""
        params = urllib.parse.urlencode({"query": "1"})
        url = f"{self.base_url}/api/v1/query?{params}"
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=5) as r:
                payload = json.load(r)
            if payload.get("status") != "success":
                return False, f"unexpected response: {payload}"
            return True, None
        except Exception as e:
            return False, str(e)


PROM_SERVICE_CANDIDATES = [
    # (namespace, service, port)
    ("monitoring", "kps-kube-prometheus-stack-prometheus", "9090"),
    ("monitoring", "kps-prometheus", "9090"),
    ("monitoring", "prometheus-operated", "9090"),
    ("openshift-monitoring", "thanos-querier", "9091"),
    ("openshift-monitoring", "prometheus-k8s", "9090"),
]


def discover_prom_service():
    for ns, name, port in PROM_SERVICE_CANDIDATES:
        rc = subprocess.run([CLI, "get", "svc", name, "-n", ns],
                            capture_output=True).returncode
        if rc == 0:
            return ns, name, port
    return None


def start_port_forward(svc, local_port, timeout=15):
    ns, name, remote_port = svc
    proc = subprocess.Popen(
        [CLI, "port-forward", f"svc/{name}", f"{local_port}:{remote_port}", "-n", ns],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    atexit.register(lambda: (proc.terminate(), proc.wait(timeout=5)) if proc.poll() is None else None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode().strip() if proc.stderr else ""
            raise RuntimeError(f"{CLI} port-forward exited: {err or 'no stderr'}")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"timed out waiting for port-forward to {svc[1]}:{svc[2]}")


def auto_token():
    """Try `oc whoami -t` (OpenShift). Returns None on failure."""
    if CLI != "oc":
        return None
    try:
        proc = subprocess.run(["oc", "whoami", "-t"], capture_output=True, text=True)
        return proc.stdout.strip() or None
    except FileNotFoundError:
        return None


# Each metric kind lists fallback expressions, tried in order. First non-None wins.
# Placeholders: {sel} = namespace/pod/container selector body (no braces),
#               {sel_lim} = same plus resource="memory",
#               {window} = lookback range.
METRIC_QUERIES = {
    "wss": [
        "container_memory_working_set_bytes{{{sel}}}",
        "node_namespace_pod_container:container_memory_working_set_bytes{{{sel}}}",
    ],
    "wss_peak": [
        "max_over_time(container_memory_working_set_bytes{{{sel}}}[{window}])",
        "max_over_time(node_namespace_pod_container:container_memory_working_set_bytes{{{sel}}}[{window}])",
    ],
    "rss": [
        "container_memory_rss{{{sel}}}",
        "node_namespace_pod_container:container_memory_rss{{{sel}}}",
    ],
    "limit": [
        "kube_pod_container_resource_limits{{{sel_lim}}}",
        "kube_pod_container_resource_limits_memory_bytes{{{sel}}}",
        "container_spec_memory_limit_bytes{{{sel}}}",
    ],
    "cpu_rate": [
        "rate(container_cpu_usage_seconds_total{{{sel}}}[{window}])",
        "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{{{sel}}}",
        "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate{{{sel}}}",
    ],
}


def first_value(prom, kind, ctx, ts):
    """Try each fallback expression for `kind`; return (value, expr_used)."""
    for tmpl in METRIC_QUERIES[kind]:
        expr = tmpl.format(**ctx)
        v = prom.query(expr, ts)
        if v is not None:
            return v, expr
    return None, None


def collect_usage(targets, prom, window):
    rows = []
    for t in targets:
        when = parse_iso(t["finished_at"])
        if not when:
            rows.append({**t, "wss_at_oom": None, "wss_peak": None,
                         "rss_at_oom": None, "limit": None, "cpu_cores_at_oom": None})
            continue
        ts = when.timestamp()
        sel = (f'namespace="{t["namespace"]}",pod="{t["pod"]}",'
               f'container="{t["container"]}"')
        ctx = {"sel": sel, "sel_lim": sel + ',resource="memory"', "window": window}
        wss, _ = first_value(prom, "wss", ctx, ts)
        wss_peak, _ = first_value(prom, "wss_peak", ctx, ts)
        rss, _ = first_value(prom, "rss", ctx, ts)
        limit, _ = first_value(prom, "limit", ctx, ts)
        cpu, _ = first_value(prom, "cpu_rate", ctx, ts)
        rows.append({
            **t,
            "wss_at_oom": wss,
            "wss_peak": wss_peak,
            "rss_at_oom": rss,
            "limit": limit,
            "cpu_cores_at_oom": cpu,
        })
    return rows


# (metric, label) — label is what we display under "Memory working set:" etc.
DIAGNOSTIC_PROBES = [
    ("Memory working set (cAdvisor)",       "container_memory_working_set_bytes"),
    ("Memory working set (recording rule)", "node_namespace_pod_container:container_memory_working_set_bytes"),
    ("Memory RSS (cAdvisor)",               "container_memory_rss"),
    ("Memory RSS (recording rule)",         "node_namespace_pod_container:container_memory_rss"),
    ("Memory limit (kube-state-metrics)",   'kube_pod_container_resource_limits{resource="memory"}'),
    ("Memory limit (KSM legacy)",           "kube_pod_container_resource_limits_memory_bytes"),
    ("Memory limit (cAdvisor)",             "container_spec_memory_limit_bytes"),
    ("CPU usage counter (cAdvisor)",        "container_cpu_usage_seconds_total"),
    ("CPU usage (recording, irate)",        "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate"),
    ("CPU usage (recording, rate)",         "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate"),
    ("OOM event counter (cAdvisor)",        "container_oom_events_total"),
    ("Pod terminated reason (KSM)",         'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}'),
]


def diagnose(prom):
    print(f"Probing Prometheus at {prom.base_url} for known OOM-relevant metrics.\n"
          "An 'OK' line means the metric exists and returns at least one series.\n"
          "Use the OK names below; the script will pick the first OK variant per kind.\n")
    width = max(len(name) for _, name in DIAGNOSTIC_PROBES)
    now = time.time()
    for label, metric in DIAGNOSTIC_PROBES:
        count = prom.query(f"count({metric})", now)
        if count is None:
            mark = "missing"
        else:
            mark = f"OK ({int(count)} series)"
        print(f"  {metric:<{width}}  {mark:<24}  # {label}")
    print()
    print("If everything is missing: Prometheus has no cAdvisor / kube-state-metrics scrape job.")
    print("If only the cAdvisor names are missing but recording rules exist: OK, the script falls back.")
    print("If `kube_pod_container_status_last_terminated_reason` is missing: KSM is not deployed —")
    print("  the dashboards in this repo and the OOM detection PromQL won't work either.")


def render_table(rows):
    if not rows:
        print("No OOMKilled containers found.")
        return
    cols = [
        ("NAMESPACE",      lambda r: r["namespace"]),
        ("WORKLOAD",       lambda r: f"{r['workload_kind']}/{r['workload']}" if r["workload"] else "-"),
        ("POD",            lambda r: r["pod"]),
        ("CONTAINER",      lambda r: r["container"]),
        ("OOM AT",         lambda r: r["finished_at"] or "-"),
        ("WSS@OOM",        lambda r: fmt_bytes(r["wss_at_oom"])),
        ("WSS PEAK",       lambda r: fmt_bytes(r["wss_peak"])),
        ("RSS@OOM",        lambda r: fmt_bytes(r["rss_at_oom"])),
        ("LIMIT",          lambda r: fmt_bytes(r["limit"])),
        ("% LIMIT",        lambda r: fmt_pct(r["wss_peak"], r["limit"])),
        ("CPU@OOM(cores)", lambda r: fmt_cores(r["cpu_cores_at_oom"])),
    ]
    cells = [[fn(r) for r in rows] for _, fn in cols]
    widths = [max(len(hdr), max(len(c) for c in col)) for (hdr, _), col in zip(cols, cells)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*[hdr for hdr, _ in cols]))
    for i in range(len(rows)):
        print(fmt.format(*[col[i] for col in cells]))


def main():
    global CLI
    p = argparse.ArgumentParser(
        description="Recover memory/CPU usage at OOM time via Prometheus.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-n", "--namespace", help="namespace (default: current oc project)")
    g.add_argument("-A", "--all-namespaces", action="store_true", help="all namespaces")
    p.add_argument("--prometheus-url", default="http://localhost:9090",
                   help="Prometheus / Thanos base URL (default: http://localhost:9090)")
    p.add_argument("--token", help="bearer token for Prometheus (default: try `oc whoami -t`)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.add_argument("--port-forward", action="store_true",
                   help="auto-start oc/kubectl port-forward to a discovered Prometheus svc")
    p.add_argument("--local-port", type=int, default=9090,
                   help="local port for --port-forward (default 9090)")
    p.add_argument("--window", default="5m",
                   help="lookback window for peak + CPU rate (default 5m)")
    p.add_argument("--diagnose", action="store_true",
                   help="probe Prometheus for known OOM-relevant metric names and exit")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--kubectl", action="store_true", help="use kubectl instead of oc")
    args = p.parse_args()

    CLI = "kubectl" if args.kubectl else "oc"
    if not shutil.which(CLI):
        sys.stderr.write(f"{CLI} not found in PATH\n")
        sys.exit(2)

    if args.all_namespaces:
        ns_args = ["-A"]
        scope = "all namespaces"
    else:
        ns = args.namespace or current_namespace()
        ns_args = ["-n", ns]
        scope = f"namespace {ns}"

    if not args.diagnose:
        targets = find_oom_targets(ns_args)
        if not targets:
            sys.stderr.write(f"# no OOMKilled containers found in {scope}\n")
            return

    prom_url = args.prometheus_url
    if args.port_forward:
        svc = discover_prom_service()
        if not svc:
            sys.stderr.write(
                "could not discover a Prometheus service. Tried:\n  "
                + "\n  ".join(f"{n}/{s}" for n, s, _ in PROM_SERVICE_CANDIDATES)
                + "\n"
            )
            sys.exit(3)
        sys.stderr.write(
            f"# port-forwarding svc/{svc[1]} -n {svc[0]} -> 127.0.0.1:{args.local_port}\n"
        )
        try:
            start_port_forward(svc, args.local_port)
        except RuntimeError as e:
            sys.stderr.write(f"port-forward failed: {e}\n")
            sys.exit(3)
        prom_url = f"http://127.0.0.1:{args.local_port}"

    token = args.token or auto_token()
    prom = Prom(prom_url, token=token, insecure=args.insecure)

    ok, err = prom.probe()
    if not ok:
        sys.stderr.write(
            f"cannot reach Prometheus at {prom_url}: {err}\n\n"
            "Fix one of:\n"
            "  - Start a port-forward in another terminal:\n"
            "      oc -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090\n"
            "  - Re-run with --port-forward (this script will set it up)\n"
            "  - Pass --prometheus-url <URL> [--token TOKEN] [--insecure]\n"
            "    e.g. for OpenShift Thanos:\n"
            "      --prometheus-url https://$(oc -n openshift-monitoring \\\n"
            "          get route thanos-querier -o jsonpath='{.spec.host}') --insecure\n"
        )
        sys.exit(3)

    if args.diagnose:
        diagnose(prom)
        return

    rows = collect_usage(targets, prom, args.window)

    if args.json:
        json.dump(rows, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    sys.stderr.write(
        f"# OOM usage for {scope} via {prom_url} (lookback={args.window})\n")
    render_table(rows)


if __name__ == "__main__":
    main()
