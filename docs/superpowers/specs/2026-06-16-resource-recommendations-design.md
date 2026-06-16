# Resource Recommendations Export — Design

Date: 2026-06-16
Component: `scripts/python/fetch-cluster-usage.py`

## Goal

For workloads that are currently **hot** — whose measured `cpu_peak` or
`mem_peak` already exceeds **80%** of their current limit — recommend new
`cpu_request` / `cpu_limit` and `mem_request` / `mem_limit` values sized from
the observed peak, so that the peak no longer exceeds **80%** of the new limit
(20% headroom). Workloads that already sit at or below 80% are **not**
recommended (the report only surfaces at-risk workloads). Then verify, per
namespace, that the **sum** of the resulting values (recommended for hot
resources, current configured for the rest) still fits inside the namespace
ResourceQuota. When the sum
would exceed the quota, the report must make it clearly visible that the
namespace quota has to be increased **before** the recommendations can be
applied.

## Locked decisions

- **Qualification filter (per resource, independent for CPU and MEM):** a
  resource is recommended only when its current peak exceeds the target % of
  its **current** limit, i.e. `peak > (target_util/100) * current_limit`.
  A resource at or below the target is left blank. A workload appears in the
  per-workload report only if it qualifies on at least one resource; if both
  CPU and MEM are at/below target, the workload is omitted entirely.
  - **Unbounded resource:** a resource that has a peak but **no current limit**
    has no headroom guarantee and cannot be expressed as a % of a limit — it is
    treated as **qualifying** (always recommended).
  - A resource with **no peak** cannot exceed the threshold, so it never
    qualifies (this subsumes the missing-peak handling: such columns stay
    blank, and a both-peaks-missing workload is omitted).
- **Limit formula:** `limit = ceil_up(peak / 0.80)`. This places `peak` at
  exactly 80% (or just under, after rounding up) of the new limit.
- **Request formula:** `request = peak` (1.0×). CPU bursts into the headroom;
  memory request sits below peak by design (burstable).
- **Rounding (always UP, so peak stays ≤ 80%):** CPU rounded up to the nearest
  **10 millicores** (0.01 cores); memory rounded up to the nearest **Mi**
  (1048576 bytes).
- **Headroom target is configurable** via `--recommend-util` (default `80`).
  The same `target_util` value is used both for the qualification threshold and
  for the limit sizing.
- **Quota check basis:** compare summed recommendations against the namespace
  ResourceQuota on **both requests and limits** (CPU and memory). Flag the
  namespace if **any** of the four dimensions exceeds its quota.
- **Namespace sum uses recommended values where available, else the
  workload's current configured request/limit.** This keeps the quota gate
  honest for namespaces that contain some workloads without usage data
  (otherwise the total would under-count and a real overflow could be missed).
- Quota source = the parsed ResourceQuota *hard* values already present on the
  namespace-level totals. A namespace **without** a quota yields status
  `no-quota` and is never flagged as exceeding.

## Architecture (Approach A — pure compute + dedicated renderers)

The existing `rollup()` / aggregation code is **not** touched. Recommendations
are computed in a small, pure, unit-testable layer and rendered by dedicated
functions that follow the existing `render_*` convention.

### New pure helpers

```python
def round_up_cpu_10m(cores: float) -> float       # ceil to nearest 0.01 cores
def round_up_mem_mi(b: int) -> int                # ceil to nearest 1 Mi

def compute_recommendation(totals: dict, target_util: float = 80.0) -> dict:
    """Return recommended request/limit for one workload's totals.
    Keys: cpu_request_rec, cpu_limit_rec, mem_request_rec, mem_limit_rec
    (each float|int or None when the corresponding peak is absent)."""
```

`compute_recommendation` reads `cpu_peak` / `mem_peak` and the current
`cpu_limit` / `mem_limit` from a workload's `totals`. For each resource it
first applies the **qualification filter**: the resource qualifies when it has
a peak AND (`current_limit is None` OR `peak > (target_util/100) *
current_limit`). For a qualifying resource:
`request = round_up(peak)`, `limit = round_up(peak / (target_util/100))`.
A non-qualifying resource's columns are `None`. When neither resource
qualifies, the caller omits the workload from the per-workload report.

### Namespace gate

```python
def namespace_recommendation_summary(ns_node, target_util) -> dict:
    """Sum, over all workloads in the namespace, the recommended request/limit
    (falling back to the workload's current configured request/limit when no
    recommendation could be computed). Compare each of the four sums against
    the namespace quota hard value. Return the sums, the quota values, a
    per-dimension status (OK / EXCEEDS / no-quota) and an overall
    quota_action (OK / INCREASE_QUOTA)."""
```

### New renderers (follow existing `render_*` signatures)

- `render_recommendations_csv(trees, fh, target_util)`
- `render_recommendations_human_csv(trees, fh, target_util)`
- `render_rec_namespaces_csv(trees, fh, target_util)`
- `render_rec_namespaces_human_csv(trees, fh, target_util)`
- `render_recommendations_text(trees, fh, target_util)` — mirrors
  `render_text()` / `summary.txt`.

