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
import subprocess
import sys


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
    """Default runner: run argv, return (returncode, stdout, stderr)."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def apply_manifest(manifest, runner=subprocess_runner, execute=False,
                   binary="kubectl", context=None, out=sys.stdout):
    """Apply every patch in `manifest` via `runner`. Re-prints the SKIPPED
    (quota) block, performs a server-side dry-run unless `execute`, treats a
    NotFound workload as a non-fatal skip, and returns a non-zero exit code if
    any patch failed under `--execute`."""
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
        return 0

    failures = 0
    for p in patches:
        label = f"{p['namespace']}/{p['kind']} {p['name']}"
        for line in p.get("summary", []) or []:
            print(f"  {label}  {line}", file=out)
        rc, so, se = runner(build_argv(p, binary, context, execute))
        if rc == 0:
            print(f"  OK   {label}", file=out)
        elif _is_not_found(so, se):
            print(f"  SKIP {label} (not found)", file=out)
        else:
            failures += 1
            print(f"  FAIL {label}: {(se or so).strip()}", file=out)

    print(f"done: {len(patches)} processed, {failures} failed", file=out)
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
    return apply_manifest(manifest, execute=args.execute,
                          binary=binary, context=args.context)


if __name__ == "__main__":
    sys.exit(main())
