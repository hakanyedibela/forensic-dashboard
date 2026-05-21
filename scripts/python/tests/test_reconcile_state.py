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


def test_desired_from_yaml_text_fallback(monkeypatch):
    monkeypatch.setattr(rs, "yaml", None)
    got = rs.desired_from_yaml_text(MULTI_DOC_YAML)
    assert ("Deployment", "batch-runner") in got
    assert ("Namespace", "pid-004-batch-phase-01-blue") in got


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
