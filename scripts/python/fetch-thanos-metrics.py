#!/usr/bin/env python3
"""
fetch-thanos-metrics.py

Fetch metrics from a Thanos Querier (or any Prometheus-compatible) HTTP API.

Supports:
  * instant queries   (/api/v1/query)
  * range queries     (/api/v1/query_range)
  * label discovery   (/api/v1/labels, /api/v1/label/<name>/values)
  * series lookup     (/api/v1/series)
  * metric-name dump  (/api/v1/label/__name__/values)

Endpoint discovery:
  --url URL              explicit Thanos / Prometheus base URL
  (otherwise) auto-pick the first known Querier Service in-cluster and
  start a kubectl/oc port-forward (matches scripts/oom-usage.py).

Auth:
  --token / --token-file bearer token
  (otherwise) `oc whoami -t` if `oc` is on PATH

Thanos-specific knobs:
  --dedup / --no-dedup            enable replica deduplication (default: on)
  --partial-response              accept partial responses
  --max-source-resolution RES     e.g. 5m, 1h
  --engine {prometheus,thanos}    server-side engine selection
  --store-matchers MATCH          repeatable; restricts the Store fan-out

Usage:
  fetch-thanos-metrics.py --query 'up' --url http://localhost:9090
  fetch-thanos-metrics.py --query 'rate(container_cpu_usage_seconds_total[5m])' \\
      --range --start -1h --end now --step 60s --output cpu.json
  fetch-thanos-metrics.py --list-metrics
  fetch-thanos-metrics.py --label-values namespace
  fetch-thanos-metrics.py --series 'kube_pod_container_status_terminated_reason{reason="OOMKilled"}'

Stdlib only. No pip install.
"""

import argparse
import atexit
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

CLI = os.environ.get("OC_BIN") or os.environ.get("KUBECTL_BIN") or (
    "oc" if shutil.which("oc") else "kubectl"
)

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


# ----------------------------------------------------------------- time parse

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


# ----------------------------------------------------------------- port-fwd

