#!/usr/bin/env bash
#
# fetch-cluster-state-loop.sh
#
# Requires bash 4+ (uses mapfile, declare -A, ${var,,}). On macOS the
# system /bin/bash is 3.2; install a newer bash via `brew install bash`
# and ensure it is on PATH ahead of /bin/bash (the shebang above honors
# PATH).
#
# Iterates over every OpenShift project whose name starts with "pid-"
# (same discovery + stage detection as check-bind-resources.sh and
# lean-inspector-loop.sh) and invokes fetch-cluster-state.py once per
# namespace. Each invocation writes its own snapshot / desired-state /
# HPA bindings tree. After the loop, per-namespace CSVs are concatenated
# into combined CSVs and a master overview is produced.
#
# Outputs (under ./reports/state-loop-<timestamp>/):
#   by-stage/<stage>/<ns>/
#       run.log
#       _overview.json
#       _hpa-validation.csv
#       _dimensions.csv
#       by-stage/<stage>/<ns>/
#           snapshot.json
#           hpa-bindings.json
#           desired/<...>.yaml
#   _hpa-validation.csv     -- concatenation of all per-ns HPA CSVs
#   _dimensions.csv         -- concatenation of all per-ns dimension CSVs
#   _master-overview.txt    -- table grouped by stage with HPA-issue counts
#
# Requires: oc (logged in), python3, fetch-cluster-state.py (next to this
# script), PyYAML (`pip install pyyaml`).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/python/fetch-cluster-state.py"

# --report-dir DIR overrides the default of ./reports/state-loop-<ts>/.
# Used by fetch-all-loop.sh to point this script and fetch-oom-loop.sh at
# the same shared report directory so their artifacts coexist.
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
HPA_CSV="${REPORT_DIR}/_hpa-validation.csv"
DIM_CSV="${REPORT_DIR}/_dimensions.csv"
MASTER_FILE="${REPORT_DIR}/_master-overview.txt"
STAGE_KEYWORDS=(ref prod test phase pnext)

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
# Helpers
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

: > "${HPA_CSV}"
: > "${DIM_CSV}"
HPA_HEADER_WRITTEN=0
DIM_HEADER_WRITTEN=0

declare -A STAGE_OF
declare -A HPA_TOTAL_OF
declare -A HPA_BAD_OF
declare -A STATUS_OF

for ns in "${project_array[@]}"; do
    stage="$(detect_stage "${ns}")"
    STAGE_OF["${ns}"]="${stage}"

    ns_out="${REPORT_DIR}/by-stage/${stage}/${ns}"
    mkdir -p "${ns_out}"

    echo "----------------------------------------"
    echo "Processing: ${ns}  [stage=${stage}]"
    echo "----------------------------------------"

    log="${ns_out}/run.log"
    if python3 "${PY_SCRIPT}" \
            --project "${ns}" \
            --output-dir "${ns_out}" \
            --workers 1 \
            > "${log}" 2>&1; then
        STATUS_OF["${ns}"]="ok"
    else
        STATUS_OF["${ns}"]="FAIL"
        echo "  [WARN] python invocation failed for ${ns} (see ${log})"
    fi

    ns_hpa="${ns_out}/_hpa-validation.csv"
    ns_dim="${ns_out}/_dimensions.csv"

    if [[ -s "${ns_hpa}" ]]; then
        if (( HPA_HEADER_WRITTEN == 0 )); then
            head -n1 "${ns_hpa}" > "${HPA_CSV}"
            HPA_HEADER_WRITTEN=1
        fi
        tail -n +2 "${ns_hpa}" >> "${HPA_CSV}"

        # Column 4 is "ok" (True/False); count totals and failures.
        read -r total bad < <(awk -F',' '
            NR > 1 { total++; if ($4 != "True") bad++ }
            END    { printf "%d %d\n", total+0, bad+0 }
        ' "${ns_hpa}")
        HPA_TOTAL_OF["${ns}"]="${total}"
        HPA_BAD_OF["${ns}"]="${bad}"
    else
        HPA_TOTAL_OF["${ns}"]=0
        HPA_BAD_OF["${ns}"]=0
    fi

    if [[ -s "${ns_dim}" ]]; then
        if (( DIM_HEADER_WRITTEN == 0 )); then
            head -n1 "${ns_dim}" > "${DIM_CSV}"
            DIM_HEADER_WRITTEN=1
        fi
        tail -n +2 "${ns_dim}" >> "${DIM_CSV}"
    fi
done

# Master overview
echo "Building master overview..."
{
    echo "============================================================"
    echo "Master overview - fetch-cluster-state loop"
    echo "Generated: $(date)"
    echo "Cluster:   $(oc whoami --show-server 2>/dev/null || echo unknown)"
    echo "Total projects: ${#project_array[@]}"
    echo "============================================================"
    echo ""
    printf '%-8s %-40s %-8s %6s %6s\n' \
        STAGE NAMESPACE STATUS HPAS BAD
    echo "----------------------------------------------------------------------------"
    for ns in "${project_array[@]}"; do
        printf '%-8s %-40s %-8s %6s %6s\n' \
            "${STAGE_OF[$ns]}" \
            "${ns}" \
            "${STATUS_OF[$ns]:-?}" \
            "${HPA_TOTAL_OF[$ns]:-?}" \
            "${HPA_BAD_OF[$ns]:-?}"
    done | sort

    echo ""
    echo "----------------------------------------------------------------------------"
    echo "Per-stage totals:"
    echo "----------------------------------------------------------------------------"
    printf '%-8s %6s %6s %6s\n' STAGE NS HPAS BAD
    for ns in "${project_array[@]}"; do
        echo "${STAGE_OF[$ns]},${HPA_TOTAL_OF[$ns]:-0},${HPA_BAD_OF[$ns]:-0}"
    done | awk -F',' '
        { ns_count[$1]++; hpas[$1]+=$2; bad[$1]+=$3 }
        END {
            for (s in ns_count) {
                printf "%-8s %6d %6d %6d\n", s, ns_count[s], hpas[s], bad[s]
            }
        }' | sort
} > "${MASTER_FILE}"

# Resources overview (cluster-wide rollup: pods, CPU/mem req+lim, PVCs,
# quota usage). Built by a separate Python aggregator so we have proper
# JSON parsing for snapshot.json. Failure is non-fatal -- the rest of the
# report is still useful.
RES_TXT="${REPORT_DIR}/_resources-overview.txt"
RES_CSV="${REPORT_DIR}/_resources-overview.csv"
echo "Building resources overview..."
if python3 "${SCRIPT_DIR}/python/aggregate-resources.py" \
        --input-dir "${REPORT_DIR}" >/dev/null; then
    echo "  ok"
else
    echo "  [WARN] resources overview generation failed" >&2
fi

echo "----------------------------------------"
echo "Done."
echo "Per-namespace:  ${REPORT_DIR}/by-stage/<stage>/<ns>/"
echo "Combined HPAs:  ${HPA_CSV}"
echo "Combined dims:  ${DIM_CSV}"
echo "Master:         ${MASTER_FILE}"
echo "Resources:      ${RES_TXT}"
echo "Resources CSV:  ${RES_CSV}"
echo "----------------------------------------"
