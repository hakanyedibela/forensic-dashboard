# RKE2 Observability Stack

Prometheus + Alertmanager + Loki + Promtail + Grafana on RKE2, with Thanos-compatible OOMKilled deep-dive dashboard.

## Layout

```
rke2-observability/
├── README.md
├── install.sh                      # runs helm installs in order; THANOS=1 enables long-term metrics
├── helm/
│   ├── values-kps.yaml             # kube-prometheus-stack values
│   ├── values-kps-thanos.yaml      # overlay enabling the Thanos sidecar (opt-in)
│   ├── values-thanos.yaml          # bitnami/thanos values (Querier + Store + Compactor + bundled MinIO)
│   └── values-loki.yaml            # Loki single-binary values
├── manifests/
│   ├── loki-datasource.yaml        # Grafana datasource for Loki
│   ├── thanos-datasource.yaml      # Grafana datasource for Thanos Querier (opt-in)
│   ├── thanos-objstore.md          # object-store secret for Thanos (MinIO / S3 / GCS / Azure)
│   ├── prometheusrules-custom.yaml # pod health + log error alerts
│   ├── oom-alerts.yaml             # OOM-specific PrometheusRule
│   └── alertmanager-secrets.md     # how to create the secrets
├── dashboards/
│   ├── dash-loki-logs.yaml         # Loki log overview dashboard
│   ├── dash-oomkilled-thanos.yaml  # OOMKilled deep-dive dashboard
│   └── dash-oom-forensics.yaml     # per-pod metrics + logs + network on one timeline
├── scripts/                        # CLI tools for OOM forensics (oc/kubectl-driven)
└── samples/                        # OOM simulation apps for dashboard testing
    ├── README.md
    ├── namespace.yaml
    ├── simulator/                  # multi-mode Python memory simulator
    └── patterns/                   # one deployment per OOM pattern (A/B/C/E)
```

## Prerequisites

- RKE2 cluster with kubectl context configured
- A default StorageClass (`kubectl get sc`) — edit `YOUR_SC` in values files if not `local-path`
- Helm v3
- RKE2 control-plane metrics exposed (see "RKE2 prep" below)

## RKE2 prep (optional, for full control-plane metrics)

On each **server** node, edit `/etc/rancher/rke2/config.yaml`:

```yaml
kube-controller-manager-arg:
  - "bind-address=0.0.0.0"
kube-scheduler-arg:
  - "bind-address=0.0.0.0"
kube-proxy-arg:
  - "metrics-bind-address=0.0.0.0"
etcd-expose-metrics: true
```

On each **agent** node:

```yaml
kube-proxy-arg:
  - "metrics-bind-address=0.0.0.0"
```

Then:

```bash
sudo systemctl restart rke2-server    # servers
sudo systemctl restart rke2-agent     # agents
```

## Install

```bash
./install.sh
```

Or step by step:

```bash
# 1. repos + namespace
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
kubectl create ns monitoring

# 2. kube-prometheus-stack
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  -n monitoring -f helm/values-kps.yaml

# 3. Loki single-binary
helm upgrade --install loki grafana/loki \
  -n monitoring -f helm/values-loki.yaml

# 4. Promtail
helm upgrade --install promtail grafana/promtail \
  -n monitoring \
  --set "config.clients[0].url=http://loki:3100/loki/api/v1/push"

# 5. extras
kubectl apply -f manifests/loki-datasource.yaml
kubectl apply -f manifests/prometheusrules-custom.yaml
kubectl apply -f manifests/oom-alerts.yaml
kubectl apply -f dashboards/
```

## Access Grafana (no ingress)

```bash
kubectl -n monitoring port-forward svc/kps-grafana 3000:80
# http://localhost:3000   user: admin   pass: ChangeMe123!
```

Or flip Grafana service to `NodePort` in `helm/values-kps.yaml` for LAN access.

## Configure alerting

See `manifests/alertmanager-secrets.md` for Slack / Teams / SMTP setup.

## Long-term metrics with Thanos (opt-in)

Default install keeps Prometheus retention at 15d (`helm/values-kps.yaml`). For 30d / 90d / 1y views — which the dashboard `oom-7d-detail` and the `oom-usage.py` script can reach — enable Thanos:

```bash
THANOS=1 ./install.sh
```

What that adds, side-by-side with the existing Prometheus:

