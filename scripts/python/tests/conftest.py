import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "fetch-cluster-usage.py"
_spec = importlib.util.spec_from_file_location("fetch_cluster_usage", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.fixture
def fcu():
    """The loaded fetch-cluster-usage module under test."""
    return _mod
