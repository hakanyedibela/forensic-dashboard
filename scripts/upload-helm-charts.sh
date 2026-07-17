#!/usr/bin/env bash
# Upload Helm charts to a JFrog Artifactory Helm repository.
#
# Accepts any mix of packaged charts (*.tgz) and unpacked chart directories
# (containing Chart.yaml); directories are packaged with `helm package` into a
# temp dir first. Each .tgz is PUT to
#   <ARTIFACTORY_URL>/artifactory/<ARTIFACTORY_REPO>/<name>-<version>.tgz
# Artifactory indexes Helm local repos automatically (index.yaml regenerates
# on upload) — no extra reindex call is needed.
#
# Already-existing versions are skipped (HEAD probe) unless --force is given,
# so re-running after a partial failure only uploads what is missing.
#
# Usage:
#   ARTIFACTORY_URL=https://artifactory.example.com \
#   ARTIFACTORY_REPO=helm-local \
#   ARTIFACTORY_TOKEN=<identity-token> \
#   ./upload-helm-charts.sh [options] <chart.tgz|chart-dir> [more...]
#
# Options:
#   -u, --url URL      Artifactory base URL (or $ARTIFACTORY_URL)
#   -r, --repo NAME    target Helm repo key, e.g. helm-local (or $ARTIFACTORY_REPO)
#   -p, --path PREFIX  optional path prefix inside the repo (default: none)
#   -f, --force        overwrite versions that already exist in the repo
#   -n, --dry-run      show what would be uploaded, upload nothing
#   -k, --insecure     skip TLS verification (self-signed Artifactory)
#   -h, --help         this help
#
# Authentication (first match wins):
#   $ARTIFACTORY_TOKEN                        -> Authorization: Bearer (identity/access token)
#   $ARTIFACTORY_API_KEY                      -> X-JFrog-Art-Api header (legacy API key)
#   $ARTIFACTORY_USER + $ARTIFACTORY_PASSWORD -> HTTP basic auth
#
# Never pass credentials as CLI arguments — they would show up in `ps` output
# and shell history. Environment variables only.
set -euo pipefail

usage() { sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

URL="${ARTIFACTORY_URL:-}"
REPO="${ARTIFACTORY_REPO:-}"
PREFIX=""
FORCE=0
DRY_RUN=0
CURL_OPTS=(--silent --show-error --fail)
ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    -u|--url)      URL="$2"; shift 2 ;;
    -r|--repo)     REPO="$2"; shift 2 ;;
    -p|--path)     PREFIX="$2"; shift 2 ;;
    -f|--force)    FORCE=1; shift ;;
    -n|--dry-run)  DRY_RUN=1; shift ;;
    -k|--insecure) CURL_OPTS+=(--insecure); shift ;;
    -h|--help)     usage ;;
    -*)            echo "unknown option: $1" >&2; usage 1 ;;
    *)             ARGS+=("$1"); shift ;;
  esac
done

[ ${#ARGS[@]} -gt 0 ] || { echo "error: no charts given" >&2; usage 1; }
[ -n "$URL" ]  || { echo "error: Artifactory URL not set (--url / \$ARTIFACTORY_URL)" >&2; exit 1; }
[ -n "$REPO" ] || { echo "error: target repo not set (--repo / \$ARTIFACTORY_REPO)" >&2; exit 1; }
URL="${URL%/}"; PREFIX="${PREFIX%/}"; PREFIX="${PREFIX#/}"

# --- auth -------------------------------------------------------------------
if [ -n "${ARTIFACTORY_TOKEN:-}" ]; then
  AUTH=(--header "Authorization: Bearer ${ARTIFACTORY_TOKEN}")
elif [ -n "${ARTIFACTORY_API_KEY:-}" ]; then
  AUTH=(--header "X-JFrog-Art-Api: ${ARTIFACTORY_API_KEY}")
elif [ -n "${ARTIFACTORY_USER:-}" ] && [ -n "${ARTIFACTORY_PASSWORD:-}" ]; then
  AUTH=(--user "${ARTIFACTORY_USER}:${ARTIFACTORY_PASSWORD}")
else
  echo "error: no credentials. Set ARTIFACTORY_TOKEN, ARTIFACTORY_API_KEY," >&2
  echo "       or ARTIFACTORY_USER + ARTIFACTORY_PASSWORD." >&2
  exit 1
fi

# --- collect .tgz files (package directories on the fly) --------------------
TMPDIR_PKG=""
# if-form, not `[ -n ] &&`: a failing last command in an EXIT trap would
# overwrite the script's exit code under `set -e`.
cleanup() { if [ -n "$TMPDIR_PKG" ]; then rm -rf "$TMPDIR_PKG"; fi; }
trap cleanup EXIT

TGZS=()
for arg in "${ARGS[@]}"; do
  if [ -f "$arg" ] && [[ "$arg" == *.tgz ]]; then
    TGZS+=("$arg")
  elif [ -d "$arg" ] && [ -f "$arg/Chart.yaml" ]; then
    command -v helm >/dev/null || { echo "error: helm not on PATH but '$arg' is an unpacked chart" >&2; exit 1; }
    [ -n "$TMPDIR_PKG" ] || TMPDIR_PKG=$(mktemp -d)
    echo "packaging $arg"
    out=$(helm package "$arg" --destination "$TMPDIR_PKG")
    TGZS+=("${out##*saved it to: }")
  else
    echo "error: '$arg' is neither a .tgz nor a chart directory (Chart.yaml missing)" >&2
    exit 1
  fi
done

# --- upload -----------------------------------------------------------------
sha256_of() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

FAILED=0
for tgz in "${TGZS[@]}"; do
  base=$(basename "$tgz")
  dest="${URL}/artifactory/${REPO}/${PREFIX:+${PREFIX}/}${base}"

  if [ "$FORCE" -eq 0 ] && \
     curl "${CURL_OPTS[@]}" "${AUTH[@]}" --head --output /dev/null "$dest" 2>/dev/null; then
    echo "skip   $base (already in repo; use --force to overwrite)"
    continue
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "would upload $base -> $dest"
    continue
  fi

  echo "upload $base -> $dest"
  # Checksum header lets Artifactory verify the transfer server-side.
  if curl "${CURL_OPTS[@]}" "${AUTH[@]}" \
       --header "X-Checksum-Sha256: $(sha256_of "$tgz")" \
       --upload-file "$tgz" --output /dev/null "$dest"; then
    echo "ok     $base"
  else
    echo "FAILED $base" >&2
    FAILED=1
  fi
done

exit "$FAILED"
