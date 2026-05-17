#!/usr/bin/env bash
#
# fetch-all-loop.sh
#
# One-shot wrapper that runs both forensic loops into the same report
# directory:
#
#   1. fetch-cluster-state-loop.sh  — cluster state + HPA validation +
#                                     resource footprint
#   2. fetch-cluster-oom-loop.sh    — OOM root-cause per namespace
#
# Layout (under ./reports/state-loop-<timestamp>/):
#
#   by-stage/<stage>/<ns>/
#     run.log              (state-loop per-ns log)
#     oom-run.log          (oom-loop per-ns log)
#     _hpa-validation.csv  (state, per ns)
#     _dimensions.csv      (state, per ns)
#     _overview.json       (state, per ns)
#     _cluster-state.xlsx  (state, per ns, if openpyxl is installed)
#     report.json          (oom, per ns; structured)
#     report.txt           (oom, per ns; human-readable)
#     by-stage/<stage>/<ns>/         (state-loop's nested per-ns dir)
#       snapshot.json
#       hpa-bindings.json
#       desired/...
#
#   _master-overview.txt         (state: HPA-centric per-ns + per-stage)
#   _resources-overview.txt/.csv (state: capacity per-ns + per-stage)
#   _hpa-validation.csv          (state: aggregated across cluster)
#   _dimensions.csv              (state: aggregated across cluster)
#   _oom-overview.txt            (oom:   per-ns + per-stage rollup)
#   _oom-findings.csv            (oom:   one row per OOMKilled container)
#   _oom-status.txt              (oom:   bash-side fallback overview)
#
# Standalone usage of each loop still works -- they default to creating
# their own ./reports/state-loop-<ts>/ directory when called without
# --report-dir.
#
# Requires bash 4+ (uses ${var,,} and declare -A inside the child scripts).
# On macOS install via `brew install bash` and make sure it precedes
# /bin/bash on PATH (the shebang above honors PATH).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
REPORT_DIR="./reports/state-loop-${TIMESTAMP}"
mkdir -p "${REPORT_DIR}"

STATE_LOOP="${SCRIPT_DIR}/fetch-cluster-state-loop.sh"
OOM_LOOP="${SCRIPT_DIR}/fetch-cluster-oom-loop.sh"

for s in "${STATE_LOOP}" "${OOM_LOOP}"; do
    if [[ ! -x "${s}" ]]; then
        echo "error: required script not found or not executable: ${s}" >&2
        exit 1
    fi
done

echo "########################################################"
echo "# Combined forensic report -> ${REPORT_DIR}"
echo "########################################################"
echo

echo "########################################################"
echo "# Phase 1/2: cluster state, HPA validation, resources"
echo "########################################################"
"${STATE_LOOP}" --report-dir "${REPORT_DIR}"

echo
echo "########################################################"
echo "# Phase 2/2: OOM root-cause"
echo "########################################################"
"${OOM_LOOP}" --report-dir "${REPORT_DIR}"

echo
echo "########################################################"
echo "# Done. Combined report at: ${REPORT_DIR}"
echo "#"
# maybe LABEL PATH
#
# Hilfs-Funktion fuer das Schluss-Echo: gibt eine formatierte Zeile mit
# LABEL und PATH aus, aber nur wenn die Datei unter PATH tatsaechlich
# existiert. So tauchen im Abschluss-Block nur Pfade auf, die wirklich
# geschrieben wurden -- z. B. fehlt _oom-overview.txt, wenn der Cluster
# keine OOMKills hatte.
#
# Parameter: $1 = Label-Text, $2 = absoluter/relativer Pfad
# Ausgabe:   eine "# <label>  <path>"-Zeile auf stdout, oder nichts.
maybe() {
    local label="$1" path="$2"
    if [[ -f "${path}" ]]; then
        printf '#   %-26s %s\n' "${label}" "${path}"
    fi
}
maybe "At-a-glance HPA health:"  "${REPORT_DIR}/_master-overview.txt"
maybe "At-a-glance resources:"   "${REPORT_DIR}/_resources-overview.txt"
maybe "At-a-glance OOMs:"        "${REPORT_DIR}/_oom-overview.txt"
echo "#"
maybe "All HPAs (CSV):"          "${REPORT_DIR}/_hpa-validation.csv"
maybe "All workloads (CSV):"     "${REPORT_DIR}/_dimensions.csv"
maybe "All OOM findings (CSV):"  "${REPORT_DIR}/_oom-findings.csv"
if [[ ! -f "${REPORT_DIR}/_oom-overview.txt" ]]; then
    echo "#"
    echo "#   (No OOMKilled containers found — OOM report skipped.)"
fi
echo "########################################################"
