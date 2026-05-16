# fetch-cluster-state

Two companion scripts that snapshot the current state of every OpenShift
project starting with `pid-`, validate HPA bindings, and produce
re-applyable "desired state" YAML manifests.

| Script                       | Role                                                                 |
|------------------------------|----------------------------------------------------------------------|
| `fetch-cluster-state.py`     | The actual worker. Can run cluster-wide in a single invocation with built-in parallelism, or be targeted at specific projects. |
| `fetch-cluster-state-loop.sh`| Bash wrapper. Mirrors the `lean-inspector-loop.sh` / `check-bind-resources.sh` loop pattern: discovers `pid-*` projects, detects stage, invokes the Python script once per namespace, aggregates the CSVs. |

Pick the Python script when you want one fast cluster-wide snapshot. Pick the
shell loop when you want each namespace processed in isolation (separate
output dirs, per-namespace `run.log`s, robust to single-namespace failures).

## Prerequisites

- `oc` (OpenShift CLI), logged in to the target cluster
- Python 3.8+
- `pip install pyyaml`
- For the loop wrapper: `bash` 4+

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
python3 fetch-cluster-state.py [--output-dir DIR]
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
./fetch-cluster-state-loop.sh
```

No arguments. For every `pid-*` project it:

1. Detects the stage.
2. Calls `python3 fetch-cluster-state.py --project <ns>
   --output-dir state-loop-<ts>/by-stage/<stage>/<ns> --workers 1`.
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
state-loop-20260513-101530/
├── by-stage/<stage>/<ns>/
│   ├── run.log
│   ├── _overview.json
│   ├── _hpa-validation.csv
│   ├── _dimensions.csv
│   └── by-stage/<stage>/<ns>/   (the Python script's own tree)
│       ├── snapshot.json
│       ├── hpa-bindings.json
│       └── desired/...
├── _hpa-validation.csv
├── _dimensions.csv
└── _master-overview.txt
```

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
