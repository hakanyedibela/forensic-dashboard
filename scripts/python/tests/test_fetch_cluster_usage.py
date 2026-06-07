def test_module_loads(fcu):
    assert hasattr(fcu, "main")


import pytest


@pytest.mark.parametrize("s,expected", [
    ("100m", 0.1),
    ("1500m", 1.5),
    ("1", 1.0),
    ("2", 2.0),
    ("250000u", 0.25),
    ("500000000n", 0.5),
    (None, None),
    (2, 2.0),
])
def test_parse_cpu(fcu, s, expected):
    assert fcu.parse_cpu(s) == expected


@pytest.mark.parametrize("s,expected", [
    ("128Mi", 128 * 1024**2),
    ("1Gi", 1024**3),
    ("512Ki", 512 * 1024),
    ("64M", 64 * 10**6),
    (None, None),
    (1024, 1024),
])
def test_parse_mem(fcu, s, expected):
    assert fcu.parse_mem(s) == expected


def test_fmt_cores(fcu):
    assert fcu.fmt_cores(0.1) == "0.100"
    assert fcu.fmt_cores(None) == "-"


def test_fmt_bytes(fcu):
    assert fcu.fmt_bytes(1536) == "1.5Ki"
    assert fcu.fmt_bytes(None) == "-"


def test_fmt_pct(fcu):
    assert fcu.fmt_pct(95.0) == "95.0%"
    assert fcu.fmt_pct(None) == "-"


@pytest.mark.parametrize("ns,stage", [
    ("pid-001-shop-ref-01-blue", "ref"),
    ("pid-002-api-test-01-blue", "test"),
    ("pid-003-web-prod-01-blue", "prod"),
    ("pid-004-batch-phase-01-blue", "phase"),
    ("pid-005-cache-pnext-01-blue", "pnext"),
    ("pid-x-prod", "prod"),
    ("kube-system", "other"),
    ("pid-007-noStage-here-99", "other"),
])
def test_detect_stage(fcu, ns, stage):
    assert fcu.detect_stage(ns) == stage
