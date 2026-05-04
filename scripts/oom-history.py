#!/usr/bin/env python3
"""Show OOMKilled status across Deployment rollout history (one block per revision). See scripts/README.md."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

CLI = "oc"


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


def parse_iso(ts):
    """Python 3.6-compatible ISO 8601 parser (no fromisoformat)."""
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


def revision_number(rs):
    try:
        return int(rs["metadata"].get("annotations", {})
                   .get("deployment.kubernetes.io/revision", "0"))
    except ValueError:
        return 0


def deployment_revisions(deploy, all_rs, all_pods, oom_event_pods):
    """Walk one Deployment's ReplicaSets in revision order (newest first)."""
    deploy_uid = deploy["metadata"]["uid"]
    owned_rs = [rs for rs in all_rs
                if any(o.get("uid") == deploy_uid
                       for o in rs.get("metadata", {}).get("ownerReferences", []))]
    owned_rs.sort(key=revision_number, reverse=True)

    revisions = []
    for rs in owned_rs:
        rs_uid = rs["metadata"]["uid"]
        rs_pods = [p for p in all_pods
                   if any(o.get("uid") == rs_uid
                          for o in p.get("metadata", {}).get("ownerReferences", []))]

        oom_containers = []
        for pod in rs_pods:
            for cs in pod.get("status", {}).get("containerStatuses", []):
                last = (cs.get("lastState") or {}).get("terminated")
                if last and last.get("reason") == "OOMKilled":
                    oom_containers.append({
                        "pod": pod["metadata"]["name"],
                        "container": cs["name"],
                        "node": pod.get("spec", {}).get("nodeName", "-"),
                        "exit_code": last.get("exitCode"),
                        "finished_at": last.get("finishedAt"),
                        "started_at": last.get("startedAt"),
                        "restart_count": cs.get("restartCount", 0),
                    })

        # Pods of this RS that recently triggered an OOMKilling kube event
        # (events typically only retained ~1h, so this catches in-flight kills)
        rs_pod_names = {p["metadata"]["name"] for p in rs_pods}
        event_oom = sorted(rs_pod_names & oom_event_pods)

        spec = rs["spec"]["template"]["spec"]
        containers_meta = []
        for c in spec.get("containers", []):
            res = c.get("resources", {}) or {}
            containers_meta.append({
                "name": c["name"],
                "image": c.get("image", "?"),
                "memory_limit": (res.get("limits") or {}).get("memory"),
                "memory_request": (res.get("requests") or {}).get("memory"),
                "cpu_limit": (res.get("limits") or {}).get("cpu"),
                "cpu_request": (res.get("requests") or {}).get("cpu"),
            })

        annotations = rs["metadata"].get("annotations", {}) or {}
        revisions.append({
            "revision": revision_number(rs),
            "rs_name": rs["metadata"]["name"],
            "rs_created": rs["metadata"].get("creationTimestamp"),
            "replicas_desired": rs.get("spec", {}).get("replicas", 0),
            "replicas_status": rs.get("status", {}).get("replicas", 0),
            "replicas_ready": rs.get("status", {}).get("readyReplicas", 0),
            "change_cause": annotations.get("kubernetes.io/change-cause", ""),
            "active": rs.get("status", {}).get("replicas", 0) > 0,
            "containers": containers_meta,
            "pods_alive": len(rs_pods),
            "oom_containers": oom_containers,
            "oom_events_in_kube": event_oom,
        })
    return revisions


