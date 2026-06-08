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


def test_usage_queries_contain_expected_promql(fcu):
    q = fcu.usage_queries("ns1", "24h", "5m")
    assert q["cpu_now"] == (
        'sum by (pod, container) '
        '(rate(container_cpu_usage_seconds_total{namespace="ns1",container!=""}[5m]))'
    )
    assert "max_over_time" in q["cpu_peak"]
    assert "[24h:5m]" in q["cpu_peak"]
    assert q["mem_now"] == (
        'sum by (pod, container) '
        '(container_memory_working_set_bytes{namespace="ns1",container!=""})'
    )
    assert "max_over_time" in q["mem_peak"] and "[24h]" in q["mem_peak"]


def test_parse_vector_by_pod_container(fcu):
    payload = {"data": {"resultType": "vector", "result": [
        {"metric": {"pod": "p1", "container": "c1"}, "value": [0, "0.25"]},
        {"metric": {"pod": "p2", "container": "c1"}, "value": [0, "0.5"]},
    ]}}
    out = fcu.parse_vector_by_pod_container(payload)
    assert out == {("p1", "c1"): 0.25, ("p2", "c1"): 0.5}


def test_parse_vector_empty(fcu):
    assert fcu.parse_vector_by_pod_container({"data": {"result": []}}) == {}


def test_attach_usage(fcu):
    leaves = [_leaf(pod="p1", container="c1", cpu_now=None, cpu_peak=None,
                    mem_now=None, mem_peak=None, cpu_avg=None)]
    usage = {
        "cpu_now": {("p1", "c1"): 0.1},
        "cpu_peak": {("p1", "c1"): 0.18},
        "cpu_avg": {("p1", "c1"): 0.12},
        "mem_now": {("p1", "c1"): 100 * 1024**2},
        "mem_peak": {("p1", "c1"): 120 * 1024**2},
    }
    fcu.attach_usage(leaves, usage)
    assert leaves[0]["cpu_now"] == pytest.approx(0.1)
    assert leaves[0]["cpu_peak"] == pytest.approx(0.18)
    assert leaves[0]["mem_peak"] == 120 * 1024**2


def test_attach_usage_missing_series_stays_none(fcu):
    leaves = [_leaf(pod="p9", container="c1", cpu_now=None)]
    fcu.attach_usage(leaves, {"cpu_now": {}, "cpu_peak": {}, "cpu_avg": {},
                              "mem_now": {}, "mem_peak": {}})
    assert leaves[0]["cpu_now"] is None


def test_thanos_ooms_and_counts(fcu):
    events = {("p1", "c1"): 5.0, ("p2", "c1"): 0.0}
    leaves = [_leaf(namespace="ns1", pod="p1", container="c1", oom_count=0)]
    ooms = fcu.thanos_ooms("ns1", events)
    assert ooms == [{"namespace": "ns1", "pod": "p1", "container": "c1",
                     "oom_events": 5}]
    fcu.attach_oom_counts(leaves, fcu.merge_ooms([], ooms))
    assert leaves[0]["oom_count"] == 1


def test_cli_client_builds_get_args(fcu):
    calls = []

    def fake_run(args):
        calls.append(args)
        return '{"items": [{"metadata": {"name": "x"}}]}'

    client = fcu.CliK8sClient(binary="kubectl", run=fake_run)
    items = client.list_pods("ns1")
    assert calls[0] == ["get", "pods", "-n", "ns1", "-o", "json"]
    assert items == [{"metadata": {"name": "x"}}]


def test_cli_client_all_namespaces(fcu):
    client = fcu.CliK8sClient(binary="oc",
                              run=lambda a: '{"items": []}')
    client.list_pods(None)  # no namespace -> -A
    seen = []
    client2 = fcu.CliK8sClient(binary="oc",
                               run=lambda a: (seen.append(a) or '{"items": []}'))
    client2.list_deployments(None)
    assert seen[0] == ["get", "deployments", "-A", "-o", "json"]


