# Scripts

Helper scripts for working with the OOM observability stack from the command line. Each script is self-contained Python 3 (no third-party deps) and works with both `oc` and `kubectl`.

> Deutsche Version: [`README.de.md`](./README.de.md)

## Index

| Script | Purpose |
|---|---|
| [`oom-extract.py`](#oom-extractpy) | List currently-OOMKilled containers in a namespace (or all) with workload, node, limits, and matching kube events. |
| [`oom-logs.py`](#oom-logspy) | Dump the **pre-OOM** (and optionally current / sibling) container logs for every OOMKilled container in a namespace. |
| [`oom-usage.py`](#oom-usagepy) | Recover **memory and CPU usage at OOM time** by querying Prometheus/Thanos for each OOMKilled container's last terminated timestamp. |
| [`oom-history.py`](#oom-historypy) | Walk each Deployment's **rollout history** (revisions / ReplicaSets) and report OOM status + pre-OOM logs per revision — answers "which version of the deployment started OOMing?". |
| [`oom-rootcause.py`](#oom-rootcausepy) | **Deep root-cause report per OOMKilled pod** in one shot: workload, node, neighbors, events, PVCs, HPA/VPA, services, NetworkPolicies, LimitRanges, ResourceQuotas, Prometheus memory/CPU/network/storage trends, pre-OOM logs, and a verdict (Pattern A/B/C/D/E) with concrete remediation commands. Use this when you need to answer **why** an OOM happened — leak vs spike vs under-provisioned vs node-pressure vs startup. |
| [`fetch-cluster-usage.py`](#fetch-cluster-usagepy) | Report configured CPU/memory **limits & requests vs. real Thanos usage** (current + peak over a window) rolled up at **namespace / workload / pod / container**, plus an **OOM-killed** list from live pod state + Thanos history. Also emits **right-sizing recommendations**, a per-namespace **storage-quota** report, and an applyable **patch manifest** of the recommended resources. Runs locally (oc/kubectl) and in-cluster as a CronJob. |
| [`apply-recommendations.py`](#apply-recommendationspy) | Apply the recommended CPU/memory resources from `fetch-cluster-usage.py` to the cluster. Reads `recommendations-apply.json`, shells out to `kubectl`/`oc patch`. **Server-side dry-run by default**; real changes need `--execute`. Namespaces whose recommendations would exceed their ResourceQuota are skipped with a warning. |

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

- Python 3.6.8+
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

- Python 3.6.8+
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

- Python 3.6.8+ (standard library only — uses `urllib`, no `requests`)
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

## `oom-history.py`

Answers **"which version of the Deployment started OOMing, and what changed between revisions?"** by walking each Deployment's `oc rollout history` — i.e. the chain of ReplicaSets owned by the Deployment — and annotating every revision with its current OOM status and pre-OOM container logs.

**By default the output is filtered to OOM only**: Deployments with no OOMKilled containers in their visible history are hidden, and within OOM-affected Deployments only the OOM-affected revisions are shown. Pass `--all` to bring the full rollout history back when you want comparison context (image diff, resource diff, change-cause across revisions).

For each Deployment in the namespace, the script:

1. Lists every ReplicaSet owned by the Deployment, sorted by `deployment.kubernetes.io/revision` annotation (newest first — same order as `oc rollout history`).
2. For each ReplicaSet (= revision) reports: revision number, RS name, age, replica counts (desired / status / ready / alive pods), `kubernetes.io/change-cause` annotation (set by `kubectl rollout` / CI tools), per-container image and resource limits/requests.
3. For each revision whose pods are still alive, picks up containers with `lastState.terminated.reason == "OOMKilled"` and lists pod / container / node / exit code / restart count / OOM timestamp.
4. Cross-references the namespace's `OOMKilling` kube events against each revision's pods (events typically retain ~1h, so this catches in-flight kills only).
5. With `--logs`, dumps `oc logs <pod> -c <ctr> --previous` for every OOMKilled container, with each block prefixed by a fixed `---` header.

### Honest limitations

- The Kubernetes API only retains pods of currently-existing ReplicaSets. Older ReplicaSets get garbage-collected once the Deployment exceeds `revisionHistoryLimit` (default `10`). Even within retained RSes, the pods of scaled-down revisions are gone — so per-revision OOM detail is reliable for the **active revision** and the most-recent few.
- For older revisions you still see the rollout metadata (image, resources, age, change-cause) — that part comes from the ReplicaSet itself, which sticks around — but the OOM column will be empty even if those revisions did OOM in their day.
- For deeper history use the Grafana drilldown (`oom-7d-detail`, backed by Prometheus / Thanos), or `oom-usage.py` which queries Prometheus at the OOM timestamp.

### Requirements

- Python 3.6.8+
- `oc` (or `kubectl` with `--kubectl`)
- An active session (`oc login`)
- RBAC: `get deployments`, `get replicasets`, `get pods`, `get events`, `get pods/log` in the target namespace(s)

### Usage

```bash
# OOM-affected Deployments + OOM-affected revisions in the current namespace (default view)
./oom-history.py

# specific namespace
./oom-history.py -n my-app

# specific Deployment only
./oom-history.py --deployment leak-a

# all OOMs in the cluster
./oom-history.py -A

# include pre-OOM container logs for each OOMKilled pod
./oom-history.py --logs --tail 200

# filter the log output to error-ish lines
./oom-history.py --logs --grep "out of memory|OutOfMemoryError|fatal|panic"

# JSON output for piping / archiving
./oom-history.py -A --json > rollout-oom-state.json

# show the full rollout history (every Deployment + every revision, even non-OOM)
./oom-history.py --all
```

### Example output (default — OOM-affected only)

```
# Deployment rollout history (OOM-affected only) — namespace oom-test

================================================================================
Deployment: oom-test/leak-a  (replicas: 0/1 ready)
================================================================================

  Revision 4 [ACTIVE, OOM x1]
    ReplicaSet:   leak-a-6f7c9b8d57
    Age:          12m ago (2026-05-04T07:58:11Z)
    Replicas:     desired=1 status=1 ready=0 alive_pods=1
    Change cause: kubectl set image deploy/leak-a app=registry/leak-a:v0.4.0
    Container:    app  image=registry/leak-a:v0.4.0  (mem=256Mi, mem-req=128Mi, cpu=200m)
    OOMKilled containers (1):
      - pod=leak-a-6f7c9b8d57-x4f2g  container=app  node=worker-2  exit=137  restarts=12  finished=2026-05-04T08:10:48Z
    Kube events (reason=OOMKilling) for these pods: leak-a-6f7c9b8d57-x4f2g

      --- pre-OOM log: oom-test/leak-a-6f7c9b8d57-x4f2g/app (oc logs --previous) ---
      | 2026-05-04T08:10:46.881Z INFO  serving request id=abc123 size=82MiB
      | 2026-05-04T08:10:47.402Z WARN  GC pause 940ms
      | 2026-05-04T08:10:47.119Z ERROR java.lang.OutOfMemoryError: Java heap space
```

Non-OOM Deployments and non-OOM revisions of OOM-affected Deployments are filtered out by default. Run `./oom-history.py --all` to see them — useful when you want to compare image / resources / change-cause between the failing revision and the previous good one to figure out what changed.

### Options

| Flag | Description |
|---|---|
| `-n, --namespace NS` | Specific namespace (default: current `oc project`). |
| `-A, --all-namespaces` | Walk every namespace's Deployments. |
| `--deployment NAME` | Only show this Deployment's history. |
| `--all` | Show every Deployment and every revision, including ones with no OOMs. Default is OOM-affected Deployments and OOM-affected revisions only. |
| `--logs` | After each revision's OOM list, dump `oc logs --previous` for each OOMKilled pod/container. |
| `--tail N` | Lines per log block when `--logs` is set. Default `100`. |
| `--grep PATTERN` | Case-insensitive regex applied per log line. Blocks with no match show `[no lines matched filter]`. |
| `--json` | Emit a structured JSON record per Deployment instead of the text report. Suitable for archiving or piping into `jq`. |
| `--kubectl` | Use `kubectl` instead of `oc`. |

### Behaviour notes

- Revision number comes from the standard `deployment.kubernetes.io/revision` annotation on each ReplicaSet — this is exactly what `oc rollout history` reads. So this script's revisions match `oc rollout history deploy/<name>` 1:1.
- `Change cause` is the `kubernetes.io/change-cause` annotation, set automatically when you use `--record` on Kubernetes ≤1.21, or set manually by CI tools (`kubectl annotate rs <name> kubernetes.io/change-cause="..."`). Often empty in modern clusters.
- StatefulSet / DaemonSet rollout histories are **not** walked. Their revisions live on `controllerrevisions` (different mechanism). Open an issue if you want them added.
- The script makes one bulk API call per resource type (`deployments`, `rs`, `pods`, `events`) regardless of how many Deployments exist, then matches in memory by owner UID. So the API load is independent of `revisionHistoryLimit`.
- Combine with `oom-usage.py` to get the actual memory/CPU values at OOM time from Prometheus, and with the Grafana dashboard `oom-7d-detail` for the long historical view.

### Exit codes

- `0` — completed (output may be empty if no Deployments / no OOMs)
- `2` — `oc` / `kubectl` not on `PATH`
- non-zero — underlying CLI command failed; stderr is forwarded

---

## `oom-rootcause.py`

Built for the question **"why did this OOM happen — leak, traffic spike, undersized limit, noisy node, or startup overrun?"** It pulls every Kubernetes resource that could explain an OOM in a single bulk fetch (one `get` per resource type, regardless of how many pods OOMed), correlates them with Prometheus memory / CPU / network / storage signal at the OOM timestamp, and prints a verdict per pod with concrete remediation commands.

The verdict maps directly onto the patterns documented in the project's main `README.md` and the dashboard `oom-7d-detail`:

| Pattern | Trigger condition |
|---|---|
| **A — Memory leak** | Memory `deriv(1h)` > +1 MiB/min sustained AND peak/limit ≥ 85% |
| **B — Spike / large request** | Network rx 1m ≥ 3× the 30-minute baseline rate |
| **C — Under-provisioned limit** | peak/limit ≥ 95% AND `deriv` flat (no leak) |
| **D — Node pressure / noisy neighbor** | ≥ 2 other pods OOMed on the same node within 1h |
| **E — Startup overrun** | Pod lifetime before OOM < 60 s AND restart count ≥ 2 |
| **? — Indeterminate** | None of the above match with confidence (Prometheus missing or signal ambiguous) |

Patterns are evaluated **in priority order** (E → D → A → B → C). The first match wins, so a pod that boots and OOMs in 30 s gets diagnosed as E even if its peak is also at the limit.

### What gets fetched

Per pod, in one bulk pass per resource type:

| Source | Used for |
|---|---|
| `pods` | OOM target discovery (`lastState.terminated.reason == "OOMKilled"`); spec (image, resources, probes, args, QoS); restart count; OOM timestamps |
| `replicasets`, `deployments`, `statefulsets`, `daemonsets` | Workload resolution (Deployment via RS lookup); rollout revision number |
| `nodes` | Node `MemoryPressure` / `DiskPressure` / `PIDPressure` conditions; capacity / allocatable |
| `events` (filtered to last 1h) | Recent `OOMKilling`, `BackOff`, `Failed`, etc. for the pod |
| `services` | Services whose selector matches the pod's labels (so you can see who's calling it) |
| `networkpolicies` | NetworkPolicies that target the pod |
| `pvcs` | Mounted PersistentVolumeClaims, status, capacity, storage class |
| `hpa` (autoscaling/v2) | Horizontal autoscaler targeting the workload, current min/max/metrics |
| `vpa` (autoscaling.k8s.io/v1, optional) | Vertical autoscaler recommendation if installed |
| `limitranges`, `resourcequotas` | Namespace-level limits that may be capping memory/CPU |
| Prometheus | `WSS@OOM`, `WSS peak 5m / 1h`, `deriv(1h)`, memory limit, CPU usage + throttling, network rx/tx (with replica dedup), 30-minute rx baseline, container fs read/write rate |
| Loki — *no*, this script doesn't query Loki | (use `oom-logs.py` or the Grafana drilldown for full log search) |
| `oc logs --previous` | Last 30 lines, filtered by `--grep` regex (default: `error\|oom\|killed\|out ?of ?memory\|fatal\|exception`) |

### Requirements

- Python 3.6.8+
- `oc` (or `kubectl` with `--kubectl`)
- An active session (`oc login`)
- A reachable Prometheus / Thanos endpoint (optional, but the verdict is much weaker without it)
- RBAC: `get pods`, `replicasets`, `deployments`, `statefulsets`, `daemonsets`, `events`, `services`, `pvcs`, `hpa`, `vpa`, `limitranges`, `resourcequotas`, `networkpolicies`, `nodes`, `pods/log` in the target scope

### Usage

```bash
# full per-pod report for every OOMKilled container in the current namespace,
# expects a Prometheus port-forward at localhost:9090
./oom-rootcause.py

# specific namespace
./oom-rootcause.py -n my-app

# one specific pod (useful when several have OOMed)
./oom-rootcause.py -n my-app --pod leak-app-84d95bfcd6-8qlgh

# include filtered pre-OOM logs at the bottom of each report
./oom-rootcause.py -n my-app --logs

# custom log filter (case-insensitive regex)
./oom-rootcause.py -n my-app --logs --grep "OutOfMemoryError|GC|allocation failed"

# one-line-per-OOM triage summary across the whole cluster
./oom-rootcause.py -A --summary

# OpenShift Thanos as the Prometheus source
./oom-rootcause.py -A \
  --prometheus-url "https://$(oc -n openshift-monitoring get route thanos-querier -o jsonpath='{.spec.host}')" \
  --insecure

# JSON output for archiving / piping into jq
./oom-rootcause.py -A --json > rootcause.json

# run without Prometheus (verdict will be limited but still useful)
./oom-rootcause.py -n my-app --no-prometheus
```

### Example output (one pod, abbreviated)

```
================================================================================
OOM ROOT-CAUSE ANALYSIS — oom-test/leak-app-84d95bfcd6-8qlgh/leak
================================================================================

CONTEXT
  Workload:        Deployment/leak-app  (rev 4, ReplicaSet leak-app-84d95bfcd6)
  Node:            worker-2
  OOM at:          2026-05-04T08:12:33Z  (3m ago)
  Started:         2026-05-04T08:00:11Z
  Lifetime:        742s before OOM
  Exit code:       137   (137 = OOMKilled)
  Restart count:   12

CONFIGURATION
  Image:           registry/leak-app:v0.4.0
  Memory limit:    256Mi   request: 128Mi
  CPU limit:       200m    request: 100m
  QoS class:       Burstable
  Probes:          livenessProbe, readinessProbe

MEMORY (Prometheus)
  WSS at OOM:      254.1Mi
  Peak (last 5m):  255.9Mi
  Peak (last 1h):  256.0Mi
  Slope (deriv 1h):+12.4 MiB/min
  Memory limit:    256.0Mi

CPU (Prometheus)
  Usage (5m avg):  0.420 cores
  Throttle (5m):   0.078 cores-equivalent

NETWORK (Prometheus)
  Rx 5m avg:       125.3KiB/s
  Tx 5m avg:       82.0KiB/s
  Rx 1m (recent):  130.1KiB/s
  Rx baseline 30m: 122.4KiB/s

STORAGE
  PVC pvc-leak-data: status=Bound, capacity=10Gi, sc=cassandra-storage, access=ReadWriteOnce

NODE
  Name:            worker-2
  Capacity:        memory=16265564Ki, cpu=8
  Allocatable:     memory=15871420Ki, cpu=7900m
  MemoryPressure  False  (KubeletHasSufficientMemory)
  Ready           True   (KubeletReady)

NEIGHBOR PODS — OOMs on the same node (worker-2) within 1h
  (none — no node-pressure pattern at this timestamp)

AUTOSCALING
  (no HPA targets this workload)
  (no VPA targets this workload — install in recommender mode for right-sizing hints)

SERVICES & NETWORK POLICY
  Service leak-app: type=ClusterIP, ports=8080

NAMESPACE CONSTRAINTS
  (no LimitRange / ResourceQuota in this namespace)

RECENT EVENTS — pod, last 1h (2 entries)
  [2026-05-04T08:12:33Z] Warning/OOMKilling (x2): Memory cgroup out of memory: ...
  [2026-05-04T08:09:48Z] Warning/OOMKilling (x1): Memory cgroup out of memory: ...

VERDICT
  Most likely cause:  A - MEMORY LEAK
  Evidence:
    - memory deriv (1h): +12.4 MiB/min sustained → leak signature
    - peak/limit: 100.0% — limit is the ceiling, not the cause
  Recommended action:
    Memory grows steadily without releasing — limit reached → OOM → restart → repeat.
    Capture a heap dump while memory is high (BEFORE the OOM):
      oc exec leak-app-84d95bfcd6-8qlgh -c leak -- jcmd 1 GC.heap_dump /tmp/heap.hprof   # JVM
      oc exec leak-app-84d95bfcd6-8qlgh -c leak -- curl localhost:6060/debug/pprof/heap > heap.out   # Go
    Don't just raise the limit — the leak will eat any new ceiling.
================================================================================
```

For a long list of OOMs, prefer `--summary`:

```
NAMESPACE  POD                          CONTAINER  PATTERN                     OOM AGE
oom-test   leak-app-84d95bfcd6-8qlgh    leak       A - MEMORY LEAK             3m ago
oom-test   spike-app-7d4b5c6789-q9k2p   app        B - SPIKE / LARGE REQUEST   42m ago
oom-test   startup-app-58f65dd8c9-bv..  startup    E - STARTUP OVERRUN         5m ago
```

### Options

| Flag | Description |
|---|---|
| `-n, --namespace NS` | Specific namespace (default: current `oc project`). |
| `-A, --all-namespaces` | Walk every namespace's pods. |
| `--pod NAME` | Restrict to a single pod by name. Useful when several OOMed and you want the deep view of one. |
| `--summary` | One-line-per-OOM table (namespace, pod, container, pattern, OOM age) instead of the full per-pod report. Run this first on a busy cluster. |
| `--logs` | Append the filtered pre-OOM container logs (`oc logs --previous --tail=30`) to each report. |
| `--grep PATTERN` | Case-insensitive regex applied to log lines when `--logs` is on. Default: `error\|oom\|killed\|out ?of ?memory\|fatal\|exception`. |
| `--prometheus-url URL` | Prometheus / Thanos base URL. Default: `http://localhost:9090`. |
| `--token TOKEN` | Bearer token for Prometheus. Default: `oc whoami -t` if available. |
| `--insecure` | Skip TLS verification (self-signed Prometheus / OpenShift Thanos route). |
| `--no-prometheus` | Skip Prometheus entirely. The verdict will only see Kubernetes-side signal (lifetime, restart count, neighbor OOMs); patterns A / B / C become unreachable but D and E still work. |
| `--diagnose` | Probe Prometheus for every metric this script depends on, list which ones return data, identify missing scrape sources (kubelet/cAdvisor vs kube-state-metrics), print install hints, then exit. Use this when the verdict shows `-` everywhere or returns "INDETERMINATE — Prometheus not reachable". |
| `--json` | Structured JSON output. Pod / Service / NetworkPolicy / etc. raw objects are stripped (they're huge); the verdict, target identifiers, metrics, and event summaries are kept. |
| `--kubectl` | Use `kubectl` instead of `oc`. |

### When to use which script

| You want… | Use |
|---|---|
| A 7-day cluster-wide list of OOMs in Grafana with click-through detail | dashboard `oom-7d-detail` |
| A current-state table (one row per OOM) | `oom-extract.py` |
| Just the pre-OOM container logs | `oom-logs.py` |
| The exact memory/CPU values at OOM time from Prometheus | `oom-usage.py` |
| Did the OOM start with a specific Deployment revision? | `oom-history.py` |
| **Why did it OOM — leak / spike / under-sized / noisy node / startup?** | **`oom-rootcause.py`** |

### Behaviour notes

- **One bulk fetch per resource type.** Even on a cluster with 100 OOMed pods across 50 namespaces, the script makes ~13 `oc get` calls total. All correlation is in-memory.
- **Prometheus failure is non-fatal.** If the endpoint is unreachable the script writes one warning to stderr and continues with K8s-only signal — patterns D and E still work, A/B/C become best-effort.
- **HA replica dedup is built in.** Every PromQL expression that crosses replicas wraps `max by (namespace,pod,container) (...)` or `max without (prometheus_replica) (...)`, so the script works on Thanos with HA Prometheus pairs without `many-to-many matching not allowed` errors.
- **VPA queries are tried, missing CRD is silent.** If your cluster has no VerticalPodAutoscaler, the `oc get vpa` call returns empty and the verdict skips that block — no failure.
- **Verdicts are heuristic, not gospel.** The thresholds (1 MiB/min for leaks, 3× rx burst for spikes, 95% for under-provisioning) are conservative defaults that match the behaviour patterns documented in the project README. Read the `Evidence:` block under the verdict — it always shows the numbers that triggered the classification, so you can decide if the heuristic fits.
- **The "?" verdict isn't a failure.** If signal is ambiguous (slow climb but limit not yet reached, or no Prometheus, or only one prior OOM with no time series), the script prints `? - INDETERMINATE` and points you at the longer-window dashboards / scripts.

### Exit codes

- `0` — completed (output may be empty if nothing OOMed)
- `2` — `oc` / `kubectl` not on `PATH`
- non-zero — a non-Prometheus underlying CLI command failed; stderr is forwarded

---

## `python/reconcile-state.py`

Reconciles the **current** cluster state (`snapshot.json`) against the **desired** manifests (`desired/*.yaml`) for every namespace under a `state-loop-<ts>/` output directory produced by `fetch-cluster-state.py`. Matching is **presence-level** only: resources are keyed by `(kind, name)` — field values are not compared.

Writes one CSV per stage:

| Output file | Content |
|---|---|
| `_reconcile-<stage>.csv` | One row per `(kind, name)` per namespace; columns below. |

### CSV columns

| Column | Values / meaning |
|---|---|
| `stage` | Stage name (e.g. `phase`, `test`, `prod`). |
| `namespace` | Kubernetes namespace name. |
| `kind` | Resource kind (`Deployment`, `HorizontalPodAutoscaler`, `Namespace`, etc.). |
| `name` | Resource name. |
| `in_current` | `True` / `False` — found in `snapshot.json`. |
| `in_desired` | `True` / `False` — found in `desired/*.yaml`. |
| `status` | `IN_SYNC`, `MISSING_IN_CLUSTER`, or `NOT_DESIRED`. |

**Status taxonomy:**

- `IN_SYNC` — present in both snapshot and desired manifests.
- `MISSING_IN_CLUSTER` — declared in desired manifests but absent from the cluster snapshot.
- `NOT_DESIRED` — present in the cluster snapshot but not covered by any desired manifest.

### Requirements

- Python 3.6.8+
- PyYAML (optional; the script falls back to a line extractor if not available)
- A `state-loop-<ts>/` directory with `by-stage/<stage>/<ns>/snapshot.json` + `desired/*.yaml`

### Usage

```bash
python3 scripts/python/reconcile-state.py \
  --input-dir reports/state-loop-<ts>/
```

Output CSVs are written to the root of `--input-dir`:

```
reports/state-loop-<ts>/_reconcile-phase.csv
reports/state-loop-<ts>/_reconcile-prod.csv
...
```

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

---

## `fetch-cluster-usage.py`

Self-contained (stdlib-only) report of **limits vs. real usage** across every
`pid-*` namespace, with OOM-killed containers from both live pod state and
Thanos history. Auto-detects its environment: in-cluster it uses the Kubernetes
REST API with the pod ServiceAccount token; locally it shells out to `oc`/`kubectl`.

### What it reports

For each level — namespace, workload (Deployment/StatefulSet/DaemonSet), pod,
container — it shows CPU request/limit/now/peak (+peak-util%) and memory
request/limit/now/peak (+peak-util%), plus OOM counts. Usage comes from Thanos
(`container_cpu_usage_seconds_total` rate, `container_memory_working_set_bytes`,
current + `*_over_time` peak/avg over `--window`).

Workloads are derived from pod `ownerReferences`, so Deployments, StatefulSets,
and DaemonSets all appear. Declared workloads with **no running pods** (e.g. a
StatefulSet scaled to 0) are also listed — marked `(idle: 0 pods)` with their
pod-template configured limits and `-` usage — so nothing is hidden just because
it's idle. These idle rows don't count toward the namespace totals (they reserve
nothing). Pass `--no-idle-workloads` to omit them.

### Usage

```bash
# Local (RKE2, kubectl), all pid-* namespaces, last 24h
python3 scripts/python/fetch-cluster-usage.py --kubectl

# Local without Thanos (configured limits + live OOM only)
python3 scripts/python/fetch-cluster-usage.py --kubectl --no-thanos

# Explicit Thanos URL + token (e.g. OpenShift querier)
python3 scripts/python/fetch-cluster-usage.py \
  --thanos-url https://thanos-querier-openshift-monitoring.apps.example.com \
  --insecure --window 7d

# One namespace, write CSV/JSON to a directory
python3 scripts/python/fetch-cluster-usage.py --namespace pid-002-api-test-01-blue \
  --output-dir ./reports/usage

# Trim text verbosity to just rollups
python3 scripts/python/fetch-cluster-usage.py --level namespace,workload
```

### In-cluster (CronJob → GitLab)

`manifests/cluster-usage-cronjob.yaml` ships a cluster-wide CronJob that **every
5 days** runs the full pipeline and pushes the reports to a GitLab repo over
HTTPS with a PAT — no image build and no runtime internet egress (a static `oc`
binary is copied from a pinned CLI image; the stdlib-only scripts come from a
ConfigMap). The pipeline (initContainers run in order; all must succeed before
the push):

| Step | Image | Does |
|---|---|---|
| `get-oc` | `quay.io/openshift/origin-cli` | copies a static `oc` into a shared `/tools` emptyDir |
| `fetch` | `python:3.12-slim` | `fetch-cluster-usage.py --in-cluster` → `/work` (+ logs to stdout/Loki) |
| `apply` | `python:3.12-slim` | `apply-recommendations.py --oc` **server-side dry-run** → `/work/apply-report.txt` (changes nothing) |
| `push` | `alpine/git` | `git clone`/`commit`/`push` `/work` → `<repo>/<subdir>/<YYYY-MM-DD>/` |

Setup:

```bash
# 1. namespace + RBAC + CronJob
oc apply -f manifests/cluster-usage-cronjob.yaml

# 2. ship BOTH scripts via the ConfigMap (re-run after editing either)
oc create configmap cluster-usage-scripts \
  --from-file=fetch-cluster-usage.py=scripts/python/fetch-cluster-usage.py \
  --from-file=apply-recommendations.py=scripts/python/apply-recommendations.py \
  -n forensic-usage --dry-run=client -o yaml | oc apply -f -

# 3. GitLab PAT (write_repository scope) — never commit the token
oc create secret generic gitlab-push \
  --from-literal=pat='glpat-xxxxxxxxxxxxxxxxxxxx' -n forensic-usage
```

Then edit the CronJob env to point at your GitLab target (`GITLAB_HOST`,
`GITLAB_PROJECT_PATH`, `GIT_BRANCH`, `GIT_NAME`, `GIT_EMAIL`, `REPORT_SUBDIR`)
and set `THANOS_URL`. On OpenShift, uncomment the `cluster-monitoring-view`
binding so the SA token is accepted by `thanos-querier`; on RKE2/kube-prometheus-stack
Thanos in-cluster usually needs no auth.

**The apply step is dry-run by default** — it validates the right-sizing patches
against the live cluster and records them in `apply-report.txt`, but mutates
nothing. To actually patch workloads on the timer, add `--execute` to the
`apply` container's args (this rolls prod workloads unattended every 5 days — read
the warning above that arg first). Server-side dry-run still needs `patch`/`update`
RBAC, which the manifest grants on Deployments/StatefulSets.

> **"Every 5 days" caveat:** the schedule is `0 6 */5 * *`. Cron's `*/5`
> day-of-month resets each month, so the gap across a month boundary can be 1–4
> days rather than exactly 5 — the conventional CronJob approximation.

Because the reports now live in GitLab (commit history = the per-run audit
trail), this revision drops the report PVC and the optional HTTP viewer that the
earlier daily version used.

### The report files (`--output-dir`)

`--output-dir DIR` writes the full machine-readable bundle (and `--format text`
still logs the human table to **stdout**, captured by Loki/EFK). The in-cluster
CronJob points `--output-dir` at an emptyDir and pushes the result to GitLab (see
[In-cluster](#in-cluster-cronjob--gitlab) above); locally you just point it at a
folder. The bundle holds a combined report plus a `by-stage/<stage>/` sub-report
per stage, the gathered `apply/` manifests, and a `LEGEND.md` describing every
column and `level` row:

```
<output-dir>/   (e.g. one commit under reports/2026-06-08/ in GitLab)
  resources.csv               # cluster/stage/namespace/workload/pod/container + per-PVC (level=pvc) rows
  resources-human.csv         # same rows, every value with its unit (200m, 6.3Mi, 4.9%)
  namespaces.csv              # one row per namespace (cpu/mem + storage quota: totals & per-storageclass)
  namespaces-human.csv        # same, unit-formatted
  recommendations.csv         # per-workload right-sizing (only hot workloads; raw numbers)
  recommendations-human.csv   # same, unit-formatted
  recommendations-apply.yaml  # applyable patch manifest (one ResourcePatch per hot workload)
  recommendations-apply.json  # machine-readable twin, consumed by apply-recommendations.py
  ooms.csv                    # one row per OOM-killed container
  report.json                 # nested tree + cluster_totals + stage_summaries
  LEGEND.md                   # Legende (German): was jede Spalte und jede `level`-Zeile bedeutet
  apply/                      # all apply manifests gathered in one place (see below)
    all.yaml   all.json       # every namespace, combined
    test.yaml  test.json      # one pair per stage — for a staged rollout
    prod.yaml  prod.json
    test/                     # one pair per namespace — for a dedicated apply
      pid-1-app-test-01.yaml  .json
      pid-2-api-test-01.yaml  .json
    prod/
      pid-3-web-prod-01.yaml  .json
    ...
  by-stage/
    ref/   {the same files}   # only ref namespaces
    test/  {...}
    prod/  {...}
    ...
```

The **`apply/` folder** gathers only the apply manifests so you never have to
dig through `by-stage/`, at three granularities:

- `all.json` — the full set across every namespace;
- `<stage>.json` — one whole stage (`apply/test.json`, `apply/prod.json`);
- `<stage>/<namespace>.json` — a single namespace, for a **dedicated** apply
  (`apply/prod/pid-3-web-prod-01.json`). A namespace with nothing to apply (no
  hot workloads and not quota-skipped) gets no file.

Point the applier straight at whichever scope you want:

```bash
./apply-recommendations.py --manifest reports/usage/apply/all.json --execute            # everything
./apply-recommendations.py --manifest reports/usage/apply/prod.json --execute           # one stage
./apply-recommendations.py --manifest reports/usage/apply/prod/pid-3-web-prod-01.json \
  --execute                                                                              # one namespace
```

(The top-level `recommendations-apply.json` is the same as `apply/all.json` — the
folder just keeps the manifests together and split by stage and namespace.)

`resources.csv` has one row per scope, identified by its `level` column:
`cluster` (grand total) → `stage` (per stage) → `namespace` → `workload` → `pod`
→ `container`. CPU is in cores, memory in bytes; see `LEGEND.md` for the rest.

At the `namespace` level (and summed into `stage`/`cluster`), the request/limit
columns come from the namespace's **ResourceQuota** *Hard* caps, and the
`*_request_used` / `*_limit_used` columns (next to `*_now`) from its *Used*
counters — i.e. how much the running pods book against the quota. `workload`/
`pod`/`container` rows keep their summed pod-spec requests/limits (quotas are
namespace-scoped, so the `_used` columns are empty there). A namespace without a
quota falls back to the summed pod specs.

#### Right-sizing recommendations

`recommendations.csv` (raw) and `recommendations-human.csv` (unit-formatted)
carry one row per **hot** workload — a workload whose CPU or memory peak over
`--window` is unbounded or above the target utilisation (`--target-util`, default
80 %). For each hot dimension: `request = round_up(peak)`,
`limit = round_up(peak / target_util)` (CPU to the next 10m, memory to the next
Mi). Quiet workloads are omitted.

#### Storage (in namespaces.csv and resources.csv)

Storage lives in the main CSVs — there is no separate `storage.csv`.

- **`namespaces.csv`** gains the per-namespace `requests.storage` quota from the
  ResourceQuota: totals `storage_used_bytes` / `storage_hard_bytes` /
  `storage_used_pct` (Used, Hard, Used÷Hard), plus a per-StorageClass triple
  `<class>_used_bytes` / `<class>_hard_bytes` / `<class>_used_pct` over a fixed
  class set (a class the quota doesn't mention shows `0`, so the matrix is dense).
  It also gets the **actual PVC usage per StorageClass**: `<class>_pvc_bytes` (sum
  of that class's PVC capacities in the namespace) and `<class>_pvc_pct` (that
  class's share of the namespace's total PVC storage) — e.g. see that file-silver
  is 62.5 % of a namespace's storage, independent of any quota.
- **`resources.csv`** gains the storage totals on the `namespace`/`stage`/`cluster`
  rows, and one **`level=pvc`** row per PersistentVolumeClaim carrying `pvc`
  (name), `storageclass`, `storageclass_description` (from the StorageClass's
  `description` annotation), and `storage_capacity_bytes` (provisioned capacity).
  The cpu/mem columns are blank on `pvc` rows and vice-versa.

The used % is the storageclass quota (Used÷Hard); per-PVC disk fill is not
queried (capacity only).

#### Apply manifest

`recommendations-apply.yaml` turns the recommendations into an applyable,
reviewable patch: one `ResourcePatch` document per hot workload that sets the
recommended container `requests`/`limits` as a strategic-merge patch — **workload
resources only, never the ResourceQuota.** A namespace whose summed
recommendations would exceed its ResourceQuota is **skipped entirely** and named
in a `SKIPPED` header comment (raise the quota first, then re-export).
`recommendations-apply.json` is the machine-readable twin that
[`apply-recommendations.py`](#apply-recommendationspy) consumes (stdlib JSON — no
YAML dependency). See that section for the dry-run / execute workflow.

Each of these views can also be streamed to stdout without `--output-dir` via
`--format`, e.g.:

```bash
# preview the recommendations table
python3 scripts/python/fetch-cluster-usage.py --kubectl --format recommendations-human

# the per-namespace summary incl. storage quota, unit-formatted
python3 scripts/python/fetch-cluster-usage.py --kubectl --format namespaces-human

# just the apply manifest (pipe straight to a file or kubectl review)
python3 scripts/python/fetch-cluster-usage.py --kubectl --format recommendations-apply

# only write files, no stdout (pair with --output-dir)
python3 scripts/python/fetch-cluster-usage.py --kubectl --format none --output-dir ./reports/usage
```

`--target-util PCT` (default 80) drives both the recommendation CSVs and the
apply manifest.

**Run history / retention.** The in-cluster CronJob pushes each run into a dated
folder in GitLab (`<subdir>/<YYYY-MM-DD>/`), so the repo's commit history *is* the
retention — no PVC, reader pod, or viewer to manage. Prune old runs with GitLab's
normal git tooling (or just leave them; the bundles are tiny CSV/JSON/YAML).

For a PVC- or local-disk-based deployment instead of (or in addition to) the
GitLab push, the script's own flags still apply: `--date-subdir` gives each run
its own dated folder rather than overwriting, and `--retention-days 30` prunes
date-named folders older than 30 days (only date-named folders are ever removed).

### Requirements

- Python 3.6.8+ (stdlib only) — runs on the RHEL 8 / OpenShift system `python3`
  as well as the `python:3.12-slim` CronJob image.
- Local: `oc` or `kubectl` (`--kubectl`) with an active session; RBAC `get`/`list`
  on pods, replicasets, deployments, statefulsets, daemonsets, resourcequotas,
  persistentvolumeclaims, storageclasses, and namespaces.
  (With `oc` it discovers namespaces via `oc get projects` — the projects you can
  see — matching `fetch-cluster-state-loop.sh`; `kubectl` lists `namespaces`.)
- In-cluster: the ServiceAccount RBAC from the manifest.
- A reachable Thanos/Prometheus query API for usage columns (optional; degrades
  to `-` when absent).

---

## `apply-recommendations.py`

Closes the loop on `fetch-cluster-usage.py`: it takes the recommended CPU/memory
resources and **applies them to the cluster** — safely. It reads the
`recommendations-apply.json` sidecar (the machine-readable twin of
`recommendations-apply.yaml`) and, for each workload, shells out to
`kubectl`/`oc patch --type=strategic` to update the container `requests`/`limits`.
It **only touches workload resources, never the ResourceQuota.**

Two safety properties are built in:

- **Server-side dry-run by default.** With no flag, every patch runs with
  `--dry-run=server`: the API server validates it against the live object
  (schema, admission, quota) and reports the result, but **nothing changes**.
  Real mutation requires the explicit `--execute` flag.
- **Quota-exceeding namespaces are skipped at the source.** Any namespace whose
  summed recommendations would exceed its ResourceQuota was already left out of
  the manifest by `fetch-cluster-usage.py` (listed in its `SKIPPED` block). The
  applier re-prints that warning so the caveat is visible at apply time — raise
  the quota manually, re-run the export, then re-apply.

Stdlib only (`json` + `subprocess`): no PyYAML, no Kubernetes client — it runs in
any fresh shell with just `kubectl`/`oc` on `PATH`.

### Requirements

- Python 3.6.8+ (standard library only)
- `kubectl` (default) or `oc` (`--oc`) on `PATH`, with an active session pointed
  at the cluster you want to change
- RBAC: `patch` on the target workload kinds (`deployments`, `statefulsets`, …)
  in the affected namespaces
- A `recommendations-apply.json` produced by `fetch-cluster-usage.py`

### Usage

```bash
# 1. generate the reports + apply manifests (part of a normal report run)
python3 scripts/python/fetch-cluster-usage.py --kubectl --output-dir ./reports/usage
#    -> manifests gathered under ./reports/usage/apply/  (all.json + one per stage)

# 2. preview — server-side dry-run, validates against the live cluster, changes nothing
./apply-recommendations.py --manifest ./reports/usage/apply/all.json

# 3. apply for real
./apply-recommendations.py --manifest ./reports/usage/apply/all.json --execute

# staged rollout: test first, verify, then prod
./apply-recommendations.py --manifest ./reports/usage/apply/test.json --execute
./apply-recommendations.py --manifest ./reports/usage/apply/prod.json --execute

# best-effort: apply only the patches that pass validation, skip the rest
./apply-recommendations.py --manifest ./reports/usage/apply/all.json --execute --skip-failed

# write a text report of what was applied (one line per recommendation)
./apply-recommendations.py --manifest ./reports/usage/apply/all.json --execute \
  --report ./reports/usage/apply-report.txt

# OpenShift (oc) + a specific kube context
./apply-recommendations.py --oc --context prod-cluster \
  --manifest ./reports/usage/apply/all.json --execute
```

### Example output (dry-run)

```
apply-recommendations: DRY-RUN (server, no changes) — 1 workload(s)
SKIPPED namespaces (raise the ResourceQuota first, then re-export):
  pid-9-api-prod-01: cpu_limit sum 4000m > quota 1000m; mem_limit sum 10Gi > quota 1Gi
  pid-3-web-prod-01/Deployment web  web: cpu req 500m→900m, lim 1000m→1130m | mem req 150Mi→200Mi, lim 200Mi→250Mi
  OK   pid-3-web-prod-01/Deployment web
done: 1 processed, 0 failed
```

`--execute` runs the same calls without `--dry-run=server`; each line then shows
`OK` / `SKIP` / `FAIL` for the real patch.

### Options

| Flag | Description |
|---|---|
| `--manifest PATH` | Path to `recommendations-apply.json`. Default: `recommendations-apply.json` in the current directory. |
| `--execute` | Apply for real. Without it the run is a server-side dry-run that changes nothing. |
| `--context NAME` | kube context (passed through as `--context`). |
| `--oc` | Use the `oc` binary instead of `kubectl`. |
| `--kubectl PATH` | kubectl binary path (default `kubectl`). |
| `--report PATH` | Also write a text report of the run to `PATH`: one line per **applied** recommendation (`<namespace>/<kind> <name>  <container: old→new>`), with a header (mode, timestamp, counts) and a `#`-comment footer listing anything not applied — failures, not-found workloads, and quota-skipped namespaces. |
| `--skip-failed` | **Best-effort.** Validate each patch with a server dry-run first and apply only the ones that pass; patches that would fail are **skipped** (not fatal) so the rest still apply and the run exits `0`. Use with `--execute`. |

### Applied-recommendations report (`--report`)

`--report PATH` records what the run did, with **one line per applied
recommendation** so it's easy to diff, grep, or paste into a change ticket. It
works in dry-run too (the header marks the mode), so you can capture the
intended change set before executing. Example:

```
# apply-recommendations report — EXECUTE (mutating) — 2026-06-24T19:16:31Z
# 2 applied, 0 failed, 1 not found, 1 namespace(s) skipped (quota)
# Applied recommendations (one line each):
pid-1060-...-417-prodda/Deployment monitoring-console  monitoring-console: cpu req 250m→250m, lim 500m→500m | mem req 238Mi→401Mi, lim 317Mi→501Mi
pid-1060-...-417-prodda/Deployment web  web: cpu req 500m→900m, lim 1000m→1130m
#
# SKIPPED — workload not found on the cluster:
#   pid-9-gone-prod-01/Deployment ghost
#
# SKIPPED namespaces (raise the ResourceQuota first):
#   pid-1000-...-shared-prodda: cpu_limit sum 163766m > quota 155000m; mem_limit sum 542402Mi > quota 534057Mi
```

### Behaviour notes

- **Dry-run is the default on purpose.** You have to opt in to mutate the
  cluster. Run the dry-run first and read the `old→new` lines before `--execute`.
- **A workload deleted between export and apply is non-fatal.** If `kubectl`
  reports `NotFound`, that workload is marked `SKIP (not found)` and the run
  continues to the next one.
- **Strategic-merge keeps untouched fields.** Only the hot dimensions are in the
  patch; cold `requests`/`limits` keys and other container fields are merged by
  key on the live object, so nothing else is disturbed. Multi-container workloads
  are patched per container name.
- **Exit code reflects success.** `0` when every patch succeeded (or there was
  nothing to apply); `1` if any patch failed under `--execute`; `2` if the
  manifest is missing or unreadable.

### Exit codes

- `0` — completed (all patches OK, or header-only manifest with nothing to apply)
- `1` — at least one patch failed under `--execute`
- `2` — manifest not found / unreadable
