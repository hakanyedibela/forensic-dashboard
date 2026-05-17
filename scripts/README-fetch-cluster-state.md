# fetch-cluster-state

Two companion scripts that snapshot the current state of every OpenShift
project starting with `pid-`, validate HPA bindings, and produce
re-applyable "desired state" YAML manifests.

| Script                              | Role                                                                 |
|-------------------------------------|----------------------------------------------------------------------|
| `scripts/python/fetch-cluster-state.py` | The actual worker. Can run cluster-wide in a single invocation with built-in parallelism, or be targeted at specific projects. |
| `scripts/fetch-cluster-state-loop.sh`   | Bash wrapper. Mirrors the `lean-inspector-loop.sh` / `check-bind-resources.sh` loop pattern: discovers `pid-*` projects, detects stage, invokes the Python script once per namespace, aggregates the CSVs. |

Pick the Python script when you want one fast cluster-wide snapshot. Pick the
shell loop when you want each namespace processed in isolation (separate
output dirs, per-namespace `run.log`s, robust to single-namespace failures).

## Prerequisites

- `oc` (OpenShift CLI), logged in to the target cluster
- Python 3.6.8+
- `pip install pyyaml` (required) and `pip install openpyxl` (optional, enables the `.xlsx` workbook)
- For the loop wrapper: `bash` 4+ (macOS ships 3.2; install a newer bash via `brew install bash` and make sure it precedes `/bin/bash` on `PATH`)

Read access on the `pid-*` projects is sufficient — the scripts never
write to the cluster.

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

## Interpreting the output

If you have never read one of these reports before, follow this order. Each
step narrows the search from "what is broken across the cluster?" down to
"what exactly is broken with this one HPA?".

### Step 1 — read the two top-level overviews

Two text dashboards sit at the root of the report, with different focus:

| File                       | Focus                                                                                  |
|----------------------------|----------------------------------------------------------------------------------------|
| `_master-overview.txt`     | **HPA health** per namespace — `STATUS`, `HPAS`, `BAD` (failed validations).            |
| `_resources-overview.txt`  | **Resource footprint** per namespace — pods, CPU/mem req+lim, PVCs, storage, quota %.   |

Start with `_master-overview.txt`. Then read `_resources-overview.txt`
for capacity context.

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

- **Speed / fewer files**: use `fetch-cluster-state.py` directly with
  `--workers 4` (or higher).
- **Per-namespace isolation, robust to failures, identical pattern to the
  other `*-loop.sh` scripts**: use `fetch-cluster-state-loop.sh`.

## Relation to the other scripts

| Script                    | Source            | Purpose                                           |
|---------------------------|-------------------|---------------------------------------------------|
| `check-bind-resources.sh` | Live cluster      | Workload inventory: pods, deployments, STS, PVCs. |
| `lean-inspector.sh`       | Local YAML files  | One-shot inspector for quotas / limits / netpol.  |
| `lean-inspector-loop.sh`  | Live cluster      | Lean inspector for every `pid-*` namespace.       |
| `fetch-cluster-state.py`  | Live cluster      | Full snapshot + HPA validation + desired-state YAML. |
| `fetch-cluster-state-loop.sh` | Live cluster  | Per-namespace driver around the Python script.    |
