# reconcile-state.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone aggregator that reconciles current cluster state (`snapshot.json`) against desired manifests (`desired/*.yaml`) per namespace and writes one presence-level drift CSV per stage.

**Architecture:** A single self-contained script `scripts/python/reconcile-state.py` modeled on `aggregate-resources.py`. Pure helper functions (current-resource extraction, desired-resource extraction, reconciliation) are unit-tested; a thin `main()` wires CLI → walk report tree → write per-stage CSVs → print summary. Desired YAML is parsed with PyYAML when available, with a regex line-extractor fallback so the script keeps a zero-hard-dependency posture.

**Tech Stack:** Python 3 (stdlib `argparse`, `csv`, `json`, `pathlib`, `re`), optional PyYAML (6.0.3 present), pytest 9.0.2 for tests.

---

## File Structure

- Create: `scripts/python/reconcile-state.py` — the aggregator (CLI + pure helpers).
- Create: `scripts/python/tests/test_reconcile_state.py` — pytest unit + integration tests. Loads the hyphenated module via `importlib`.

The hyphenated filename matches siblings (`fetch-cluster-state.py`, `aggregate-resources.py`), so tests import it dynamically rather than with a normal `import`.

---

### Task 1: Module skeleton + dynamic-import test harness

**Files:**
- Create: `scripts/python/reconcile-state.py`
- Create: `scripts/python/tests/test_reconcile_state.py`

- [ ] **Step 1: Create the module skeleton**

Create `scripts/python/reconcile-state.py`:

```python
#!/usr/bin/env python3
"""
reconcile-state.py

Reconcile the *current* cluster state (snapshot.json) against the *desired*
manifests (desired/*.yaml) for every namespace under a state-loop output
directory produced by fetch-cluster-state.py, and write one presence-level
drift CSV per stage:

  * _reconcile-<stage>.csv  -- one row per (kind, name) per namespace, with
                               in_current / in_desired flags and a status
                               (IN_SYNC / MISSING_IN_CLUSTER / NOT_DESIRED)

Matching is presence-level: resources are keyed by (kind, name). Field values
are NOT compared. The report mainly validates that the desired manifests
faithfully cover what the cluster actually has.

Usage:
    python3 reconcile-state.py --input-dir reports/state-loop-<ts>/

Requires:
    * Python 3.6.8+
    * PyYAML (optional; falls back to a kind/name line extractor if missing)
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

try:
    import yaml  # optional
except ImportError:  # pragma: no cover
    yaml = None


# Kinds we reconcile, in stable CSV sort-friendly order. Maps the snapshot.json
# array key -> Kubernetes kind. The Namespace object itself is handled
# separately (it has no array; the snapshot *is* the namespace).
SNAPSHOT_KEY_TO_KIND = {
    "deployments": "Deployment",
    "statefulsets": "StatefulSet",
    "services": "Service",
    "hpas": "HorizontalPodAutoscaler",
    "pvcs": "PersistentVolumeClaim",
    "resourceQuotas": "ResourceQuota",
    "limitRanges": "LimitRange",
    "networkPolicies": "NetworkPolicy",
}

CSV_HEADER = [
    "stage",
    "namespace",
    "kind",
    "name",
    "in_current",
    "in_desired",
    "status",
]
```

- [ ] **Step 2: Create the test harness that loads the hyphenated module**

Create `scripts/python/tests/test_reconcile_state.py`:

```python
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "reconcile-state.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reconcile_state", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load_module()


def test_module_loads_and_exposes_constants():
    assert rs.CSV_HEADER[0] == "stage"
    assert rs.SNAPSHOT_KEY_TO_KIND["hpas"] == "HorizontalPodAutoscaler"
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (1 passed)

- [ ] **Step 4: Commit**

```bash
git add scripts/python/reconcile-state.py scripts/python/tests/test_reconcile_state.py
git commit -m "feat: reconcile-state skeleton + test harness"
```

---

### Task 2: Extract current resources from a snapshot dict

**Files:**
- Modify: `scripts/python/reconcile-state.py`
- Test: `scripts/python/tests/test_reconcile_state.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reconcile_state.py`:

```python
def test_current_resources_includes_namespace_and_arrays():
    snapshot = {
        "namespace": "pid-004-batch-phase-01-blue",
        "deployments": [{"name": "batch-runner"}],
        "statefulsets": [],
        "hpas": [{"name": "batch-runner"}],
        "services": [],
        "pvcs": [],
        "resourceQuotas": [],
        "limitRanges": [],
        "networkPolicies": [],
    }
    got = rs.current_resources(snapshot)
    assert ("Namespace", "pid-004-batch-phase-01-blue") in got
    assert ("Deployment", "batch-runner") in got
    assert ("HorizontalPodAutoscaler", "batch-runner") in got
    # empty arrays contribute nothing
    assert ("Service", "") not in got