```
                       ┌──────────────────────┐
                       │  Prometheus pod      │
                       │  ┌────────────────┐  │           ┌──────────────────────┐
   scrape ─────────────┼─►│ Prometheus     │──┼──────────►│  Grafana             │
                       │  │ (15d local)    │  │ live      │  - Prometheus DS     │
                       │  └────────────────┘  │           │  - Thanos DS         │
                       │  ┌────────────────┐  │           └──────────▲───────────┘
                       │  │ Thanos sidecar │  │                      │
                       │  └───────┬────────┘  │                      │ history
                       └──────────┼───────────┘                      │
                                  │ upload blocks                    │
                                  ▼                                  │
                          ┌───────────────┐    fan-out:    ┌─────────┴──────────┐
                          │  MinIO bucket │◄──────────────►│   Thanos Querier   │
                          │  (S3 in prod) │   read history │   (PromQL endpoint)│
                          └───────┬───────┘                └─────────▲──────────┘
                                  │                                  │
                          ┌───────▼───────┐                          │
                          │ Store Gateway │──────────────────────────┘
                          └───────────────┘
                          ┌───────────────┐
                          │   Compactor   │  compacts + downsamples old blocks
                          └───────────────┘
```

Components installed (all in the `monitoring` namespace, helm release `thanos`):

| Component | What it does | Service |
|---|---|---|
| Prometheus sidecar (in the kps Prometheus pod) | Uploads finalised TSDB blocks to object storage every ~2h | gRPC: `kps-kube-prometheus-stack-thanos-discovery:10901` (headless, used for SD by the Querier) |
| Thanos Querier | PromQL endpoint, fans out to live Sidecar + historical Store Gateway, dedupes HA replicas via `prometheus_replica` external label | HTTP: `thanos-query:9090`, gRPC: `thanos-query-grpc:10901` |
| Thanos Store Gateway | Reads historical blocks from object storage and serves them to the Querier | gRPC: `thanos-storegateway:10901` |
| Thanos Compactor | Compacts and downsamples blocks (5m + 1h resolutions) — required for fast long-range queries | none (worker only) |
| Bundled MinIO (lab/dev) | S3-compatible object store, single replica | API: `thanos-minio:9000`, console: `thanos-minio:9001` |

Dashboards stay backwards-compatible: every panel still works against the original Prometheus datasource. Switch the datasource picker (or set the dashboard variable `datasource` to `Thanos`) to query the long-range view.

### Verify

```bash
# Sidecar uploading blocks?
kubectl -n monitoring logs -l app.kubernetes.io/name=prometheus -c thanos-sidecar --tail=50

# Querier sees both sources?
kubectl -n monitoring port-forward svc/thanos-query 9090
# open http://localhost:9090/stores → expect one Sidecar entry + one Store Gateway entry

# Bucket has data?
kubectl -n monitoring run -it --rm mc --image=minio/mc --restart=Never -- \
  sh -c 'mc alias set local http://thanos-minio:9000 admin ChangeMe123! && mc ls local/thanos/'
```

### Real S3 / GCS / Azure / Ceph

The bundled MinIO is fine for a lab; for production swap it out: set `minio.enabled: false` in `helm/values-thanos.yaml` and recreate the `thanos-objstore-config` secret with the real credentials. Backend-specific snippets live in `manifests/thanos-objstore.md`.

### Sizing

Defaults in `helm/values-thanos.yaml` are lab-sized: ~3–5 GiB memory across 4 pods, ~50 GiB of PVCs (Store Gateway 10G, Compactor 20G, MinIO 20G) on the same `cassandra-storage` StorageClass that Prometheus uses. Adjust there for production retention.

### How it interacts with the rest of the project

- **Dashboards** — the `oom-7d-detail`, `oomkilled-thanos`, and `oom-combined` dashboards now reach data older than Prometheus's local 15d once you switch the `datasource` variable to `Thanos`.
- **`scripts/oom-usage.py`** — point `--prometheus-url http://localhost:9090` at a `kubectl port-forward svc/thanos-query 9090` and recover memory/CPU even for OOMs that happened weeks ago. The dedup label is already set; no extra flags needed.
- **HA-replica defenses in dashboards** — the `max by (namespace,pod,container)` wrappers added in `dash-oom-7d-detail.yaml` keep working both with and without Thanos. With Thanos's `replicaLabel: prometheus_replica` (set in `values-thanos.yaml`), the Querier already dedupes; the wrappers become no-ops on already-unique series.

