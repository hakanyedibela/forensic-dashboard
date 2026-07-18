# Helm Chart Upload & Embed Scripts

Tooling to get Helm charts into a JFrog Artifactory Helm repository — from Linux/macOS (`.sh`), Windows PowerShell (`.ps1`), or plain Windows CMD (`.cmd`) — plus a helper to convert `.tgz` chart archives into self-extracting shell scripts (useful for transferring binaries through channels that only accept text).

| Script | Platform | Purpose |
|---|---|---|
| [`upload-helm-charts.sh`](#upload-helm-chartssh--linuxmacos) | Linux / macOS (bash) | Upload `.tgz` charts or chart directories to Artifactory |
| [`upload-helm-charts.ps1`](#upload-helm-chartsps1--windows-powershell) | Windows PowerShell 5.1 / PowerShell 7+ | Same, PowerShell port |
| [`upload-helm-charts.cmd`](#upload-helm-chartscmd--windows-cmd) | Windows CMD (needs `curl.exe`, built into Win10 1803+) | Same, CMD port |
| [`embed-tgz.sh`](#embed-tgzsh--tgz--self-extracting-script--back) | Linux / macOS (bash), generated scripts are POSIX `sh` | Embed `.tgz` files into self-extracting scripts and restore them back |

---

## Common concepts (all three upload scripts)

All three upload scripts behave identically:

- **Input**: any mix of packaged charts (`*.tgz`) and unpacked chart directories (containing `Chart.yaml`). Directories are packaged on the fly with `helm package` into a temp dir — `helm` must be on `PATH` only in that case.
- **Target**: each `.tgz` is `PUT` to
  `<URL>/artifactory/<REPO>/[<prefix>/]<name>-<version>.tgz`.
  Artifactory reindexes Helm local repos automatically on upload — no manual reindex call needed.
- **Idempotent**: versions that already exist in the repo are detected via a `HEAD` probe and **skipped** unless forced. Re-running after a partial failure only uploads what is missing.
- **Checksum**: every upload sends an `X-Checksum-Sha256` header so Artifactory verifies the transfer server-side.
- **Exit code**: `0` if everything uploaded/skipped cleanly, `1` if any upload failed (other charts still get attempted).

### Authentication — environment variables only

Credentials are **never** passed as CLI arguments (they would leak into `ps` output and shell history). First match wins:

| Variable(s) | Sent as |
|---|---|
| `ARTIFACTORY_TOKEN` | `Authorization: Bearer <token>` (identity/access token — preferred) |
| `ARTIFACTORY_API_KEY` | `X-JFrog-Art-Api` header (legacy API key) |
| `ARTIFACTORY_USER` + `ARTIFACTORY_PASSWORD` | HTTP basic auth |

Also read from the environment (overridable per call via options):

| Variable | Meaning |
|---|---|
| `ARTIFACTORY_URL` | Base URL, e.g. `https://artifactory.example.com` |
| `ARTIFACTORY_REPO` | Target Helm repo key, e.g. `helm-local` |

---

## `upload-helm-charts.sh` — Linux/macOS

### Usage

```bash
export ARTIFACTORY_URL=https://artifactory.example.com
export ARTIFACTORY_REPO=helm-local
export ARTIFACTORY_TOKEN='<identity-token>'

# dry run first — shows skip/would-upload per chart, uploads nothing
./scripts/upload-helm-charts.sh --dry-run *.tgz

# real upload: the three charts in the repo root
./scripts/upload-helm-charts.sh alertmanager-1.40.2.tgz grafana-10.5.15.tgz thanos-17.3.1.tgz

# mix of .tgz and unpacked chart dirs (dirs get `helm package`d first)
./scripts/upload-helm-charts.sh mychart-1.2.3.tgz helm/mychart/

# into a subfolder of the repo, overwriting existing versions
./scripts/upload-helm-charts.sh --path infra/monitoring --force *.tgz

# self-signed Artifactory
./scripts/upload-helm-charts.sh --insecure *.tgz

# URL/repo per call instead of env
./scripts/upload-helm-charts.sh -u https://artifactory.example.com -r helm-local *.tgz
```

### Options

| Flag | Description |
|---|---|
| `-u, --url URL` | Artifactory base URL (default: `$ARTIFACTORY_URL`) |
| `-r, --repo NAME` | Target Helm repo key (default: `$ARTIFACTORY_REPO`) |
| `-p, --path PREFIX` | Optional path prefix inside the repo |
| `-f, --force` | Overwrite versions that already exist |
| `-n, --dry-run` | Show what would be uploaded, upload nothing |
| `-k, --insecure` | Skip TLS verification (self-signed Artifactory) |
| `-h, --help` | Help |

### Example output

```
skip   alertmanager-1.40.2.tgz (already in repo; use --force to overwrite)
upload grafana-10.5.15.tgz -> https://artifactory.example.com/artifactory/helm-local/grafana-10.5.15.tgz
ok     grafana-10.5.15.tgz
upload thanos-17.3.1.tgz -> https://artifactory.example.com/artifactory/helm-local/thanos-17.3.1.tgz
ok     thanos-17.3.1.tgz
```

---

## `upload-helm-charts.ps1` — Windows PowerShell

Works on Windows PowerShell 5.1 **and** PowerShell 7+. `-Insecure` uses `-SkipCertificateCheck` on 7+ and a `ServicePointManager` callback on 5.1; on 5.1 TLS 1.2 is forced (old defaults may negotiate lower).

### Usage

```powershell
$env:ARTIFACTORY_URL   = "https://artifactory.example.com"
$env:ARTIFACTORY_REPO  = "helm-local"
$env:ARTIFACTORY_TOKEN = "<identity-token>"

# dry run
.\scripts\upload-helm-charts.ps1 -DryRun .\alertmanager-1.40.2.tgz .\grafana-10.5.15.tgz

# real upload, mixed .tgz + unpacked chart dir
.\scripts\upload-helm-charts.ps1 .\mychart-1.2.3.tgz .\helm\mychart\

# subfolder + overwrite + self-signed
.\scripts\upload-helm-charts.ps1 -PathPrefix infra/monitoring -Force -Insecure .\*.tgz
```

> If script execution is blocked: `powershell -ExecutionPolicy Bypass -File .\scripts\upload-helm-charts.ps1 ...`

### Parameters

| Parameter | Description |
|---|---|
| `Charts` (positional) | One or more `*.tgz` files or unpacked chart directories |
| `-Url` | Artifactory base URL (default: `$env:ARTIFACTORY_URL`) |
| `-Repo` | Target Helm repo key (default: `$env:ARTIFACTORY_REPO`) |
| `-PathPrefix` | Optional path prefix inside the repo |
| `-Force` | Overwrite existing versions |
| `-DryRun` | Show what would be uploaded, upload nothing |
| `-Insecure` | Skip TLS verification |

---

## `upload-helm-charts.cmd` — Windows CMD

Needs `curl.exe` (built into Windows 10 1803+ / Server 2019+). SHA-256 is computed with `certutil` (always present).

### Usage

```bat
set ARTIFACTORY_URL=https://artifactory.example.com
set ARTIFACTORY_REPO=helm-local
set ARTIFACTORY_TOKEN=<identity-token>

rem dry run
scripts\upload-helm-charts.cmd /dryrun alertmanager-1.40.2.tgz grafana-10.5.15.tgz

rem real upload with prefix, overwrite, self-signed
scripts\upload-helm-charts.cmd /path infra/monitoring /force /insecure thanos-17.3.1.tgz

rem unpacked chart dir (needs helm on PATH)
scripts\upload-helm-charts.cmd helm\mychart
```

### Options

| Switch | Description |
|---|---|
| `/force` | Overwrite versions that already exist |
| `/dryrun` | Show what would be uploaded, upload nothing |
| `/insecure` | Skip TLS verification |
| `/path X` | Optional path prefix inside the repo |

---

## `embed-tgz.sh` — .tgz ⇄ self-extracting script ⇄ back

Converts binary `.tgz` chart archives into standalone, text-only, self-extracting POSIX shell scripts (base64 payload + embedded sha256), and restores them back. The generated `.tgz.sh` needs **nothing from this repo** to restore itself — any POSIX `sh` with `base64` will do. Typical use: moving chart binaries through mail gateways, ticket systems, or copy-paste-only channels that reject binary attachments.

### Embed

```bash
# one .tgz.sh next to each input
./scripts/embed-tgz.sh embed alertmanager-1.40.2.tgz grafana-10.5.15.tgz thanos-17.3.1.tgz
# -> alertmanager-1.40.2.tgz.sh, grafana-10.5.15.tgz.sh, thanos-17.3.1.tgz.sh

# into a dedicated output dir, overwriting existing files
./scripts/embed-tgz.sh embed -d ./embedded -f *.tgz
```

### Restore — two equivalent ways

```bash
# a) run the generated script directly (self-extracting)
./alertmanager-1.40.2.tgz.sh              # restores alertmanager-1.40.2.tgz into cwd
./alertmanager-1.40.2.tgz.sh other.tgz    # or into an explicit path
FORCE=1 ./alertmanager-1.40.2.tgz.sh      # overwrite an existing file

# b) via the tool (batch-friendly)
./scripts/embed-tgz.sh extract alertmanager-1.40.2.tgz.sh
./scripts/embed-tgz.sh extract -d ./restored *.tgz.sh
./scripts/embed-tgz.sh extract -o /tmp/x.tgz alertmanager-1.40.2.tgz.sh   # single input only
```

Both paths verify the embedded sha256 after writing; on mismatch the output is deleted and the exit code is non-zero. Existing files are never overwritten unless forced (`--force` for the tool, `FORCE=1` env for the generated script).

### Options

| Flag | Mode | Description |
|---|---|---|
| `-d, --output-dir DIR` | both | Write generated/restored files into `DIR` |
| `-o, --output FILE` | extract | Explicit output path (only with a single input) |
| `-f, --force` | both | Overwrite existing output files |
| `-h, --help` | — | Help |

### Round-trip check

```bash
./scripts/embed-tgz.sh embed grafana-10.5.15.tgz
./scripts/embed-tgz.sh extract -o /tmp/check.tgz grafana-10.5.15.tgz.sh
sha256sum grafana-10.5.15.tgz /tmp/check.tgz    # identical
```

---

## Typical end-to-end workflow

```bash
# 1. package/collect charts (already .tgz here)
ls *.tgz
# alertmanager-1.40.2.tgz  grafana-10.5.15.tgz  thanos-17.3.1.tgz

# 2. (optional) text-encode for transfer through a binary-hostile channel
./scripts/embed-tgz.sh embed -d ./embedded *.tgz
# ... transfer ./embedded/*.tgz.sh, then on the other side:
for f in *.tgz.sh; do sh "$f"; done

# 3. verify against Artifactory first
export ARTIFACTORY_URL=https://artifactory.example.com \
       ARTIFACTORY_REPO=helm-local \
       ARTIFACTORY_TOKEN='<token>'
./scripts/upload-helm-charts.sh --dry-run *.tgz

# 4. upload
./scripts/upload-helm-charts.sh *.tgz

# 5. consume from Artifactory
helm repo add myrepo "$ARTIFACTORY_URL/artifactory/api/helm/$ARTIFACTORY_REPO" \
  --username <user> --password <token>
helm repo update
helm search repo myrepo/
```
