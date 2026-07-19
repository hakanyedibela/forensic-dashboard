#!/usr/bin/env bash
# Download pinned CLI tools for RHEL (linux/amd64) and optionally embed each
# one into a self-extracting shell script via embed-cli.sh (for transfer
# through text-only channels).
#
# Tools and pinned versions (override via environment):
#   aws     AWS CLI v2        $AWS_CLI_VERSION  (default 2.35.14)  -> installer .zip
#   helm    Helm              $HELM_VERSION     (default 3.21.2)   -> single binary
#   argo    Argo Workflows    $ARGO_VERSION     (default 3.4.4)    -> single binary
#   argocd  Argo CD           $ARGOCD_VERSION   (default 3.4.4)    -> single binary
#   sops    SOPS              $SOPS_VERSION     (default 3.13.2)   -> single binary
#
# By default fetches aws, helm, argo, sops. Pass tool names to select a
# subset (e.g. `fetch-cli-tools.sh helm sops`); pass `argocd` explicitly if
# you want the Argo CD CLI instead of (or in addition to) Argo Workflows.
#
# helm/argo are unpacked from their upstream archives to the bare binary;
# sops is downloaded directly. aws-cli v2 is NOT a single binary — it stays
# as the upstream installer zip (unzip + sudo ./aws/install on the target).
# Where upstream publishes a .sha256 (helm, sops), it is verified.
#
# Usage:
#   fetch-cli-tools.sh [options] [tool ...]
#
# Options:
#   -d, --output-dir DIR   where to put the downloads (default: ./cli-tools)
#   -e, --embed            after download, run embed-cli.sh on each artifact
#   -n, --dry-run          print what would be downloaded, fetch nothing
#   -f, --force            re-download / re-embed even if files exist
#   -h, --help             this help
set -euo pipefail

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

AWS_CLI_VERSION="${AWS_CLI_VERSION:-2.35.14}"
HELM_VERSION="${HELM_VERSION:-3.21.2}"
ARGO_VERSION="${ARGO_VERSION:-3.4.4}"
ARGOCD_VERSION="${ARGOCD_VERSION:-3.4.4}"
SOPS_VERSION="${SOPS_VERSION:-3.13.2}"

OUTDIR="./cli-tools"
EMBED=0
DRY_RUN=0
FORCE=0
TOOLS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -d|--output-dir) OUTDIR="$2"; shift 2 ;;
    -e|--embed)      EMBED=1; shift ;;
    -n|--dry-run)    DRY_RUN=1; shift ;;
    -f|--force)      FORCE=1; shift ;;
    -h|--help)       usage ;;
    -*)              echo "unknown option: $1" >&2; usage 1 ;;
    *)               TOOLS+=("$1"); shift ;;
  esac