## OOM analysis

Open dashboard **OOMKilled — Thanos Deep Dive** in Grafana.

### Diagnostic flow

```
  [OOMKilled alert fires]
           │
           ▼
  1. How often? (frequency)  ──► one-off vs chronic
           │
           ▼
  2. Who? (which container)  ──► single vs whole workload
           │
           ▼
  3. Shape of memory curve   ──► leak / spike / baseline-too-high / noisy neighbor
           │
           ▼
  4. Confirm with logs / events / VPA
           │
           ▼
  5. Apply fix (limit bump / leak fix / right-sizing / HPA)
```

### 5-minute triage checklist

- [ ] Panel 3 — which pod/container/node?
- [ ] Panel 5 — peak/limit ratio? (→ limit vs leak)
- [ ] Panel 6 — positive slope? (→ leak confirmed)
- [ ] Panel 7 — P99 ≫ P95? (→ spike-driven)
- [ ] Panel 9 — container lifetime? (→ startup vs runtime)
- [ ] `kubectl get events` + `kubectl logs --previous` for error text
- [ ] Loki at OOM timestamp for request correlation
- [ ] Node `MemAvailable` at OOM time (platform vs app)
- [ ] VPA recommendation → target limit
- [ ] Decide: raise limit, fix leak, scale out, or fix node

### Panel interpretation

#### Panel 1 — "OOMKills last 24h / 7d / 30d"

| Signal | Diagnosis |
|---|---|
| `24h = 0, 7d > 0` | Sporadic — correlate with traffic peaks, batch jobs, deploys |
| `24h > 7d/7` | Escalating — prioritize |
| `30d flat high` | Chronic under-provisioning — stop patching, right-size |

#### Panel 2 — "OOMKill events timeline"

| Pattern | Diagnosis |
|---|---|
| Clustered spikes at same time daily | Cron / nightly batch / scheduled job |
| Cluster-wide simultaneous OOMs | Node memory pressure, not app bug |
| One namespace only | App issue, not platform |

#### Panel 3 — "OOMKilled containers (detail)"

| Signal | Diagnosis |
|---|---|
| Same `node` repeatedly | Node pressure / noisy neighbor → check other pods |
| Spread across nodes | App-level problem |
| High restart count + CrashLoopBackOff | App literally can't start inside the limit |
| Low restarts, long uptime | Gradual leak or spike kill |

#### Panel 5 — "% of memory limit consumed (peak)" — most useful panel

| Peak % of limit | Diagnosis |
|---|---|
| **> 100%** | OOM happened — confirmed |
| **95–100%** | Limit too tight for normal peaks → **bump limit** |
| **80–95%** | Borderline → add headroom or watch for growth |
| **< 50% but still OOM** | Investigate cgroup / node pressure / kernel OOM, not the limit |

#### Panel 6 — "Memory growth rate (deriv)" — leak detector

| Shape | Diagnosis |
|---|---|
| Positive non-zero slope sustained for hours | **Memory leak** → heap dump, profile |
| Sawtooth (up then drop on restart) | Confirms leak + OOM pattern |
| Flat at the top | Reached ceiling but stable → sizing problem, not leak |
| Negative after deploy | Fix deployed |

#### Panel 7 — "P95 / P99 memory peak per workload"

| Signal | Diagnosis |
|---|---|
| P99 ≫ P95 | **Spike-driven OOM** — rare requests (large payloads, slow consumers, retries) |
| P95 ≈ P99 high | **Baseline too high** — right-size, fix baseline allocation |
| P95 low, P99 very high | GC / caching — tune JVM/Go GC |

#### Panel 8 — "OOMKills by hour-of-day heatmap"

| Pattern | Diagnosis |
|---|---|
| Bright vertical band at specific hour | Cron / scheduled traffic |
| Uniform spread | Organic load |
| Weekday vs weekend pattern | User traffic driven |

#### Panel 9 — "Avg container lifetime before OOM"

| Lifetime | Diagnosis |
|---|---|
| Seconds to minutes | Can't start within limit → misconfigured app, large init, JVM `-Xmx` > container limit |
| Hours | Gradual leak |
| Days | Slow leak or memory fragmentation (common in long-running C/C++) |