### Wiring

- `write_report_files(...)` gains the five new writes (gated on
  `recommendations_enabled` / `target_util`, threaded through from CLI).
- `write_all_reports(...)` already calls `write_report_files` for the main dir
  and each `by-stage/<stage>/` folder, so the new files appear in all of them
  automatically.

## Output files

All written to the output dir and each `by-stage/<stage>/` folder.

### 1. `recommendations.csv` — one row per qualifying (hot) workload

Columns:
```
stage, namespace, workload_kind, workload,
cpu_peak_cores, cpu_request_cur_cores, cpu_limit_cur_cores,
cpu_request_rec_cores, cpu_limit_rec_cores,
mem_peak_bytes, mem_request_cur_bytes, mem_limit_cur_bytes,
mem_request_rec_bytes, mem_limit_rec_bytes
```
Current values are included so old-vs-new is visible in one row. Recommendation
cells are blank for a resource that does not qualify (at/below target, or no
peak). Only workloads qualifying on at least one resource appear.

### 2. `recommendations-namespaces.csv` — one row per namespace (quota gate)

Columns:
```
stage, namespace,
cpu_request_rec_sum_cores, cpu_request_quota_cores,
cpu_limit_rec_sum_cores,   cpu_limit_quota_cores,
mem_request_rec_sum_bytes, mem_request_quota_bytes,
mem_limit_rec_sum_bytes,   mem_limit_quota_bytes,
cpu_request_status, cpu_limit_status, mem_request_status, mem_limit_status,
quota_action
```
- `*_status` ∈ `OK` / `EXCEEDS` / `no-quota`.
- `quota_action` = `INCREASE_QUOTA` if any dimension is `EXCEEDS`, else `OK`.

### 3. `recommendations-human.csv` / 4. `recommendations-namespaces-human.csv`

Same rows as the raw CSVs, but every numeric value carries its unit
(`200m`, `64.0Mi`, …) using `fmt_cores` / `fmt_bytes`, matching the repo's
Excel / German-locale convention. Headers drop the `_cores` / `_bytes`
suffixes (as the existing human CSVs do).

### 5. `recommendations.txt` — human-readable, styled like `summary.txt`

Per namespace:
- A workload table listing only the qualifying (hot) workloads:
  `WORKLOAD | CPU peak | CPU req cur→rec | CPU lim cur→rec | MEM peak |
  MEM req cur→rec | MEM lim cur→rec`. A namespace with no hot workloads shows
  a short "no workloads over target" line instead of an empty table.
- A **NAMESPACE QUOTA CHECK** block: summed value (recommended for hot
  resources, current for the rest) vs quota per dimension, with a prominent
  `>>> INCREASE QUOTA FIRST <<<` marker when `quota_action = INCREASE_QUOTA`.

## CLI

- `--recommend-util FLOAT` (default `80`) — target peak-as-%-of-limit.
- `--no-recommendations` — opt out of producing the five files.
- Files are written whenever `--output-dir` is set (same trigger as the
  existing outputs).

## Edge cases

- **All peaks missing (current sample data):** nothing qualifies, so
  `recommendations.csv` / `-human.csv` and `recommendations.txt` workload
  sections come out empty (header only / no workload rows). The namespace gate
  falls back entirely to current configured request/limit, so
  `recommendations-namespaces.csv` still reports a meaningful quota comparison
  (current totals vs quota).
- **No hot workloads but usage data present:** same as above — empty
  per-workload report, namespace gate reflects current vs quota.
- **Unbounded resource (peak but no current limit):** always qualifies; a
  recommended limit is produced from the peak.
- **Namespace without quota:** all four statuses `no-quota`, `quota_action`
  `OK`, never flagged.
- **Idle / zero-pod workloads:** no peak → never qualify, omitted from the
  per-workload report; their current configured request/limit still counts
  toward the namespace sum only if they carry configured values.

## Testing

Unit tests (in `scripts/python/tests/`):
- `compute_recommendation`: formula correctness; rounding-up keeps
  `peak ≤ target_util%` of the limit; **qualification filter** — resource at
  or below target → None; resource above target → recommended; per-resource
  independence (CPU hot, MEM cold → CPU only); unbounded resource (peak, no
  limit) → recommended; both at/below target → all-None (workload omitted);
  both-missing → all-None.
- `round_up_cpu_10m` / `round_up_mem_mi`: boundary values round up, exact
  multiples unchanged.
- `namespace_recommendation_summary`: EXCEEDS when sum > quota; OK when within;
  `no-quota` when quota absent; mixed namespace where some workloads have no
  recommendation and fall back to current values.

## Documentation

`reports-usage/LEGEND.md`: add a section describing the five new files, the
request/limit formula, the rounding rule, the `--recommend-util` flag, and the
`quota_action` flag semantics.
