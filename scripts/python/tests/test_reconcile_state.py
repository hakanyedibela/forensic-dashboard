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