### Signal-pattern decision tree

#### Pattern A — "Memory climbs steadily, restart, climbs again"

**Signals:** Panel 6 deriv consistently positive • Panel 7 P99 hitting limit repeatedly • Panel 8 uniform time distribution

**Diagnosis:** Memory leak

**Actions:**
1. Grab a heap dump while memory is high (before OOM):
   ```bash
   kubectl exec -it <pod> -- jcmd 1 GC.heap_dump /tmp/heap.hprof            # Java
   kubectl exec -it <pod> -- curl localhost:6060/debug/pprof/heap > heap.out  # Go
   ```
2. Tune GC (Java: `-XX:MaxRAMPercentage=75`; Go: `GOMEMLIMIT=80% of container limit`)
3. **Don't just raise the limit** — the leak will eat it.

#### Pattern B — "Flat baseline, sudden spike → OOM"

**Signals:** Panel 6 flat with occasional sharp rise • Panel 7 P99 spikes, P95 fine • Panel 3 single-node OOMs across various pods

**Diagnosis:** Request-driven spike (large payload, N+1 DB, pagination without limit, streaming buffered in memory)

**Actions:**
1. Correlate with request logs at the OOM timestamp:
   ```
   {namespace="$ns", pod="$pod"} |~ "(?i)POST|PUT" | json | duration_ms > 1000
   ```
2. Add request / response size limits
3. Apply memory-based HPA (scale out before saturation)
4. If you can't fix the spike, raise the limit + set `Guaranteed` QoS.

#### Pattern C — "High baseline, no leak, still OOM"

**Signals:** Panel 5 peak/limit > 0.9 *constantly* • Panel 6 flat • Panel 7 P95 ≈ P99

**Diagnosis:** Limit too low for the workload

**Actions:**
1. Install VPA in recommender-only mode:
   ```bash
   kubectl get vpa <name> -o jsonpath='{.status.recommendation}'
   ```
2. Raise `resources.limits.memory` to `P99 * 1.3`
3. Verify requests match typical usage (scheduler headroom).

#### Pattern D — "OOMs on specific node only"

**Signals:** Panel 3 table: same `node` across many rows • Other workloads also OOMing on that node

**Diagnosis:** Node memory pressure / noisy neighbor / overcommit

**Actions:**
1. Check node:
   ```bash
   kubectl describe node <node> | grep -A5 "Allocated resources"
   kubectl top pods -A --sort-by=memory | head -20
   ```
2. Look for missing limits on neighbors
3. Cordon + drain the node, investigate system-level memory (kubelet, containerd, runaway DaemonSet)
4. Kernel OOMs:
   ```bash
   ssh <node> "sudo dmesg -T | grep -i 'killed process'"
   ```

#### Pattern E — "Dies in seconds, never starts"

**Signals:** Panel 9 lifetime < 60s • Panel 3 restart count climbing fast

**Diagnosis:** Startup allocation > container limit (JVM `-Xmx` > limit, Python/Node loading large model)

**Actions:**
1. `kubectl logs <pod> --previous` — look for `OutOfMemoryError` before exit
2. JVM: drop `-Xmx` or use `-XX:MaxRAMPercentage=75`
3. Python ML: check model fits (often 2× model size at load time)
4. Raise limit to ≥ startup peak.

### PromQL query cheatsheet

| Pattern | What it tells you |
|---|---|
| `max_over_time(m[1h])` | Absolute peak inside each 1h window |
| `topk(10, max by(...)(max_over_time(...)))` | **Top peak** workloads |
| `m / on(...) group_left() kube_pod_container_resource_limits{resource="memory"}` | Ratio to limit — real OOM predictor |
| `quantile_over_time(0.95, ...)` | P95 peak — separates steady pressure from spikes |
| `deriv(m[6h])` | Positive slope = memory leak candidate |
| `increase(kube_pod_container_status_terminated_reason{reason="OOMKilled"}[24h])` | Event count in window |
| `changes(restarts_total[5m]) > 0 and on(...) last_terminated_reason{reason="OOMKilled"} == 1` | Event moments — perfect for annotations |
| `time() - kube_pod_start_time and on(...) last_terminated_reason{reason="OOMKilled"}` | Time-to-OOM per pod |

### Common root-cause cheat table

