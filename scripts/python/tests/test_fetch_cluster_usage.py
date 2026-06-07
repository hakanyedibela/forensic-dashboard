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


def _pod(name, ns="ns1", owner=None, labels=None):
    md = {"name": name, "namespace": ns, "labels": labels or {}}
    if owner:
        md["ownerReferences"] = [owner]
    return {"metadata": md, "spec": {}, "status": {}}


def test_workload_for_deployment_via_rs(fcu):
    rs = {"metadata": {"name": "web-abc", "namespace": "ns1",
                       "ownerReferences": [{"kind": "Deployment", "name": "web",
                                            "controller": True}]}}
    rs_index = {("ns1", "web-abc"): rs}
    pod = _pod("web-abc-123", owner={"kind": "ReplicaSet", "name": "web-abc",
                                     "controller": True})
    assert fcu.workload_for(pod, rs_index) == ("Deployment", "web")


def test_workload_for_statefulset(fcu):
    pod = _pod("db-0", owner={"kind": "StatefulSet", "name": "db",
                              "controller": True})
    assert fcu.workload_for(pod, {}) == ("StatefulSet", "db")


def test_workload_for_orphan(fcu):
    assert fcu.workload_for(_pod("loose"), {}) == ("", "")


def test_sum_limit_all_present(fcu):
    assert fcu.sum_limit([0.1, 0.2, 0.3]) == pytest.approx(0.6)


def test_sum_limit_any_missing_is_none(fcu):
    assert fcu.sum_limit([0.1, None, 0.3]) is None


def test_sum_limit_empty_is_none(fcu):
    assert fcu.sum_limit([]) is None


def test_sum_usage_partial(fcu):
    assert fcu.sum_usage([1.0, None, 2.0]) == pytest.approx(3.0)


def test_sum_usage_all_none(fcu):
    assert fcu.sum_usage([None, None]) is None


def test_util_pct(fcu):
    assert fcu.util_pct(0.95, 1.0) == pytest.approx(95.0)


def test_util_pct_no_limit_or_usage(fcu):
    assert fcu.util_pct(0.5, None) is None
    assert fcu.util_pct(None, 1.0) is None
    assert fcu.util_pct(0.5, 0) is None


def _leaf(**kw):
    base = dict(
        namespace="ns1", pod="p1", container="c1",
        cpu_request=0.05, cpu_limit=0.2, cpu_now=0.1, cpu_peak=0.15, cpu_avg=0.1,
        mem_request=64 * 1024**2, mem_limit=128 * 1024**2,
        mem_now=100 * 1024**2, mem_peak=120 * 1024**2,
        oom_count=0,
    )
    base.update(kw)
    return base


def test_rollup_sums_and_counts(fcu):
    leaves = [_leaf(container="c1"), _leaf(container="c2", cpu_limit=0.3,
                                           mem_limit=256 * 1024**2)]
    agg = fcu.rollup(leaves)
    assert agg["cpu_limit"] == pytest.approx(0.5)
    assert agg["mem_limit"] == 384 * 1024**2
    assert agg["cpu_now"] == pytest.approx(0.2)
    assert agg["container_count"] == 2
    assert agg["cpu_peak_util_pct"] == pytest.approx(0.3 / 0.5 * 100)


def test_rollup_unset_limit_blocks_util(fcu):
    leaves = [_leaf(cpu_limit=None)]
    agg = fcu.rollup(leaves)
    assert agg["cpu_limit"] is None
    assert agg["cpu_peak_util_pct"] is None


def test_rollup_pod_count_distinct(fcu):
    leaves = [_leaf(pod="p1"), _leaf(pod="p1", container="c2"), _leaf(pod="p2")]
    agg = fcu.rollup(leaves)
    assert agg["pod_count"] == 2
    assert agg["container_count"] == 3