def test_rest_client_builds_url(fcu):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return {"items": [{"metadata": {"name": "p"}}]}

    client = fcu.RestK8sClient(host="https://k8s:6443", token="t",
                               get_json=fake_get)
    client.list_pods("ns1")
    assert seen["url"] == "https://k8s:6443/api/v1/namespaces/ns1/pods"
    client.list_deployments(None)
    assert seen["url"] == "https://k8s:6443/apis/apps/v1/deployments"


def test_choose_backend_prefers_rest_in_cluster(fcu, monkeypatch, tmp_path):
    tok = tmp_path / "token"
    tok.write_text("abc")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    kind = fcu.choose_backend_kind(force_cli=False, force_rest=False,
                                   token_path=str(tok))
    assert kind == "rest"


def test_choose_backend_falls_back_to_cli(fcu, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    kind = fcu.choose_backend_kind(force_cli=False, force_rest=False,
                                   token_path="/nonexistent")
    assert kind == "cli"


def test_choose_backend_force_cli(fcu, monkeypatch, tmp_path):
    tok = tmp_path / "token"
    tok.write_text("abc")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert fcu.choose_backend_kind(force_cli=True, force_rest=False,
                                   token_path=str(tok)) == "cli"


def test_pick_cli_binary(fcu, monkeypatch):
    monkeypatch.delenv("OC_BIN", raising=False)
    monkeypatch.delenv("KUBECTL_BIN", raising=False)
    monkeypatch.setattr(fcu.shutil, "which",
                        lambda b: "/usr/bin/oc" if b == "oc" else None)
    assert fcu.pick_cli_binary(prefer_kubectl=False) == "oc"
    monkeypatch.setattr(fcu.shutil, "which",
                        lambda b: "/usr/bin/kubectl" if b == "kubectl" else None)
    assert fcu.pick_cli_binary(prefer_kubectl=False) == "kubectl"


class FakeK8s:
    def __init__(self, pods=None, rs=None):
        self._pods = pods or []
        self._rs = rs or []

    def list_pods(self, namespace=None):
        return self._pods

    def list_replicasets(self, namespace=None):
        return self._rs

    def list_namespaces(self):
        return []


def test_collect_namespace_no_thanos(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "pid-001-shop-ref-01-blue",
                     "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"}}}]},
        "status": {"containerStatuses": []},
    }]
    node = fcu.collect_namespace(
        fcu_k8s=FakeK8s(pods=pods),
        namespace="pid-001-shop-ref-01-blue",
        thanos=None, window="24h", step="5m")
    assert node["stage"] == "ref"
    assert node["totals"]["cpu_limit"] == pytest.approx(0.2)
    assert node["totals"]["cpu_now"] is None
    assert node["workloads"][0]["pods"][0]["name"] == "web-1"


def test_collect_namespace_with_thanos(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"}}}]},
        "status": {"containerStatuses": []},
    }]
    from conftest import FakeThanos
    thanos = FakeThanos({
        "rate(container_cpu_usage_seconds_total": [
            {"metric": {"pod": "web-1", "container": "web"},
             "value": [0, "0.05"]}],
        "container_memory_working_set_bytes": [
            {"metric": {"pod": "web-1", "container": "web"},
             "value": [0, str(80 * 1024**2)]}],
        "container_oom_events_total": [
            {"metric": {"pod": "web-1", "container": "web"},
             "value": [0, "2"]}],
    })
    node = fcu.collect_namespace(fcu_k8s=FakeK8s(pods=pods), namespace="ns1",
                                 thanos=thanos, window="24h", step="5m")
    leaf = node["workloads"][0]["pods"][0]["containers"][0]
    assert leaf["cpu_now"] == pytest.approx(0.05)
    assert leaf["mem_now"] == 80 * 1024**2
    assert node["totals"]["oom_count"] == 1
    assert any(o["source"] in ("thanos", "both") for o in node["ooms"])


