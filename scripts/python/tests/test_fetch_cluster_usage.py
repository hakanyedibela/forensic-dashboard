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


@pytest.mark.parametrize("v,expected", [
    (None, "-"),
    (0, "0c"),
    (0.0, "0c"),
    (24.5, "24.5c"),         # >= 1 core -> trimmed decimal + 'c' (cores) suffix
    (1.0, "1c"),
    (0.5, "500m"),           # sub-core -> milli
    (0.1, "100m"),
    (0.0028, "2.8m"),
    (0.001, "1m"),           # milli/micro boundary
    (5.25e-07, "0.525µ"),    # tiny peak -> micro (µ sign), never "0.000"
])
def test_fmt_cores(fcu, v, expected):
    assert fcu.fmt_cores(v) == expected


def test_fmt_cores_ge_one_carries_unit_not_excel_date(fcu):
    # A bare '18.5' is read by German-locale Excel as a date; the 'c' suffix
    # keeps CPU cells textual. Every >=1-core value must end in 'c'.
    for v in (18.5, 1.2, 10.0, 24.0):
        assert fcu.fmt_cores(v).endswith("c")


def test_fmt_cores_micro_uses_mu_sign(fcu):
    out = fcu.fmt_cores(2.8e-06)
    assert out.endswith("µ")


def test_fmt_cores_never_scientific_or_zero_flattened(fcu):
    # A real (tiny) Thanos peak must stay readable, not collapse to 0.000.
    out = fcu.fmt_cores(2.8e-06)
    assert "e" not in out and out != "0.000"


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
    assert container_row["cpu_limit_cores"] == pytest.approx(0.2)


def test_render_csv_has_header_and_rows(fcu):
    buf = io.StringIO()
    fcu.render_resources_csv([_sample_tree(fcu)], buf)
    text = buf.getvalue()
    assert text.splitlines()[0].startswith("level,stage,namespace")
    assert "container" in text


def test_render_resources_human_csv_formats_values(fcu):
    buf = io.StringIO()
    fcu.render_resources_human_csv([_sample_tree(fcu)], buf)
    lines = buf.getvalue().splitlines()
    header = lines[0]
    # identity columns stay; metric headers drop the raw-unit suffix because the
    # value now carries its own unit (200m, 6.3Mi, 4.9%).
    assert header.startswith("level,stage,namespace")
    assert "cpu_peak" in header and "mem_peak" in header
    assert "cpu_peak_cores" not in header and "mem_peak_bytes" not in header
    body = "\n".join(lines[1:])
    # formatted, human-readable values — never raw floats / scientific notation
    assert "200m" in body          # cpu_limit 0.2 cores
    assert "e-" not in body
    assert "Mi" in body or "Ki" in body


def test_render_resources_human_csv_row_count_matches_raw(fcu):
    raw, human = io.StringIO(), io.StringIO()
    fcu.render_resources_csv([_sample_tree(fcu)], raw)
    fcu.render_resources_human_csv([_sample_tree(fcu)], human)
    assert len(raw.getvalue().splitlines()) == len(human.getvalue().splitlines())


def test_render_namespaces_human_csv_formats_values(fcu):
    buf = io.StringIO()
    fcu.render_namespaces_human_csv([_sample_tree(fcu)], buf)
    lines = buf.getvalue().splitlines()
    header = lines[0]
    assert header.startswith("stage,namespace")
    assert "cpu_peak" in header and "mem_peak" in header
    assert "cpu_peak_cores" not in header and "mem_peak_bytes" not in header
    body = "\n".join(lines[1:])
    assert "e-" not in body  # no scientific notation
    assert "Mi" in body or "Ki" in body


def test_render_namespaces_human_csv_one_row_per_namespace(fcu):
    raw, human = io.StringIO(), io.StringIO()
    fcu.render_namespaces_csv([_sample_tree(fcu)], raw)
    fcu.render_namespaces_human_csv([_sample_tree(fcu)], human)
    assert len(raw.getvalue().splitlines()) == len(human.getvalue().splitlines())


def test_format_choices_include_human_csvs(fcu):
    a = fcu.build_parser().parse_args(
        ["--format", "resources-human", "--format", "namespaces-human"])
    assert a.format == ["resources-human", "namespaces-human"]


