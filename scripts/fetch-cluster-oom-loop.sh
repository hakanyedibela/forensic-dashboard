#!/usr/bin/env bash
#
# fetch-cluster-oom-loop.sh
#
# Requires bash 4+ (uses mapfile, declare -A, ${var,,}). On macOS the
# system /bin/bash is 3.2; install a newer bash via `brew install bash`
# and ensure it is on PATH ahead of /bin/bash (the shebang above honors
# PATH).
#
# Iterates over every OpenShift project whose name starts with "pid-"
# (same discovery + stage detection as fetch-cluster-state-loop.sh) and
# invokes fetch-cluster-oom.py once per namespace. Per-namespace output is
# saved as JSON (for the aggregator) and as text (for humans). The
# aggregator then produces a cluster-wide OOM overview and a findings CSV.
#
# Writes into ./reports/state-loop-<timestamp>/ by default so the OOM
# artifacts sit next to fetch-cluster-state-loop.sh's outputs. Filename
# prefixes keep the two scripts' files distinct:
#   - master overview:   _oom-overview.txt               (vs _master-overview.txt)
#   - findings CSV:      _oom-findings.csv               (vs _hpa-validation.csv)
#   - per-ns run log:    oom-run.log                     (vs state-loop's run.log)
#
# Pass --report-dir DIR to write into an existing directory (this is what
# fetch-all-loop.sh does to combine state+oom into one folder).
#
# Outputs are only created when at least one OOMKilled container is found
# somewhere in the cluster. Namespaces without OOMs produce no files; if
# the entire cluster has zero OOMs the script writes nothing and (when it
# created the report dir itself) removes the empty dir on exit.
#
# Outputs (under ${REPORT_DIR}) when OOMs are found:
#   by-stage/<stage>/<ns>/                only for namespaces with OOMs
#       oom-run.log     stdout/stderr of the python invocations
#       report.json     full structured report (fetch-cluster-oom.py --json)
#       report.txt      human-readable per-namespace report
#   _oom-overview.txt   per-namespace + per-stage rollup (STATUS, OOMS, PATTERNS)
#   _oom-findings.csv   one row per OOMKilled container across the cluster
#   _oom-status.txt     bash-side fallback overview
#
# Environment variables (all optional):
#   OOM_PROMETHEUS_URL   pass through to --prometheus-url
#   OOM_PROMETHEUS_PORT  pass through to --prometheus-port
#   OOM_TOKEN            pass through to --token
#   OOM_INSECURE=1       pass through to --insecure
# When none of these are set, the loop passes --no-prometheus so the run
# stays fast and self-contained. Enable Prometheus when you want metric-
# based verdicts (A/B/C/D/E patterns).
#
# Requires: oc (logged in), python3, fetch-cluster-oom.py (under scripts/python/).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/python/fetch-cluster-oom.py"
AGGREGATOR="${SCRIPT_DIR}/python/aggregate-oom.py"

# --report-dir DIR overrides the default of ./reports/state-loop-<ts>/.
# When fetch-all-loop.sh invokes this script it passes the same directory
# fetch-cluster-state-loop.sh wrote into so the two reports merge.
REPORT_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --report-dir)
            REPORT_DIR="${2:-}"
            if [[ -z "${REPORT_DIR}" ]]; then
                echo "error: --report-dir requires an argument" >&2
                exit 1
            fi
            shift 2
            ;;
        -h|--help)
            echo "Usage: $(basename "$0") [--report-dir DIR]"
            echo "  --report-dir DIR  Write outputs to DIR instead of ./reports/state-loop-<ts>/."
            echo ""
            echo "Environment:"
            echo "  OOM_PROMETHEUS_URL, OOM_PROMETHEUS_PORT, OOM_TOKEN, OOM_INSECURE=1"
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            echo "Usage: $(basename "$0") [--report-dir DIR]" >&2
            exit 1
            ;;
    esac
done

if [[ -z "${REPORT_DIR}" ]]; then
    TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    REPORT_DIR="./reports/state-loop-${TIMESTAMP}"
fi
OVERVIEW="${REPORT_DIR}/_oom-overview.txt"
FINDINGS_CSV="${REPORT_DIR}/_oom-findings.csv"
STAGE_KEYWORDS=(ref prod test phase pnext)

# Remember whether the report dir already existed so we can rmdir it on
# exit if we end up writing nothing (no OOMs found). Touching an existing
# dir is fine; we only ever rmdir something we created ourselves.
DIR_PREEXISTED=0
if [[ -d "${REPORT_DIR}" ]]; then
    DIR_PREEXISTED=1