import io
import json


def _sample_tree(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"},
                "requests": {"cpu": "50m", "memory": "64Mi"}}}]},
        "status": {"containerStatuses": []},
    }]

    class K:
        list_pods = lambda self, ns=None: pods
        list_replicasets = lambda self, ns=None: []
    return fcu.collect_namespace(fcu_k8s=K(), namespace="ns1", thanos=None,
                                 window="24h", step="5m")


def test_flatten_rows_one_per_level(fcu):
    rows = fcu.flatten_rows([_sample_tree(fcu)])
    levels = {r["level"] for r in rows}
    assert levels == {"namespace", "workload", "pod", "container"}
    container_row = next(r for r in rows if r["level"] == "container")
    assert container_row["namespace"] == "ns1"
    assert container_row["cpu_limit"] == pytest.approx(0.2)


def test_render_csv_has_header_and_rows(fcu):
    buf = io.StringIO()
    fcu.render_resources_csv([_sample_tree(fcu)], buf)
    text = buf.getvalue()
    assert text.splitlines()[0].startswith("level,stage,namespace")
    assert "container" in text


def test_render_json_structure(fcu):
    buf = io.StringIO()
    fcu.render_json([_sample_tree(fcu)], buf, window="24h", cluster="c1")
    obj = json.loads(buf.getvalue())
    assert obj["window"] == "24h"
    assert obj["cluster"] == "c1"
    assert obj["namespaces"][0]["namespace"] == "ns1"


def test_render_text_contains_levels(fcu):
    buf = io.StringIO()
    fcu.render_text([_sample_tree(fcu)], buf, levels=("namespace", "workload",
                                                      "pod", "container"))
    out = buf.getvalue()
    assert "ns1" in out
    assert "CPU lim" in out
    assert "MEM lim" in out


def test_render_text_level_filter(fcu):
    buf = io.StringIO()
    fcu.render_text([_sample_tree(fcu)], buf, levels=("namespace",))
    out = buf.getvalue()
    assert "CONTAINERS" not in out


def test_select_namespaces_pattern(fcu):
    all_ns = [{"metadata": {"name": "pid-001-shop-ref-01-blue"}},
              {"metadata": {"name": "kube-system"}},
              {"metadata": {"name": "pid-002-api-test-01-blue"}}]

    class K:
        def list_namespaces(self):
            return all_ns
    names = fcu.select_namespaces(K(), pattern=r"^pid-", explicit=None,
                                  all_namespaces=False)
    assert names == ["pid-001-shop-ref-01-blue", "pid-002-api-test-01-blue"]


def test_select_namespaces_explicit(fcu):
    class K:
        def list_namespaces(self):
            raise AssertionError("must not be called when explicit given")
    assert fcu.select_namespaces(K(), pattern=r"^pid-", explicit=["a", "b"],
                                 all_namespaces=False) == ["a", "b"]


def test_build_parser_defaults(fcu):
    args = fcu.build_parser().parse_args([])
    assert args.pattern == "^pid-"
    assert args.window == "24h"
    assert args.step == "5m"
    assert args.level == "namespace,workload,pod,container"


def test_main_help_exits_zero(fcu):
    with pytest.raises(SystemExit) as e:
        fcu.main(["--help"])
    assert e.value.code == 0


def test_make_thanos_no_thanos_returns_none(fcu):
    args = fcu.build_parser().parse_args(["--no-thanos"])
    assert fcu._make_thanos(args) is None


def test_make_thanos_degrades_when_no_querier(fcu, monkeypatch):
    # No URL, no env, no discoverable querier -> degrade to None (not crash).
    monkeypatch.delenv("THANOS_URL", raising=False)
    monkeypatch.setattr(fcu, "discover_querier", lambda: None)
    args = fcu.build_parser().parse_args([])
    assert fcu._make_thanos(args) is None