def test_current_resources_dedupes():
    snapshot = {
        "namespace": "ns1",
        "deployments": [{"name": "a"}, {"name": "a"}],
    }
    got = rs.current_resources(snapshot)
    assert sum(1 for k in got if k == ("Deployment", "a")) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py::test_current_resources_includes_namespace_and_arrays -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'current_resources'`

- [ ] **Step 3: Implement `current_resources`**

Add to `reconcile-state.py`:

```python
def current_resources(snapshot):
    """Liefert die Menge der aktuellen Ressourcen als {(kind, name)}.

    Die Namespace-Ressource selbst wird immer aufgenommen; danach jede
    benannte Ressource aus den bekannten Snapshot-Arrays. Fehlende oder leere
    Arrays tragen nichts bei. Doppelte (kind, name) werden durch das Set
    automatisch zusammengefasst.
    """
    found = set()
    ns = snapshot.get("namespace")
    if ns:
        found.add(("Namespace", ns))
    for key, kind in SNAPSHOT_KEY_TO_KIND.items():
        for item in snapshot.get(key) or []:
            name = item.get("name")
            if name:
                found.add((kind, name))
    return found
```

- [ ] **Step 4: Run to verify both tests pass**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/reconcile-state.py scripts/python/tests/test_reconcile_state.py
git commit -m "feat: extract current resources from snapshot"
```

---

### Task 3: Extract desired resources from YAML text (fallback parser)

**Files:**
- Modify: `scripts/python/reconcile-state.py`
- Test: `scripts/python/tests/test_reconcile_state.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reconcile_state.py`:

```python
MULTI_DOC_YAML = """\
apiVersion: v1
kind: Namespace
metadata:
  name: pid-004-batch-phase-01-blue
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: batch-runner
  namespace: pid-004-batch-phase-01-blue
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: batch-runner
"""


def test_desired_from_yaml_text_extracts_kind_name():
    got = rs.desired_from_yaml_text(MULTI_DOC_YAML)
    assert ("Namespace", "pid-004-batch-phase-01-blue") in got
    assert ("Deployment", "batch-runner") in got
    assert ("HorizontalPodAutoscaler", "batch-runner") in got


def test_desired_from_yaml_text_ignores_empty_docs():
    got = rs.desired_from_yaml_text("---\n\n---\n")
    assert got == set()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py::test_desired_from_yaml_text_extracts_kind_name -v`
Expected: FAIL with `AttributeError: ... 'desired_from_yaml_text'`

- [ ] **Step 3: Implement `desired_from_yaml_text`**

Add to `reconcile-state.py`. Uses PyYAML if present, else a line extractor that
reads `kind:` and the *first* `name:` under each document's metadata. The
fallback assumes the fetch script's stable formatting (kind at column 0,
`metadata.name` is the first `name:` key in a doc), which holds for all
manifests fetch-cluster-state.py emits.

```python
def desired_from_yaml_text(text):
    """Liefert {(kind, name)} aus einem (Multi-Dokument-)YAML-Text.

    Nutzt PyYAML, wenn verfuegbar. Andernfalls greift ein einfacher
    Zeilen-Extraktor: pro Dokument das erste 'kind:' und das erste 'name:'.
    Das reicht fuer die von fetch-cluster-state.py erzeugten Manifeste, die
    eine stabile Formatierung haben.
    """
    found = set()
    if yaml is not None:
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            name = (doc.get("metadata") or {}).get("name")
            if kind and name:
                found.add((kind, name))
        return found

    # Fallback: split on document markers, scan lines.
    for chunk in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        kind = None
        name = None
        for line in chunk.splitlines():
            stripped = line.strip()
            if kind is None and stripped.startswith("kind:"):
                kind = stripped[len("kind:"):].strip()
            elif name is None and re.match(r"name:\s*\S", stripped):
                name = stripped[len("name:"):].strip()
            if kind and name:
                break
        if kind and name:
            found.add((kind, name))
    return found
```

- [ ] **Step 4: Run to verify tests pass (PyYAML path)**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 5: Verify the fallback path explicitly**

Append a test that forces the fallback by temporarily nulling `rs.yaml`:

```python
def test_desired_from_yaml_text_fallback(monkeypatch):
    monkeypatch.setattr(rs, "yaml", None)
    got = rs.desired_from_yaml_text(MULTI_DOC_YAML)
    assert ("Deployment", "batch-runner") in got
    assert ("Namespace", "pid-004-batch-phase-01-blue") in got
```

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add scripts/python/reconcile-state.py scripts/python/tests/test_reconcile_state.py
git commit -m "feat: parse desired resources from YAML (PyYAML + fallback)"
```

---

### Task 4: Read a namespace directory into (current, desired) sets

**Files:**
- Modify: `scripts/python/reconcile-state.py`
- Test: `scripts/python/tests/test_reconcile_state.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reconcile_state.py`. Builds a tmp namespace dir on disk:

```python
import json as _json


def _write_ns(tmp_path, snapshot, desired_files):
    ns_dir = tmp_path / "by-stage" / "phase" / "pid-004-batch-phase-01-blue"
    ns_dir.mkdir(parents=True)
    (ns_dir / "snapshot.json").write_text(_json.dumps(snapshot))
    if desired_files is not None:
        d = ns_dir / "desired"
        d.mkdir()
        for fname, content in desired_files.items():
            (d / fname).write_text(content)
    return ns_dir


def test_read_namespace_returns_current_and_desired(tmp_path):
    ns_dir = _write_ns(
        tmp_path,
        {"namespace": "pid-004-batch-phase-01-blue",
         "deployments": [{"name": "batch-runner"}]},
        {"40-deployments.yaml":
         "kind: Deployment\nmetadata:\n  name: batch-runner\n"},
    )
    current, desired = rs.read_namespace(ns_dir)
    assert ("Deployment", "batch-runner") in current
    assert ("Deployment", "batch-runner") in desired


def test_read_namespace_missing_desired_dir(tmp_path):
    ns_dir = _write_ns(
        tmp_path,
        {"namespace": "ns1", "deployments": [{"name": "a"}]},
        None,
    )
    current, desired = rs.read_namespace(ns_dir)
    assert ("Deployment", "a") in current
    assert desired == set()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py::test_read_namespace_returns_current_and_desired -v`
Expected: FAIL with `AttributeError: ... 'read_namespace'`

- [ ] **Step 3: Implement `read_namespace`**

Add to `reconcile-state.py`:

```python
def read_namespace(ns_dir):
    """Liest ein Namespace-Verzeichnis und liefert (current, desired) als Sets.

    current stammt aus snapshot.json, desired aus allen desired/*.yaml-Dateien.
    Fehlt das desired/-Verzeichnis, ist desired leer (alle Ressourcen gelten
    dann als NOT_DESIRED).
    """
    snapshot_path = ns_dir / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    current = current_resources(snapshot)

    desired = set()
    desired_dir = ns_dir / "desired"
    if desired_dir.is_dir():
        for path in sorted(desired_dir.glob("*.yaml")):
            desired |= desired_from_yaml_text(path.read_text())
    return current, desired
```

- [ ] **Step 4: Run to verify tests pass**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/reconcile-state.py scripts/python/tests/test_reconcile_state.py
git commit -m "feat: read namespace dir into current/desired sets"
```

---

### Task 5: Reconcile into sorted CSV rows

**Files:**
- Modify: `scripts/python/reconcile-state.py`
- Test: `scripts/python/tests/test_reconcile_state.py`

- [ ] **Step 1: Write the failing test**

Append to `test_reconcile_state.py`:

```python
def test_reconcile_rows_all_three_statuses():
    current = {("Namespace", "ns1"), ("Deployment", "a"), ("Service", "extra")}
    desired = {("Namespace", "ns1"), ("Deployment", "a"), ("HorizontalPodAutoscaler", "missing")}
    rows = rs.reconcile_rows("phase", "ns1", current, desired)
    by_key = {(r["kind"], r["name"]): r for r in rows}

    assert by_key[("Deployment", "a")]["status"] == "IN_SYNC"
    assert by_key[("Deployment", "a")]["in_current"] == "True"
    assert by_key[("Deployment", "a")]["in_desired"] == "True"

    assert by_key[("Service", "extra")]["status"] == "NOT_DESIRED"
    assert by_key[("Service", "extra")]["in_desired"] == "False"

    assert by_key[("HorizontalPodAutoscaler", "missing")]["status"] == "MISSING_IN_CLUSTER"
    assert by_key[("HorizontalPodAutoscaler", "missing")]["in_current"] == "False"

    # every row carries stage + namespace
    assert all(r["stage"] == "phase" and r["namespace"] == "ns1" for r in rows)


def test_reconcile_rows_sorted_by_kind_then_name():
    current = {("Service", "b"), ("Deployment", "z"), ("Deployment", "a")}
    rows = rs.reconcile_rows("s", "ns", current, set())
    keys = [(r["kind"], r["name"]) for r in rows]
    assert keys == sorted(keys)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py::test_reconcile_rows_all_three_statuses -v`
