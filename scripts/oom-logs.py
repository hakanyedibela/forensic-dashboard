#!/usr/bin/env python3
"""Dump pre-OOM (and optional current) logs for OOMKilled containers via oc/kubectl. See scripts/README.md."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

CLI = "oc"


def _exec(args, check):
    proc = subprocess.run([CLI, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    if check and proc.returncode != 0:
        sys.stderr.write(f"command failed: {CLI} {' '.join(args)}\n{proc.stderr}\n")
        sys.exit(proc.returncode)
    return proc


def run(*args, check=True):
    return _exec(list(args), check).stdout


def run_logs(*args):
    """`oc logs --previous` may legitimately fail (no prior container). Don't exit."""
    proc = _exec(list(args), check=False)
    return proc.stdout, proc.stderr, proc.returncode


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
                "restart_count": cs.get("restartCount", 0),
                "finished_at": last.get("finishedAt"),
                "exit_code": last.get("exitCode"),
            })
    targets.sort(key=lambda t: (t["namespace"], t["workload"], t["pod"], t["container"]))
    return targets


def find_workload_siblings(ns, kind, workload, rs_idx):
    """Pods that belong to the same workload but did not OOMKill themselves.
    Useful to see what 'healthy' replicas look like at the same time."""
    if not workload:
        return []
    pods = get_json("get", "pods", "-n", ns, "-o", "json").get("items", [])
    siblings = []
    for pod in pods:
        k, w = workload_for(pod, rs_idx)
        if k == kind and w == workload:
            siblings.append(pod["metadata"]["name"])
    return sorted(siblings)


def dump_section(target, label, extra_args, tail, since, grep_re, sink):
    ns, pod, ctr = target["namespace"], target["pod"], target["container"]
    args = ["logs", pod, "-c", ctr, "-n", ns, *extra_args]
    if tail > 0:
        args += [f"--tail={tail}"]
    if since:
        args += [f"--since={since}"]
    out, err, rc = run_logs(*args)
    wl = f"{target['workload_kind']}/{target['workload']}" if target["workload"] else "-"
    sink.write("\n")
    sink.write("=" * 80 + "\n")
    sink.write(f"# {label}: {ns}/{pod}  container={ctr}\n")
    sink.write(f"# workload={wl}  oomed_at={target['finished_at'] or '?'}  "
               f"exit={target['exit_code']}  restarts={target['restart_count']}\n")
    sink.write("=" * 80 + "\n")
    if rc != 0:
        sink.write(f"[no logs available — {CLI} exit {rc}]\n")
        if err.strip():
            sink.write(err.strip() + "\n")
        return
    if grep_re:
        kept = [ln for ln in out.splitlines() if grep_re.search(ln)]
        sink.write(("\n".join(kept) + "\n") if kept else "[no lines matched filter]\n")
    else:
        sink.write(out if out.endswith("\n") or not out else out + "\n")


def main():
    global CLI
    p = argparse.ArgumentParser(description="Dump logs from OOMKilled containers.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-n", "--namespace", help="namespace (default: current oc project)")
    g.add_argument("-A", "--all-namespaces", action="store_true", help="all namespaces")
    p.add_argument("--tail", type=int, default=200,
                   help="lines per section (0 = all). Default: 200")
    p.add_argument("--since", help="only logs newer than e.g. 1h, 24h, 7d")
    p.add_argument("--current", action="store_true",
                   help="also dump current (post-restart) container logs")
    p.add_argument("--no-previous", action="store_true",
                   help="skip the --previous (pre-OOM) logs")
    p.add_argument("--include-siblings", action="store_true",
                   help="also fetch current logs from other live pods of the same workload")
    p.add_argument("--grep", help="regex filter (case-insensitive) applied to each line")
    p.add_argument("--output-dir",
                   help="write one file per (namespace,pod,container) instead of stdout")
    p.add_argument("--list", action="store_true",
                   help="only list affected pods/containers, do not fetch logs")
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

    targets = find_oom_targets(ns_args)
    if not targets:
        sys.stderr.write(f"# no OOMKilled containers found in {scope}\n")
        return

    if args.list:
        for t in targets:
            wl = f"{t['workload_kind']}/{t['workload']}" if t["workload"] else "-"
            print(f"{t['namespace']}\t{wl}\t{t['pod']}\t{t['container']}\t{t['finished_at']}")
        return

    grep_re = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    want_prev = not args.no_previous
    want_curr = args.current
    rs_idx = build_rs_index(ns_args) if args.include_siblings else {}

    def open_sink(t):
        if not args.output_dir:
            return sys.stdout, False
        os.makedirs(args.output_dir, exist_ok=True)
        path = os.path.join(args.output_dir,
                            f"{t['namespace']}__{t['pod']}__{t['container']}.log")
        sys.stderr.write(f"writing {path}\n")
        return open(path, "w"), True

    seen_siblings = set()
    for t in targets:
        sink, owns = open_sink(t)
        try:
            if want_prev:
                dump_section(t, "PREVIOUS (pre-OOM)", ["--previous"],
                             args.tail, args.since, grep_re, sink)
            if want_curr:
                dump_section(t, "CURRENT", [],
                             args.tail, args.since, grep_re, sink)
            if args.include_siblings:
                for sibling in find_workload_siblings(t["namespace"],
                                                     t["workload_kind"], t["workload"], rs_idx):
                    key = (t["namespace"], sibling, t["container"])
                    if sibling == t["pod"] or key in seen_siblings:
                        continue
                    seen_siblings.add(key)
                    sib_target = dict(t, pod=sibling, finished_at=None,
                                      restart_count=0, exit_code=None)
                    dump_section(sib_target, "SIBLING (live workload peer)", [],
                                 args.tail, args.since, grep_re, sink)
        finally:
            if owns:
                sink.close()


if __name__ == "__main__":
    main()
