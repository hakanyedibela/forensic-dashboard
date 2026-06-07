# Cluster Usage Report (`fetch-cluster-usage.py`) — Design

**Date:** 2026-06-07
**Status:** Approved (brainstorm), pending implementation plan
**Author:** Hakan Yedibela + Claude

## 1. Goal

A single self-contained Python 3 script that runs over all `pid-*` namespaces
("stages") and reports, for every level of the Kubernetes object hierarchy
where CPU/memory is consumed:

- the **configured** `limits`/`requests` for CPU and memory, and
- the **real** CPU/memory usage pulled from **Thanos** (current value plus
  peak/avg over a lookback window), and
- a list of all **OOM-killed** containers (from both live pod state and Thanos
  history).

Rollups are produced at four levels: **namespace → workload (Deployment /
StatefulSet / DaemonSet) → pod → container**.

The script must run both **as a Kubernetes CronJob in-cluster** and **from a
developer laptop**, with no third-party Python dependencies (stdlib only),
consistent with the rest of `scripts/python/`.

### Target environments

| | Production | Local test |
|---|---|---|
| Cluster | OpenShift | RKE2 |
| CLI binary | `oc` | `kubectl` (`--kubectl`) |
| Thanos service | `openshift-monitoring/thanos-querier:9091` | kube-prometheus-stack Thanos in `monitoring` |
| Thanos auth | **required** — SA bound to `cluster-monitoring-view` (in-cluster) or `oc whoami -t` (local); often `--insecure` for the route | **none** typically |

Both must work from the same code path: CLI backend auto-picks `oc` then
`kubectl` (override `--kubectl`); Thanos endpoint + auth degrade per §4.2.

## 2. Deliverables

| Path | Purpose |
|---|---|
| `scripts/python/fetch-cluster-usage.py` | The script. |
| `scripts/python/tests/test_fetch_cluster_usage.py` | Pytest unit tests (no cluster needed). |
| `manifests/cluster-usage-cronjob.yaml` | Cluster-wide CronJob + RBAC + ConfigMap. |
| `scripts/README.md` (+ `README.de.md`) | Documentation entry for the new script. |

## 3. Reuse

The script reuses patterns already proven in the repo rather than reinventing:

- `Thanos` HTTP client class, time parsing, and the port-forward fallback from
  `fetch-thanos-metrics.py` (incl. `QUERIER_CANDIDATES`).
- Owner/workload resolution (`workload_for`), `parse_mem`, `fmt_bytes`,
  `parse_iso`, and OOM-target discovery patterns from `fetch-cluster-oom.py`.
- Stage detection convention `pid-<id>-<app>-<STAGE>-<num>-<suffix>` with
  keywords `ref/prod/test/phase/pnext`, ported from the loop shell scripts to
  Python.

## 4. Architecture

### 4.1 Dual Kubernetes backend (CronJob-critical)

A `K8sClient` abstraction with two interchangeable backends, auto-detected at
startup:

- **`RestK8sClient`** — in-cluster. Talks to `$KUBERNETES_SERVICE_HOST` /
  `$KUBERNETES_SERVICE_PORT` over HTTPS using the mounted ServiceAccount token
  (`/var/run/secrets/kubernetes.io/serviceaccount/token`) and CA
  (`.../ca.crt`). Pure stdlib `urllib`. No `oc`/`kubectl` binary in the image.
- **`CliK8sClient`** — local/dev. Shells out to `oc`/`kubectl get ... -o json`,
  exactly like the existing scripts. Honors `$OC_BIN` / `$KUBECTL_BIN`.

Auto-detect: SA token file present **and** `$KUBERNETES_SERVICE_HOST` set →
REST; otherwise CLI. Override with `--in-cluster` / `--cli`.

Both backends expose the same minimal surface, e.g.:

```
list_namespaces() -> [ns_obj]
list_pods(namespace=None) -> [pod_obj]
list_replicasets(namespace=None) -> [rs_obj]
list_deployments(namespace=None) -> [obj]
list_statefulsets(namespace=None) -> [obj]
list_daemonsets(namespace=None) -> [obj]
```