fi
mkdir -p "${REPORT_DIR}"

if ! command -v oc >/dev/null 2>&1; then
    echo "error: oc is required" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required" >&2
    exit 1
fi
if [[ ! -f "${PY_SCRIPT}" ]]; then
    echo "error: ${PY_SCRIPT} not found" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Build the Prometheus argument list once.
# ---------------------------------------------------------------------------
PROM_ARGS=()
if [[ -n "${OOM_PROMETHEUS_URL:-}" ]]; then
    PROM_ARGS+=(--prometheus-url "${OOM_PROMETHEUS_URL}")
elif [[ -n "${OOM_PROMETHEUS_PORT:-}" ]]; then
    PROM_ARGS+=(--prometheus-port "${OOM_PROMETHEUS_PORT}")
else
    PROM_ARGS+=(--no-prometheus)
fi
if [[ -n "${OOM_TOKEN:-}" ]]; then
    PROM_ARGS+=(--token "${OOM_TOKEN}")
fi
if [[ "${OOM_INSECURE:-0}" == "1" ]]; then
    PROM_ARGS+=(--insecure)
fi

# ---------------------------------------------------------------------------
# Helpers (same stage detection as fetch-cluster-state-loop.sh)
# ---------------------------------------------------------------------------

detect_stage() {
    local ns="$1"
    local lower="${ns,,}"
    IFS='-' read -r -a parts <<< "${lower}"
    if [[ ${#parts[@]} -gt 3 ]]; then
        local seg3="${parts[3]}"
        for k in "${STAGE_KEYWORDS[@]}"; do
            [[ "${seg3}" == "${k}" ]] && { echo "${k}"; return; }
        done
    fi
    for p in "${parts[@]}"; do
        for k in "${STAGE_KEYWORDS[@]}"; do
            [[ "${p}" == "${k}" ]] && { echo "${k}"; return; }
        done
    done
    echo "other"
}

count_ooms_in_json() {
    # Robust to invalid / empty JSON: returns 0 then.
    python3 - "$1" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    print(len(data) if isinstance(data, list) else 0)
except Exception:
    print(0)
PY
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

echo "Discovering pid-* projects..."
projects=$(oc get projects -o name 2>/dev/null | awk -F'/' '{print $2}' | grep -E '^pid-' || true)

if [[ -z "${projects}" ]]; then
    echo "No matching pid-* projects found."
    exit 0
fi

mapfile -t project_array <<< "${projects}"
echo "Found ${#project_array[@]} pid-* projects."
echo "Prometheus args: ${PROM_ARGS[*]}"

declare -A STAGE_OF
declare -A OOM_COUNT_OF
declare -A STATUS_OF
TOTAL_OOMS=0
NS_WITH_OOMS=0

# Per-namespace pass. We probe with --json into a temp file first; if the
# namespace has zero OOMKilled containers we discard the temp files and
# never create a per-namespace directory. Only namespaces with at least
# one OOM produce on-disk artifacts.
for ns in "${project_array[@]}"; do
    stage="$(detect_stage "${ns}")"
    STAGE_OF["${ns}"]="${stage}"

    echo "----------------------------------------"
    echo "Processing: ${ns}  [stage=${stage}]"
    echo "----------------------------------------"

    tmp_json="$(mktemp)"
    tmp_log="$(mktemp)"
    # Track ourselves so we can clean up if we bail out early.
    trap 'rm -f "${tmp_json}" "${tmp_log}" 2>/dev/null || true' EXIT

    json_ok=0
    if python3 "${PY_SCRIPT}" -n "${ns}" --json "${PROM_ARGS[@]}" \
            > "${tmp_json}" 2> "${tmp_log}"; then
        json_ok=1
    fi

    if (( json_ok == 1 )); then
        STATUS_OF["${ns}"]="ok"
        count="$(count_ooms_in_json "${tmp_json}")"
        OOM_COUNT_OF["${ns}"]="${count}"
    else
        STATUS_OF["${ns}"]="FAIL"
        count=0
        OOM_COUNT_OF["${ns}"]="?"
        echo "  [WARN] JSON pass failed for ${ns}; see oom-run.log if created"
    fi

    if (( count == 0 )) && [[ "${STATUS_OF[$ns]}" == "ok" ]]; then
        echo "  no OOMKilled containers — skipping per-namespace output"
        rm -f "${tmp_json}" "${tmp_log}"
        continue
    fi

    # Either we found OOMs, or the JSON pass failed and we want the log
    # preserved for debugging. Materialize the per-namespace directory.
    ns_out="${REPORT_DIR}/by-stage/${stage}/${ns}"
    mkdir -p "${ns_out}"
    mv "${tmp_json}" "${ns_out}/report.json"
    mv "${tmp_log}"  "${ns_out}/oom-run.log"

    # Text pass — only when we actually have something to report on, so we
    # don't pay for the second cluster fetch when the JSON came back empty.
    if (( count > 0 )); then
        python3 "${PY_SCRIPT}" -n "${ns}" "${PROM_ARGS[@]}" \
            > "${ns_out}/report.txt" 2>> "${ns_out}/oom-run.log" \
            || echo "  [WARN] text pass failed for ${ns} (see ${ns_out}/oom-run.log)"
        NS_WITH_OOMS=$((NS_WITH_OOMS + 1))
        TOTAL_OOMS=$((TOTAL_OOMS + count))
        echo "  ${count} OOMKilled container(s) — wrote ${ns_out}/"
    fi
done
trap - EXIT

# ---------------------------------------------------------------------------
# Skip the aggregation step entirely if there is nothing to aggregate.
# Cleans up the report directory iff we created it ourselves — never
# touches a directory that was passed in via --report-dir and already
# contained other files (that is the fetch-all-loop.sh case).
# ---------------------------------------------------------------------------
if (( TOTAL_OOMS == 0 )); then
    echo "----------------------------------------"
    echo "No OOMKilled containers found across ${#project_array[@]} project(s)."
    echo "Skipping aggregate files; no OOM report generated."
    if (( DIR_PREEXISTED == 0 )); then
        # Best-effort: remove the empty directory we created. rmdir is
        # safe -- it refuses to delete non-empty dirs, so if state-loop or
        # anyone else has put files there it stays put.
        rmdir "${REPORT_DIR}" 2>/dev/null || true
        rmdir "$(dirname "${REPORT_DIR}")" 2>/dev/null || true
    fi
    echo "----------------------------------------"
    exit 0
fi

# ---------------------------------------------------------------------------
# Aggregation (per-ns + per-stage rollups and findings CSV)
# ---------------------------------------------------------------------------
echo "Building OOM overview..."
if [[ -f "${AGGREGATOR}" ]]; then
    if python3 "${AGGREGATOR}" --input-dir "${REPORT_DIR}" >/dev/null; then
        echo "  ok"
    else
        echo "  [WARN] aggregator failed; falling back to minimal overview" >&2
    fi
else
    echo "  [WARN] ${AGGREGATOR} not found; writing minimal overview only" >&2
fi

# Always emit a tiny status overview from the bash side too, so the loop
# stays useful even if the Python aggregator is missing or fails. Named
# with the _oom- prefix to avoid colliding with the state-loop's files.
MINI_OVERVIEW="${REPORT_DIR}/_oom-status.txt"
{
    echo "============================================================"
    echo "OOM loop status"
    echo "Generated: $(date)"
    echo "Cluster:   $(oc whoami --show-server 2>/dev/null || echo unknown)"
    echo "Total projects: ${#project_array[@]}"
    echo "Namespaces with OOMs: ${NS_WITH_OOMS}"
    echo "Total OOMKilled containers: ${TOTAL_OOMS}"
    echo "Prometheus args: ${PROM_ARGS[*]}"
    echo "============================================================"
    echo ""
    printf '%-8s %-40s %-8s %6s\n' STAGE NAMESPACE STATUS OOMS
    echo "------------------------------------------------------------------"
    for ns in "${project_array[@]}"; do
        printf '%-8s %-40s %-8s %6s\n' \
            "${STAGE_OF[$ns]}" \
            "${ns}" \
            "${STATUS_OF[$ns]:-?}" \
            "${OOM_COUNT_OF[$ns]:-?}"
    done | sort
} > "${MINI_OVERVIEW}"

echo "----------------------------------------"
echo "Done. ${TOTAL_OOMS} OOMKilled container(s) in ${NS_WITH_OOMS} namespace(s)."
echo "Per-namespace:  ${REPORT_DIR}/by-stage/<stage>/<ns>/"
echo "Status:         ${MINI_OVERVIEW}"
[[ -f "${OVERVIEW}" ]]     && echo "OOM overview:   ${OVERVIEW}"
[[ -f "${FINDINGS_CSV}" ]] && echo "Findings CSV:   ${FINDINGS_CSV}"
echo "----------------------------------------"