def test_render_stdout_formats_resources_human(fcu):
    buf = io.StringIO()
    fcu.render_stdout_formats([_sample_tree(fcu)], ["resources-human"], buf,
                              window="7d", cluster="c1",
                              levels=("namespace", "workload", "pod", "container"))
    out = buf.getvalue()
    assert out.splitlines()[0].startswith("level,stage,namespace")
    assert "200m" in out and "e-" not in out


def test_render_stdout_formats_namespaces_human(fcu):
    buf = io.StringIO()
    fcu.render_stdout_formats([_sample_tree(fcu)], ["namespaces-human"], buf,
                              window="7d", cluster="c1", levels=("namespace",))
    out = buf.getvalue()
    assert out.splitlines()[0].startswith("stage,namespace")
    assert "cpu_peak" in out.splitlines()[0]
    assert "e-" not in out


def test_format_choice_none_accepted(fcu):
    assert fcu.build_parser().parse_args(["--format", "none"]).format == ["none"]


def test_render_stdout_formats_none_writes_nothing(fcu):
    buf = io.StringIO()
    fcu.render_stdout_formats([_sample_tree(fcu)], ["none"], buf,
                              window="7d", cluster="c1", levels=("namespace",))
    assert buf.getvalue() == ""


def test_render_stdout_formats_text_still_works(fcu):
    buf = io.StringIO()
    fcu.render_stdout_formats([_sample_tree(fcu)], ["text"], buf,
                              window="7d", cluster="c1", levels=("namespace",))
    assert "CPU peak" in buf.getvalue()


def test_csv_headers_carry_units(fcu):
    header = fcu.CSV_COLUMNS
    for col in ("cpu_request_cores", "cpu_limit_cores", "cpu_now_cores",
                "cpu_peak_cores", "cpu_avg_cores", "mem_request_bytes",
                "mem_limit_bytes", "mem_now_bytes", "mem_peak_bytes",
                "cpu_peak_util_pct", "mem_peak_util_pct"):
        assert col in header
    # the bare (unit-less) metric names must no longer be column headers
    for bare in ("cpu_limit", "mem_now", "cpu_peak"):
        assert bare not in header


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


def test_write_report_files_writes_human_summary(fcu, tmp_path):
    out = fcu.write_report_files([_sample_tree(fcu)], str(tmp_path / "r"),
                                 window="7d", cluster="c1")
    summary = tmp_path / "r" / "summary.txt"
    assert summary.exists()
    text = summary.read_text()
    # the readable table, not raw CSV numbers
    assert "ns1" in text
    assert "CPU peak" in text and "MEM peak" in text
    assert "OOM" in text
    assert out == str(tmp_path / "r")


def test_write_report_files_writes_resources_human_csv(fcu, tmp_path):
    fcu.write_report_files([_sample_tree(fcu)], str(tmp_path / "r"),
                           window="7d", cluster="c1")
    human = tmp_path / "r" / "resources-human.csv"
    assert human.exists()
    text = human.read_text()
    assert text.splitlines()[0].startswith("level,stage,namespace")
    assert "e-" not in text  # no scientific notation


def test_write_report_files_writes_namespaces_human_csv(fcu, tmp_path):
    fcu.write_report_files([_sample_tree(fcu)], str(tmp_path / "r"),
                           window="7d", cluster="c1")
    human = tmp_path / "r" / "namespaces-human.csv"
    assert human.exists()
    text = human.read_text()
    assert text.splitlines()[0].startswith("stage,namespace")
    assert "e-" not in text  # no scientific notation


def test_report_files_with_micro_peak_are_utf8(fcu, tmp_path, monkeypatch):
    # A micro-scale CPU peak renders as the µ sign; the files must be written as
    # UTF-8 so a C/POSIX-locale CronJob doesn't hit UnicodeEncodeError. Simulate
    # that locale: make text-mode open() default to ascii (as Linux LANG=C does)
    # unless an explicit encoding is passed. Without encoding="utf-8" in the
    # writer this raises; with it, it passes.
    import builtins
    real_open = builtins.open

    def ascii_default_open(file, mode="r", *args, **kwargs):
        if "b" not in mode and "encoding" not in kwargs and not args:
            kwargs["encoding"] = "ascii"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", ascii_default_open)

    totals = fcu.rollup([])
    totals["cpu_peak"] = 5.25e-07
    node = {"namespace": "ns1", "stage": "ref", "totals": totals,
            "ooms": [], "workloads": []}
    fcu.write_report_files([node], str(tmp_path / "r"), window="7d", cluster="c1")

    monkeypatch.setattr(builtins, "open", real_open)  # read back normally
    for fname in ("summary.txt", "resources-human.csv", "namespaces-human.csv"):
        raw = (tmp_path / "r" / fname).read_bytes()
        assert b"\xc2\xb5" in raw, f"{fname} missing UTF-8 µ"     # b'\xc2\xb5' == 'µ'
        raw.decode("utf-8")  # must be valid UTF-8 (raises otherwise)