| Symptom combo | Most likely cause | Fix |
|---|---|---|
| Deriv positive, P99 = limit, uptime hours | Leak | Heap dump → fix code |
| Deriv flat, P99 = limit, P95 = limit | Under-provisioned | Raise limit |
| Deriv flat, P95 low, P99 spikes | Request spike | HPA on memory, limit payload |
| Same node keeps OOMing | Noisy neighbor | Cordon + investigate |
| Dies in < 60s repeatedly | Startup > limit | Fix `-Xmx` / load sizing |
| JVM heap healthy, container RSS high | Native leak | Netty / NIO / JNI audit |
| OOM only during deploys | Rolling both old+new copies | Temp RAM headroom or `maxSurge=0` |
| OOM only at 03:00 daily | Cron / backup / batch | Isolate cron pod resources |
| OOM after a dependency call | Large response buffered | Stream, paginate, set limits |

### Hands-on correlation commands

Pod events:
```bash
kubectl get events -n <ns> \
  --field-selector reason=OOMKilling,involvedObject.name=<pod>
```

Loki logs at OOM time (Grafana → Explore):
```
{namespace="<ns>", pod="<pod>"}
  |~ "(?i)out of memory|heap|gc|allocation failed|killed"
```

Node memory pressure for the OOM host:
```promql
node_memory_MemAvailable_bytes{instance=~".*"} / node_memory_MemTotal_bytes
```

JVM-specific (needs JMX exporter):
```promql
jvm_memory_bytes_used{area="heap"}  / jvm_memory_bytes_max{area="heap"}
```
- Heap ≈ 1 but `working_set / limit` only 0.5 → heap too small, increase `-Xmx`
- Heap fine but container memory high → **native leak** (NIO buffers, JNI, Netty pools)

Go-specific:
```promql
go_memstats_heap_inuse_bytes
go_memstats_heap_sys_bytes
```
- `heap_sys - heap_inuse` huge → lazy OS return, set `GOMEMLIMIT`

## Root cause investigation playbook

Run these in order when an OOMKill fires. Each step confirms or eliminates a hypothesis.

### 1. Confirm it was OOM (not liveness probe, not evicted)
```bash
kubectl -n <ns> describe pod <pod> | grep -A8 'Last State'
```
`Reason: OOMKilled` + `Exit Code: 137` — confirmed. Other exit codes mean something else.

### 2. Previous-container logs
```bash
kubectl -n <ns> logs <pod> --previous --tail=100
```
Java: look for `OutOfMemoryError: Java heap space` / `Metaspace` / `Direct buffer memory` — each points at a different area.

### 3. Kubernetes events
```bash
kubectl -n <ns> get events --sort-by='.lastTimestamp' --field-selector reason=OOMKilling
kubectl get events -A --field-selector reason=SystemOOM
```

### 4. Kernel OOM killer on the node
```bash
NODE=$(kubectl -n <ns> get pod <pod> -o jsonpath='{.spec.nodeName}')
ssh $NODE "sudo dmesg -T | grep -i -A3 'killed process'"
```
Tells you total RSS at kill time and whether it was a cgroup OOM (container limit hit) vs system OOM (node out of RAM).

### 5. cgroup / container stats
```promql
container_memory_working_set_bytes{pod="<pod>"}
  / container_spec_memory_limit_bytes{pod="<pod>"}

node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes

container_oom_events_total{pod="<pod>"}
```

### 6. Runtime-specific diagnostics (while still alive)

**JVM**
```bash
kubectl exec -it <pod> -- jcmd 1 GC.heap_info
kubectl exec -it <pod> -- jcmd 1 VM.native_memory summary   # needs -XX:NativeMemoryTracking=summary
kubectl exec -it <pod> -- jcmd 1 GC.heap_dump /tmp/h.hprof
kubectl cp <ns>/<pod>:/tmp/h.hprof ./heap.hprof
```

**Go**
```bash
kubectl port-forward <pod> 6060
go tool pprof -http :8081 http://localhost:6060/debug/pprof/heap
```

**Node.js**: `kubectl exec <pod> -- kill -USR2 1` (requires `--inspect`)

**Python**: `tracemalloc` / `memray` (needs app cooperation)