def dump_logs(ns, pod, container, tail, grep_re, indent="      "):
    args = ["logs", pod, "-c", container, "-n", ns, "--previous"]
    if tail > 0:
        args.append(f"--tail={tail}")
    proc = subprocess.run([CLI, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    print(f"{indent}--- pre-OOM log: {ns}/{pod}/{container} ({CLI} logs --previous) ---")
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip().splitlines()[0] if proc.stderr.strip() else f"{CLI} exit {proc.returncode}"
        print(f"{indent}[no previous log: {msg}]")
        return
    out = proc.stdout
    if grep_re:
        out = "\n".join(ln for ln in out.splitlines() if grep_re.search(ln))
        if not out.strip():
            print(f"{indent}[no lines matched filter]")
            return
    for line in out.splitlines():
        print(f"{indent}| {line}")


def render_text(history, with_logs, tail, grep_re):
    if not history:
        print("# no Deployments matched")
        return
    for d in history:
        print()
        print("=" * 80)
        print(f"Deployment: {d['namespace']}/{d['deployment']}  "
              f"(replicas: {d['replicas_ready']}/{d['replicas_desired']} ready)")
        print("=" * 80)
        if not d["revisions"]:
            print("  (no ReplicaSets found — Deployment never rolled out, or RS GC ran)")
            continue
        for rev in d["revisions"]:
            tags = []
            if rev["active"]:
                tags.append("ACTIVE")
            if rev["oom_containers"]:
                tags.append(f"OOM x{len(rev['oom_containers'])}")
            tag = (" [" + ", ".join(tags) + "]") if tags else ""
            print(f"\n  Revision {rev['revision']}{tag}")
            print(f"    ReplicaSet:   {rev['rs_name']}")
            print(f"    Age:          {fmt_age(rev['rs_created'])} ago "
                  f"({rev['rs_created']})")
            print(f"    Replicas:     desired={rev['replicas_desired']} "
                  f"status={rev['replicas_status']} ready={rev['replicas_ready']} "
                  f"alive_pods={rev['pods_alive']}")
            if rev["change_cause"]:
                print(f"    Change cause: {rev['change_cause']}")
            for c in rev["containers"]:
                bits = []
                if c["memory_limit"]:
                    bits.append(f"mem={c['memory_limit']}")
                if c["memory_request"]:
                    bits.append(f"mem-req={c['memory_request']}")
                if c["cpu_limit"]:
                    bits.append(f"cpu={c['cpu_limit']}")
                if c["cpu_request"]:
                    bits.append(f"cpu-req={c['cpu_request']}")
                res_str = " (" + ", ".join(bits) + ")" if bits else ""
                print(f"    Container:    {c['name']}  image={c['image']}{res_str}")
            if rev["oom_containers"]:
                print(f"    OOMKilled containers ({len(rev['oom_containers'])}):")
                for k in rev["oom_containers"]:
                    print(f"      - pod={k['pod']}  container={k['container']}  "
                          f"node={k['node']}  exit={k['exit_code']}  "
                          f"restarts={k['restart_count']}  "
                          f"finished={k['finished_at']}")
                if rev["oom_events_in_kube"]:
                    print(f"    Kube events (reason=OOMKilling) for these pods: "
                          f"{', '.join(rev['oom_events_in_kube'])}")
                if with_logs:
                    print()
                    for k in rev["oom_containers"]:
                        dump_logs(d["namespace"], k["pod"], k["container"],
                                  tail, grep_re)
                        print()


def main():
    global CLI
    p = argparse.ArgumentParser(
        description="Show OOMKilled status across Deployment rollout history.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-n", "--namespace",
                   help="namespace (default: current oc project)")
    g.add_argument("-A", "--all-namespaces", action="store_true",
                   help="all namespaces")
    p.add_argument("--deployment",
                   help="filter to a single Deployment by name")
    p.add_argument("--only-oom", action="store_true",
                   help="only show Deployments whose history contains OOMKilled containers")
    p.add_argument("--logs", action="store_true",
                   help="dump --previous container logs for each OOMKilled pod")
    p.add_argument("--tail", type=int, default=100,
                   help="lines per log block when --logs is set (default 100)")
    p.add_argument("--grep",
                   help="case-insensitive regex filter applied to log lines")
    p.add_argument("--json", action="store_true",
                   help="emit structured JSON instead of the text report")
    p.add_argument("--kubectl", action="store_true",
                   help="use kubectl instead of oc")
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

    deploys = get_json("get", "deployments", *ns_args, "-o", "json").get("items", [])
    if args.deployment:
        deploys = [d for d in deploys
                   if d["metadata"]["name"] == args.deployment]
    if not deploys:
        sys.stderr.write(f"# no Deployments found in {scope}"
                         + (f" matching {args.deployment!r}" if args.deployment else "")
                         + "\n")
        return

    all_rs = get_json("get", "rs", *ns_args, "-o", "json").get("items", [])
    all_pods = get_json("get", "pods", *ns_args, "-o", "json").get("items", [])
    events = get_json("get", "events", *ns_args,
                      "--field-selector", "reason=OOMKilling",
                      "-o", "json").get("items", [])
    oom_event_pods = {ev.get("involvedObject", {}).get("name")
                      for ev in events
                      if ev.get("involvedObject", {}).get("kind") == "Pod"}

    grep_re = re.compile(args.grep, re.IGNORECASE) if args.grep else None

    history = []
    for d in deploys:
        revs = deployment_revisions(d, all_rs, all_pods, oom_event_pods)
        record = {
            "namespace": d["metadata"]["namespace"],
            "deployment": d["metadata"]["name"],
            "replicas_desired": d.get("spec", {}).get("replicas", 0),
            "replicas_ready": d.get("status", {}).get("readyReplicas", 0),
            "revisions": revs,
        }
        if args.only_oom and not any(r["oom_containers"] for r in revs):
            continue
        history.append(record)

    if args.json:
        json.dump(history, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    sys.stderr.write(f"# Deployment rollout history + OOM status — {scope}\n")
    render_text(history, args.logs, args.tail, grep_re)


if __name__ == "__main__":
    main()