Returned objects are the standard Kubernetes JSON dicts, so downstream logic is
backend-agnostic.

### 4.2 Thanos client & endpoint resolution

Reuse the `Thanos` class shape from `fetch-thanos-metrics.py`. Endpoint resolves
in order:

1. `--thanos-url URL`
2. `$THANOS_URL`
3. in-cluster: first reachable service from `QUERIER_CANDIDATES`, addressed by
   in-cluster DNS `http://<svc>.<ns>:<port>`
4. local: `kubectl/oc port-forward` fallback (reused from `fetch-thanos-metrics.py`)

Auth token: `--token` / `--token-file` → SA token file → `oc whoami -t`.
`--insecure` for self-signed in-cluster routes.

If Thanos is unreachable, the script still emits the configured limits/requests
and the **live** OOM list, marking usage columns as `-` (degrade, don't crash) —
same philosophy as `fetch-cluster-oom.py`'s Prometheus-optional path.

### 4.3 Namespace discovery & scope

- Default: every namespace whose name matches `--pattern` (default `^pid-`).
- `--namespace NS` (repeatable): restrict to specific namespaces — what a
  **per-namespace** CronJob passes via `$MY_POD_NAMESPACE`.
- `--all-namespaces`: drop the pattern filter.
- Stage derived from the namespace name per the project convention; namespaces
  that do not match fall under stage `other`.

## 5. Data collection & metrics

### 5.1 Configured side (from K8s)

Per namespace, fetch pods + replicasets + workload lists. Configured
`requests`/`limits` for cpu/memory come from `pod.spec.containers[].resources`
(authoritative; works without kube-state-metrics). CPU parsed to cores, memory
to bytes (`parse_mem`).

### 5.2 Real usage (from Thanos)

Aggregated by `(namespace, pod, container)`:

- **CPU (cores)**
  - now: `rate(container_cpu_usage_seconds_total{...}[5m])`
  - peak: `max_over_time(rate(container_cpu_usage_seconds_total{...}[5m])[<window>:<step>])`
  - avg: `avg_over_time(rate(container_cpu_usage_seconds_total{...}[5m])[<window>:<step>])`
- **Memory (bytes)**
  - now: `container_memory_working_set_bytes{...}`
  - peak: `max_over_time(container_memory_working_set_bytes{...}[<window>])`

To minimize round-trips, usage is fetched per namespace with a `by (pod,
container)` aggregation rather than one query per container.

Window: `--window 24h` (default), `--step 5m` (default), both configurable.
"Now" is an instant query at run time.

### 5.3 Rollup

A per-`(ns,pod,container)` record is the leaf. Rollups sum upward:

- **pod** = sum of its containers
- **workload** = sum of pods owned by it (owner chain pod → ReplicaSet →
  Deployment, or pod → StatefulSet/DaemonSet)
- **namespace** = sum of all pods

Each row at every level carries:

| Field | Notes |
|---|---|
| `cpu_request`, `cpu_limit` (cores) | summed; `None` if unset on any contributing container |
| `cpu_now`, `cpu_peak`, `cpu_avg` (cores) | from Thanos |
| `cpu_peak_util_pct` | `cpu_peak / cpu_limit`, only when limit known |
| `mem_request`, `mem_limit` (bytes) | summed |
| `mem_now`, `mem_peak` (bytes) | from Thanos |
| `mem_peak_util_pct` | `mem_peak / mem_limit`, only when limit known |
| `oom_count` | OOM-killed containers within scope |
| `pod_count`, `container_count` | for namespace/workload levels |

Util% is computed **only where a limit exists**; otherwise rendered `-`. The
configured-limit denominator is summed over the **currently-present pods** so it
shares the same population as the usage numerator, keeping util% meaningful.

### 5.4 OOM list (both sources, de-duplicated)

- **Live (K8s):** containers whose `status.containerStatuses[].lastState
  .terminated.reason == OOMKilled` — with `exitCode`, `finishedAt`,
  `restartCount`.
- **Thanos (historical):** `increase(container_oom_events_total{...}[<window>]) > 0`
  and `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` —
  catches OOMs whose pods are gone/recreated.

De-duplicated by `(namespace, pod, container)`; each entry tagged
`source = live | thanos | both`. Live records win on timestamp fields.

## 6. Output

Three formats; text to stdout by default, files when `--output-dir DIR` is given.

- **Text** (default; CronJob-log friendly): a cluster-wide rollup table, then
  per-namespace sections containing namespace-summary, workload, pod, and
  container sub-tables plus an OOM table. Dynamic column widths like existing
  renderers. `--level namespace,workload,pod,container` trims verbosity
  (default: all four).
- **CSV:** `resources.csv` (one row per level, with a `level` discriminator
  column) and `ooms.csv`, in the spirit of `_dimensions.csv` /
  `_oom-findings.csv`.
- **JSON:** nested structured report:
  ```json
  {
    "generated": "<iso>",
    "cluster": "<server>",
    "window": "24h",
    "namespaces": [
      {"namespace": "...", "stage": "...", "totals": {...},
       "workloads": [{"kind": "...", "name": "...", "totals": {...},
                      "pods": [{"name": "...", "totals": {...},
                                "containers": [{...}]}]}],
       "ooms": [{...}]}
    ]
  }
  ```

CSV/JSON always carry all four levels regardless of the text `--level` filter.

## 7. Deploy manifests (`manifests/cluster-usage-cronjob.yaml`)

A single cluster-wide CronJob, no image build required:

- **ConfigMap** holding `fetch-cluster-usage.py` (script shipped as data).
- **CronJob** using `python:3.12-slim`, mounting the ConfigMap and running
  `python3 /app/fetch-cluster-usage.py` on a schedule. `THANOS_URL` env knob.
- **ServiceAccount** + **ClusterRole** (`get`,`list` on `pods`, `replicasets`,
  `deployments`, `statefulsets`, `daemonsets`, `namespaces`) +
  **ClusterRoleBinding**.
- Documented note + optional binding to `cluster-monitoring-view` so the SA
  token is accepted by OpenShift's `thanos-querier`. (Vanilla KPS Thanos
  in-cluster typically needs no auth.)

