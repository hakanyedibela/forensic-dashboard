# Resource Recommendations Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a recommendation export (CSV + human CSV + text) to `fetch-cluster-usage.py` that, for every "hot" workload (peak already over 80% of its current limit), recommends new request/limit values sizing peak to ≤80% of the new limit, and flags namespaces whose summed recommendations would exceed the namespace quota.

**Architecture:** Pure, unit-testable helpers (`compute_recommendation`, `namespace_recommendation_summary`, rounding) plus dedicated `render_*` functions following the file's existing CSV/text-render conventions. The core `rollup()`/aggregation is untouched. Five new files are written into the main output dir and each `by-stage/<stage>/` folder via the existing `write_report_files` / `write_all_reports`.

**Tech Stack:** Python 3 stdlib only (`csv`, `math`, `argparse`); pytest (module loaded via `tests/conftest.py`'s `fcu` fixture).

---

## File Structure

- **Modify** `scripts/python/fetch-cluster-usage.py`
  - Add `import math` (Task 1).
  - New pure helpers: `round_up_cpu_10m`, `round_up_mem_mi`, `_qualifies`, `compute_recommendation`, `_quota_status`, `namespace_recommendation_summary`.
  - New field tables + renderers: `REC_FIELDS`/`REC_COLUMNS`/`recommendation_rows`/`render_recommendations_csv`/`render_recommendations_human_csv`; `REC_NS_FIELDS`/`REC_NS_COLUMNS`/`render_rec_namespaces_csv`/`render_rec_namespaces_human_csv`; `render_recommendations_text`.
  - Wiring: extend `write_report_files`, `write_all_reports`, `build_parser`, `main`.
  - Extend the `LEGEND_TEXT` constant.
- **Modify** `scripts/python/tests/test_fetch_cluster_usage.py` — add unit + integration tests.

All new helpers live alongside the existing helpers of the same kind (rounding near `fmt_*`; compute near `rollup`; renderers near `render_resources_csv`; wiring in `write_report_files`).

**Conventions to follow (already in the file):**
- Source-key → unit-bearing-header field tables, e.g. `CSV_FIELDS` (line ~954).
- `_human_header()` (line ~1029) drops `_cores`/`_bytes` suffixes for the human CSVs.
- `csv.DictWriter(..., extrasaction="ignore")`, writing `""` for `None`.
- `fmt_cores` / `fmt_bytes` / `fmt_pct` (lines 74–111) and `_print_table` (line ~1462) for text.
- Tests load the module via the `fcu` fixture (`tests/conftest.py`).

Run tests from repo root with: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`

---

### Task 1: Rounding helpers (`round_up_cpu_10m`, `round_up_mem_mi`)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (add `import math`; add helpers after `fmt_pct`, ~line 111)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing tests** — append to the test file:

```python
import math as _math  # noqa: F401  (sanity that math import path works)


@pytest.mark.parametrize("cores,expected", [
    (0.10, 0.10),          # exact 100m stays 100m (no spurious bump)
    (0.101, 0.11),         # 101m -> next 10m = 110m
    (0.1001, 0.11),        # just over 100m -> 110m
    (0.0, 0.0),
    (None, None),
])
def test_round_up_cpu_10m(fcu, cores, expected):
    got = fcu.round_up_cpu_10m(cores)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("b,expected", [
    (1024 * 1024, 1024 * 1024),            # exact 1Mi stays 1Mi
    (1024 * 1024 + 1, 2 * 1024 * 1024),    # just over 1Mi -> 2Mi
    (0, 0),
    (None, None),
])
def test_round_up_mem_mi(fcu, b, expected):
    assert fcu.round_up_mem_mi(b) == expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "round_up" -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'round_up_cpu_10m'`

- [ ] **Step 3: Add `import math`** — in the import block (after `import json`, line 10):

```python
import json
import math
```

- [ ] **Step 4: Implement the helpers** — insert immediately after `fmt_pct` (after line 111):

```python
def round_up_cpu_10m(cores):
    """Round CPU cores UP to the next 10 millicores (0.01 cores). None -> None.
    An exact multiple is left unchanged (the 1e-9 nudge absorbs float noise so
    100m does not creep to 110m)."""
    if cores is None:
        return None
    return math.ceil(cores * 1000.0 / 10.0 - 1e-9) * 10 / 1000.0


def round_up_mem_mi(b):
    """Round bytes UP to the next Mi (1048576 bytes). None -> None. Exact
    multiples unchanged."""
    if b is None:
        return None
    mi = 1024 * 1024
    return math.ceil(b / mi - 1e-9) * mi
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "round_up" -v`
Expected: PASS (all parametrized cases)

- [ ] **Step 6: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: add round-up helpers for resource recommendations"
```

---

### Task 2: `compute_recommendation` + `_qualifies`

The per-workload sizing. `request = round_up(peak)`, `limit = round_up(peak / (target_util/100))`, restricted to resources that are "hot" (peak over target% of current limit, or no current limit).

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (after `rollup`, ~line 215)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_compute_recommendation_cpu_hot(fcu):
    # peak 0.9 cores, current limit 1.0 -> 90% > 80% -> qualifies.
    totals = {"cpu_peak": 0.9, "cpu_limit": 1.0, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert rec["cpu_request_rec"] == pytest.approx(0.9, abs=1e-9)   # round_up(0.9)=0.90
    # limit = 0.9 / 0.8 = 1.125 -> round up to 10m -> 1.13
    assert rec["cpu_limit_rec"] == pytest.approx(1.13, abs=1e-9)
    # peak as % of new limit must be <= target
    assert 0.9 / rec["cpu_limit_rec"] * 100 <= 80.0 + 1e-9
    assert rec["mem_request_rec"] is None and rec["mem_limit_rec"] is None


def test_compute_recommendation_cpu_cold_skipped(fcu):
    # peak 0.5 of limit 1.0 -> 50% <= 80% -> does NOT qualify.
    totals = {"cpu_peak": 0.5, "cpu_limit": 1.0, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert all(v is None for v in rec.values())


def test_compute_recommendation_per_resource(fcu):
    # CPU cold, MEM hot -> only MEM filled.
    mi = 1024 * 1024
    totals = {"cpu_peak": 0.1, "cpu_limit": 1.0,
              "mem_peak": 95 * mi, "mem_limit": 100 * mi}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert rec["cpu_request_rec"] is None and rec["cpu_limit_rec"] is None
    assert rec["mem_request_rec"] == 95 * mi                      # round_up(95Mi)
    # 95Mi / 0.8 = 118.75Mi -> round up to 119Mi
    assert rec["mem_limit_rec"] == 119 * mi


def test_compute_recommendation_unbounded_no_limit(fcu):
    # peak present, no current limit -> always qualifies.
    totals = {"cpu_peak": 0.3, "cpu_limit": None, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert rec["cpu_request_rec"] == pytest.approx(0.3, abs=1e-9)
    assert rec["cpu_limit_rec"] == pytest.approx(0.38, abs=1e-9)  # 0.375 -> 0.38


def test_compute_recommendation_no_peak(fcu):
    totals = {"cpu_peak": None, "cpu_limit": 1.0, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert all(v is None for v in rec.values())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "compute_recommendation" -v`
Expected: FAIL with `AttributeError: ... 'compute_recommendation'`

- [ ] **Step 3: Implement** — insert after `rollup` (after the `return agg` at ~line 215):

```python
def _qualifies(peak, current_limit, frac):
    """A resource qualifies for a recommendation when it has no current limit
    (unbounded -> always) or its peak exceeds `frac` of the current limit."""
    if current_limit is None:
        return True
    return peak > frac * current_limit


def compute_recommendation(totals, target_util=80.0):
    """Recommended request/limit for one workload's `totals`, restricted to
    'hot' resources. request = round_up(peak); limit = round_up(peak / frac)
    where frac = target_util/100. Returns a dict with keys cpu_request_rec,
    cpu_limit_rec, mem_request_rec, mem_limit_rec — each None when that resource
    has no peak or is not hot."""
    frac = target_util / 100.0
    rec = {"cpu_request_rec": None, "cpu_limit_rec": None,
           "mem_request_rec": None, "mem_limit_rec": None}

    cpu_peak = totals.get("cpu_peak")
    if cpu_peak is not None and _qualifies(cpu_peak, totals.get("cpu_limit"), frac):
        rec["cpu_request_rec"] = round_up_cpu_10m(cpu_peak)
        rec["cpu_limit_rec"] = round_up_cpu_10m(cpu_peak / frac)

    mem_peak = totals.get("mem_peak")
    if mem_peak is not None and _qualifies(mem_peak, totals.get("mem_limit"), frac):
        rec["mem_request_rec"] = round_up_mem_mi(mem_peak)
        rec["mem_limit_rec"] = round_up_mem_mi(mem_peak / frac)
    return rec
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "compute_recommendation" -v`
Expected: PASS (all five tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: add compute_recommendation (hot-workload right-sizing)"
```

---

### Task 3: Namespace quota gate (`namespace_recommendation_summary` + `_quota_status`)

Sum, per namespace, the recommended value where a workload/resource is hot, else its current configured value; compare each of the four sums to the namespace quota hard value.

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (after `compute_recommendation`)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def _ns_node(workloads, quota):
    """Build a minimal namespace node: workloads is a list of totals dicts;
    quota is the namespace totals carrying the ResourceQuota hard caps."""
    return {
        "stage": "test", "namespace": "ns-x",
        "workloads": [{"kind": "Deployment", "name": f"w{i}", "totals": t}
                      for i, t in enumerate(workloads)],
        "totals": quota,
    }


def test_namespace_summary_exceeds(fcu):
    # One hot CPU workload: peak 0.9/limit 1.0 -> limit_rec 1.13; quota cpu_limit 1.0.
    node = _ns_node(
        [{"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": 1.0, "cpu_limit": 1.0,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_rec_sum"] == pytest.approx(1.13, abs=1e-9)
    assert s["cpu_limit_status"] == "EXCEEDS"
    assert s["quota_action"] == "INCREASE_QUOTA"


def test_namespace_summary_ok(fcu):
    # Hot workload but quota is generous.
    node = _ns_node(
        [{"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": 10.0, "cpu_limit": 10.0,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_status"] == "OK"
    assert s["quota_action"] == "OK"


def test_namespace_summary_no_quota(fcu):
    node = _ns_node(
        [{"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": None, "cpu_limit": None,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_status"] == "no-quota"
    assert s["quota_action"] == "OK"


def test_namespace_summary_mixed_fallback(fcu):
    # w0 is cold -> falls back to its current cpu_limit (0.2);
    # w1 is hot -> uses recommended cpu_limit (0.9/0.8=1.125 -> 1.13).
    # sum = 0.2 + 1.13 = 1.33 > quota 1.0 -> EXCEEDS.
    node = _ns_node(
        [{"cpu_peak": 0.05, "cpu_limit": 0.2, "cpu_request": 0.1,
          "mem_peak": None, "mem_limit": None, "mem_request": None},
         {"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": 5.0, "cpu_limit": 1.0,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_rec_sum"] == pytest.approx(1.33, abs=1e-9)
    assert s["cpu_limit_status"] == "EXCEEDS"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "namespace_summary" -v`
Expected: FAIL with `AttributeError: ... 'namespace_recommendation_summary'`

- [ ] **Step 3: Implement** — insert after `compute_recommendation`:

```python
# base field -> the matching recommendation key in compute_recommendation output.
_REC_BASES = {"cpu_request": "cpu_request_rec", "cpu_limit": "cpu_limit_rec",
              "mem_request": "mem_request_rec", "mem_limit": "mem_limit_rec"}


def _quota_status(total, quota):
    """no-quota when the namespace has no cap; EXCEEDS when the summed
    recommendation is above the cap; OK otherwise (including nothing to place)."""
    if quota is None:
        return "no-quota"
    if total is None:
        return "OK"
    return "EXCEEDS" if total > quota else "OK"


def namespace_recommendation_summary(node, target_util=80.0):
    """Per-namespace gate: for each workload use the recommended value where the
    resource is hot, else its current configured value; sum per dimension
    (unset contributors skipped) and compare to the namespace quota hard cap.
    Returns stage/namespace, per-dimension *_rec_sum / *_quota / *_status, and
    an overall quota_action (INCREASE_QUOTA if any dimension EXCEEDS, else OK)."""
    collected = {base: [] for base in _REC_BASES}
    for wl in node["workloads"]:
        rec = compute_recommendation(wl["totals"], target_util)
        for base, rkey in _REC_BASES.items():
            val = rec[rkey]
            if val is None:
                val = wl["totals"].get(base)   # cold/no-peak -> keep current
            collected[base].append(val)

    result = {"stage": node["stage"], "namespace": node["namespace"]}
    exceeds = False
    for base in _REC_BASES:
        total = sum_usage(collected[base])     # skip None contributors
        quota = node["totals"].get(base)
        status = _quota_status(total, quota)
        result[base + "_rec_sum"] = total
        result[base + "_quota"] = quota
        result[base + "_status"] = status
        exceeds = exceeds or status == "EXCEEDS"
    result["quota_action"] = "INCREASE_QUOTA" if exceeds else "OK"
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "namespace_summary" -v`
Expected: PASS (all four tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: add namespace recommendation quota gate"
```

---

### Task 4: Per-workload CSV renderers (`recommendations.csv` + human)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (after `render_namespaces_human_csv`, ~line 1152)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
import csv as _csv
import io


def _hot_cpu_tree():
    # Complete enough for write_report_files (resources.csv / summary.txt need
    # workloads[*]["pods"] and node totals counts) as well as the rec renderers.
    return [{
        "stage": "test", "namespace": "ns-x",
        "totals": {"cpu_request": 5.0, "cpu_limit": 5.0,
                   "mem_request": None, "mem_limit": None,
                   "pod_count": 2, "container_count": 2, "oom_count": 0},
        "workloads": [
            {"kind": "Deployment", "name": "hot", "pods": [], "totals":
                {"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
                 "mem_peak": None, "mem_limit": None, "mem_request": None}},
            {"kind": "Deployment", "name": "cold", "pods": [], "totals":
                {"cpu_peak": 0.1, "cpu_limit": 1.0, "cpu_request": 0.5,
                 "mem_peak": None, "mem_limit": None, "mem_request": None}},
        ],
        "ooms": [],
    }]


def test_render_recommendations_csv_only_hot(fcu):
    buf = io.StringIO()
    fcu.render_recommendations_csv(_hot_cpu_tree(), buf, target_util=80.0)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert [r["workload"] for r in rows] == ["hot"]          # cold omitted
    assert rows[0]["cpu_limit_rec_cores"] == "1.13"
    assert rows[0]["mem_limit_rec_bytes"] == ""              # no mem peak -> blank


def test_render_recommendations_csv_empty_when_no_peaks(fcu):
    tree = [{
        "stage": "test", "namespace": "ns-x",
        "totals": {"cpu_request": None, "cpu_limit": None,
                   "mem_request": None, "mem_limit": None},
        "workloads": [{"kind": "Deployment", "name": "w", "totals":
            {"cpu_peak": None, "cpu_limit": 1.0, "cpu_request": 0.5,
             "mem_peak": None, "mem_limit": None, "mem_request": None}}],
        "ooms": [],
    }]
    buf = io.StringIO()
    fcu.render_recommendations_csv(tree, buf, target_util=80.0)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert rows == []                                        # header only


def test_render_recommendations_human_csv_units(fcu):
    buf = io.StringIO()
    fcu.render_recommendations_human_csv(_hot_cpu_tree(), buf, target_util=80.0)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert rows[0]["cpu_limit_rec"] == "1.13c"              # cores formatted
    assert rows[0]["cpu_peak"] == "900m"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "render_recommendations" -v`
Expected: FAIL with `AttributeError: ... 'render_recommendations_csv'`

- [ ] **Step 3: Implement** — insert after `render_namespaces_human_csv` (~line 1152):

```python
# (csv header, source key) for the per-workload recommendation CSV. Current
# (cur) values sit beside the recommended (rec) ones so old-vs-new is one row.
REC_FIELDS = [
    ("stage", "stage"), ("namespace", "namespace"),
    ("workload_kind", "workload_kind"), ("workload", "workload"),
    ("cpu_peak_cores", "cpu_peak"),
    ("cpu_request_cur_cores", "cpu_request"),
    ("cpu_limit_cur_cores", "cpu_limit"),
    ("cpu_request_rec_cores", "cpu_request_rec"),
    ("cpu_limit_rec_cores", "cpu_limit_rec"),
    ("mem_peak_bytes", "mem_peak"),
    ("mem_request_cur_bytes", "mem_request"),
    ("mem_limit_cur_bytes", "mem_limit"),
    ("mem_request_rec_bytes", "mem_request_rec"),
    ("mem_limit_rec_bytes", "mem_limit_rec"),
]
REC_COLUMNS = [h for h, _ in REC_FIELDS]
REC_HUMAN_COLUMNS = [_human_header(h) for h, _ in REC_FIELDS]

_REC_CPU_KEYS = {"cpu_peak", "cpu_request", "cpu_limit",
                 "cpu_request_rec", "cpu_limit_rec"}
_REC_MEM_KEYS = {"mem_peak", "mem_request", "mem_limit",
                 "mem_request_rec", "mem_limit_rec"}


def _rec_human(key, value):
    """Format one recommendation cell with its unit (cores/bytes); identity
    strings pass through."""
    if key in _REC_CPU_KEYS:
        return fmt_cores(value)
    if key in _REC_MEM_KEYS:
        return fmt_bytes(value)
    return "" if value is None else value


def recommendation_rows(trees, target_util=80.0):
    """One dict per qualifying (hot) workload, keyed by REC_FIELDS source keys.
    A workload is included only if it qualifies on at least one resource."""
    rows = []
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        for wl in node["workloads"]:
            rec = compute_recommendation(wl["totals"], target_util)
            if all(v is None for v in rec.values()):
                continue
            t = wl["totals"]
            rows.append({
                "stage": node["stage"], "namespace": node["namespace"],
                "workload_kind": wl["kind"], "workload": wl["name"],
                "cpu_peak": t.get("cpu_peak"),
                "cpu_request": t.get("cpu_request"),
                "cpu_limit": t.get("cpu_limit"),
                "cpu_request_rec": rec["cpu_request_rec"],
                "cpu_limit_rec": rec["cpu_limit_rec"],
                "mem_peak": t.get("mem_peak"),
                "mem_request": t.get("mem_request"),
                "mem_limit": t.get("mem_limit"),
                "mem_request_rec": rec["mem_request_rec"],
                "mem_limit_rec": rec["mem_limit_rec"],
            })
    return rows


def render_recommendations_csv(trees, stream, target_util=80.0):
    """Per-workload recommendation CSV (only hot workloads; raw numbers)."""
    writer = csv.DictWriter(stream, fieldnames=REC_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in recommendation_rows(trees, target_util):
        writer.writerow({h: ("" if row.get(k) is None else row.get(k))
                         for h, k in REC_FIELDS})


def render_recommendations_human_csv(trees, stream, target_util=80.0):
    """Human-readable twin: same rows, each metric formatted with its unit."""
    writer = csv.DictWriter(stream, fieldnames=REC_HUMAN_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for row in recommendation_rows(trees, target_util):
        writer.writerow({_human_header(h): _rec_human(k, row.get(k))
                         for h, k in REC_FIELDS})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "render_recommendations" -v`
Expected: PASS (all three tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: add per-workload recommendation CSV renderers"
```

---

### Task 5: Namespace-gate CSV renderers (`recommendations-namespaces.csv` + human)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (after `render_recommendations_human_csv`)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_render_rec_namespaces_csv_flag(fcu):
    tree = _hot_cpu_tree()
    tree[0]["totals"] = {"cpu_request": 0.5, "cpu_limit": 1.0,
                         "mem_request": None, "mem_limit": None}
    # hot rec cpu_limit 1.13 + cold fallback cpu_limit 1.0 = 2.13 > quota 1.0.
    buf = io.StringIO()
    fcu.render_rec_namespaces_csv(tree, buf, target_util=80.0)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert len(rows) == 1
    assert rows[0]["cpu_limit_status"] == "EXCEEDS"
    assert rows[0]["quota_action"] == "INCREASE_QUOTA"
    assert rows[0]["mem_limit_status"] == "no-quota"


def test_render_rec_namespaces_human_csv_units(fcu):
    tree = _hot_cpu_tree()
    tree[0]["totals"] = {"cpu_request": 0.5, "cpu_limit": 1.0,
                         "mem_request": None, "mem_limit": None}
    buf = io.StringIO()
    fcu.render_rec_namespaces_human_csv(tree, buf, target_util=80.0)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert rows[0]["cpu_limit_quota"] == "1c"
    assert rows[0]["quota_action"] == "INCREASE_QUOTA"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "rec_namespaces" -v`
Expected: FAIL with `AttributeError: ... 'render_rec_namespaces_csv'`

- [ ] **Step 3: Implement** — insert after `render_recommendations_human_csv`:

```python
# (csv header, source key) for the per-namespace recommendation gate CSV.
REC_NS_FIELDS = [
    ("stage", "stage"), ("namespace", "namespace"),
    ("cpu_request_rec_sum_cores", "cpu_request_rec_sum"),
    ("cpu_request_quota_cores", "cpu_request_quota"),
    ("cpu_limit_rec_sum_cores", "cpu_limit_rec_sum"),
    ("cpu_limit_quota_cores", "cpu_limit_quota"),
    ("mem_request_rec_sum_bytes", "mem_request_rec_sum"),
    ("mem_request_quota_bytes", "mem_request_quota"),
    ("mem_limit_rec_sum_bytes", "mem_limit_rec_sum"),
    ("mem_limit_quota_bytes", "mem_limit_quota"),
    ("cpu_request_status", "cpu_request_status"),
    ("cpu_limit_status", "cpu_limit_status"),
    ("mem_request_status", "mem_request_status"),
    ("mem_limit_status", "mem_limit_status"),
    ("quota_action", "quota_action"),
]
REC_NS_COLUMNS = [h for h, _ in REC_NS_FIELDS]
REC_NS_HUMAN_COLUMNS = [_human_header(h) for h, _ in REC_NS_FIELDS]

_REC_NS_CPU_KEYS = {"cpu_request_rec_sum", "cpu_request_quota",
                    "cpu_limit_rec_sum", "cpu_limit_quota"}
_REC_NS_MEM_KEYS = {"mem_request_rec_sum", "mem_request_quota",
                    "mem_limit_rec_sum", "mem_limit_quota"}


def _rec_ns_human(key, value):
    if key in _REC_NS_CPU_KEYS:
        return fmt_cores(value)
    if key in _REC_NS_MEM_KEYS:
        return fmt_bytes(value)
    return "" if value is None else value


def render_rec_namespaces_csv(trees, stream, target_util=80.0):
    """Per-namespace recommendation gate CSV (summed rec vs quota + status)."""
    writer = csv.DictWriter(stream, fieldnames=REC_NS_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        s = namespace_recommendation_summary(node, target_util)
        writer.writerow({h: ("" if s.get(k) is None else s.get(k))
                         for h, k in REC_NS_FIELDS})


def render_rec_namespaces_human_csv(trees, stream, target_util=80.0):
    """Human-readable twin of render_rec_namespaces_csv."""
    writer = csv.DictWriter(stream, fieldnames=REC_NS_HUMAN_COLUMNS,
                            extrasaction="ignore")
    writer.writeheader()
    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        s = namespace_recommendation_summary(node, target_util)
        writer.writerow({_human_header(h): _rec_ns_human(k, s.get(k))
                         for h, k in REC_NS_FIELDS})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "rec_namespaces" -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: add namespace-gate recommendation CSV renderers"
```

---

### Task 6: Text renderer (`recommendations.txt`)

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (after `render_text`, ~line 1493)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing test** — append:

```python
def test_render_recommendations_text(fcu):
    tree = _hot_cpu_tree()
    tree[0]["totals"] = {"cpu_request": 0.5, "cpu_limit": 1.0,
                         "mem_request": None, "mem_limit": None}
    buf = io.StringIO()
    fcu.render_recommendations_text(tree, buf, target_util=80.0)
    out = buf.getvalue()
    assert "RESOURCE RECOMMENDATIONS" in out
    assert "NAMESPACE QUOTA CHECK" in out
    assert "Deployment/hot" in out            # hot workload listed
    assert "Deployment/cold" not in out       # cold workload omitted
    assert ">>> INCREASE QUOTA FIRST <<<" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "recommendations_text" -v`
Expected: FAIL with `AttributeError: ... 'render_recommendations_text'`

- [ ] **Step 3: Implement** — insert after `render_text` (after its final block, ~line 1493):

```python
_REC_TEXT_HEADERS = ["WORKLOAD", "CPU peak", "CPU req cur->rec",
                     "CPU lim cur->rec", "MEM peak", "MEM req cur->rec",
                     "MEM lim cur->rec"]


def render_recommendations_text(trees, stream, target_util=80.0):
    """summary.txt-style text: per namespace, a table of hot workloads
    (peak, current->recommended) plus a NAMESPACE QUOTA CHECK block that prints
    a loud marker when the summed recommendation exceeds the quota."""
    stream.write("\n" + "=" * 100 + "\n")
    stream.write(f"RESOURCE RECOMMENDATIONS — size peak to <= {target_util:.0f}% "
                 "of new limit; only workloads currently over target are listed\n")
    stream.write("=" * 100 + "\n")

    for node in sorted(trees, key=lambda n: (n["stage"], n["namespace"])):
        stream.write("\n" + "-" * 100 + "\n")
        stream.write(f"NAMESPACE  {node['namespace']}   [stage={node['stage']}]\n")
        stream.write("-" * 100 + "\n")

        rows = []
        for wl in node["workloads"]:
            rec = compute_recommendation(wl["totals"], target_util)
            if all(v is None for v in rec.values()):
                continue
            t = wl["totals"]
            rows.append([
                f"{wl['kind']}/{wl['name']}",
                fmt_cores(t.get("cpu_peak")),
                f"{fmt_cores(t.get('cpu_request'))}->{fmt_cores(rec['cpu_request_rec'])}",
                f"{fmt_cores(t.get('cpu_limit'))}->{fmt_cores(rec['cpu_limit_rec'])}",
                fmt_bytes(t.get("mem_peak")),
                f"{fmt_bytes(t.get('mem_request'))}->{fmt_bytes(rec['mem_request_rec'])}",
                f"{fmt_bytes(t.get('mem_limit'))}->{fmt_bytes(rec['mem_limit_rec'])}",
            ])
        if rows:
            _print_table(_REC_TEXT_HEADERS, rows, stream)
        else:
            stream.write("  (no workloads over target)\n")

        s = namespace_recommendation_summary(node, target_util)
        stream.write("\n  NAMESPACE QUOTA CHECK\n")
        qrows = [
            ["CPU requests", fmt_cores(s["cpu_request_rec_sum"]),
             fmt_cores(s["cpu_request_quota"]), s["cpu_request_status"]],
            ["CPU limits", fmt_cores(s["cpu_limit_rec_sum"]),
             fmt_cores(s["cpu_limit_quota"]), s["cpu_limit_status"]],
            ["MEM requests", fmt_bytes(s["mem_request_rec_sum"]),
             fmt_bytes(s["mem_request_quota"]), s["mem_request_status"]],
            ["MEM limits", fmt_bytes(s["mem_limit_rec_sum"]),
             fmt_bytes(s["mem_limit_quota"]), s["mem_limit_status"]],
        ]
        _print_table(["DIMENSION", "REC SUM", "QUOTA", "STATUS"], qrows, stream)
        if s["quota_action"] == "INCREASE_QUOTA":
            stream.write("  >>> INCREASE QUOTA FIRST <<<\n")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "recommendations_text" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: add recommendations.txt renderer"
```

---

### Task 7: Wire into file writing + CLI

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` — `write_report_files` (~line 1322), `write_all_reports` (~line 1354), `build_parser` (~line 1545 output group), `main` (~line 1610 + ~line 1655)
- Test: `scripts/python/tests/test_fetch_cluster_usage.py`

- [ ] **Step 1: Write the failing integration test** — append:

```python
def test_write_report_files_emits_recommendation_files(fcu, tmp_path):
    tree = _hot_cpu_tree()
    tree[0]["totals"] = {"cpu_request": 0.5, "cpu_limit": 1.0,
                         "mem_request": None, "mem_limit": None}
    fcu.write_report_files(tree, str(tmp_path), window="24h", cluster="local",
                           target_util=80.0)
    for name in ("recommendations.csv", "recommendations-human.csv",
                 "recommendations-namespaces.csv",
                 "recommendations-namespaces-human.csv", "recommendations.txt"):
        assert (tmp_path / name).exists(), f"missing {name}"
    assert ">>> INCREASE QUOTA FIRST <<<" in (tmp_path / "recommendations.txt").read_text()


def test_write_report_files_no_recommendations_flag(fcu, tmp_path):
    tree = _hot_cpu_tree()
    fcu.write_report_files(tree, str(tmp_path), window="24h", cluster="local",
                           recommendations=False)
    assert not (tmp_path / "recommendations.csv").exists()
    assert (tmp_path / "resources.csv").exists()   # the rest still written


def test_recommend_util_validation(fcu):
    assert fcu.main(["--no-thanos", "--recommend-util", "0"]) == 2
    assert fcu.main(["--no-thanos", "--recommend-util", "150"]) == 2
```

Note: the validation test calls `main` with an invalid `--recommend-util`; validation must run **before** any cluster access so the bad value returns `2` without a backend. Place the check at the very top of `main` (Step 5).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "write_report_files_emits or no_recommendations_flag or recommend_util_validation" -v`
Expected: FAIL (`write_report_files` has no `target_util` kwarg / recommendation files absent / `main` returns 0 or errors on backend)

- [ ] **Step 3: Extend `write_report_files`** — change its signature and add the writes. Replace the `def write_report_files(...)` line (~1322) and add the new block just before `write_legend(out_dir)` (~1348).

Change the signature:

```python
def write_report_files(trees, out_dir, window, cluster,
                       summary_kinds=("cluster", "stage"),
                       recommendations=True, target_util=80.0):
```

Insert just before `write_legend(out_dir)`:

```python
    if recommendations:
        with open(os.path.join(out_dir, "recommendations.csv"), "w",
                  encoding="utf-8") as f:
            render_recommendations_csv(trees, f, target_util)
        with open(os.path.join(out_dir, "recommendations-human.csv"), "w",
                  encoding="utf-8") as f:
            render_recommendations_human_csv(trees, f, target_util)
        with open(os.path.join(out_dir, "recommendations-namespaces.csv"), "w",
                  encoding="utf-8") as f:
            render_rec_namespaces_csv(trees, f, target_util)
        with open(os.path.join(out_dir, "recommendations-namespaces-human.csv"),
                  "w", encoding="utf-8") as f:
            render_rec_namespaces_human_csv(trees, f, target_util)
        with open(os.path.join(out_dir, "recommendations.txt"), "w",
                  encoding="utf-8") as f:
            f.write(f"Resource recommendations — window {window}, "
                    f"cluster {cluster}\n")
            render_recommendations_text(trees, f, target_util)
```

- [ ] **Step 4: Extend `write_all_reports`** — replace the function (~line 1354) so the flags thread through to both the combined and the by-stage writes:

```python
def write_all_reports(trees, out_dir, window, cluster,
                      recommendations=True, target_util=80.0):
    """Combined report (cluster + per-stage rollups) at out_dir, plus a
    self-contained per-stage report under out_dir/by-stage/<stage>/."""
    write_report_files(trees, out_dir, window, cluster,
                       summary_kinds=("cluster", "stage"),
                       recommendations=recommendations, target_util=target_util)
    for stage, stage_nodes in sorted(group_by_stage(trees).items()):
        write_report_files(stage_nodes,
                           os.path.join(out_dir, "by-stage", stage),
                           window, cluster, summary_kinds=("stage",),
                           recommendations=recommendations,
                           target_util=target_util)
    return out_dir
```

- [ ] **Step 5: Add CLI flags + validation** — in `build_parser`, inside the `out` group, after the `--no-idle-workloads` argument (~line 1561):

```python
    out.add_argument("--recommend-util", type=float, default=80.0,
                     help="Target peak as %% of the recommended limit "
                          "(default: 80). Also the threshold above which a "
                          "workload counts as 'hot' and gets a recommendation.")
    out.add_argument("--no-recommendations", action="store_true",
                     help="Skip the recommendation files (recommendations*.csv "
                          "/ recommendations.txt).")
```

In `main`, add validation immediately after `args = build_parser().parse_args(argv)` and **before** the `global CLI` / `CLI = pick_cli_binary(...)` lines (~line 1610), so an invalid value returns `2` without touching any backend:

```python
    args = build_parser().parse_args(argv)
    if not 0 < args.recommend_util < 100:
        sys.stderr.write("error: --recommend-util must be > 0 and < 100.\n")
        return 2
    global CLI
```

(The `if`/`return` before `global CLI` is valid — `CLI` is only *assigned* after the declaration, never before it.)

- [ ] **Step 6: Pass the flags in `main`'s write call** — replace the `write_all_reports(...)` call (~line 1655):

```python
        write_all_reports(trees, target, args.window, cluster,
                          recommendations=not args.no_recommendations,
                          target_util=args.recommend_util)
```

- [ ] **Step 7: Run the integration tests to verify they pass**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -k "write_report_files_emits or no_recommendations_flag or recommend_util_validation" -v`
Expected: PASS (all three)

- [ ] **Step 8: Run the full test suite**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`
Expected: PASS (all tests, new and pre-existing)

- [ ] **Step 9: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py scripts/python/tests/test_fetch_cluster_usage.py
git commit -m "feat: write recommendation files + add --recommend-util / --no-recommendations"
```

---

### Task 8: Document the new files in LEGEND

**Files:**
- Modify: `scripts/python/fetch-cluster-usage.py` (the `LEGEND_TEXT` constant — the ooms.csv columns section is its tail, ending with the `finished_at` line before the closing `"""`)

- [ ] **Step 1: Add a recommendations section to `LEGEND_TEXT`** — find this exact text near the end of `LEGEND_TEXT`:

```
- `restart_count`, `exit_code` (137 = OOMKilled), `finished_at` — aus dem Live-Pod-Zustand.
"""
```

Replace it with:

```
- `restart_count`, `exit_code` (137 = OOMKilled), `finished_at` — aus dem Live-Pod-Zustand.

## Empfehlungs-Dateien (recommendations*)
Erzeugt zusätzlich, wenn Nutzungsdaten vorliegen (sonst leer). Steuerbar über
`--recommend-util` (Standard 80) und `--no-recommendations`.
- `recommendations.csv` / `recommendations-human.csv` — eine Zeile je **heißem**
  Workload (aktueller Peak über `--recommend-util` % des aktuellen Limits, oder
  ganz ohne Limit). Spalten: aktueller Peak, aktuelle (`_cur`) und empfohlene
  (`_rec`) Requests/Limits für CPU und Speicher. Pro Ressource (CPU/Speicher)
  unabhängig: ist eine Ressource nicht heiß, bleiben ihre `_rec`-Spalten leer.
- `recommendations-namespaces.csv` / `…-human.csv` — eine Zeile je Namespace:
  Summe der Empfehlungen (heiße Ressourcen empfohlen, sonst aktueller Wert) je
  Dimension gegen die Namespace-Quota. `*_status` ∈ `OK` / `EXCEEDS` /
  `no-quota`; `quota_action` = `INCREASE_QUOTA`, wenn eine Dimension die Quota
  übersteigt — dann muss die Namespace-Quota **vorher** erhöht werden.
- `recommendations.txt` — menschenlesbar wie `summary.txt`: je Namespace die
  heißen Workloads (Peak, aktuell→empfohlen) und ein Block **NAMESPACE QUOTA
  CHECK** mit der Markierung `>>> INCREASE QUOTA FIRST <<<` bei Überschreitung.

### Empfehlungs-Formel
- Limit = aufgerundet(Peak ÷ (`--recommend-util`/100)) → der Peak liegt danach
  bei höchstens `--recommend-util` % des neuen Limits.
- Request = aufgerundet(Peak).
- Aufrunden: CPU auf die nächsten 10 Millicores, Speicher auf das nächste Mi.
"""
```

- [ ] **Step 2: Verify the legend renders** — regenerate it into a temp dir and check the new section is present:

Run:
```bash
python -c "import importlib.util, pathlib, tempfile, os; \
p=pathlib.Path('scripts/python/fetch-cluster-usage.py'); \
s=importlib.util.spec_from_file_location('m', p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); \
d=tempfile.mkdtemp(); m.write_legend(d); \
print('OK' if 'Empfehlungs-Dateien' in open(os.path.join(d,'LEGEND.md'), encoding='utf-8').read() else 'MISSING')"
```
Expected: `OK`

- [ ] **Step 3: Run the full test suite once more**

Run: `python -m pytest scripts/python/tests/test_fetch_cluster_usage.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add scripts/python/fetch-cluster-usage.py
git commit -m "docs: document recommendation files in LEGEND"
```

---

## Notes for the implementer

- **Why peaks are empty in the committed `reports-usage/` sample:** that snapshot was produced without Thanos usage data, so `cpu_peak`/`mem_peak` are blank and *nothing* qualifies as hot. The recommendation CSVs will be header-only and `recommendations.txt` will show "(no workloads over target)" until the script runs against a cluster with Thanos. This is expected — do not "fix" it by inventing peak data.
- **Regenerating the sample (optional, not required by any task):** the committed `reports-usage/` files are not regenerated here; the recommendation files will appear there only after a real run with `--output-dir reports-usage`.
- **Rounding sanity:** `round_up_cpu_10m` works in millicores with a `-1e-9` nudge so exact 10m multiples don't creep up; `round_up_mem_mi` does the same in Mi. Both keep peak ≤ target% of the limit because they only ever round the limit *up*.
- **Quota source:** namespace `totals["cpu_request"/"cpu_limit"/"mem_request"/"mem_limit"]` are overwritten with the ResourceQuota *Hard* caps by `apply_quota_to_totals` (line ~295), so they are exactly "the resources given to the namespace." `None` there means no quota → `no-quota` status.
```
