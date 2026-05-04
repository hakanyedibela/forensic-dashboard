# Scripts

Helper scripts for working with the OOM observability stack from the command line. Each script is self-contained Python 3 (no third-party deps) and works with both `oc` and `kubectl`.

> Deutsche Version: [`README.de.md`](./README.de.md)

## Index

| Script | Purpose |
|---|---|
| [`oom-extract.py`](#oom-extractpy) | List currently-OOMKilled containers in a namespace (or all) with workload, node, limits, and matching kube events. |
| [`oom-logs.py`](#oom-logspy) | Dump the **pre-OOM** (and optionally current / sibling) container logs for every OOMKilled container in a namespace. |
| [`oom-usage.py`](#oom-usagepy) | Recover **memory and CPU usage at OOM time** by querying Prometheus/Thanos for each OOMKilled container's last terminated timestamp. |

---

## `oom-extract.py`

Extracts OOMKilled information for pods in a namespace by querying the Kubernetes / OpenShift API via `oc` (default) or `kubectl`.

For each container whose **last terminated reason** was `OOMKilled` it reports:

| Column | Source |
|---|---|
| `NAMESPACE` | `pod.metadata.namespace` |
| `WORKLOAD` | `pod.metadata.ownerReferences` (ReplicaSet → Deployment auto-resolved) |
| `POD` | `pod.metadata.name` |
| `CONTAINER` | `containerStatuses[].name` |
| `NODE` | `pod.spec.nodeName` |
| `RESTARTS` | `containerStatuses[].restartCount` |
| `EXIT` | `lastState.terminated.exitCode` (137 for OOM) |
| `OOM AGE` | `lastState.terminated.finishedAt` |
| `MEM LIMIT` | `pod.spec.containers[].resources.limits.memory` |
| `MEM REQ` | `pod.spec.containers[].resources.requests.memory` |
| `K8S EVENT` | most recent `reason=OOMKilling` event for the pod |

### Requirements

- Python 3.8+
- `oc` (or `kubectl` with `--kubectl`)
- An active session (`oc login` / `kubectl config use-context …`)
- RBAC: `get pods`, `get replicasets`, `get events` in the target namespace(s)

### Usage

```bash
# current oc project
./oom-extract.py

# specific namespace
./oom-extract.py -n my-app

# all namespaces (cluster-wide scan)
./oom-extract.py -A

# JSON output (pipe into jq, save for later, etc.)
./oom-extract.py -A --json

# skip the events lookup (faster on busy clusters)
./oom-extract.py -A --no-events

# use kubectl instead of oc
./oom-extract.py --kubectl -n my-app
```

### Example output

```
# OOMKilled containers in namespace oom-test (lastState.terminated.reason)
NAMESPACE  WORKLOAD            POD                          CONTAINER   NODE       RESTARTS  EXIT  OOM AGE   MEM LIMIT  MEM REQ  K8S EVENT
oom-test   Deployment/leak-a   leak-a-6f7c9b8d57-x4f2g      app         worker-2   12        137   3m ago    256.0Mi    128.0Mi  3m ago
oom-test   Deployment/spike-b  spike-b-7d4b5c6789-q9k2p     app         worker-1   2         137   42m ago   512.0Mi    256.0Mi  -
oom-test   StatefulSet/db      db-0                         postgres    worker-3   1         137   1h ago    1.0Gi      512.0Mi  -
```

### Options

| Flag | Description |
|---|---|
| `-n, --namespace NS` | Specific namespace (default: current `oc project`). |
| `-A, --all-namespaces` | Scan every namespace. Mutually exclusive with `-n`. |
| `--json` | Emit a JSON array instead of a text table. |
| `--no-events` | Skip the `OOMKilling` events lookup (one fewer API call). |
| `--kubectl` | Use `kubectl` instead of `oc`. |

### How workload is resolved

The script walks `pod.metadata.ownerReferences`:

- **Deployment** — owner is a `ReplicaSet`; the script bulk-fetches all ReplicaSets in the scope once and follows their owner reference to the parent Deployment.
- **StatefulSet / DaemonSet / Job / CronJob** — owner is reported directly.
- **Bare pod** — column shows `-`.

### What this script does *not* do

- It does **not** see deleted pods. The Kubernetes API only returns currently-existing pods, so any pod that was removed after its OOM (e.g. by a `Job` cleanup or manual `oc delete`) is invisible here. For a historical 7-day view backed by Prometheus, use the Grafana dashboard **OOMKilled — 7d Detail + Drilldown** (uid `oom-7d-detail`).
- It does **not** correlate logs. Use Loki / the Grafana drilldown for that.
- It does **not** read kernel `dmesg`. For node-level OOMs (system OOM vs cgroup OOM), `ssh <node> "sudo dmesg -T | grep -i 'killed process'"` is still the source of truth.
- The `K8S EVENT` column is best-effort: kube events are typically retained for ~1 hour, so older OOMs will show `-` even if `lastState` still reports OOMKilled.

### Exit codes

- `0` — completed (rows may be empty)
- `2` — `oc` / `kubectl` not on `PATH`
- non-zero — underlying CLI command failed (stderr is forwarded)

---

## `oom-logs.py`

Finds the same OOMKilled containers as `oom-extract.py` and then, for each one, dumps the container log via `oc logs … --previous` (the log lines from the process that was killed — usually the most useful forensic artefact).

By default: previous logs only, last 200 lines per container, current `oc project`, written to stdout with a clear header per (pod, container).

### What gets fetched

| Section | When emitted | Source |
|---|---|---|
| `PREVIOUS (pre-OOM)` | always (unless `--no-previous`) | `oc logs <pod> -c <ctr> --previous` |
| `CURRENT` | with `--current` | `oc logs <pod> -c <ctr>` |
| `SIBLING (live workload peer)` | with `--include-siblings` | `oc logs <other-pod> -c <ctr>` for every other live pod of the same Deployment/StatefulSet/DaemonSet |

Each section is preceded by a fixed `===` header line carrying namespace, pod, container, workload, OOM timestamp, exit code, and restart count.

### Requirements

- Python 3.8+
- `oc` (or `kubectl` with `--kubectl`)
- An active session (`oc login`)
- RBAC: `get pods`, `get replicasets`, `get pods/log` in the target namespace(s)

### Usage

```bash
# pre-OOM logs for every OOMKilled container in the current namespace
./oom-logs.py

# specific namespace, last 500 lines
./oom-logs.py -n my-app --tail 500

# all OOMs cluster-wide, only the lines that look like errors
./oom-logs.py -A --grep "out of memory|OutOfMemoryError|fatal|panic"

# also include the current container log (after restart) and live siblings
./oom-logs.py -n my-app --current --include-siblings

# all log lines (no tail), since the last hour
./oom-logs.py -n my-app --tail 0 --since 1h

# write one file per (namespace,pod,container) into ./oom-logs/
./oom-logs.py -A --output-dir ./oom-logs

# just enumerate affected pods, no log fetch
./oom-logs.py -A --list
```

### Example output

```
================================================================================
# PREVIOUS (pre-OOM): oom-test/leak-a-6f7c9b8d57-x4f2g  container=app
# workload=Deployment/leak-a  oomed_at=2026-05-04T08:12:33Z  exit=137  restarts=12
================================================================================
2026-05-04T08:12:30.881Z INFO  serving request id=abc123 size=82MiB
2026-05-04T08:12:31.402Z WARN  GC pause 940ms
2026-05-04T08:12:32.119Z ERROR java.lang.OutOfMemoryError: Java heap space
        at com.example.Worker.handle(Worker.java:142)
        ...
```

### Options

| Flag | Description |
|---|---|
| `-n, --namespace NS` | Specific namespace (default: current `oc project`). |
| `-A, --all-namespaces` | Scan every namespace. Mutually exclusive with `-n`. |
| `--tail N` | Lines per section. `0` = all available. Default `200`. |
| `--since DURATION` | Pass-through to `oc logs --since`, e.g. `1h`, `24h`, `7d`. |
| `--current` | Also dump the current (post-restart) container log. |
| `--no-previous` | Skip the `--previous` section (rare; usually you want it). |
| `--include-siblings` | For each affected workload, also fetch current logs from its other live pods (handy for "is this happening to all replicas, or just one?"). |
| `--grep PATTERN` | Case-insensitive regex applied per line. Each section keeps only matching lines (and notes if none matched). |
| `--output-dir DIR` | Write one file per (namespace,pod,container) into `DIR` instead of stdout. Files are named `<ns>__<pod>__<container>.log`. |
| `--list` | Just enumerate affected pods (TSV), do not call `oc logs`. |
| `--kubectl` | Use `kubectl` instead of `oc`. |

### Behaviour notes

- `oc logs --previous` legitimately fails when a container has never restarted. The script catches that, writes a `[no logs available — oc exit N]` placeholder for the affected section, and continues with the next target. It does **not** abort the whole run.
- The same caveats from `oom-extract.py` apply: deleted pods are invisible, and only containers whose `lastState.terminated.reason == "OOMKilled"` are picked up. For 7-day historical correlation, use the Grafana drilldown (`oom-7d-detail`) and the Loki panel.
- `--include-siblings` issues one `get pods` per affected workload and one `oc logs` per sibling. On busy clusters with many OOM workloads this multiplies API load — keep it off by default.
- Output is plain text and pipe-safe (no colour codes), so `./oom-logs.py -A | less` and `./oom-logs.py -A | grep -i 'oom'` work as expected.

### Exit codes

- `0` — completed (output may be empty if nothing was OOMKilled)
- `2` — `oc` / `kubectl` not on `PATH`
- non-zero — a non-`logs` underlying CLI command failed (`get pods`, `get rs`); stderr is forwarded

---

## `oom-usage.py`

Answers the question **"how much memory and CPU was the container actually using when it got killed?"** That value is *not* in the Kubernetes API — `lastState.terminated` only carries the reason and the exit code. It *is* in Prometheus, which scrapes cAdvisor every ~30s.

The script discovers OOMKilled containers (same logic as `oom-extract.py`), reads `lastState.terminated.finishedAt` as the OOM timestamp, then for each one runs an instant Prometheus query at that timestamp.

### Metrics queried

For each kind the script tries the listed expressions in order and uses the **first one that returns a value**. So if your Prometheus only has the recording-rule alias, or only the legacy KSM metric, the table still fills in.

| Column | PromQL fallback chain |
|---|---|
| `WSS@OOM` | `container_memory_working_set_bytes{…}` → `node_namespace_pod_container:container_memory_working_set_bytes{…}` |
| `WSS PEAK` | `max_over_time(container_memory_working_set_bytes{…}[<window>])` → `max_over_time(node_namespace_pod_container:container_memory_working_set_bytes{…}[<window>])` |
| `RSS@OOM` | `container_memory_rss{…}` → `node_namespace_pod_container:container_memory_rss{…}` |
| `LIMIT` | `kube_pod_container_resource_limits{…,resource="memory"}` → `kube_pod_container_resource_limits_memory_bytes{…}` (legacy KSM) → `container_spec_memory_limit_bytes{…}` |
| `% LIMIT` | `WSS PEAK / LIMIT` × 100 |
| `CPU@OOM(cores)` | `rate(container_cpu_usage_seconds_total{…}[<window>])` → `node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{…}` → `…:sum_rate{…}` |

All queries are evaluated at `t = OOM time` (the `lastState.terminated.finishedAt` of the killed container), with the namespace/pod/container labels pinned exactly. Default `<window>` is `5m` (`--window`).

`working_set` is the metric the kernel OOM-killer actually decides on, so `WSS PEAK / LIMIT` near or above 100% is the smoking gun for a limit-driven kill. A peak well *below* the limit (say 40%) plus a real OOM means the kill came from node memory pressure, not the cgroup limit — different fix.

### Don't have these metrics? Use `--diagnose`

If columns come back as `-` even though Prometheus is reachable, your scrape jobs probably don't expose the names this script expects (common with stripped-down monitoring stacks, custom relabel rules, or heavily filtered Thanos receivers).

`--diagnose` runs `count(<metric>)` for every known variant and reports which exist:

```bash
./oom-usage.py --diagnose                      # local Prometheus
./oom-usage.py --port-forward --diagnose       # auto-tunnel and probe
```

Example output:

```
Probing Prometheus at http://127.0.0.1:9090 for known OOM-relevant metrics.
An 'OK' line means the metric exists and returns at least one series.
Use the OK names below; the script will pick the first OK variant per kind.

  container_memory_working_set_bytes                                      OK (5234 series)         # Memory working set (cAdvisor)
  node_namespace_pod_container:container_memory_working_set_bytes         missing                  # Memory working set (recording rule)
  container_memory_rss                                                    missing                  # Memory RSS (cAdvisor)
  node_namespace_pod_container:container_memory_rss                       OK (5234 series)         # Memory RSS (recording rule)
  kube_pod_container_resource_limits{resource="memory"}                   OK (1024 series)         # Memory limit (kube-state-metrics)
  kube_pod_container_resource_limits_memory_bytes                         missing                  # Memory limit (KSM legacy)
  container_spec_memory_limit_bytes                                       OK (5234 series)         # Memory limit (cAdvisor)
  container_cpu_usage_seconds_total                                       OK (5234 series)         # CPU usage counter (cAdvisor)
  node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate  missing                # CPU usage (recording, irate)
  container_oom_events_total                                              OK (1024 series)         # OOM event counter (cAdvisor)
  kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}    OK (12 series)           # Pod terminated reason (KSM)
```

How to read it:
- **Everything missing** → no cAdvisor / kube-state-metrics scrape job at all. The Grafana dashboards in this repo also won't work. Fix: deploy `kube-prometheus-stack` (see this repo's `install.sh`) or scrape `kubelet/cadvisor` and `kube-state-metrics`.
- **cAdvisor names missing but recording rules OK** → typical kube-prometheus-stack-with-allowlist setup. The script's fallbacks pick up the recording rules — re-run without `--diagnose` and the table should fill in.
- **`kube_pod_container_status_last_terminated_reason` missing** → kube-state-metrics is not deployed. Detection of OOMKills via Prometheus is impossible; you can still use `oom-extract.py` and `oom-logs.py` (which read directly from the API), but the dashboard `oom-7d-detail` and the `LIMIT` column here will be empty.

### Requirements

- Python 3.8+ (standard library only — uses `urllib`, no `requests`)
- `oc` (or `kubectl` with `--kubectl`) for pod discovery
- A reachable Prometheus / Thanos HTTP endpoint
- For OpenShift Thanos: a bearer token (auto-fetched from `oc whoami -t` if not provided)

### Reaching Prometheus

Four common patterns:

```bash
# 1. Auto port-forward (easiest — script discovers the svc and tunnels for you)
./oom-usage.py --port-forward                           # tries kps + openshift-monitoring services

# 2. Manual port-forward to kube-prometheus-stack (matches this repo's install)
oc -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090
./oom-usage.py                                          # default URL is http://localhost:9090

# 3. OpenShift Thanos querier (production OpenShift clusters)
./oom-usage.py \
  --prometheus-url "https://$(oc -n openshift-monitoring get route thanos-querier -o jsonpath='{.spec.host}')" \
  --insecure                                            # token is auto-fetched via `oc whoami -t`

# 4. Custom Prometheus with explicit bearer token
./oom-usage.py --prometheus-url https://prom.internal --token "$MY_TOKEN"
```

If the URL is unreachable the script aborts with **one** helpful message (exit code `3`) instead of repeating the same connection error per PromQL expression per pod.

`--port-forward` discovery order:

| # | Namespace | Service | Port |
|---|---|---|---|
| 1 | `monitoring` | `kps-kube-prometheus-stack-prometheus` | 9090 |
| 2 | `monitoring` | `prometheus-operated` | 9090 |
| 3 | `openshift-monitoring` | `thanos-querier` | 9091 |
| 4 | `openshift-monitoring` | `prometheus-k8s` | 9090 |

The first one that exists wins. Override the local port with `--local-port N` if 9090 is already in use. The port-forward is torn down automatically when the script exits.

### Usage

```bash
# current namespace, default 5-minute peak window
./oom-usage.py

# specific namespace
./oom-usage.py -n my-app

# all namespaces, look back 15 minutes for the peak (slow-leak case)
./oom-usage.py -A --window 15m

# emit machine-readable JSON
./oom-usage.py -A --json
```

### Example output

```
# OOM usage for namespace oom-test via http://localhost:9090 (lookback=5m)
NAMESPACE  WORKLOAD            POD                          CONTAINER  OOM AT                WSS@OOM   WSS PEAK  RSS@OOM   LIMIT     % LIMIT  CPU@OOM(cores)
oom-test   Deployment/leak-a   leak-a-6f7c9b8d57-x4f2g      app        2026-05-04T08:12:33Z  248.4Mi   255.9Mi   240.1Mi   256.0Mi   99.9%    0.420
oom-test   Deployment/spike-b  spike-b-7d4b5c6789-q9k2p     app        2026-05-04T07:30:11Z  410.7Mi   511.3Mi   402.0Mi   512.0Mi   99.9%    1.230
oom-test   StatefulSet/db      db-0                         postgres   2026-05-04T05:02:48Z  430.0Mi   480.5Mi   420.0Mi   1.0Gi     46.9%    0.090
```

The `db-0` row above (% LIMIT = 47%) is the textbook signature of a **node-pressure** OOM rather than a cgroup-limit OOM — the container wasn't anywhere near its own limit.

### Options

| Flag | Description |
|---|---|
| `-n, --namespace NS` | Specific namespace (default: current `oc project`). |
| `-A, --all-namespaces` | Scan every namespace. |
| `--prometheus-url URL` | Prometheus / Thanos base URL. Default: `http://localhost:9090`. |
| `--token TOKEN` | Bearer token. Defaults to `oc whoami -t` if available. |
| `--insecure` | Skip TLS verification (self-signed Prometheus, OpenShift route with internal CA). |
| `--port-forward` | Discover a Prometheus service and start `oc port-forward` automatically; torn down on exit. Overrides `--prometheus-url`. |
| `--local-port N` | Local port for `--port-forward` (default `9090`). Use a different value if `9090` is taken. |
| `--window DURATION` | PromQL range for `max_over_time` and `rate`. Default `5m`. Increase for slow leaks. |
| `--diagnose` | Probe Prometheus for every known OOM-relevant metric variant, print which ones return data, then exit. Useful when usage columns come back as `-`. |
| `--json` | JSON array, one row per OOMKilled container, with raw byte/cores values. |
| `--kubectl` | Use `kubectl` instead of `oc` for pod discovery. |

### Behaviour notes

- A `-` in any usage column means Prometheus returned no sample for that selector at that timestamp. Common causes: the OOM is older than the Prometheus retention, the URL/token is wrong, or the container labels in cAdvisor don't match (e.g. cluster has a non-default cAdvisor relabel).
- Per-query failures print one warning line to stderr and the script continues — one missing sample doesn't abort the whole table.
- The lookback `--window` controls *both* the peak (`max_over_time`) and the CPU rate (`rate`). For chronic-leak workloads where memory built up over hours, increase to `1h` or `6h` to see the actual climb.
- The script does not dedupe rows: if a pod has multiple OOMKilled containers, you get one line per container (which is what you want for sidecar-heavy workloads).
- Kernel-level confirmation (`dmesg` "Killed process N (java) total-vm:… anon-rss:…") is not fetched here. It's the most accurate single number but requires `oc debug node/<node>` and root on the node — out of scope for a routine extraction script.

### Exit codes

- `0` — completed
- `2` — `oc` / `kubectl` not on `PATH`
- `3` — Prometheus is unreachable, or `--port-forward` could not start a tunnel
- non-zero — a non-Prometheus underlying CLI command failed; stderr is forwarded

---

## Adding a new script

Conventions for additions to this directory:

1. **Self-contained.** No `requirements.txt`. Standard library only unless there's a strong reason.
2. **`oc` first, `kubectl` opt-in.** Mirror the `--kubectl` flag from `oom-extract.py` so scripts work on both vanilla Kubernetes and OpenShift.
3. **Argparse, not positional surprises.** Every script must support `--help`.
4. **JSON mode.** If output is tabular, also offer `--json` for piping.
5. **Keep the script docstring to one line.** Full usage docs live in this README under a new section, indexed in the table at the top.
6. **Make it executable.** `chmod +x scripts/<name>.py` and start the file with `#!/usr/bin/env python3`.
7. **Read-only by default.** If a script mutates cluster state, it must require an explicit `--apply` flag and dry-run by default.