def test_write_all_reports_writes_per_stage_summary(fcu, tmp_path):
    tree = _sample_tree(fcu)  # stage detected from "ns1" -> "other"
    fcu.write_all_reports([tree], str(tmp_path / "r"), window="7d", cluster="c1")
    assert (tmp_path / "r" / "summary.txt").exists()
    assert (tmp_path / "r" / "by-stage" / tree["stage"] / "summary.txt").exists()


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


def test_render_namespaces_csv_one_row_per_namespace(fcu):
    ta = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    tb = _mk_totals(fcu, 0.4, 0.3, 200, 150)
    trees = [_node("pid-b", "test", tb), _node("pid-a", "ref", ta)]
    buf = io.StringIO()
    fcu.render_namespaces_csv(trees, buf)
    rows = list(_csv.DictReader(io.StringIO(buf.getvalue())))
    # one row per namespace, sorted by (stage, namespace) -> ref before test
    assert [r["namespace"] for r in rows] == ["pid-a", "pid-b"]
    assert rows[0]["stage"] == "ref"
    assert rows[0]["cpu_limit_cores"] == "0.2"
    assert rows[1]["mem_limit_bytes"] == "200"
    # concise: no per-workload/pod/container identity columns, no level column
    header = buf.getvalue().splitlines()[0]
    for absent in ("level", "workload_kind", "workload", "pod", "container"):
        assert absent not in header.split(",")
    # carries unit-bearing metric columns
    for present in ("cpu_limit_cores", "mem_limit_bytes", "cpu_now_cores",
                    "mem_now_bytes"):
        assert present in header.split(",")


def test_render_text_has_by_namespace_table(fcu):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    buf = io.StringIO()
    fcu.render_text([_node("pid-a", "ref", t)], buf, levels=("namespace",))
    out = buf.getvalue()
    assert "BY NAMESPACE" in out
    assert "ref/pid-a" in out


def test_write_report_files_writes_namespaces_csv(fcu, tmp_path):
    t = _mk_totals(fcu, 0.2, 0.1, 100, 50)
    fcu.write_report_files([_node("pid-a", "ref", t)], str(tmp_path),
                           window="24h", cluster="c1")
    assert (tmp_path / "namespaces.csv").exists()


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


# --- namespace ResourceQuota (Hard caps + Used counters) --------------------

def _quota(hard=None, used=None, name="compute"):
    return {"metadata": {"name": name, "namespace": "ns1"},
            "spec": {"hard": hard or {}},
            "status": {"hard": hard or {}, "used": used or {}}}


def test_parse_quota_hard_and_used(fcu):
    q = fcu.parse_quota([_quota(
        hard={"requests.cpu": "5", "limits.cpu": "10",
              "requests.memory": "10Gi", "limits.memory": "20Gi"},
        used={"requests.cpu": "1", "limits.cpu": "6",
              "requests.memory": "2Gi", "limits.memory": "4Gi"})])
    assert q["cpu_request_hard"] == pytest.approx(5.0)
    assert q["cpu_limit_hard"] == pytest.approx(10.0)
    assert q["mem_request_hard"] == 10 * 1024**3
    assert q["mem_limit_hard"] == 20 * 1024**3
    assert q["cpu_request_used"] == pytest.approx(1.0)
    assert q["cpu_limit_used"] == pytest.approx(6.0)
    assert q["mem_request_used"] == 2 * 1024**3
    assert q["mem_limit_used"] == 4 * 1024**3


def test_parse_quota_accepts_bare_cpu_memory_aliases(fcu):
    # `cpu`/`memory` are the legacy aliases for requests.cpu / requests.memory.
    q = fcu.parse_quota([_quota(hard={"cpu": "2", "memory": "4Gi"},
                                used={"cpu": "500m", "memory": "1Gi"})])
    assert q["cpu_request_hard"] == pytest.approx(2.0)
    assert q["mem_request_hard"] == 4 * 1024**3
    assert q["cpu_request_used"] == pytest.approx(0.5)
    assert q["mem_request_used"] == 1024**3


