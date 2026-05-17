# fetch-cluster-state + fetch-cluster-oom

Two pipelines that snapshot the forensic state of every OpenShift
project starting with `pid-`, plus a combined wrapper that runs both:

- **fetch-cluster-state** — cluster state, HPA validation, resource
  footprint, re-applyable "desired state" YAML manifests.
- **fetch-cluster-oom** — per-pod OOM root-cause reports with a
  verdict (A/B/C/D/E pattern) plus a cluster-wide rollup.

| Script                                  | Role                                                                 |
|-----------------------------------------|----------------------------------------------------------------------|
| `scripts/python/fetch-cluster-state.py` | The state worker. Runs cluster-wide in one invocation with built-in parallelism, or targets specific projects. |
| `scripts/fetch-cluster-state-loop.sh`   | Bash wrapper around the state worker. Discovers `pid-*` projects, detects stage, invokes the Python script once per namespace, aggregates the CSVs. |
| `scripts/python/fetch-cluster-oom.py`   | The OOM worker. For each container that was OOMKilled, builds a full root-cause report (workload, node, neighbors, events, autoscaling, Prometheus metrics, pre-OOM logs, verdict). |
| `scripts/fetch-cluster-oom-loop.sh`     | Bash wrapper around the OOM worker. Same `pid-*` discovery; writes per-namespace JSON + text reports only when OOMs are actually found. |
| `scripts/python/aggregate-resources.py` | Cluster-wide resource rollup over the state-loop output (pods, CPU/mem, PVCs, quota %). |
| `scripts/python/aggregate-oom.py`       | Cluster-wide OOM rollup (pattern counts, findings CSV) over the oom-loop output. |
| `scripts/fetch-all-loop.sh`             | One-shot wrapper: runs both loops into a single shared report directory. **Use this if you don't know where to start.** |

Pick the Python scripts when you want one fast cluster-wide snapshot.
Pick a shell loop when you want each namespace processed in isolation
(separate output dirs, per-namespace `run.log`s, robust to single-namespace
failures). Pick `fetch-all-loop.sh` when you want one combined report
covering both state and OOMs.

## Prerequisites