def test_build_tree_container_rows_carry_util_pct(fcu):
    # Every level, including the container leaf, must carry peak-util% so the
    # text/CSV renderers show it (not just the rollup levels).
    leaf = _leaf(namespace="ns1", pod="p1", container="c1",
                 cpu_peak=0.15, cpu_limit=0.2,
                 mem_peak=110 * 1024**2, mem_limit=128 * 1024**2)
    leaf["workload_kind"] = "Deployment"
    leaf["workload"] = "web"
    node = fcu.build_namespace_tree("ns1", [leaf], [])
    c = node["workloads"][0]["pods"][0]["containers"][0]
    assert c["cpu_peak_util_pct"] == pytest.approx(75.0)
    assert c["mem_peak_util_pct"] == pytest.approx(110 / 128 * 100)


def test_discover_querier_missing_binary_returns_none(fcu, monkeypatch):
    # python:3.12-slim CronJob image has no oc/kubectl -> subprocess raises
    # FileNotFoundError; discovery must degrade to None, not crash.
    def boom(*a, **k):
        raise FileNotFoundError("no such binary")
    monkeypatch.setattr(fcu.subprocess, "run", boom)
    assert fcu.discover_querier() is None


def test_make_thanos_degrades_when_binary_missing(fcu, monkeypatch):
    monkeypatch.delenv("THANOS_URL", raising=False)

    def boom(*a, **k):
        raise FileNotFoundError("no such binary")
    monkeypatch.setattr(fcu.subprocess, "run", boom)
    args = fcu.build_parser().parse_args([])
    assert fcu._make_thanos(args) is None


def test_build_tree_container_util_none_without_limit(fcu):
    leaf = _leaf(namespace="ns1", pod="p1", container="c1",
                 cpu_peak=0.15, cpu_limit=None)
    leaf["workload_kind"] = "Deployment"
    leaf["workload"] = "web"
    node = fcu.build_namespace_tree("ns1", [leaf], [])
    c = node["workloads"][0]["pods"][0]["containers"][0]
    assert c["cpu_peak_util_pct"] is None


def test_cli_list_namespaces_uses_projects_for_oc(fcu):
    seen = []
    client = fcu.CliK8sClient(
        binary="oc", run=lambda a: (seen.append(a) or '{"items": []}'))
    client.list_namespaces()
    assert seen[0] == ["get", "projects", "-o", "json"]


def test_cli_list_namespaces_uses_namespaces_for_kubectl(fcu):
    seen = []
    client = fcu.CliK8sClient(
        binary="kubectl", run=lambda a: (seen.append(a) or '{"items": []}'))
    client.list_namespaces()
    assert seen[0] == ["get", "namespaces", "-o", "json"]


def test_rest_list_namespaces_uses_namespaces(fcu):
    seen = {}
    client = fcu.RestK8sClient(host="https://k8s:6443", token="t",
                               get_json=lambda url: (seen.update(url=url) or
                                                     {"items": []}))
    client.list_namespaces()
    assert seen["url"] == "https://k8s:6443/api/v1/namespaces"


def test_dated_output_dir(fcu):
    now = fcu.datetime(2026, 6, 7, 6, 0, tzinfo=fcu.timezone.utc)
    assert fcu.dated_output_dir("/reports", now) == "/reports/2026-06-07"


def test_write_report_files(fcu, tmp_path):
    out = fcu.write_report_files([_sample_tree(fcu)], str(tmp_path / "2026-06-07"),
                                 window="24h", cluster="c1")
    assert (tmp_path / "2026-06-07" / "resources.csv").exists()
    assert (tmp_path / "2026-06-07" / "ooms.csv").exists()
    report = tmp_path / "2026-06-07" / "report.json"
    assert report.exists()
    obj = json.loads(report.read_text())
    assert obj["namespaces"][0]["namespace"] == "ns1"
    assert out == str(tmp_path / "2026-06-07")


