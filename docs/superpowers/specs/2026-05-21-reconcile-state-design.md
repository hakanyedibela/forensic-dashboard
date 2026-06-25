# reconcile-state.py — Design

**Date:** 2026-05-21
**Status:** Approved

## Purpose

Read a `state-loop-<ts>` report directory produced by
`fetch-cluster-state.py` and, for every namespace, reconcile the **current**
cluster resources (`snapshot.json`) against the **desired** manifests
(`desired/*.yaml`). Emit one CSV per stage that lists every resource and
whether it is in sync.

The reconciliation is **presence-level**: it matches resources by
`(kind, name)` and reports whether each side has it. It does not compare
field values. Its main use is validation — surfacing resources that exist in
the cluster but got no desired manifest, or desired manifests with no live
counterpart.

## Approach

Standalone aggregator script `scripts/python/reconcile-state.py`, modeled on
the existing `scripts/python/aggregate-resources.py`:

- CLI: `python3 reconcile-state.py --input-dir reports/state-loop-<ts>/`
- Discovers namespaces by recursively globbing `snapshot.json` under the input
  dir (`rglob`), mirroring `aggregate-resources.py`'s `find_snapshots`. This
  tolerates the loop wrapper's double-nested layout
  (`by-stage/<stage>/<ns>/by-stage/<stage>/<ns>/snapshot.json`). `stage` and
  `namespace` come from the snapshot's own fields; results are deduped on
  `(stage, namespace)`, preferring the deepest path.
- Reads `snapshot.json` (current) and the sibling `desired/*.yaml` (desired)
  per namespace.
- Writes one `_reconcile-<stage>.csv` per stage at the input-dir root.
- Stays self-contained (no shared package), matching the existing
  one-script-per-aggregation pattern.

## Resource identity

Each resource is keyed by `(kind, name)` within its namespace.

Kinds covered (union of what both sides track):

```
Namespace, Deployment, StatefulSet, Service, HorizontalPodAutoscaler,
PersistentVolumeClaim, ResourceQuota, LimitRange, NetworkPolicy
```

### Current side — from `snapshot.json`

Map snapshot arrays to kinds:

| snapshot key      | kind                     |
|-------------------|--------------------------|
| (top-level)       | `Namespace` (the ns name)|
| `deployments`     | `Deployment`             |
| `statefulsets`    | `StatefulSet`            |
| `services`        | `Service`                |
| `hpas`            | `HorizontalPodAutoscaler`|
| `pvcs`            | `PersistentVolumeClaim`  |
| `resourceQuotas`  | `ResourceQuota`          |
| `limitRanges`     | `LimitRange`             |
| `networkPolicies` | `NetworkPolicy`          |

Each element contributes its `name`. The namespace itself is always added as
one `Namespace` row.

### Desired side — from `desired/*.yaml`

Parse every YAML document (multi-doc, `---`-separated) under `desired/` and
collect `(kind, metadata.name)`. PyYAML is used when available (it is, 6.0.3);
a lightweight `kind:` / `name:` line extractor is the fallback so the script
keeps a zero-hard-dependency posture like its siblings.

## Status taxonomy (presence-level)

| status               | meaning                                          |
|----------------------|--------------------------------------------------|
| `IN_SYNC`            | present in both current and desired              |
| `MISSING_IN_CLUSTER` | in desired, absent from snapshot                 |
| `NOT_DESIRED`        | in snapshot, no desired manifest                 |

## CSV output

One row per resource. Columns:

```
stage,namespace,kind,name,in_current,in_desired,status
```

`in_current` / `in_desired` are `True`/`False`. Rows sorted by
`(namespace, kind, name)` for stable diffs.

File naming: `_reconcile-<stage>.csv` at the input-dir root, one per stage
that has at least one namespace (e.g. `_reconcile-phase.csv`).

## stdout summary

After writing, print a short per-stage summary (namespaces processed, total
resources, out-of-sync count) in the plain style of the existing `run.log`
output.

## Edge cases

- No `snapshot.json` found anywhere under the input dir: print a "No
  namespaces found" message and write nothing.
- Namespace dir missing `desired/`: every current resource becomes
  `NOT_DESIRED`.
- Empty arrays in snapshot: contribute no rows (no error).
- Duplicate `(kind, name)` within one side: deduplicated.

## Testing

- A small fixture report tree (one stage, one namespace) with a known
  snapshot + desired set covering all three statuses; assert the generated
  CSV rows exactly.
- Fallback YAML extractor unit test: feed a multi-doc string, assert the
  `(kind, name)` pairs.
- Empty / missing `desired/` produces all-`NOT_DESIRED` rows.

## Out of scope

- Field-level value comparison (replicas, images, HPA min/max, requests).
- Cross-run drift (comparing two different `state-loop-<ts>` snapshots).