Expected: FAIL with `AttributeError: ... 'reconcile_rows'`

- [ ] **Step 3: Implement `reconcile_rows`**

Add to `reconcile-state.py`:

```python
def reconcile_rows(stage, namespace, current, desired):
    """Vergleicht current/desired (presence-level) und liefert CSV-Zeilen.

    Eine Zeile je (kind, name) aus der Vereinigung beider Mengen, sortiert
    nach (kind, name) fuer stabile Diffs.
    """
    rows = []
    for kind, name in sorted(current | desired):
        in_cur = (kind, name) in current
        in_des = (kind, name) in desired
        if in_cur and in_des:
            status = "IN_SYNC"
        elif in_des:
            status = "MISSING_IN_CLUSTER"
        else:
            status = "NOT_DESIRED"
        rows.append({
            "stage": stage,
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "in_current": str(in_cur),
            "in_desired": str(in_des),
            "status": status,
        })
    return rows
```

- [ ] **Step 4: Run to verify tests pass**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/reconcile-state.py scripts/python/tests/test_reconcile_state.py
git commit -m "feat: reconcile current/desired into sorted CSV rows"
```

---

### Task 6: Walk the report tree, write per-stage CSVs, CLI + summary

**Files:**
- Modify: `scripts/python/reconcile-state.py`
- Test: `scripts/python/tests/test_reconcile_state.py`

- [ ] **Step 1: Write the failing integration test**

Append to `test_reconcile_state.py`:

```python
import csv as _csv


def test_run_writes_per_stage_csv(tmp_path, capsys):
    # phase namespace: one in-sync deployment, one cluster-only service
    _write_ns(
        tmp_path,
        {"namespace": "pid-004-batch-phase-01-blue",
         "deployments": [{"name": "batch-runner"}],
         "services": [{"name": "orphan"}]},
        {"40-deployments.yaml":
         "kind: Deployment\nmetadata:\n  name: batch-runner\n"},
    )

    written = rs.run(tmp_path)

    out_csv = tmp_path / "_reconcile-phase.csv"
    assert out_csv in written
    assert out_csv.exists()

    with out_csv.open() as fh:
        rows = list(_csv.DictReader(fh))
    statuses = {(r["kind"], r["name"]): r["status"] for r in rows}
    assert statuses[("Deployment", "batch-runner")] == "IN_SYNC"
    assert statuses[("Service", "orphan")] == "NOT_DESIRED"
    assert statuses[("Namespace", "pid-004-batch-phase-01-blue")] == "MISSING_IN_CLUSTER"

    summary = capsys.readouterr().out
    assert "phase" in summary


def test_run_skips_namespace_without_snapshot(tmp_path):
    ns_dir = tmp_path / "by-stage" / "test" / "ns-no-snap"
    ns_dir.mkdir(parents=True)
    written = rs.run(tmp_path)
    assert written == []
```

Note: in the first test the `desired/00-namespace.yaml` is intentionally
absent, so the namespace object is `MISSING_IN_CLUSTER` — this exercises that
status without a contrived fixture.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py::test_run_writes_per_stage_csv -v`
Expected: FAIL with `AttributeError: ... 'run'`

- [ ] **Step 3: Implement `run`, `write_csv`, `main`**

Add to `reconcile-state.py`:

```python
def write_csv(path, rows):
    """Schreibt CSV_HEADER + rows nach path."""
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def run(input_dir):
    """Reconcilet alle Namespaces unter input_dir und schreibt je Stage eine
    CSV-Datei in input_dir. Liefert die Liste der geschriebenen Pfade.

    Layout: input_dir/by-stage/<stage>/<ns>/{snapshot.json, desired/*.yaml}
    """
    input_dir = Path(input_dir)
    by_stage = input_dir / "by-stage"
    rows_by_stage = {}

    if by_stage.is_dir():
        for stage_dir in sorted(p for p in by_stage.iterdir() if p.is_dir()):
            stage = stage_dir.name
            for ns_dir in sorted(p for p in stage_dir.iterdir() if p.is_dir()):
                if not (ns_dir / "snapshot.json").is_file():
                    sys.stderr.write(
                        "warn: skipping %s (no snapshot.json)\n" % ns_dir)
                    continue
                current, desired = read_namespace(ns_dir)
                rows = reconcile_rows(stage, ns_dir.name, current, desired)
                rows_by_stage.setdefault(stage, []).extend(rows)

    written = []
    for stage, rows in sorted(rows_by_stage.items()):
        out = input_dir / ("_reconcile-%s.csv" % stage)
        write_csv(out, rows)
        written.append(out)
        n_ns = len({r["namespace"] for r in rows})
        n_out = sum(1 for r in rows if r["status"] != "IN_SYNC")
        print("  [%s] %d namespace(s), %d resource(s), %d out-of-sync -> %s"
              % (stage, n_ns, len(rows), n_out, out.name))

    if not written:
        print("No namespaces found under %s/by-stage" % input_dir)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile current vs desired cluster state into per-stage CSVs.")
    parser.add_argument(
        "--input-dir", required=True,
        help="A state-loop-<ts> report directory (contains by-stage/).")
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        parser.error("input-dir does not exist: %s" % input_dir)

    run(input_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify all tests pass**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add scripts/python/reconcile-state.py scripts/python/tests/test_reconcile_state.py
git commit -m "feat: walk report tree, write per-stage reconcile CSVs + CLI"
```