### 7. Inside the container
```bash
kubectl exec -it <pod> -- sh -c 'ps aux --sort=-rss | head -20'
kubectl exec -it <pod> -- cat /proc/1/status | grep -E 'Vm|Rss'
kubectl exec -it <pod> -- cat /sys/fs/cgroup/memory.current
kubectl exec -it <pod> -- cat /sys/fs/cgroup/memory.peak       # cgroup v2 — gold
```
`memory.peak` is the actual peak RSS the kernel observed, independent of scrape interval.

### 8. Use `working_set`, not `usage`
| Metric | Use for |
|---|---|
| `container_memory_working_set_bytes` | **OOM analysis** (matches kernel decision) |
| `container_memory_usage_bytes` | Debug only — includes reclaimable page cache |

### 9. Correlate with logs & network
Use the **OOM Forensics** dashboard — metrics, network, CPU throttling, and Loki logs on one timeline, filtered to the pod.

### 10. Check for neighbors on the same node
```promql
sum by (node) (
  increase(kube_pod_container_status_terminated_reason{reason="OOMKilled"}[1h])
)
```
Multiple pods OOMing on the same node → oversubscription / noisy neighbor.

### 11. Compare to VPA recommendation
```bash
kubectl get vpa -A
kubectl get vpa <name> -o jsonpath='{.status.recommendation}' | jq
```

## OOM Forensics dashboard

`dashboards/dash-oom-forensics.yaml` installs a per-pod investigation view with:

- Working set vs limit vs request (with dashed reference lines)
- Memory deriv (leak detector)
- Page faults, CPU usage, CPU throttling
- Network rx/tx, dropped packets, errors
- Container filesystem usage + disk I/O
- Collapsible JVM panels (heap, GC, threads) — shown if `jmx_exporter` metrics present
- Collapsible Go runtime panels (`heap_inuse` / `heap_sys` / goroutines / GC)
- Loki log panels filtered to `error|oom|killed|heap|allocation failed`
- Log rate by `level` (JSON-log apps)
- Full log stream
- OOM event annotations overlaid on every panel

The main OOM dashboard has a dashboard-level link to open Forensics for the currently-selected pod. Clicking it carries over `namespace` and `pod` as variables.

## Test the dashboard with synthetic OOMs

To validate that Prometheus, Loki, alerts, and the dashboard all react correctly, deploy the simulator workloads in `samples/`. They reproduce patterns A, B, C, and E from the table above.

```bash
kubectl apply -f samples/namespace.yaml
kubectl apply -f samples/simulator/configmap.yaml
kubectl apply -f samples/patterns/
```

Watch OOMs happen:
```bash
kubectl -n oom-test get pods -w
```

Open the dashboard, set `namespace = oom-test`, and each pattern should light up the panels as described in the `samples/README.md`. Cleanup:
```bash
kubectl delete namespace oom-test
```

## Troubleshooting

### `failed to install CRD ... conflicts with "kubectl"`

Prometheus-Operator CRDs already exist on the cluster (applied client-side previously), and Helm 3.14+ uses server-side apply which refuses to overwrite another manager's fields.

Check existing state:
```bash
kubectl get crd | grep monitoring.coreos.com
kubectl get prometheuses,alertmanagers,servicemonitors -A
helm list -A | grep -iE 'prom|monitor'
```

**Path A — no existing CRs (fresh cluster):** delete and reinstall
```bash
helm uninstall kps -n monitoring || true
kubectl get crd -o name | grep monitoring.coreos.com | xargs -r kubectl delete
./install.sh
```

**Path B — CRs exist, preserve them:** take ownership + `--skip-crds`
```bash
VER=v0.76.0
for f in alertmanagerconfigs alertmanagers podmonitors probes \
         prometheusagents prometheuses prometheusrules scrapeconfigs \
         servicemonitors thanosrulers; do
  kubectl apply --server-side --force-conflicts \
    -f "https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/${VER}/example/prometheus-operator-crd/monitoring.coreos.com_${f}.yaml"
done

helm uninstall kps -n monitoring || true
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  -n monitoring -f helm/values-kps.yaml --skip-crds
```

## Change the default Grafana admin password

Edit `helm/values-kps.yaml` → `grafana.adminPassword`, then `helm upgrade kps ...`.

Or use an existing secret:

```bash
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user=admin \
  --from-literal=admin-password='strong-password'
```

And set:

```yaml
grafana:
  admin:
    existingSecret: grafana-admin
```
