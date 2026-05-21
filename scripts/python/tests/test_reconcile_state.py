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
