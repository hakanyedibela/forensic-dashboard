import io
import json
import sys


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


# --- missing CLI binary: clean exit 2 with a hint, not a traceback ----------

def test_main_missing_binary_returns_2_with_oc_hint(apprec, tmp_path, capsys):
    path = tmp_path / "recommendations-apply.json"
    path.write_text(json.dumps(_manifest([_patch_doc()])))
    rc = apprec.main(["--manifest", str(path), "--kubectl", "no-such-bin-xyz123"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no-such-bin-xyz123" in err      # names the missing binary
    assert "--oc" in err                     # hints to use oc (since not --oc)


def test_main_missing_oc_binary_omits_oc_hint(apprec, tmp_path, capsys,
                                               monkeypatch):
    # With --oc already chosen, the "(or pass --oc ...)" hint is pointless.
    monkeypatch.setattr(apprec.shutil, "which", lambda _b: None)
    path = tmp_path / "recommendations-apply.json"
    path.write_text(json.dumps(_manifest([_patch_doc()])))
    rc = apprec.main(["--manifest", str(path), "--oc"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "'oc'" in err                     # names the oc binary (repr-quoted)
    assert "--oc" not in err                  # no redundant hint


def test_main_header_only_manifest_skips_binary_check(apprec, tmp_path, capsys):
    # No patches -> nothing to run -> a missing binary must NOT be fatal.
    path = tmp_path / "recommendations-apply.json"
    path.write_text(json.dumps(_manifest([])))
    rc = apprec.main(["--manifest", str(path), "--kubectl", "no-such-bin-xyz123"])
    assert rc == 0
    assert "nothing to apply" in capsys.readouterr().out


# --- subprocess_runner (real subprocess, Python 3.6-compatible kwargs) -------

def test_subprocess_runner_captures_output(apprec):
    # Exercises the real subprocess.run call path (the injected-runner tests
    # bypass it). Guards against re-introducing capture_output/text=, which are
    # Python 3.7+ and break on the RHEL 8 / OpenShift system python3 (3.6).
    rc, out, err = apprec.subprocess_runner(
        [sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('e')"])
    assert rc == 0
    assert out.strip() == "hi"          # stdout captured as text
    assert err.strip() == "e"           # stderr captured as text


def test_subprocess_runner_nonzero_exit(apprec):
    rc, out, err = apprec.subprocess_runner([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert rc == 3


# --- text report of applied recommendations (--report) ----------------------

def _applied_lines(text):
    """The report's recommendation lines (everything that isn't a # comment)."""
    return [l for l in text.splitlines() if l and not l.startswith("#")]


def test_apply_manifest_writes_report_one_line_per_applied(apprec, tmp_path):
    runner = _recording_runner([(0, "patched (dry run)", "")])
    report = tmp_path / "apply-report.txt"
    rc = apprec.apply_manifest(_manifest([_patch_doc()]), runner=runner,
                               execute=False, out=io.StringIO(),
                               report=str(report))
    assert rc == 0
    text = report.read_text()
    body = _applied_lines(text)
    assert len(body) == 1                       # one applied recommendation
    assert "ns1/Deployment web" in body[0]      # workload identity
    assert "cpu req 500m→900m" in body[0]       # the old->new change
    assert "DRY-RUN" in text                     # header records the mode
    assert "1 applied" in text


def test_apply_report_multi_container_one_line_each(apprec, tmp_path):
    doc = _patch_doc()
    doc["summary"] = ["web: cpu req 500m→900m, lim 1000m→1130m",
                      "sidecar: mem req 64Mi→128Mi, lim 128Mi→160Mi"]
    runner = _recording_runner([(0, "patched", "")])
    report = tmp_path / "r.txt"
    apprec.apply_manifest(_manifest([doc]), runner=runner, execute=True,
                          out=io.StringIO(), report=str(report))
    body = _applied_lines(report.read_text())
    assert len(body) == 2                        # one line per container rec
    assert any("web:" in l for l in body)
    assert any("sidecar:" in l for l in body)


def test_apply_report_excludes_failed_and_lists_them(apprec, tmp_path):
    runner = _recording_runner([(1, "", "admission webhook denied the request")])
    report = tmp_path / "r.txt"
    apprec.apply_manifest(_manifest([_patch_doc()]), runner=runner,
                          execute=True, out=io.StringIO(), report=str(report))
    text = report.read_text()
    assert _applied_lines(text) == []            # nothing applied
    assert "0 applied, 1 failed" in text
    assert "FAILED" in text and "web" in text


def test_apply_report_notfound_is_recorded_not_applied(apprec, tmp_path):
    runner = _recording_runner([
        (1, "", 'Error from server (NotFound): deployments.apps "web" not found')])
    report = tmp_path / "r.txt"
    apprec.apply_manifest(_manifest([_patch_doc()]), runner=runner,
                          execute=True, out=io.StringIO(), report=str(report))
    text = report.read_text()
    assert _applied_lines(text) == []
    assert "not found" in text


def test_main_passes_report_path_with_quota_skips(apprec, tmp_path):
    # header-only manifest (no patches) so main needs no kubectl binary, but the
    # quota-skipped namespaces still land in the report footer.
    manifest = _manifest([], skipped=[{"namespace": "pid-9-app-prod-01",
                                       "reasons": ["cpu_limit sum 4000m > quota 1000m"]}])
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(manifest))
    rpath = tmp_path / "apply-report.txt"
    rc = apprec.main(["--manifest", str(mpath), "--report", str(rpath)])
    assert rc == 0
    text = rpath.read_text()
    assert "0 applied" in text
    assert "pid-9-app-prod-01" in text
    assert "cpu_limit sum 4000m > quota 1000m" in text