- `oc` (OpenShift CLI), logged in to the target cluster
- Python 3.6.8+ (standard library only for `fetch-cluster-oom.py` and the aggregators; `fetch-cluster-state.py` needs `pyyaml`)
- `pip install pyyaml` (required for `fetch-cluster-state.py`) and `pip install openpyxl` (optional, enables the `.xlsx` workbook)
- For the loop wrappers: `bash` 4+ (macOS ships 3.2; install a newer bash via `brew install bash` and make sure it precedes `/bin/bash` on `PATH`)
- Optional for richer OOM verdicts: a reachable Prometheus (default `http://localhost:9090`; use a port-forward to your cluster's Prometheus / Thanos)

Read access on the `pid-*` projects is sufficient — the scripts never
write to the cluster. Cluster-scoped read access (`nodes`) helps the OOM
report find noisy-neighbor patterns.

## Stage detection

Stage is derived from the namespace name using the convention

```
pid-<id>-<app>-<STAGE>-<num>-<suffix>
```

Example: `pid-1000-akte-ref-100-prodda` → stage `ref`.

Detection order:

1. The 4th dash-segment (index 3).
2. Any other segment matching a known keyword (full-segment match).
3. `other`.

Known keywords: `ref`, `prod`, `test`, `phase`, `pnext`. Adjust the
`STAGE_KEYWORDS` constant in either script to change them.

## fetch-cluster-state.py

```bash
python3 scripts/python/fetch-cluster-state.py [--output-dir DIR]
                                              [--stage STAGE [--stage STAGE ...]]
                                              [--project NS  [--project NS ...]]
                                              [--workers N]
```

Options:

- `--output-dir`: base directory (default `./state-<timestamp>`).
- `--stage`: only process namespaces in this stage (repeatable).
- `--project`: only process the named project(s) (repeatable). When given,
  skips the `oc get projects` discovery.
- `--workers`: number of parallel namespaces processed at once (default 4).

### What it collects per namespace

| Kind                | Captured fields                                                |
|---------------------|----------------------------------------------------------------|
| `Namespace`         | labels, annotations, environment label                         |
| `Deployment`        | replicas, ready, container count, images, summed CPU/mem req+lim |
| `StatefulSet`       | same as Deployment                                             |
| `HorizontalPodAutoscaler` | `scaleTargetRef`, min/max, current/desired replicas, metrics, status conditions, **binding validation** |
| `Service`           | type, clusterIP, ports                                         |
| `PersistentVolumeClaim` | phase, storage size (GiB), storage class, access modes      |
| `ResourceQuota`     | `hard` + `used`                                                |
| `LimitRange`        | full limits spec                                               |
| `NetworkPolicy`     | name list                                                      |

### HPA binding validation

For every HPA the script checks:

- `scaleTargetRef.name` is not empty.
- `scaleTargetRef.kind` is `Deployment` or `StatefulSet`.
- The referenced workload actually exists in the namespace.
- `minReplicas` and `maxReplicas` are present, and `min ≤ max`.
- At least one metric is configured.
- If a `Resource` metric is used, the target containers have
  `resources.requests` set (otherwise the HPA stays inactive).
- Surfaces `ScalingActive=False` / `AbleToScale=False` conditions from
  `.status.conditions`.

Results are stored under each namespace as `hpa-bindings.json` and rolled
up into `_hpa-validation.csv`.

### Desired state output

Per namespace the script writes a `desired/` directory containing one YAML
file per resource kind, stripped of server-injected fields
(`resourceVersion`, `uid`, `managedFields`, `creationTimestamp`,
`last-applied-configuration`, `deployment.kubernetes.io/revision`, ...).

The files are prefixed numerically so `oc apply -f desired/` applies them
in dependency order:

```
00-namespace.yaml
10-resourcequotas.yaml
20-limitranges.yaml
30-networkpolicies.yaml
40-deployments.yaml
41-statefulsets.yaml
50-services.yaml
60-pvcs.yaml
70-hpas.yaml
```

The idea: commit `desired/` to git, edit, then `oc diff -f desired/` and
`oc apply -f desired/` to reach the target state.

### Output layout

```
state-20260513-101530/
├── by-stage/
│   ├── ref/
│   │   └── pid-1000-akte-ref-100-prodda/
│   │       ├── snapshot.json
│   │       ├── hpa-bindings.json
│   │       └── desired/
│   │           ├── 00-namespace.yaml
│   │           └── ...
│   └── prod/
├── _overview.json
├── _hpa-validation.csv
└── _dimensions.csv
```

## fetch-cluster-state-loop.sh

Wraps the Python script with the same `pid-*` loop used by
`check-bind-resources.sh` and `lean-inspector-loop.sh`.

```bash
./scripts/fetch-cluster-state-loop.sh
```

No arguments. Outputs are written to `./reports/state-loop-<timestamp>/`
relative to the current directory — run it from the repo root so the
reports end up in `<repo>/reports/`.

For every `pid-*` project it:

1. Detects the stage.
2. Calls `python3 scripts/python/fetch-cluster-state.py --project <ns>
   --output-dir reports/state-loop-<ts>/by-stage/<stage>/<ns> --workers 1`.
3. Captures stdout/stderr into `run.log` inside that namespace folder.
4. Concatenates the per-namespace `_hpa-validation.csv` and
   `_dimensions.csv` into combined files at the root.
5. Builds `_master-overview.txt` with per-namespace status + HPA-issue
   counts and per-stage totals.

If a single namespace fails, the loop continues and marks it as `FAIL` in
the master overview. The failure is also visible in that namespace's
`run.log`.

### Output layout

```
reports/state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/
│   ├── run.log
│   ├── _overview.json
│   ├── _hpa-validation.csv
│   ├── _dimensions.csv
│   ├── _cluster-state.xlsx                (if openpyxl is installed)
│   └── by-stage/<stage>/<ns>/             (the Python script's own tree)
│       ├── snapshot.json
│       ├── hpa-bindings.json
│       └── desired/...
├── _hpa-validation.csv                    (aggregated across all namespaces)
├── _dimensions.csv                        (aggregated across all namespaces)
├── _master-overview.txt                   (HPA-centric: status, BAD count)
├── _resources-overview.txt                (resources: pods, CPU/mem, PVCs, quota %)
└── _resources-overview.csv                (same, raw numbers, for pivoting)
```

> **Note** — the per-namespace tree appears under `by-stage/<stage>/<ns>/` twice
> (once created by the loop, once by the Python script). The aggregated CSVs
> at the root level are unaffected; only the per-namespace artefacts
> (`snapshot.json`, `hpa-bindings.json`, `desired/`) sit one level deeper than
> you might expect.

## fetch-cluster-oom.py

```bash
python3 scripts/python/fetch-cluster-oom.py [-n NS | -A] [--pod NAME]
                                            [--summary | --json]
                                            [--logs [--grep REGEX]]
                                            [--prometheus-url URL]
                                            [--prometheus-port N]
                                            [--token TOKEN]
                                            [--insecure]
                                            [--no-prometheus]
                                            [--diagnose]
                                            [--kubectl]
```

For every container whose `lastState.terminated.reason == OOMKilled`, the
script builds a full root-cause report and concludes with a verdict
(pattern A/B/C/D/E or `?`).

### What it pulls per OOMKilled container

| Source                 | What is collected                                                                 |
|------------------------|-----------------------------------------------------------------------------------|
| Pod / container spec   | image, requests/limits, QoS class, probes, args, exit code, restart count         |
| Workload resolution    | follows `ownerReferences` once: ReplicaSet → Deployment, etc.                     |
| Node                   | capacity, allocatable, pressure conditions                                        |
| Neighbor OOMs          | other OOMs on the same node within ±1h (evidence for noisy-neighbor pattern)      |
| Events                 | pod events from the last hour                                                     |
| Storage                | PVCs the pod mounts (phase, capacity, storage class, access modes)                |
| Autoscaling            | matching HPA + VPA (if any), with current/desired replicas and metric definitions |
| Network                | services selecting this pod, NetworkPolicies whose podSelector matches            |
| Namespace constraints  | LimitRanges, ResourceQuotas with used/hard                                        |
| Prometheus (optional)  | working-set, peaks (5m/1h), slope (deriv 1h), memory limit, CPU rate/throttle, rx/tx, fs reads/writes |
| Pre-OOM logs (optional)| `oc logs --previous`, filtered by a regex (default: `error|oom|killed|out ?of ?memory|fatal|exception`) |

### Verdicts — the A/B/C/D/E patterns

The verdict is the first matching pattern in priority order; everything
else lands in `?` for manual inspection.

| Pattern | Trigger                                                                                  | Typical cause                                                              |
|---------|------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| `E`     | lifetime < 60s **and** restart_count ≥ 2                                                 | App can't initialise inside the memory limit (JVM `-Xmx`, large ML model, big cache warmup) |
| `D`     | ≥ 2 other pods OOMed on the same node within ±1h                                         | Node is over-committed; missing limit on a neighbor or kernel OOM           |
| `A`     | memory deriv (1h) ≥ ~17 KiB/s **and** peak/limit ≥ 85 %                                  | Steady memory growth — leak. Limit is the ceiling, not the cause            |
| `B`     | network rx (1m) ≥ 3 × rx baseline (30m avg of 5m rate)                                   | Spike / large request — body buffered in memory                              |
| `C`     | peak/limit ≥ 95 % **and** deriv flat                                                     | Normal peak ≥ memory limit — workload is under-provisioned                   |
| `?`     | none of the above with confidence                                                        | Indeterminate — manual inspection                                            |

Each verdict comes with an `evidence` list (what was observed) and a
`fixes` list (concrete `oc` commands to investigate further).

### Output modes

| Flag         | Output                                                                                  |
|--------------|------------------------------------------------------------------------------------------|
| (none)       | Full text report per OOM (sections: CONTEXT, CONFIGURATION, MEMORY, CPU, NETWORK, STORAGE, NODE, NEIGHBORS, AUTOSCALING, SERVICES & NETPOL, NAMESPACE CONSTRAINTS, EVENTS, VERDICT) |
| `--summary`  | One line per OOM (`NAMESPACE  POD  CONTAINER  PATTERN  OOM AGE`). Quick triage.          |
| `--json`     | Structured array (one entry per OOM). What the loop wrapper feeds to `aggregate-oom.py`. |
| `--diagnose` | Probe mode: prints which Prometheus scrape sources are healthy / missing, then exits.    |

### Prerequisites for the Prometheus path

Resource-metric verdicts (A, B, C) only fire when the script can reach
Prometheus. Without it the verdict path is restricted to D (neighbors)
and E (lifetime) — still useful but blind to memory shape. `--diagnose`
checks which metric families are available and prints install hints if
something is missing.

## fetch-cluster-oom-loop.sh

Wraps the OOM worker the same way `fetch-cluster-state-loop.sh` wraps
the state worker.

```bash
./scripts/fetch-cluster-oom-loop.sh [--report-dir DIR]
```

Defaults output to `./reports/state-loop-<timestamp>/` (same directory
naming as the state loop, so the two reports can share a folder via
`--report-dir` or via `fetch-all-loop.sh`). For every `pid-*` project it:

1. Detects the stage (same rule as the state loop).
2. Calls `python3 scripts/python/fetch-cluster-oom.py -n <ns> --json` into
   a temp file.
3. If the namespace has **zero** OOMKilled containers, the temp file is
   discarded and **no per-namespace directory is created**.
4. If there are OOMs, materialises `by-stage/<stage>/<ns>/` with
   `report.json`, `report.txt`, `oom-run.log`.
5. After the loop, runs `aggregate-oom.py` and writes
   `_oom-overview.txt` + `_oom-findings.csv` + `_oom-status.txt`.
6. If the whole cluster has zero OOMs, the loop writes nothing and (if
   it created the report dir itself) removes the empty dir on exit.

### Environment variables (Prometheus opt-in)

The OOM loop defaults to `--no-prometheus` so a single run is fast and
predictable. Enable Prometheus through env vars:

| Variable                | Effect                                                |
|-------------------------|-------------------------------------------------------|
| `OOM_PROMETHEUS_URL`    | Pass through to `--prometheus-url`                    |
| `OOM_PROMETHEUS_PORT`   | Pass through to `--prometheus-port`                   |
| `OOM_TOKEN`             | Pass through to `--token`                             |
| `OOM_INSECURE=1`        | Pass through to `--insecure`                          |

If neither URL nor PORT is set, the loop appends `--no-prometheus` and
the verdict is limited to patterns D / E (or `?`).

### Output layout (only when OOMs are found)

```
reports/state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/                # only namespaces with OOMs
│   ├── oom-run.log
│   ├── report.json                       # full structured JSON
│   └── report.txt                        # human-readable per-namespace report
├── _oom-overview.txt                     # per-namespace + per-stage rollup
├── _oom-findings.csv                     # one row per OOMKilled container
└── _oom-status.txt                       # bash-side fallback overview
```

## fetch-all-loop.sh — combined wrapper

If you want **one report covering both state and OOMs**, run this and
skip the individual loops:

```bash
./scripts/fetch-all-loop.sh
```

It creates a single `./reports/state-loop-<timestamp>/`, then runs
`fetch-cluster-state-loop.sh` and `fetch-cluster-oom-loop.sh` in
sequence, both pointed at that directory via `--report-dir`. Filenames
are prefixed so the two scripts' artifacts coexist without collision
(`_master-*`, `_resources-*`, `_hpa-*`, `_dimensions.csv` from the state
side; `_oom-*` from the OOM side; per-namespace `run.log` vs
`oom-run.log`).

Combined layout (when OOMs are found):

```
reports/state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/
│   ├── run.log                           (state)
│   ├── oom-run.log                       (oom — only if OOMs in ns)
│   ├── _overview.json                    (state, per ns)
│   ├── _hpa-validation.csv               (state, per ns)
│   ├── _dimensions.csv                   (state, per ns)
│   ├── _cluster-state.xlsx               (state, per ns)
│   ├── report.json                       (oom, only if OOMs in ns)
│   ├── report.txt                        (oom, only if OOMs in ns)
│   └── by-stage/<stage>/<ns>/            (state, double-nested)
│       ├── snapshot.json
│       ├── hpa-bindings.json
│       └── desired/...
├── _master-overview.txt                  (state)
├── _resources-overview.txt + .csv        (state)
├── _hpa-validation.csv                   (state, aggregated)
├── _dimensions.csv                       (state, aggregated)
├── _oom-overview.txt                     (oom, only if OOMs found)
├── _oom-findings.csv                     (oom, only if OOMs found)
└── _oom-status.txt                       (oom, only if OOMs found)
```

The final epilogue only prints paths to files that actually exist, so
"no OOMs found" runs produce a clean summary without dead links.

## Interpreting the output

If you have never read one of these reports before, follow this order. Each
step narrows the search from "what is broken across the cluster?" down to
"what exactly is broken with this one HPA?".

### Step 1 — read the top-level overviews

Up to three text dashboards sit at the root of the report, with
different focus:

| File                       | Focus                                                                                  | Present when                  |
|----------------------------|----------------------------------------------------------------------------------------|--------------------------------|
| `_master-overview.txt`     | **HPA health** per namespace — `STATUS`, `HPAS`, `BAD` (failed validations).            | state-loop ran                |
| `_resources-overview.txt`  | **Resource footprint** per namespace — pods, CPU/mem req+lim, PVCs, storage, quota %.   | state-loop ran                |
| `_oom-overview.txt`        | **OOM activity** per namespace — `STATUS`, `OOMS`, `PATTERNS` (e.g. `A x2, C x1`).      | oom-loop found ≥ 1 OOM        |

Start with `_master-overview.txt`. Then `_resources-overview.txt` for
capacity context. Then `_oom-overview.txt` if it exists — its absence
already tells you the cluster had zero OOMKills at the time of the run.

#### `_master-overview.txt`

One row per namespace plus per-stage totals.

```
STAGE    NAMESPACE                          STATUS  HPAS  BAD
phase    pid-004-batch-phase-01-blue        ok       1     1
prod     pid-003-web-prod-01-blue           ok       1     1
ref      pid-001-shop-ref-01-blue           ok       1     0
```

| Column      | Meaning                                                            |
|-------------|--------------------------------------------------------------------|
| `STATUS`    | Did the Python invocation succeed? `ok` or `FAIL` (see `run.log`). |
| `HPAS`      | Number of HPAs that exist in the namespace.                        |
| `BAD`       | Number of HPAs that failed validation (issues > 0).                |

**Skim this first.** If everything is `STATUS=ok` and `BAD=0` you can stop
here. Anything else → step 2.

#### `_resources-overview.txt`

Same shape, different columns:

```
STAGE    NAMESPACE                              PODS    WL  CPU_REQ  CPU_LIM  MEM_REQ  MEM_LIM PVCS  STOR QUOTAS LR SVCS NP QMAX%
test     pid-002-api-test-01-blue                  3   1/1     250m     700m    160Mi    320Mi    0     0      1  1    2  0   30%
```

| Column          | Meaning                                                                              |
|-----------------|--------------------------------------------------------------------------------------|
| `PODS`          | Sum of `replicas` across all Deployments and StatefulSets.                           |
| `WL`            | `<deployments>/<statefulsets>` count.                                                |
| `CPU_REQ` / `CPU_LIM` | Sum of `replicas × container requests/limits` across all workloads.            |
| `MEM_REQ` / `MEM_LIM` | Same for memory.                                                              |
| `PVCS`          | Number of PersistentVolumeClaims.                                                    |
| `STOR`          | Sum of PVC storage in GiB.                                                           |
| `QUOTAS` / `LR` | Counts of ResourceQuotas / LimitRanges in the namespace.                             |
| `SVCS` / `NP`   | Counts of Services / NetworkPolicies.                                                |
| `QMAX%`         | Highest `used / hard` ratio across **any** ResourceQuota dimension. `-` if no quota. |

Use it to spot:
- Workloads with **no requests** (`CPU_REQ=0m, MEM_REQ=0Mi` for an active pod count).
- Namespaces nearing their quota ceiling (`QMAX%` ≥ 80 %).
- Outlier resource footprints between stages (e.g. `prod` requesting less than `test`).

`_resources-overview.csv` has the same data with raw numbers (millicores,
MiB, GiB) for spreadsheet pivoting.

### Step 2 — open the aggregated `_hpa-validation.csv`

(Skip this step if `BAD=0` in `_master-overview.txt` for every namespace.)


One row per HPA across the whole cluster. Sort or filter by `ok`:

| Field                 | Meaning                                                                  |
|-----------------------|--------------------------------------------------------------------------|
| `ok`                  | `True` = no issues found, `False` = at least one issue.                  |
| `targetFound`         | Does the `scaleTargetRef` resolve to an existing Deployment/StatefulSet? |
| `targetHasRequests`   | Do *all* containers on the target Deployment have `resources.requests`?  |
| `targetSpecReplicas`  | Replicas the target is currently configured for.                         |
| `currentReplicas`     | Live `.status.currentReplicas` of the HPA.                               |
| `desiredReplicas`     | Live `.status.desiredReplicas` of the HPA.                               |
| `metricsCount`        | Number of metric entries on `.spec.metrics`.                             |
| `issues`              | **Semicolon-separated** list of every problem found. This is the why.    |

`ok=False` rows are the ones to act on. The `issues` column is the verdict.

### Step 3 — open `by-stage/<stage>/<ns>/.../hpa-bindings.json` for context

For a specific bad HPA, this file contains the same data structured plus
the full list of `.status.conditions` from Kubernetes. Useful when the CSV
issue is vague (e.g. "condition ScalingActive=False") and you want the
reason/message Kubernetes attached.

### Step 4 — only then look at the per-namespace artefacts

| File                                | What it gives you                                                                 |
|-------------------------------------|-----------------------------------------------------------------------------------|
| `snapshot.json`                     | Normalised current state. No verdicts — just facts (replicas, requests, images). |
| `desired/*.yaml`                    | Re-applyable manifests stripped of runtime fields. Diff against git or re-apply.  |
| `_dimensions.csv`                   | Per-workload CPU/memory requests & limits. Use for capacity / sizing reviews.     |
| `_overview.json`                    | The same summary numbers `_master-overview.txt` shows, structured.                |
| `_cluster-state.xlsx`               | Excel workbook with conditional formatting (red/amber/green). Easiest visual.     |
| `run.log`                           | stdout/stderr of the Python invocation for that namespace.                        |

### What the colours in `_cluster-state.xlsx` mean

| Colour | Meaning                                                                  |
|--------|--------------------------------------------------------------------------|
| Red    | HPA has at least one issue, PVC not bound, or quota usage ≥ 95 %.        |
| Amber  | Quota usage 80–95 %, or a workload with no `resources.requests` set.     |
| Green  | HPA passed validation cleanly.                                           |

### Common HPA `issues` messages

| Message                                                                            | Root cause                                                                                              | Fix                                                                            |
|------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| `target Deployment/<name> not found in namespace`                                  | `scaleTargetRef` points at a Deployment/StatefulSet that does not exist.                                | Fix the `scaleTargetRef.name`, or create the missing workload.                 |
| `HPA uses Resource metric but target containers have no resources.requests`        | A `Resource` metric (e.g. CPU) needs the target's containers to have `resources.requests` set.          | Add `resources.requests.cpu` / `memory` to the Deployment's containers.        |
| `no metrics configured`                                                            | `.spec.metrics` is empty.                                                                               | Add at least one metric entry.                                                 |
| `minReplicas missing` / `maxReplicas missing` / `minReplicas (N) > maxReplicas (M)`| Misconfigured spec.                                                                                     | Set both, with `min ≤ max`.                                                    |
| `unsupported scaleTargetRef.kind='<kind>'`                                         | HPA target is neither `Deployment` nor `StatefulSet`.                                                   | Re-target the HPA at a supported kind.                                         |
| `condition ScalingActive=False (FailedGetResourceMetric)`                          | Cluster-side: the HPA controller cannot fetch metrics. Usually no `metrics-server` installed.            | Install metrics-server. The HPA spec itself is fine.                            |
| `condition AbleToScale=False (FailedGetScale)`                                     | Cluster-side: the HPA cannot fetch the current scale (often because the target does not exist).         | Usually accompanied by the `target ... not found` issue above — fix that first. |

### Reading the OOM overview (`_oom-overview.txt`)

Same shape as `_master-overview.txt`, different columns:

```
STAGE    NAMESPACE                          STATUS  OOMS  PATTERNS
prod     pid-003-web-prod-01-blue           ok       2    A x2
phase    pid-004-batch-phase-01-blue        ok       1    E x1
```

| Column     | Meaning                                                                              |
|------------|--------------------------------------------------------------------------------------|
| `STATUS`   | Did `fetch-cluster-oom.py` succeed for the namespace? `ok` / `FAIL` (see `oom-run.log`). |
| `OOMS`     | Number of OOMKilled containers found in the namespace.                                  |
| `PATTERNS` | Compact count of verdict patterns across those OOMs (e.g. `A x2, C x1`).                |

Pattern legend:

| Code | Meaning                                                                  |
|------|--------------------------------------------------------------------------|
| `A`  | MEMORY LEAK — memory grew steadily, hit the limit                         |
| `B`  | SPIKE / LARGE REQUEST — a traffic burst preceded the OOM                  |
| `C`  | UNDER-PROVISIONED LIMIT — normal peak ≥ limit                             |
| `D`  | NODE PRESSURE / NOISY NEIGHBOR — other pods OOMed on the same node        |
| `E`  | STARTUP OVERRUN — app can't initialise inside the limit                   |
| `?`  | INDETERMINATE — manual inspection needed (often: Prometheus not reachable) |

Then open `_oom-findings.csv` for one row per OOMKilled container with
the columns you can pivot on: `stage, namespace, pod, container, node,
workload, oom_at, age, lifetime_s, restart_count, exit_code,
pattern_short, pattern, evidence`. `pattern_short` is one of
`A/B/C/D/E/?`, ready for filtering.

For the deep dive on a specific OOM, open the per-namespace
`report.txt` (full text) or `report.json` (structured) under
`by-stage/<stage>/<ns>/`.

> **Heads-up** — when a namespace has zero OOMs, the loop writes
> **nothing** for it: no per-namespace dir, no `report.json`, no
> `report.txt`. Absence of a folder under `by-stage/` means that
> namespace was healthy, not that the loop failed.

### Config bug vs. cluster-side condition

The validator does not distinguish between *the manifest is wrong* and
*the cluster cannot evaluate it*. Both end up as `ok=False`. The rule of
thumb:

- Issues that start with **`condition ...=False (...)`** are reported by the
  Kubernetes HPA controller and usually point at a cluster-side problem
  (missing metrics-server, RBAC, network) rather than a manifest defect.
- All other issues are **static config defects** detected by this script
  from the manifests alone — they will be wrong on every cluster.

For example, on a local CRC cluster without metrics-server *every* HPA
that uses a Resource metric will show `ScalingActive=False` even though
the spec is correct. Installing `metrics-server` flips those to `ok=True`.


## When to use which

- **I don't know where to start, give me one combined report**: run
  `./scripts/fetch-all-loop.sh`.
- **Speed / fewer files / I want one cluster-wide snapshot**: use
  `fetch-cluster-state.py` directly with `--workers 4` (or higher).
- **Per-namespace isolation, robust to failures**: use
  `fetch-cluster-state-loop.sh`.
- **Investigate a specific OOM in depth**: use `fetch-cluster-oom.py`
  directly with `-n <ns> --pod <name> --logs --prometheus-url <url>`.
- **Cluster-wide OOM sweep**: use `fetch-cluster-oom-loop.sh` (or the
  combined `fetch-all-loop.sh`).

## Relation to the other scripts

| Script                          | Source            | Purpose                                                  |
|---------------------------------|-------------------|----------------------------------------------------------|
| `check-bind-resources.sh`       | Live cluster      | Workload inventory: pods, deployments, STS, PVCs.        |
| `lean-inspector.sh`             | Local YAML files  | One-shot inspector for quotas / limits / netpol.         |
| `lean-inspector-loop.sh`        | Live cluster      | Lean inspector for every `pid-*` namespace.              |
| `fetch-cluster-state.py`        | Live cluster      | Full snapshot + HPA validation + desired-state YAML.     |
| `fetch-cluster-state-loop.sh`   | Live cluster      | Per-namespace driver around the state Python script.     |
| `fetch-cluster-oom.py`          | Live cluster (+Prom)| Per-OOM root-cause report with A/B/C/D/E verdict.       |
| `fetch-cluster-oom-loop.sh`     | Live cluster (+Prom)| Per-namespace driver around the OOM Python script.      |
| `aggregate-resources.py`        | state-loop output | Resources rollup `_resources-overview.{txt,csv}`.        |
| `aggregate-oom.py`              | oom-loop output   | OOM rollup `_oom-overview.txt` + `_oom-findings.csv`.    |
| `fetch-all-loop.sh`             | Live cluster (+Prom)| Combined wrapper: runs state-loop + oom-loop into one dir. |