---

### Task 7: End-to-end run against a real report + README note

**Files:**
- Modify: `scripts/python/reconcile-state.py` (only if the smoke run reveals a bug)
- Modify: `scripts/README.md`

- [ ] **Step 1: Smoke-test against an existing report**

Run:
```bash
python3 scripts/python/reconcile-state.py \
  --input-dir scripts/reports/state-loop-20260517-143709
```
Expected: prints a `[phase] 1 namespace(s), ...` line and writes
`scripts/reports/state-loop-20260517-143709/_reconcile-phase.csv`.

- [ ] **Step 2: Inspect the CSV**

Run: `cat scripts/reports/state-loop-20260517-143709/_reconcile-phase.csv`
Expected: header + rows for `Deployment/batch-runner` (IN_SYNC),
`HorizontalPodAutoscaler/batch-runner` (IN_SYNC), `Namespace/...` (IN_SYNC,
since `desired/00-namespace.yaml` exists in this report). No crash.

- [ ] **Step 3: Remove the smoke-test artifact**

The reports dir holds committed fixtures; do not commit generated CSVs there.
Run: `git status --short scripts/reports` and `git checkout -- scripts/reports` /
`rm` any new `_reconcile-*.csv` so the report tree stays clean. Confirm with
`git status --short`.

- [ ] **Step 4: Document usage in scripts/README.md**

Add a short section near the other `scripts/python/*` aggregators describing:
- what reconcile-state.py does (presence-level current-vs-desired drift),
- the command (`python3 scripts/python/reconcile-state.py --input-dir reports/state-loop-<ts>/`),
- the output (`_reconcile-<stage>.csv`, columns, the three statuses).

Match the surrounding README's existing heading style and tone.

- [ ] **Step 5: Run the full test suite once more**

Run: `python3 -m pytest scripts/python/tests/test_reconcile_state.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add scripts/README.md
git commit -m "docs: document reconcile-state.py usage"
```

---

## Self-Review

**Spec coverage:**
- Purpose / presence-level reconciliation → Tasks 2–5.
- Resource identity `(kind, name)` + kind list / snapshot key map → Task 1 (`SNAPSHOT_KEY_TO_KIND`), Task 2.
- Current from snapshot.json (incl. Namespace) → Task 2.
- Desired from desired/*.yaml (PyYAML + fallback) → Task 3.
- Status taxonomy (IN_SYNC / MISSING_IN_CLUSTER / NOT_DESIRED) → Task 5.
- CSV columns + sorting → Task 1 (`CSV_HEADER`), Task 5.
- Per-stage `_reconcile-<stage>.csv` at input root → Task 6.
- stdout summary → Task 6.
- Edge cases: missing snapshot.json (skip+warn) Task 6; missing desired/ (all NOT_DESIRED) Task 4; empty arrays Task 2; dedupe Task 2.
- Testing: fixture tree all-three-statuses Task 6; fallback extractor Task 3; missing desired Task 4. All covered.

**Placeholder scan:** No TBD/TODO; every code step shows full code. Task 7 Step 4 (README) describes content to add and matches the existing file's style — acceptable since it's prose, not code.

**Type/name consistency:** `current_resources`, `desired_from_yaml_text`, `read_namespace`, `reconcile_rows`, `write_csv`, `run`, `main` used consistently across tasks. `SNAPSHOT_KEY_TO_KIND` and `CSV_HEADER` defined Task 1, used Tasks 2/5/6. Row dict keys match `CSV_HEADER` exactly. `run()` returns list of `Path`, asserted in Task 6 tests.
