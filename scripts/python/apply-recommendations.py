#!/usr/bin/env python3
"""Apply resource recommendations to the cluster.

Reads the machine-readable `recommendations-apply.json` produced by
fetch-cluster-usage.py and, for each ResourcePatch, shells out to
`kubectl`/`oc patch --type=strategic`. **Server-side dry-run by default** — it
validates against the live object and shows old->new but changes nothing;
real changes require the explicit `--execute` flag.

Stdlib only (json + subprocess): no YAML or Kubernetes client dependency. The
sidecar JSON carries the same patches as `recommendations-apply.yaml`, which
stays the human-reviewable artifact.

Usage:
    apply-recommendations.py [--manifest recommendations-apply.json]
                             [--execute] [--context NAME] [--oc]
                             [--kubectl PATH]
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def load_manifest(path):
    """Parse the recommendations-apply.json sidecar (stdlib json)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_argv(doc, binary="kubectl", context=None, execute=False):
    """kubectl/oc argv for one ResourcePatch. The strategic-merge body is passed
    as JSON (valid kubectl `--patch` input, no shell-quoting pitfalls). Without
    `execute` a server-side dry-run flag is appended so nothing is mutated."""
    argv = [binary]
    if context:
        argv += ["--context", context]
    argv += ["patch", doc["kind"], doc["name"],
             "-n", doc["namespace"],
             "--type=strategic",
             "--patch", json.dumps(doc["patch"])]
    if not execute:
        argv.append("--dry-run=server")
    return argv


def _is_not_found(stdout, stderr):
    blob = f"{stdout}\n{stderr}".lower()
    return "notfound" in blob.replace(" ", "") or "not found" in blob


def subprocess_runner(argv):
    """Default runner: run argv, return (returncode, stdout, stderr).

    Uses stdout/stderr=PIPE + universal_newlines rather than capture_output /
    text (which are Python 3.7+) so the script runs on the RHEL 8 / OpenShift
    system python3 (3.6.8), matching the rest of the repo's 3.6.8+ target."""
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    return proc.returncode, proc.stdout, proc.stderr


def write_apply_report(path, mode, results, skipped):
    """Write a text report of the run: one line per **applied** recommendation
    (`<namespace>/<kind> <name>  <container: old->new>`), preceded by a header
    (mode, timestamp, counts) and followed by `#`-comment sections listing
    anything not applied — failures, not-found workloads, and quota-skipped
    namespaces — so the report is a complete, honest record."""
    applied = [r for r in results if r["status"] == "applied"]
    failed = [r for r in results if r["status"] == "failed"]
    notfound = [r for r in results if r["status"] == "not-found"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# apply-recommendations report — {mode} — {ts}\n")
        f.write(f"# {len(applied)} applied, {len(failed)} failed, "
                f"{len(notfound)} not found, "
                f"{len(skipped)} namespace(s) skipped (quota)\n")
        f.write("# Applied recommendations (one line each):\n")
        for r in applied:
            for line in r["summary"] or ["(no per-container detail)"]:
                f.write(f"{r['label']}  {line}\n")
        if failed:
            f.write("#\n# FAILED (not applied):\n")
            for r in failed:
                f.write(f"#   {r['label']}: {r['detail']}\n")
        if notfound:
            f.write("#\n# SKIPPED — workload not found on the cluster:\n")
            for r in notfound:
                f.write(f"#   {r['label']}\n")
        if skipped:
            f.write("#\n# SKIPPED namespaces (raise the ResourceQuota first):\n")
            for s in skipped:
                reasons = "; ".join(s.get("reasons", [])) or "quota exceeded"
                f.write(f"#   {s['namespace']}: {reasons}\n")
    return path


def apply_manifest(manifest, runner=subprocess_runner, execute=False,
                   binary="kubectl", context=None, out=None, report=None):
    """Apply every patch in `manifest` via `runner`. Re-prints the SKIPPED
    (quota) block, performs a server-side dry-run unless `execute`, treats a
    NotFound workload as a non-fatal skip, and returns a non-zero exit code if
    any patch failed under `--execute`. When `report` is a path, also writes a
    text report of the applied recommendations there (see write_apply_report)."""
    if out is None:                      # resolve at call time, not import time
        out = sys.stdout
    skipped = manifest.get("skipped", []) or []
    patches = manifest.get("patches", []) or []
    mode = "EXECUTE (mutating)" if execute else "DRY-RUN (server, no changes)"
    print(f"apply-recommendations: {mode} — {len(patches)} workload(s)", file=out)

    if skipped:
        print("SKIPPED namespaces (raise the ResourceQuota first, then re-export):",
              file=out)
        for s in skipped:
            reasons = "; ".join(s.get("reasons", [])) or "quota exceeded"
            print(f"  {s['namespace']}: {reasons}", file=out)

    if not patches:
        print("nothing to apply", file=out)
        if report is not None:
            write_apply_report(report, mode, [], skipped)
        return 0

    failures = 0
    results = []
    for p in patches:
        label = f"{p['namespace']}/{p['kind']} {p['name']}"
        summary = p.get("summary", []) or []
        for line in summary:
            print(f"  {label}  {line}", file=out)
        rc, so, se = runner(build_argv(p, binary, context, execute))
        if rc == 0:
            status, detail = "applied", ""
            print(f"  OK   {label}", file=out)
        elif _is_not_found(so, se):
            status, detail = "not-found", ""
            print(f"  SKIP {label} (not found)", file=out)
        else:
            status, detail = "failed", (se or so).strip()
            failures += 1
            print(f"  FAIL {label}: {detail}", file=out)
        results.append({"label": label, "summary": summary, "status": status,
                        "detail": detail})

    print(f"done: {len(patches)} processed, {failures} failed", file=out)
    if report is not None:
        write_apply_report(report, mode, results, skipped)
    return 1 if failures else 0


def build_parser():
    p = argparse.ArgumentParser(
        description="Apply resource recommendations (dry-run by default).")
    p.add_argument("--manifest", default="recommendations-apply.json",
                   help="Path to recommendations-apply.json (default: "
                        "recommendations-apply.json in the current directory).")
    p.add_argument("--execute", action="store_true",
                   help="Apply for real. Without this flag the run is a "
                        "server-side dry-run that changes nothing.")
    p.add_argument("--context", help="kube context (passed as --context).")
    p.add_argument("--oc", action="store_true",
                   help="Use the 'oc' binary instead of 'kubectl'.")
    p.add_argument("--kubectl", default="kubectl",
                   help="kubectl binary path (default: kubectl).")
    p.add_argument("--report", metavar="PATH",
                   help="Also write a text report listing each applied "
                        "recommendation (one per line) to PATH.")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    binary = "oc" if args.oc else args.kubectl
    try:
        manifest = load_manifest(args.manifest)
    except FileNotFoundError:
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read manifest {args.manifest}: {e}", file=sys.stderr)
        return 2
    # Fail fast with a clear message if the patch binary is missing (only when
    # there's actually something to apply — a header-only manifest needs no CLI).
    if manifest.get("patches") and shutil.which(binary) is None:
        hint = " (or pass --oc to use the oc binary)" if not args.oc else ""
        print(f"{binary!r} not found on PATH — install it{hint}, or point "
              f"--kubectl at the binary.", file=sys.stderr)
        return 2
    return apply_manifest(manifest, execute=args.execute,
                          binary=binary, context=args.context,
                          report=args.report)


if __name__ == "__main__":
    sys.exit(main())