def test_parse_quota_empty_is_all_none(fcu):
    q = fcu.parse_quota([])
    assert all(v is None for v in q.values())
    assert set(q) == {
        "cpu_request_hard", "cpu_limit_hard", "mem_request_hard",
        "mem_limit_hard", "cpu_request_used", "cpu_limit_used",
        "mem_request_used", "mem_limit_used"}


def test_parse_quota_multiple_takes_min_hard_max_used(fcu):
    # Two quotas capping limits.cpu: the binding Hard is the smaller cap.
    q = fcu.parse_quota([
        _quota(hard={"limits.cpu": "10"}, used={"limits.cpu": "6"}, name="a"),
        _quota(hard={"limits.cpu": "4"}, used={"limits.cpu": "6"}, name="b")])
    assert q["cpu_limit_hard"] == pytest.approx(4.0)
    assert q["cpu_limit_used"] == pytest.approx(6.0)


def test_apply_quota_to_totals_overrides_and_recomputes_util(fcu):
    totals = fcu.rollup([_leaf(cpu_limit=0.2, cpu_peak=2.0,
                               mem_limit=128 * 1024**2, mem_peak=1024**3)])
    q = fcu.parse_quota([_quota(
        hard={"requests.cpu": "5", "limits.cpu": "10",
              "requests.memory": "8Gi", "limits.memory": "16Gi"},
        used={"requests.cpu": "1", "limits.cpu": "6",
              "requests.memory": "2Gi", "limits.memory": "4Gi"})])
    fcu.apply_quota_to_totals(totals, q)
    assert totals["cpu_limit"] == pytest.approx(10.0)      # Hard, not pod 0.2
    assert totals["cpu_request"] == pytest.approx(5.0)
    assert totals["cpu_limit_used"] == pytest.approx(6.0)
    assert totals["mem_request_used"] == 2 * 1024**3
    # peak-util% recomputed against the Hard limit (2.0 / 10 = 20%)
    assert totals["cpu_peak_util_pct"] == pytest.approx(20.0)
    # measured usage is untouched
    assert totals["cpu_peak"] == pytest.approx(2.0)


class QuotaFakeK8s:
    def __init__(self, pods=None, quotas=None):
        self._pods = pods or []
        self._quotas = quotas or []

    def list_pods(self, namespace=None):
        return self._pods

    def list_replicasets(self, namespace=None):
        return []

    def list_resourcequotas(self, namespace=None):
        return self._quotas


def test_collect_namespace_uses_quota_for_namespace_totals(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "requests": {"cpu": "50m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"}}}]},
        "status": {"containerStatuses": []},
    }]
    quotas = [_quota(
        hard={"requests.cpu": "5", "limits.cpu": "10",
              "requests.memory": "10Gi", "limits.memory": "20Gi"},
        used={"requests.cpu": "1", "limits.cpu": "6",
              "requests.memory": "2Gi", "limits.memory": "4Gi"})]
    node = fcu.collect_namespace(QuotaFakeK8s(pods=pods, quotas=quotas), "ns1",
                                 thanos=None, window="24h", step="5m")
    t = node["totals"]
    # namespace request/limit columns come from the quota Hard caps
    assert t["cpu_limit"] == pytest.approx(10.0)
    assert t["cpu_request"] == pytest.approx(5.0)
    assert t["mem_limit"] == 20 * 1024**3
    # the new Used columns are populated from the quota Used counters
    assert t["cpu_limit_used"] == pytest.approx(6.0)
    assert t["cpu_request_used"] == pytest.approx(1.0)
    assert t["mem_request_used"] == 2 * 1024**3
    # container level is unchanged (still the pod-spec value, not the quota)
    leaf = node["workloads"][0]["pods"][0]["containers"][0]
    assert leaf["cpu_limit"] == pytest.approx(0.2)
    assert leaf.get("cpu_limit_used") is None


def test_collect_namespace_no_quota_keeps_pod_spec_totals(fcu):
    pods = [{
        "metadata": {"name": "web-1", "namespace": "ns1", "labels": {}},
        "spec": {"nodeName": "n1", "containers": [
            {"name": "web", "resources": {
                "limits": {"cpu": "200m", "memory": "128Mi"}}}]},
        "status": {"containerStatuses": []},
    }]
    node = fcu.collect_namespace(QuotaFakeK8s(pods=pods, quotas=[]), "ns1",
                                 thanos=None, window="24h", step="5m")
    # no quota -> namespace totals stay the summed pod specs
    assert node["totals"]["cpu_limit"] == pytest.approx(0.2)
    assert node["totals"]["cpu_limit_used"] is None