def discover_querier():
    for ns, name, port in QUERIER_CANDIDATES:
        rc = subprocess.run([CLI, "get", "svc", name, "-n", ns],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
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


# ----------------------------------------------------------------- HTTP client

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


# ----------------------------------------------------------------- pretty table

def print_table(payload, *, stream=sys.stdout):
    """Compact rendering for instant/range query results."""
    data = payload.get("data", {})
    rtype = data.get("resultType")
    result = data.get("result", [])
    if not result:
        stream.write("(no samples)\n")
        return

    if rtype in ("vector", "scalar"):
        rows = []
        for s in result:
            metric = s.get("metric", {})
            label = metric.get("__name__", "") or _fmt_labels(metric)
            ts, val = s.get("value", [None, None])
            rows.append((label, _fmt_labels(metric, skip="__name__"), val))
        _print_rows(["metric", "labels", "value"], rows, stream)
        return

    if rtype == "matrix":
        rows = []
        for s in result:
            metric = s.get("metric", {})
            name = metric.get("__name__", "") or "(expr)"
            values = s.get("values", [])
            n = len(values)
            first = values[0][1] if n else ""
            last = values[-1][1] if n else ""
            rows.append((name, _fmt_labels(metric, skip="__name__"),
                         str(n), str(first), str(last)))
        _print_rows(["metric", "labels", "points", "first", "last"], rows, stream)
        return

    stream.write(json.dumps(payload, indent=2) + "\n")


def _fmt_labels(metric, skip=None):
    items = [f'{k}="{v}"' for k, v in sorted(metric.items()) if k != skip]
    return "{" + ",".join(items) + "}" if items else ""


def _print_rows(headers, rows, stream):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    stream.write(fmt.format(*headers) + "\n")
    stream.write(fmt.format(*["-" * w for w in widths]) + "\n")
    for r in rows:
        stream.write(fmt.format(*[str(c) for c in r]) + "\n")


# ----------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        description="Fetch metrics from a Thanos Querier / Prometheus-compatible API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    src = p.add_argument_group("endpoint")
    src.add_argument("--url", help="Base URL of the Thanos Querier (e.g. http://localhost:9090). "
                                   "If omitted, auto-discover a Service and port-forward.")
    src.add_argument("--local-port", type=int, default=19090,
                     help="Local port for auto port-forward (default: 19090)")
    src.add_argument("--insecure", action="store_true",
                     help="Skip TLS verification (typical for in-cluster Querier routes).")
    src.add_argument("--token", help="Bearer token.")
    src.add_argument("--token-file", help="Read bearer token from file.")
    src.add_argument("--no-auto-token", action="store_true",
                     help="Disable `oc whoami -t` fallback.")
    src.add_argument("--timeout", type=int, default=30, help="HTTP timeout (s).")

    th = p.add_argument_group("thanos knobs")
    th.add_argument("--dedup", dest="dedup", action="store_true", default=True,
                    help="Enable replica dedup (default).")
    th.add_argument("--no-dedup", dest="dedup", action="store_false",
                    help="Disable replica dedup.")
    th.add_argument("--partial-response", action="store_true",
                    help="Accept partial responses if some Stores are unreachable.")
    th.add_argument("--max-source-resolution",
                    help="Use downsampled blocks no finer than this (e.g. 5m, 1h).")
    th.add_argument("--engine", choices=["prometheus", "thanos"],
                    help="Server-side query engine.")
    th.add_argument("--store-matcher", action="append", default=[],
                    help="Restrict to Stores whose external labels match (repeatable). "
                         "Example: --store-matcher '{cluster=\"prod\"}'")

    mode = p.add_argument_group("mode (mutually exclusive)")
    g = mode.add_mutually_exclusive_group()
    g.add_argument("--query", action="append", default=[],
                   help="PromQL expression. Repeat for multiple. "
                        "Without --range this is an instant query.")
    g.add_argument("--queries-file",
                   help="File with one PromQL expression per line "
                        "(blank lines and # comments ignored).")
    g.add_argument("--list-metrics", action="store_true",
                   help="Dump all metric names (label __name__).")
    g.add_argument("--labels", action="store_true",
                   help="Dump all label names.")
    g.add_argument("--label-values", metavar="NAME",
                   help="Dump values for one label.")
    g.add_argument("--series", action="append", default=[],
                   help="Series lookup matcher (repeatable).")

    rng = p.add_argument_group("range query")
    rng.add_argument("--range", action="store_true",
                     help="Treat --query as a range query.")
    rng.add_argument("--start", default="-1h",
                     help="Start time: RFC3339, unix seconds, 'now', or offset like '-1h'. "
                          "Default: -1h.")
    rng.add_argument("--end", default="now",
                     help="End time (same formats). Default: now.")
    rng.add_argument("--step", default="60s",
                     help="Resolution step for range queries / lookback window. Default: 60s.")
    rng.add_argument("--time",
                     help="Evaluation time for instant queries (default: now).")

    out = p.add_argument_group("output")
    out.add_argument("--output", "-o",
                     help="Write raw JSON to FILE. If multiple queries are given, "
                          "FILE may contain '{i}' for the index or '{q}' for a slug.")
    out.add_argument("--table", action="store_true",
                     help="Also print a compact table to stdout.")
    out.add_argument("--pretty", action="store_true",
                     help="Pretty-print JSON.")
    return p


def resolve_token(args):
    if args.token:
        return args.token
    if args.token_file:
        with open(args.token_file) as f:
            return f.read().strip()
    if not args.no_auto_token:
        return auto_token()
    return None


def resolve_endpoint(args):
    if args.url:
        return args.url
    svc = discover_querier()
    if not svc:
        sys.stderr.write(
            "error: no --url given and no known Querier Service found.\n"
            "       Set --url or ensure one of these exists: "
            + ", ".join(f"{ns}/{n}" for ns, n, _ in QUERIER_CANDIDATES) + "\n"
        )
        sys.exit(2)
    ns, name, port = svc
    sys.stderr.write(f"info: port-forwarding {ns}/{name}:{port} -> 127.0.0.1:{args.local_port}\n")
    start_port_forward(svc, args.local_port)
    return f"http://127.0.0.1:{args.local_port}"


def gather_queries(args):
    qs = list(args.query or [])
    if args.queries_file:
        with open(args.queries_file) as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    qs.append(s)
    return qs


def _slug(expr, n=40):
    s = re.sub(r"[^A-Za-z0-9]+", "_", expr).strip("_")
    return s[:n] or "q"


def write_output(args, idx, expr, payload):
    blob = json.dumps(payload, indent=2 if args.pretty else None)
    target = args.output
    if target:
        if "{i}" in target or "{q}" in target:
            target = target.format(i=idx, q=_slug(expr))
        with open(target, "w") as f:
            f.write(blob)
            if not blob.endswith("\n"):
                f.write("\n")
        sys.stderr.write(f"wrote {target}\n")
    else:
        sys.stdout.write(blob + "\n")


def main():
    args = build_parser().parse_args()

    base_url = resolve_endpoint(args)
    token = resolve_token(args)

    client = Thanos(
        base_url,
        token=token,
        insecure=args.insecure,
        timeout=args.timeout,
        dedup=args.dedup,
        partial_response=args.partial_response,
        max_source_resolution=args.max_source_resolution,
        engine=args.engine,
        store_matchers=args.store_matcher,
    )

    ok, err = client.probe()
    if not ok:
        sys.stderr.write(f"error: cannot reach {base_url}: {err}\n")
        sys.exit(1)

    # Discovery modes -------------------------------------------------------
    if args.list_metrics:
        payload = client.label_values("__name__")
        write_output(args, 0, "__name__", payload)
        if args.table:
            for v in payload.get("data", []):
                print(v)
        return

    if args.labels:
        payload = client.labels()
        write_output(args, 0, "labels", payload)
        if args.table:
            for v in payload.get("data", []):
                print(v)
        return

    if args.label_values:
        payload = client.label_values(args.label_values)
        write_output(args, 0, args.label_values, payload)
        if args.table:
            for v in payload.get("data", []):
                print(v)
        return

    if args.series:
        start = parse_time(args.start)
        end = parse_time(args.end)
        payload = client.series(args.series, start=start, end=end)
        write_output(args, 0, "series", payload)
        if args.table:
            for s in payload.get("data", []):
                print(_fmt_labels(s))
        return

    # Query modes -----------------------------------------------------------
    queries = gather_queries(args)
    if not queries:
        sys.stderr.write("error: nothing to do. Pass --query, --queries-file, "
                         "--list-metrics, --labels, --label-values, or --series.\n")
        sys.exit(2)

    if args.range:
        start = parse_time(args.start)
        end = parse_time(args.end)
        step = parse_step(args.step)
        for i, q in enumerate(queries):
            payload = client.query_range(q, start, end, step)
            write_output(args, i, q, payload)
            if args.table:
                sys.stdout.write(f"\n# {q}\n")
                print_table(payload)
    else:
        ts = parse_time(args.time) if args.time else time.time()
        for i, q in enumerate(queries):
            payload = client.query(q, ts=ts)
            write_output(args, i, q, payload)
            if args.table:
                sys.stdout.write(f"\n# {q}\n")
                print_table(payload)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