done
[ ${#TOOLS[@]} -gt 0 ] || TOOLS=(aws helm argo sops)

sha256_of() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() {  # $1 = url, $2 = dest file
  echo "download $1"
  curl --silent --show-error --fail --location --output "$2" "$1"
}

# verify_sha URL FILE — fetch upstream "<sha256>  <name>" file and compare.
verify_sha() {
  local url="$1" file="$2" want got
  want=$(curl --silent --show-error --fail --location "$url" | cut -d' ' -f1)
  got=$(sha256_of "$file")
  if [ "$want" != "$got" ]; then
    echo "error: sha256 mismatch for $file (want $want, got $got)" >&2
    rm -f "$file"
    return 1
  fi
  echo "sha256 ok $file"
}

skip_existing() {  # $1 = path; returns 0 (=skip) when present and not --force
  [ -e "$1" ] && [ "$FORCE" -eq 0 ]
}

FAILED=0
ARTIFACTS=()

do_aws() {
  local zip="awscli-exe-linux-x86_64-${AWS_CLI_VERSION}.zip"
  local url="https://awscli.amazonaws.com/${zip}"
  local dest="$OUTDIR/$zip"
  if [ "$DRY_RUN" -eq 1 ]; then echo "would fetch $url -> $dest"; return; fi
  if skip_existing "$dest"; then echo "skip   $dest (exists; --force to refetch)"; ARTIFACTS+=("$dest"); return; fi
  fetch "$url" "$dest"
  # AWS publishes no plain .sha256 next to the zip (only GPG .sig) — record
  # our own checksum so the transfer is at least locally verifiable.
  sha256_of "$dest" > "${dest}.sha256.local"
  ARTIFACTS+=("$dest")
}

do_helm() {
  local tgz="helm-v${HELM_VERSION}-linux-amd64.tar.gz"
  local url="https://get.helm.sh/${tgz}"
  local dest="$OUTDIR/helm"
  if [ "$DRY_RUN" -eq 1 ]; then echo "would fetch $url -> $dest (binary unpacked from tar.gz)"; return; fi
  if skip_existing "$dest"; then echo "skip   $dest (exists; --force to refetch)"; ARTIFACTS+=("$dest"); return; fi
  fetch "$url" "$OUTDIR/$tgz"
  verify_sha "${url}.sha256" "$OUTDIR/$tgz"
  tar -xzf "$OUTDIR/$tgz" -C "$OUTDIR" --strip-components=1 linux-amd64/helm
  rm -f "$OUTDIR/$tgz"
  chmod +x "$dest"
  ARTIFACTS+=("$dest")
}

do_argo() {
  local gz="argo-linux-amd64.gz"
  local url="https://github.com/argoproj/argo-workflows/releases/download/v${ARGO_VERSION}/${gz}"
  local dest="$OUTDIR/argo"
  if [ "$DRY_RUN" -eq 1 ]; then echo "would fetch $url -> $dest (gunzipped)"; return; fi
  if skip_existing "$dest"; then echo "skip   $dest (exists; --force to refetch)"; ARTIFACTS+=("$dest"); return; fi
  fetch "$url" "$OUTDIR/$gz"
  gunzip -f "$OUTDIR/$gz"          # leaves $OUTDIR/argo-linux-amd64
  mv "$OUTDIR/argo-linux-amd64" "$dest"
  chmod +x "$dest"
  ARTIFACTS+=("$dest")
}

do_argocd() {
  local url="https://github.com/argoproj/argo-cd/releases/download/v${ARGOCD_VERSION}/argocd-linux-amd64"
  local dest="$OUTDIR/argocd"
  if [ "$DRY_RUN" -eq 1 ]; then echo "would fetch $url -> $dest"; return; fi
  if skip_existing "$dest"; then echo "skip   $dest (exists; --force to refetch)"; ARTIFACTS+=("$dest"); return; fi
  fetch "$url" "$dest"
  chmod +x "$dest"
  ARTIFACTS+=("$dest")
}

do_sops() {
  local bin="sops-v${SOPS_VERSION}.linux.amd64"
  local url="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/${bin}"
  local dest="$OUTDIR/sops"
  if [ "$DRY_RUN" -eq 1 ]; then echo "would fetch $url -> $dest"; return; fi
  if skip_existing "$dest"; then echo "skip   $dest (exists; --force to refetch)"; ARTIFACTS+=("$dest"); return; fi
  fetch "$url" "$OUTDIR/$bin"
  # sops publishes one checksums.txt per release, not per-file .sha256
  local sums="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.checksums.txt"
  local want got
  want=$(curl --silent --show-error --fail --location "$sums" | awk -v f="$bin" '$2 == f {print $1}')
  got=$(sha256_of "$OUTDIR/$bin")
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    echo "error: sha256 mismatch for $bin (want ${want:-<not found in checksums.txt>}, got $got)" >&2
    rm -f "$OUTDIR/$bin"
    return 1
  fi
  echo "sha256 ok $OUTDIR/$bin"
  mv "$OUTDIR/$bin" "$dest"
  chmod +x "$dest"
  ARTIFACTS+=("$dest")
}

[ "$DRY_RUN" -eq 1 ] || mkdir -p "$OUTDIR"
for tool in "${TOOLS[@]}"; do
  case "$tool" in
    aws)    do_aws    || FAILED=1 ;;
    helm)   do_helm   || FAILED=1 ;;
    argo)   do_argo   || FAILED=1 ;;
    argocd) do_argocd || FAILED=1 ;;
    sops)   do_sops   || FAILED=1 ;;
    *) echo "error: unknown tool '$tool' (aws|helm|argo|argocd|sops)" >&2; FAILED=1 ;;
  esac
done

if [ "$EMBED" -eq 1 ] && [ "$DRY_RUN" -eq 0 ] && [ ${#ARTIFACTS[@]} -gt 0 ]; then
  EMBED_OPTS=()
  [ "$FORCE" -eq 1 ] && EMBED_OPTS+=(--force)
  "$SCRIPT_DIR/embed-cli.sh" embed "${EMBED_OPTS[@]+"${EMBED_OPTS[@]}"}" "${ARTIFACTS[@]}" || FAILED=1
fi

exit "$FAILED"