## 8. Testing

Pytest unit tests (stdlib + pytest only, no cluster), built test-first:

- stage detection across well-formed and malformed namespace names
- owner/workload resolution (pod → RS → Deployment; pod → STS/DS)
- rollup math (container → pod → workload → namespace sums)
- util% edge cases: missing limit, zero limit, partial limits across containers
- OOM de-dup and `source` tagging (live-only, thanos-only, both)

A `FakeK8sClient` and a `FakeThanos` (canned query responses) drive the tests so
no live cluster or network is required.

## 9. CLI surface (summary)

```
fetch-cluster-usage.py
  # scope
  --pattern REGEX           namespace filter (default ^pid-)
  --namespace NS            repeatable; restrict to specific namespaces
  --all-namespaces          ignore --pattern
  # backend
  --in-cluster / --cli      force K8s backend (default: auto-detect)
  --kubectl                 use kubectl instead of oc for the CLI backend
  # thanos
  --thanos-url URL
  --token / --token-file
  --insecure
  --no-thanos               skip usage queries (configured + live OOM only)
  --window 24h  --step 5m
  --local-port 19090        port for the local port-forward fallback
  # output
  --level LEVELS            comma list for text verbosity (default: all)
  --output-dir DIR          also write resources.csv, ooms.csv, report.json
  --format text|json|csv    stdout format (default text; repeatable)
```

## 10. Out of scope (YAGNI)

- Pushing results to object storage / a database (CronJob writes to logs or an
  optional mounted volume via `--output-dir`).
- Grafana dashboard wiring (existing dashboards already cover OOM views).
- VPA-style right-sizing recommendations (util% is the input; recommendation is
  a possible future follow-up).
- Per-namespace CronJob manifests (the script supports `--namespace`, but only
  the cluster-wide manifest is delivered now).