def test_merge_ooms_dedup_and_source(fcu):
    live = [{"namespace": "ns1", "pod": "p1", "container": "c1",
             "restart_count": 3, "finished_at": "2026-06-07T10:00:00Z",
             "exit_code": 137}]
    thanos = [
        {"namespace": "ns1", "pod": "p1", "container": "c1", "oom_events": 5},
        {"namespace": "ns1", "pod": "p2", "container": "c1", "oom_events": 1},
    ]
    merged = fcu.merge_ooms(live, thanos)
    by_key = {(o["namespace"], o["pod"], o["container"]): o for o in merged}
    assert by_key[("ns1", "p1", "c1")]["source"] == "both"
    assert by_key[("ns1", "p1", "c1")]["oom_events"] == 5
    assert by_key[("ns1", "p1", "c1")]["restart_count"] == 3
    assert by_key[("ns1", "p2", "c1")]["source"] == "thanos"


def test_merge_ooms_live_only(fcu):
    live = [{"namespace": "ns1", "pod": "p1", "container": "c1",
             "restart_count": 1}]
    merged = fcu.merge_ooms(live, [])
    assert merged[0]["source"] == "live"


def _pod_full(name, ns, containers, owner=None, node="n1", statuses=None):
    md = {"name": name, "namespace": ns, "labels": {}}
    if owner:
        md["ownerReferences"] = [owner]
    return {
        "metadata": md,
        "spec": {"nodeName": node, "containers": containers},
        "status": {"containerStatuses": statuses or []},
    }


def test_pods_to_leaves_configured(fcu):
    pods = [_pod_full(
        "web-abc-1", "ns1",
        containers=[{"name": "web", "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"}}}],
        owner={"kind": "ReplicaSet", "name": "web-abc", "controller": True},
    )]
    rs = {"metadata": {"name": "web-abc", "namespace": "ns1",
                       "ownerReferences": [{"kind": "Deployment", "name": "web",
                                            "controller": True}]}}
    leaves = fcu.pods_to_leaves(pods, {("ns1", "web-abc"): rs})
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf["workload_kind"] == "Deployment"
    assert leaf["workload"] == "web"
    assert leaf["cpu_limit"] == pytest.approx(0.2)
    assert leaf["mem_request"] == 64 * 1024**2
    assert leaf["cpu_now"] is None
    assert leaf["oom_count"] == 0


def test_pods_to_leaves_no_resources(fcu):
    pods = [_pod_full("bare", "ns1", containers=[{"name": "c"}])]
    leaves = fcu.pods_to_leaves(pods, {})
    assert leaves[0]["cpu_limit"] is None
    assert leaves[0]["mem_request"] is None


def test_live_ooms_from_pods(fcu):
    statuses = [{"name": "c", "restartCount": 2, "lastState": {"terminated": {
        "reason": "OOMKilled", "exitCode": 137,
        "finishedAt": "2026-06-07T10:00:00Z"}}}]
    pods = [_pod_full("p1", "ns1", containers=[{"name": "c"}], statuses=statuses)]
    ooms = fcu.live_ooms_from_pods(pods)
    assert ooms == [{"namespace": "ns1", "pod": "p1", "container": "c",
                     "restart_count": 2, "exit_code": 137,
                     "finished_at": "2026-06-07T10:00:00Z"}]


def test_live_ooms_ignores_non_oom(fcu):
    statuses = [{"name": "c", "lastState": {"terminated": {"reason": "Error"}}}]
    pods = [_pod_full("p1", "ns1", containers=[{"name": "c"}], statuses=statuses)]
    assert fcu.live_ooms_from_pods(pods) == []


def test_build_namespace_tree(fcu):
    leaves = [
        _leaf(namespace="ns1", pod="web-1", container="web"),
        _leaf(namespace="ns1", pod="web-2", container="web"),
    ]
    for leaf in leaves:
        leaf["workload_kind"] = "Deployment"
        leaf["workload"] = "web"
    ooms = [{"namespace": "ns1", "pod": "web-1", "container": "web",
             "source": "live"}]
    node = fcu.build_namespace_tree("ns1", leaves, ooms)
    assert node["namespace"] == "ns1"
    assert node["stage"] == "other"
    assert node["totals"]["container_count"] == 2
    assert len(node["workloads"]) == 1
    wl = node["workloads"][0]
    assert (wl["kind"], wl["name"]) == ("Deployment", "web")
    assert wl["totals"]["pod_count"] == 2
    assert len(wl["pods"]) == 2
    assert wl["pods"][0]["containers"][0]["container"] == "web"
    assert node["ooms"] == ooms
