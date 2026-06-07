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


class FakeThanos:
    """Returns canned instant-vector payloads keyed by substring of the query."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.available = True

    def query(self, expr, ts=None):
        for needle, result in self.responses.items():
            if needle in expr:
                return {"data": {"resultType": "vector", "result": result}}
        return {"data": {"resultType": "vector", "result": []}}


@pytest.fixture
def fake_thanos():
    return FakeThanos