def test_cli_list_resourcequotas_args(fcu):
    seen = []
    client = fcu.CliK8sClient(
        binary="oc", run=lambda a: (seen.append(a) or '{"items": []}'))
    client.list_resourcequotas("ns1")
    assert seen[0] == ["get", "resourcequotas", "-n", "ns1", "-o", "json"]


def test_rest_list_resourcequotas_url(fcu):
    seen = {}
    client = fcu.RestK8sClient(host="https://k8s:6443", token="t",
                               get_json=lambda url: (seen.update(url=url) or
                                                     {"items": []}))
    client.list_resourcequotas("ns1")
    assert seen["url"] == "https://k8s:6443/api/v1/namespaces/ns1/resourcequotas"


def test_aggregate_totals_sums_quota_used(fcu):
    # stage/cluster roll-ups sum the per-namespace quota Used counters.
    t1 = fcu.rollup([_leaf()])
    t2 = fcu.rollup([_leaf()])
    t1["cpu_limit_used"], t2["cpu_limit_used"] = 6.0, 4.0
    t1["mem_request_used"], t2["mem_request_used"] = 1024**3, 1024**3
    agg = fcu.aggregate_totals([t1, t2])
    assert agg["cpu_limit_used"] == pytest.approx(10.0)
    assert agg["mem_request_used"] == 2 * 1024**3


def test_csv_has_quota_used_columns_next_to_now(fcu):
    cols = fcu.CSV_COLUMNS
    for col in ("cpu_request_used_cores", "cpu_limit_used_cores",
                "mem_request_used_bytes", "mem_limit_used_bytes"):
        assert col in cols
    # the new CPU used columns sit between cpu_now and cpu_peak
    assert (cols.index("cpu_now_cores") < cols.index("cpu_request_used_cores")
            < cols.index("cpu_limit_used_cores") < cols.index("cpu_peak_cores"))
    assert (cols.index("mem_now_bytes") < cols.index("mem_request_used_bytes")
            < cols.index("mem_limit_used_bytes") < cols.index("mem_peak_bytes"))


def test_usage_headers_have_used_columns(fcu):
    for h in ("CPU req-used", "CPU lim-used", "MEM req-used", "MEM lim-used"):
        assert h in fcu._USAGE_HEADERS
    hs = fcu._USAGE_HEADERS
    assert hs.index("CPU now") < hs.index("CPU req-used") < hs.index("CPU peak")


@pytest.mark.parametrize("cores,expected", [
    (0.10, 0.10),          # exact 100m stays 100m (no spurious bump)
    (0.101, 0.11),         # 101m -> next 10m = 110m
    (0.001, 0.01),         # 1m -> next 10m = 10m
    (0.0, 0.0),
    (None, None),
])
def test_round_up_cpu_10m(fcu, cores, expected):
    got = fcu.round_up_cpu_10m(cores)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("b,expected", [
    (1024 * 1024, 1024 * 1024),            # exact 1Mi stays 1Mi
    (1024 * 1024 + 1, 2 * 1024 * 1024),    # just over 1Mi -> 2Mi
    (1, 1024 * 1024),                       # sub-Mi positive -> 1Mi
    (0, 0),
    (None, None),
])
def test_round_up_mem_mi(fcu, b, expected):
    assert fcu.round_up_mem_mi(b) == expected


