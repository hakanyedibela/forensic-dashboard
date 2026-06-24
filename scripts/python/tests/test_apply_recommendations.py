import io
import json


def _patch_doc(ns="ns1", kind="Deployment", name="web"):
    return {"namespace": ns, "kind": kind, "name": name,
            "summary": ["web: cpu req 500m→900m, lim 1000m→1130m"],
            "patch": {"spec": {"template": {"spec": {"containers": [
                {"name": "web", "resources": {
                    "requests": {"cpu": "900m"}, "limits": {"cpu": "1130m"}}}]}}}}}


def _manifest(patches=None, skipped=None):
    return {"generated_by": "fetch-cluster-usage.py", "target_util": 80.0,
            "skipped": skipped or [], "patches": patches or []}


# --- build_argv -------------------------------------------------------------

def test_build_argv_dry_run_by_default(apprec):
    argv = apprec.build_argv(_patch_doc(), binary="kubectl")
    assert argv[0] == "kubectl"
    assert "patch" in argv and "Deployment" in argv and "web" in argv
    assert argv[argv.index("-n") + 1] == "ns1"
    assert "--type=strategic" in argv
    assert "--dry-run=server" in argv          # dry-run unless execute
    # the --patch body is valid JSON of the strategic-merge patch
    body = json.loads(argv[argv.index("--patch") + 1])
    assert body["spec"]["template"]["spec"]["containers"][0]["name"] == "web"


def test_build_argv_execute_omits_dry_run(apprec):
    argv = apprec.build_argv(_patch_doc(), execute=True)
    assert "--dry-run=server" not in argv


def test_build_argv_oc_and_context(apprec):
    argv = apprec.build_argv(_patch_doc(), binary="oc", context="prod")
    assert argv[0] == "oc"
    assert argv[1:3] == ["--context", "prod"]


# --- apply_manifest ---------------------------------------------------------

def _recording_runner(results):
    """results: list of (rc, stdout, stderr) returned in order; records argvs."""
    calls = []

    def run(argv):
        calls.append(argv)
        return results[len(calls) - 1]
    run.calls = calls
    return run


def test_apply_dry_run_calls_runner_per_patch(apprec):
    runner = _recording_runner([(0, "patched (dry run)", "")])
    out = io.StringIO()
    rc = apprec.apply_manifest(_manifest([_patch_doc()]), runner=runner,
                               execute=False, out=out)
    assert rc == 0
    assert len(runner.calls) == 1
    assert "--dry-run=server" in runner.calls[0]
    assert "DRY-RUN" in out.getvalue()
    assert "OK" in out.getvalue()


def test_apply_not_found_is_skipped_not_fatal(apprec):
    runner = _recording_runner([
        (1, "", 'Error from server (NotFound): deployments.apps "web" not found'),
        (0, "patched", "")])
    out = io.StringIO()
    rc = apprec.apply_manifest(
        _manifest([_patch_doc(name="web"), _patch_doc(name="api")]),
        runner=runner, execute=True, out=out)
    assert rc == 0                              # NotFound is non-fatal
    text = out.getvalue()
    assert "SKIP" in text and "not found" in text
    assert len(runner.calls) == 2              # continued to the second patch


def test_apply_failure_under_execute_exits_nonzero(apprec):
    runner = _recording_runner([(1, "", "admission webhook denied the request")])
    out = io.StringIO()
    rc = apprec.apply_manifest(_manifest([_patch_doc()]), runner=runner,
                               execute=True, out=out)
    assert rc == 1
    assert "FAIL" in out.getvalue()


def test_apply_header_only_manifest_nothing_to_apply(apprec):
    runner = _recording_runner([])
    out = io.StringIO()
    rc = apprec.apply_manifest(_manifest([]), runner=runner, out=out)
    assert rc == 0
    assert "nothing to apply" in out.getvalue()
    assert runner.calls == []


def test_apply_reprints_skipped_block(apprec):
    skipped = [{"namespace": "pid-9-app-prod-01",
                "reasons": ["cpu_limit sum 4200m > quota 4000m"]}]
    out = io.StringIO()
    rc = apprec.apply_manifest(_manifest([], skipped=skipped),
                               runner=_recording_runner([]), out=out)
    assert rc == 0
    text = out.getvalue()
    assert "SKIPPED" in text
    assert "pid-9-app-prod-01" in text
    assert "cpu_limit sum 4200m > quota 4000m" in text


# --- main / manifest loading ------------------------------------------------

def test_main_missing_manifest_returns_2(apprec, tmp_path):
    rc = apprec.main(["--manifest", str(tmp_path / "nope.json")])
    assert rc == 2


def test_load_manifest_roundtrip(apprec, tmp_path):
    path = tmp_path / "recommendations-apply.json"
    path.write_text(json.dumps(_manifest([_patch_doc()])))
    m = apprec.load_manifest(str(path))
    assert m["patches"][0]["name"] == "web"