def test_prune_old_reports_removes_only_old_date_dirs(fcu, tmp_path):
    base = tmp_path
    for name in ("2026-05-01", "2026-06-06", "latest", "notes.txt"):
        if name.endswith(".txt"):
            (base / name).write_text("keep me")
        else:
            (base / name).mkdir()
    now = fcu.datetime(2026, 6, 7, tzinfo=fcu.timezone.utc)
    removed = fcu.prune_old_reports(str(base), retention_days=30, now=now)
    assert removed == ["2026-05-01"]
    assert not (base / "2026-05-01").exists()
    assert (base / "2026-06-06").exists()       # within retention
    assert (base / "latest").exists()           # not a date dir -> untouched
    assert (base / "notes.txt").exists()         # not a dir -> untouched


def test_prune_old_reports_zero_is_noop(fcu, tmp_path):
    (tmp_path / "2020-01-01").mkdir()
    now = fcu.datetime(2026, 6, 7, tzinfo=fcu.timezone.utc)
    assert fcu.prune_old_reports(str(tmp_path), retention_days=0, now=now) == []
    assert (tmp_path / "2020-01-01").exists()


def test_build_parser_persistence_defaults(fcu):
    args = fcu.build_parser().parse_args([])
    assert args.date_subdir is False
    assert args.retention_days == 0
    args2 = fcu.build_parser().parse_args(["--date-subdir", "--retention-days", "30"])
    assert args2.date_subdir is True and args2.retention_days == 30


# --- declared-but-idle workloads -------------------------------------------

def _wl(kind, name, containers, replicas=0):
    spec = {"template": {"spec": {"containers": containers}}}
    if replicas is not None:
        spec["replicas"] = replicas
    return {"metadata": {"name": name, "namespace": "ns1"}, "spec": spec}


class IdleFakeK8s:
    """Fake client exposing the full list_* surface for idle-workload tests."""

    def __init__(self, pods=None, rs=None, deployments=None, statefulsets=None,
                 daemonsets=None):
        self._pods = pods or []
        self._rs = rs or []
        self._deploy = deployments or []
        self._sts = statefulsets or []
        self._ds = daemonsets or []

    def list_pods(self, namespace=None):
        return self._pods

    def list_replicasets(self, namespace=None):
        return self._rs

    def list_deployments(self, namespace=None):
        return self._deploy

    def list_statefulsets(self, namespace=None):
        return self._sts

    def list_daemonsets(self, namespace=None):
        return self._ds


