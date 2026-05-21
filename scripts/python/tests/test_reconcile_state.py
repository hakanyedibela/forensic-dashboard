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
