# Report: CLI Tools & Helm Chart Packaging — PatchDay Q2 2026

**Date:** 2026-07-22
**Author:** Hakan Yedibela
**Repository:** [github.com/hakanyedibela/forensic-dashboard](https://github.com/hakanyedibela/forensic-dashboard)
**Scope:** Tooling for fetching, packaging, transferring, and uploading third-party software (CLI tools, Helm charts) to restricted target environments (RHEL, air-gapped clients).

---

## 1. CLI tools — versions, sources, artifacts

All tools fetched for **RHEL / linux-amd64** via `scripts/fetch-cli-tools.sh`.

| Tool | Version | Download source | Release notes / changelog | Artifact | Checksum verification |
|---|---|---|---|---|---|
| AWS CLI v2 | latest (rolling, fetched 2026-07-21); pinnable, e.g. `2.35.14` | [awscli.amazonaws.com/awscli-exe-linux-x86_64.zip](https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip) | [github.com/aws/aws-cli/blob/v2/CHANGELOG.rst](https://github.com/aws/aws-cli/blob/v2/CHANGELOG.rst) | installer `.zip` (70 MB) + `.7z` twin (40 MB) | self-recorded `.sha256.local` (AWS ships only GPG `.sig`) |
| Helm | 3.21.2 | [get.helm.sh/helm-v3.21.2-linux-amd64.tar.gz](https://get.helm.sh/helm-v3.21.2-linux-amd64.tar.gz) | [github.com/helm/helm/releases/tag/v3.21.2](https://github.com/helm/helm/releases/tag/v3.21.2) | single binary `helm` (56 MB, ELF x86-64, static) | upstream `.sha256` verified ✔ |
| Argo Workflows CLI | 3.4.4 | [github.com/argoproj/argo-workflows/releases/download/v3.4.4/argo-linux-amd64.gz](https://github.com/argoproj/argo-workflows/releases/download/v3.4.4/argo-linux-amd64.gz) | [github.com/argoproj/argo-workflows/releases/tag/v3.4.4](https://github.com/argoproj/argo-workflows/releases/tag/v3.4.4) | single binary `argo` (99 MB) | — (no upstream per-file checksum) |
| Argo CD CLI (optional, not in default set) | 3.4.4 | [github.com/argoproj/argo-cd/releases/download/v3.4.4/argocd-linux-amd64](https://github.com/argoproj/argo-cd/releases/download/v3.4.4/argocd-linux-amd64) | [github.com/argoproj/argo-cd/releases/tag/v3.4.4](https://github.com/argoproj/argo-cd/releases/tag/v3.4.4) | single binary `argocd` | — |
| SOPS | 3.13.2 | [github.com/getsops/sops/releases/download/v3.13.2/sops-v3.13.2.linux.amd64](https://github.com/getsops/sops/releases/download/v3.13.2/sops-v3.13.2.linux.amd64) | [github.com/getsops/sops/releases/tag/v3.13.2](https://github.com/getsops/sops/releases/tag/v3.13.2) | single binary `sops` (50 MB) | upstream `checksums.txt` verified ✔ |

Known SHA-256 values from the verified fetch runs:

| File | SHA-256 |
|---|---|
| `awscli-exe-linux-x86_64.7z` (latest, fetched 2026-07-21) | `39e29fde031e6122450dcbfc56daa6ada27a3a2ade3f1202c2fd3d7684f3a6e1` |
| `sops` binary v3.13.2 linux-amd64 | `154dfe4cd70554bdd82b98e4cd4acf191d43d01ead6f00a73477aa44c4ac42ef` |

All binaries are statically linked Go executables (aws-cli bundles its own Python); no RPM dependencies beyond glibc — RHEL 8/9 compatible.

---

## 2. Helm charts — packaged and uploaded to JFrog Artifactory

Charts packaged in this repository and uploaded via `scripts/upload-helm-charts.sh` (`PUT` to `<url>/artifactory/<repo>/<chart>-<version>.tgz`, `X-Checksum-Sha256` server-side verification, HEAD-probe idempotency):

| Chart | Chart version | Package | Registry search |
|---|---|---|---|
| alertmanager | 1.40.2 | `alertmanager-1.40.2.tgz` (16 KB) | [artifacthub.io/packages/search?ts_query_web=alertmanager](https://artifacthub.io/packages/search?ts_query_web=alertmanager) |
| grafana | 10.5.15 | `grafana-10.5.15.tgz` (50 KB) | [artifacthub.io/packages/search?ts_query_web=grafana](https://artifacthub.io/packages/search?ts_query_web=grafana) |
| thanos | 17.3.1 | `thanos-17.3.1.tgz` (224 KB) | [artifacthub.io/packages/search?ts_query_web=thanos](https://artifacthub.io/packages/search?ts_query_web=thanos) |

Consumption from Artifactory: `helm repo add <name> <url>/artifactory/api/helm/<repo>`.

---

## 3. Scripts created (all in `scripts/`, documented in the repo)

| Script | Purpose | Documentation |
|---|---|---|
| `upload-helm-charts.sh` / `.ps1` / `.cmd` | Upload Helm charts to JFrog Artifactory (bash / PowerShell 5.1+7 / CMD, identical behavior: dry-run, force, HEAD-skip, checksum header) | [`scripts/README-upload-helm-charts.md`](../scripts/README-upload-helm-charts.md) |
| `embed-tgz.sh` | Embed `.tgz` chart archives into self-extracting POSIX scripts (base64 + sha256) for text-only transfer channels | [`scripts/README-upload-helm-charts.md`](../scripts/README-upload-helm-charts.md) |
| `embed-cli.sh` | Generalized embedder: any binary → self-extracting script; sha256-verified restore, executable bit preserved | [`scripts/README-cli-tools.md`](../scripts/README-cli-tools.md) |
| `fetch-cli-tools.sh` | Download the pinned tool set above (linux-amd64), upstream checksum verification, aws `.zip`+`.7z` twin, `--embed` chaining | [`scripts/README-cli-tools.md`](../scripts/README-cli-tools.md) |

Restore on target needs only POSIX `sh`, `base64`, `sed`, `sha256sum` (RHEL coreutils). AWS CLI install on target: `unzip` + `sudo ./aws/install`; 7z unpack: `dnf install p7zip p7zip-plugins`.

---

## 4. Repository history fix (2026-07-21)

- **Problem:** push to `main` rejected — four local commits contained fetched binaries up to 132 MB (`scripts/embedded/argo.sh`); GitHub hard limit is 100 MB per file ([docs.github.com — large files](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)). `git rm --cached` alone cannot fix this: push transfers every object in the commit range.
- **Fix:** squashed the four unpushed commits into one clean commit (`git reset --soft origin/main` + recommit); verified zero blobs > 1 MB in the push range. Safety branch `backup/embedded-cli-tools` kept until push confirmed.
- **Prevention:** `.gitignore` now excludes `scripts/cli-tools/`, `scripts/embedded/`, `*.zip`, `*.7z`, `*.tgz`, `*.tar.gz`.

---

## 5. Delivery to restricted client (2026-07-21/22)

- Client policy: downloads only via Firefox browser, no CLI tools.
- Method: local `python3 -m http.server` (bound to 127.0.0.1, single-file directory) + [ngrok](https://ngrok.com) HTTPS tunnel with basic auth; ngrok free-tier interstitial acknowledged in browser.
- Delivered: `awscli-exe-linux-x86_64.7z` (40 MB, latest AWS CLI v2), SHA-256 `39e29fde031e6122450dcbfc56daa6ada27a3a2ade3f1202c2fd3d7684f3a6e1`.
- Tunnel and server terminated immediately after confirmed download; credentials invalidated with tunnel teardown.

---

## 6. Source link index

| Source | URL |
|---|---|
| Project repository | https://github.com/hakanyedibela/forensic-dashboard |
| AWS CLI v2 downloads (docs) | https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html |
| AWS CLI changelog | https://github.com/aws/aws-cli/blob/v2/CHANGELOG.rst |
| Helm releases | https://github.com/helm/helm/releases |
| Argo Workflows releases | https://github.com/argoproj/argo-workflows/releases |
| Argo CD releases | https://github.com/argoproj/argo-cd/releases |
| SOPS releases | https://github.com/getsops/sops/releases |
| ArtifactHub (chart registry) | https://artifacthub.io |
| JFrog Artifactory Helm repos (docs) | https://jfrog.com/help/r/jfrog-artifactory-documentation/helm-repositories |
| ngrok | https://ngrok.com |
| GitHub file-size limits | https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github |