def test_idle_workload_entry_template_totals(fcu):
    sts = _wl("StatefulSet", "db", containers=[
        {"name": "db", "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"cpu": "500m", "memory": "512Mi"}}}],
        replicas=0)
    entries = fcu.idle_workload_entries([("StatefulSet", sts)], present_keys=set())
    assert len(entries) == 1
    e = entries[0]
    assert (e["kind"], e["name"]) == ("StatefulSet", "db")
    assert e["idle"] is True
    assert e["pods"] == []
    assert e["totals"]["cpu_limit"] == pytest.approx(0.5)
    assert e["totals"]["mem_limit"] == 512 * 1024**2
    assert e["totals"]["cpu_now"] is None
    assert e["totals"]["pod_count"] == 0
    assert e["totals"]["container_count"] == 1


def test_idle_workload_entries_skips_present(fcu):
    sts = _wl("StatefulSet", "db", containers=[{"name": "db"}])
    entries = fcu.idle_workload_entries([("StatefulSet", sts)],
                                        present_keys={("StatefulSet", "db")})
    assert entries == []


def test_collect_namespace_includes_idle_statefulset(fcu):
    # One running Deployment pod + a StatefulSet scaled to 0 (no pods).
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {},
                     "ownerReferences": [{"kind": "StatefulSet", "name": "web",
                                          "controller": True}]},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {"limits": {"cpu": "200m"}}}]},
        "status": {"containerStatuses": []},
    }]
    idle_sts = _wl("StatefulSet", "db", containers=[
        {"name": "db", "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}}}],
        replicas=0)
    # 'web' StatefulSet has a running pod; 'db' is idle.
    running_sts = _wl("StatefulSet", "web", containers=[{"name": "web"}], replicas=1)
    client = IdleFakeK8s(pods=pods, statefulsets=[running_sts, idle_sts])
    node = fcu.collect_namespace(client, "ns1", thanos=None, window="24h", step="5m")
    by = {(w["kind"], w["name"]): w for w in node["workloads"]}
    assert ("StatefulSet", "db") in by          # idle one is shown
    assert by[("StatefulSet", "db")].get("idle") is True
    assert by[("StatefulSet", "db")]["totals"]["pod_count"] == 0
    assert by[("StatefulSet", "db")]["totals"]["cpu_limit"] == pytest.approx(0.5)
    # the running one is NOT duplicated as idle
    assert by[("StatefulSet", "web")].get("idle") is not True
    # namespace totals exclude the idle workload (no running pods)
    assert node["totals"]["pod_count"] == 1


def test_collect_namespace_idle_opt_out(fcu):
    idle_sts = _wl("StatefulSet", "db", containers=[{"name": "db"}], replicas=0)
    client = IdleFakeK8s(pods=[], statefulsets=[idle_sts])
    node = fcu.collect_namespace(client, "ns1", thanos=None, window="24h",
                                 step="5m", include_idle=False)
    assert node["workloads"] == []


def test_build_parser_no_idle_flag(fcu):
    assert fcu.build_parser().parse_args([]).no_idle_workloads is False
    assert fcu.build_parser().parse_args(
        ["--no-idle-workloads"]).no_idle_workloads is True


def test_render_text_marks_idle_workload(fcu):
    node = {"namespace": "ns1", "stage": "ref",
            "totals": fcu.rollup([]), "ooms": [],
            "workloads": [{"kind": "StatefulSet", "name": "db", "idle": True,
                           "totals": fcu._template_totals(
                               [{"name": "db", "resources": {
                                   "limits": {"cpu": "500m"}}}]),
                           "pods": []}]}
    buf = io.StringIO()
    fcu.render_text([node], buf, levels=("workload",))
    out = buf.getvalue()
    assert "StatefulSet/db" in out and "idle: 0 pods" in out


def test_collect_namespace_idle_tolerates_minimal_client(fcu):
    # A client without list_statefulsets etc. must not crash (idle skipped).
    class Minimal:
        list_pods = lambda self, ns=None: []
        list_replicasets = lambda self, ns=None: []
    node = fcu.collect_namespace(Minimal(), "ns1", thanos=None, window="24h",
                                 step="5m")
    assert node["workloads"] == []


# --- stage / cluster rollups + legend --------------------------------------

import csv as _csv


def _node(ns, stage, totals, workloads=None, ooms=None):
    return {"namespace": ns, "stage": stage, "totals": totals,
            "workloads": workloads or [], "ooms": ooms or []}


def _mk_totals(fcu, cpu_limit, cpu_peak, mem_limit, mem_peak, oom=0):
    leaf = _leaf(cpu_limit=cpu_limit, cpu_peak=cpu_peak, cpu_now=cpu_peak,
                 cpu_avg=cpu_peak, mem_limit=mem_limit, mem_peak=mem_peak,
                 mem_now=mem_peak, oom_count=oom)
    return fcu.rollup([leaf])


def test_cluster_summary_sums_namespaces(fcu):
    t1 = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    t2 = _mk_totals(fcu, 0.3, 0.15, 200, 80)
    trees = [_node("a", "ref", t1), _node("b", "test", t2)]
    c = fcu.cluster_summary(trees)
    assert c["cpu_limit"] == pytest.approx(0.5)
    assert c["mem_limit"] == 300
    assert c["pod_count"] == 2
    assert c["container_count"] == 2


def test_stage_summaries_group_and_aggregate(fcu):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    trees = [_node("a", "ref", t), _node("b", "ref", t), _node("c", "test", t)]
    ss = dict(fcu.stage_summaries(trees))
    assert sorted(ss) == ["ref", "test"]
    assert ss["ref"]["container_count"] == 2
    assert ss["test"]["container_count"] == 1


def test_summary_rows_shape(fcu):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    trees = [_node("a", "ref", t), _node("b", "test", t)]
    rows = fcu.summary_rows(trees, kinds=("cluster", "stage"))
    assert rows[0]["level"] == "cluster"
    assert rows[0]["stage"] == "" and rows[0]["namespace"] == ""
    stage_rows = [r for r in rows if r["level"] == "stage"]
    assert {r["stage"] for r in stage_rows} == {"ref", "test"}
    assert all(r["namespace"] == "" for r in stage_rows)


def test_render_csv_includes_cluster_and_stage_rows(fcu):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    trees = [_node("a", "ref", t)]
    buf = io.StringIO()
    fcu.render_resources_csv(trees, buf)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert rows[0]["level"] == "cluster"
    assert any(r["level"] == "stage" for r in rows)
    assert any(r["level"] == "namespace" for r in rows)


def test_render_csv_summary_kinds_stage_only(fcu):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    buf = io.StringIO()
    fcu.render_resources_csv([_node("a", "ref", t)], buf, summary_kinds=("stage",))
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    assert rows[0]["level"] == "stage"
    assert not any(r["level"] == "cluster" for r in rows)


def test_render_json_includes_summaries(fcu):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    buf = io.StringIO()
    fcu.render_json([_node("a", "ref", t)], buf, window="24h", cluster="c1")
    obj = json.loads(buf.getvalue())
    assert obj["cluster"] == "c1"            # server name unchanged
    assert "cluster_totals" in obj
    assert obj["stage_summaries"][0]["stage"] == "ref"


def test_write_legend_is_german_only(fcu, tmp_path):
    fcu.write_legend(str(tmp_path))
    # LEGEND.md holds the German legend...
    txt = (tmp_path / "LEGEND.md").read_text(encoding="utf-8")
    for token in ("Legende", "Spalten", "Cores", "tatsächliche",
                  "level", "cluster", "stage", "cpu_limit", "oom_events"):
        assert token in txt
    # ...and there is no separate LEGEND.de.md.
    assert not (tmp_path / "LEGEND.de.md").exists()


def test_write_all_reports_combined_and_by_stage(fcu, tmp_path):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    trees = [_node("pid-a-ref", "ref", t), _node("pid-b-test", "test", t)]
    fcu.write_all_reports(trees, str(tmp_path), window="24h", cluster="c1")
    # combined
    assert (tmp_path / "resources.csv").exists()
    assert (tmp_path / "LEGEND.md").exists()
    combined = list(_csv.DictReader(
        io.StringIO((tmp_path / "resources.csv").read_text())))
    assert combined[0]["level"] == "cluster"
    # per-stage folders
    assert (tmp_path / "by-stage" / "ref" / "resources.csv").exists()
    assert (tmp_path / "by-stage" / "test" / "report.json").exists()
    assert (tmp_path / "by-stage" / "ref" / "LEGEND.md").exists()
    assert not (tmp_path / "by-stage" / "ref" / "LEGEND.de.md").exists()
    assert not (tmp_path / "LEGEND.de.md").exists()
    stage_rows = list(_csv.DictReader(
        io.StringIO((tmp_path / "by-stage" / "ref" / "resources.csv").read_text())))
    assert stage_rows[0]["level"] == "stage" and stage_rows[0]["stage"] == "ref"
    assert not any(r["level"] == "cluster" for r in stage_rows)
