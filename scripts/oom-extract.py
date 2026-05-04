#!/usr/bin/env python3
"""Extract OOMKilled containers in a namespace via oc/kubectl. See scripts/README.md."""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

CLI = "oc"


def run(*args, check=True):
    proc = subprocess.run([CLI, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.stderr.write(f"command failed: {CLI} {' '.join(args)}\n{proc.stderr}\n")
        sys.exit(proc.returncode)
    return proc.stdout


def current_namespace():
    if CLI == "oc":
        out = run("project", "-q", check=False).strip()
        if out:
            return out
    out = run("config", "view", "--minify", "-o", "jsonpath={..namespace}", check=False).strip()
    return out or "default"


def get_json(*args):
    raw = run(*args, check=False)
    if not raw.strip():
        return {"items": []}
    return json.loads(raw)


def build_rs_index(ns_args):
    """One bulk `get rs` so workload resolution is O(1) per pod, not one call per pod."""
    items = get_json("get", "rs", *ns_args, "-o", "json").get("items", [])
    idx = {}
    for rs in items:
        ns = rs["metadata"]["namespace"]
        name = rs["metadata"]["name"]
        owner = next((o for o in rs.get("metadata", {}).get("ownerReferences", []) if o.get("controller")), None)
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


def parse_mem(s):
    if s is None:
        return None
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
    except ValueError:
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


def fmt_age(ts):
    if not ts:
        return "-"
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    s = int((datetime.now(timezone.utc) - when).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def container_resources(pod, container_name):
    for c in pod["spec"]["containers"]:
        if c["name"] == container_name:
            res = c.get("resources", {})
            return (parse_mem(res.get("limits", {}).get("memory")),
                    parse_mem(res.get("requests", {}).get("memory")))
    return None, None


def collect(pods, rs_idx):
    rows = []
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        node = pod.get("spec", {}).get("nodeName", "-")
        kind, workload = workload_for(pod, rs_idx)
        for cs in pod.get("status", {}).get("containerStatuses", []):
            last = (cs.get("lastState") or {}).get("terminated")
            if not last or last.get("reason") != "OOMKilled":
                continue
            limit, request = container_resources(pod, cs["name"])
            rows.append({
                "namespace": ns,
                "workload_kind": kind,
                "workload": workload,
                "pod": name,
                "container": cs["name"],
                "node": node,
                "restarts": cs.get("restartCount", 0),
                "exit_code": last.get("exitCode"),
                "started_at": last.get("startedAt"),
                "finished_at": last.get("finishedAt"),
                "limit_bytes": limit,
                "request_bytes": request,
                "ready": cs.get("ready"),
            })
    rows.sort(key=lambda r: (r["finished_at"] or ""), reverse=True)
    return rows


def merge_events(rows, events):
    by_pod = {}
    for ev in events:
        obj = ev.get("involvedObject", {})
        if obj.get("kind") != "Pod":
            continue
        key = (obj.get("namespace"), obj.get("name"))
        ts = ev.get("lastTimestamp") or ev.get("eventTime")
        if ts and (key not in by_pod or ts > by_pod[key]):
            by_pod[key] = ts
    for r in rows:
        r["last_event"] = by_pod.get((r["namespace"], r["pod"]))
    return rows


def render_table(rows):
    if not rows:
        print("No OOMKilled containers found.")
        return
    cols = [
        ("NAMESPACE", lambda r: r["namespace"]),
        ("WORKLOAD",  lambda r: f"{r['workload_kind']}/{r['workload']}" if r["workload"] else "-"),
        ("POD",       lambda r: r["pod"]),
        ("CONTAINER", lambda r: r["container"]),
        ("NODE",      lambda r: r["node"]),
        ("RESTARTS",  lambda r: str(r["restarts"])),
        ("EXIT",      lambda r: "-" if r["exit_code"] is None else str(r["exit_code"])),
        ("OOM AGE",   lambda r: fmt_age(r["finished_at"])),
        ("MEM LIMIT", lambda r: fmt_bytes(r["limit_bytes"])),
        ("MEM REQ",   lambda r: fmt_bytes(r["request_bytes"])),
        ("K8S EVENT", lambda r: fmt_age(r.get("last_event"))),
    ]
    cells = [[fn(r) for r in rows] for _, fn in cols]
    widths = [max(len(hdr), max(len(c) for c in col)) for (hdr, _), col in zip(cols, cells)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*[hdr for hdr, _ in cols]))
    for i in range(len(rows)):
        print(fmt.format(*[col[i] for col in cells]))


def main():
    global CLI
    p = argparse.ArgumentParser(description="Extract OOMKilled info via oc/kubectl.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-n", "--namespace", help="namespace (default: current oc project)")
    g.add_argument("-A", "--all-namespaces", action="store_true", help="all namespaces")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--no-events", action="store_true", help="skip k8s event lookup")
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

    pods = get_json("get", "pods", *ns_args, "-o", "json").get("items", [])
    rs_idx = build_rs_index(ns_args)
    rows = collect(pods, rs_idx)
    if not args.no_events:
        events = get_json("get", "events", *ns_args,
                          "--field-selector", "reason=OOMKilling",
                          "-o", "json").get("items", [])
        merge_events(rows, events)

    if args.json:
        json.dump(rows, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    sys.stderr.write(f"# OOMKilled containers in {scope} (lastState.terminated.reason)\n")
    render_table(rows)


if __name__ == "__main__":
    main()