def test_compute_recommendation_cpu_hot(fcu):
    # peak 0.9 cores, current limit 1.0 -> 90% > 80% -> qualifies.
    totals = {"cpu_peak": 0.9, "cpu_limit": 1.0, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert rec["cpu_request_rec"] == pytest.approx(0.9, abs=1e-9)   # round_up(0.9)=0.90
    # limit = 0.9 / 0.8 = 1.125 -> round up to 10m -> 1.13
    assert rec["cpu_limit_rec"] == pytest.approx(1.13, abs=1e-9)
    # peak as % of new limit must be <= target
    assert 0.9 / rec["cpu_limit_rec"] * 100 <= 80.0 + 1e-9
    assert rec["mem_request_rec"] is None and rec["mem_limit_rec"] is None


def test_compute_recommendation_cpu_cold_skipped(fcu):
    # peak 0.5 of limit 1.0 -> 50% <= 80% -> does NOT qualify.
    totals = {"cpu_peak": 0.5, "cpu_limit": 1.0, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert all(v is None for v in rec.values())


def test_compute_recommendation_per_resource(fcu):
    # CPU cold, MEM hot -> only MEM filled.
    mi = 1024 * 1024
    totals = {"cpu_peak": 0.1, "cpu_limit": 1.0,
              "mem_peak": 95 * mi, "mem_limit": 100 * mi}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert rec["cpu_request_rec"] is None and rec["cpu_limit_rec"] is None
    assert rec["mem_request_rec"] == 95 * mi                      # round_up(95Mi)
    # 95Mi / 0.8 = 118.75Mi -> round up to 119Mi
    assert rec["mem_limit_rec"] == 119 * mi


def test_compute_recommendation_unbounded_no_limit(fcu):
    # peak present, no current limit -> always qualifies.
    totals = {"cpu_peak": 0.3, "cpu_limit": None, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert rec["cpu_request_rec"] == pytest.approx(0.3, abs=1e-9)
    assert rec["cpu_limit_rec"] == pytest.approx(0.38, abs=1e-9)  # 0.375 -> 0.38


def test_compute_recommendation_no_peak(fcu):
    totals = {"cpu_peak": None, "cpu_limit": 1.0, "mem_peak": None, "mem_limit": None}
    rec = fcu.compute_recommendation(totals, target_util=80.0)
    assert all(v is None for v in rec.values())


@pytest.mark.parametrize("peak,limit,frac,expected", [
    (0.8, 1.0, 0.8, False),    # exactly at threshold -> not hot (strict >)
    (0.81, 1.0, 0.8, True),    # just over threshold -> hot
    (0.5, None, 0.8, True),    # no current limit -> always qualifies
])
def test_qualifies_boundary(fcu, peak, limit, frac, expected):
    assert fcu._qualifies(peak, limit, frac) is expected


def test_compute_recommendation_rejects_bad_target(fcu):
    with pytest.raises(ValueError):
        fcu.compute_recommendation({"cpu_peak": 0.9, "cpu_limit": 1.0}, target_util=0.0)


def _ns_node(workloads, quota):
    """Build a minimal namespace node: workloads is a list of totals dicts;
    quota is the namespace totals carrying the ResourceQuota hard caps."""
    return {
        "stage": "test", "namespace": "ns-x",
        "workloads": [{"kind": "Deployment", "name": f"w{i}", "totals": t}
                      for i, t in enumerate(workloads)],
        "totals": quota,
    }


def test_namespace_summary_exceeds(fcu):
    # One hot CPU workload: peak 0.9/limit 1.0 -> limit_rec 1.13; quota cpu_limit 1.0.
    node = _ns_node(
        [{"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": 1.0, "cpu_limit": 1.0,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_rec_sum"] == pytest.approx(1.13, abs=1e-9)
    assert s["cpu_limit_status"] == "EXCEEDS"
    assert s["quota_action"] == "INCREASE_QUOTA"


def test_namespace_summary_ok(fcu):
    # Hot workload but quota is generous.
    node = _ns_node(
        [{"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": 10.0, "cpu_limit": 10.0,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_status"] == "OK"
    assert s["quota_action"] == "OK"


def test_namespace_summary_no_quota(fcu):
    node = _ns_node(
        [{"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": None, "cpu_limit": None,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_status"] == "no-quota"
    assert s["quota_action"] == "OK"


def test_namespace_summary_mixed_fallback(fcu):
    # w0 is cold -> falls back to its current cpu_limit (0.2);
    # w1 is hot -> uses recommended cpu_limit (0.9/0.8=1.125 -> 1.13).
    # sum = 0.2 + 1.13 = 1.33 > quota 1.0 -> EXCEEDS.
    node = _ns_node(
        [{"cpu_peak": 0.05, "cpu_limit": 0.2, "cpu_request": 0.1,
          "mem_peak": None, "mem_limit": None, "mem_request": None},
         {"cpu_peak": 0.9, "cpu_limit": 1.0, "cpu_request": 0.5,
          "mem_peak": None, "mem_limit": None, "mem_request": None}],
        {"cpu_request": 5.0, "cpu_limit": 1.0,
         "mem_request": None, "mem_limit": None})
    s = fcu.namespace_recommendation_summary(node, target_util=80.0)
    assert s["cpu_limit_rec_sum"] == pytest.approx(1.33, abs=1e-9)
    assert s["cpu_limit_status"] == "EXCEEDS"
