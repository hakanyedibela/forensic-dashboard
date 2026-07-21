# CLI Tool Fetch & Embed Scripts

Same idea as [`embed-tgz.sh`](./README-upload-helm-charts.md#embed-tgzsh--tgz--self-extracting-script--back), generalized to arbitrary CLI binaries: download pinned tool versions for RHEL (linux/amd64), embed each one into a standalone self-extracting shell script (base64 + sha256), transfer through text-only channels, restore on the target — checksum-verified and with the executable bit back in place.

| Script | Purpose |
|---|---|
| [`fetch-cli-tools.sh`](#fetch-cli-toolssh) | Download pinned versions of aws-cli, helm, argo, argocd, sops for linux/amd64; optionally embed them in one go |
| [`embed-cli.sh`](#embed-clish) | Embed any binary file into a self-extracting script and restore it back (generalized `embed-tgz.sh`) |

Pinned versions (defaults, overridable via environment):

| Tool | Version | Env override | Artifact |
|---|---|---|---|
| AWS CLI v2 | **latest** (rolling; pin with e.g. `2.35.14`) | `AWS_CLI_VERSION` | installer zip (`awscli-exe-linux-x86_64[-<v>].zip`) **+ `.7z` twin** (contents recompressed; needs `7zz`/`7z`/`7za` + `unzip` on PATH, otherwise skipped with a warning) |
| Helm | 3.21.2 | `HELM_VERSION` | single binary `helm` (unpacked from upstream tar.gz, sha256-verified) |
| Argo Workflows CLI | 3.4.4 | `ARGO_VERSION` | single binary `argo` (gunzipped) |
| Argo CD CLI | 3.4.4 | `ARGOCD_VERSION` | single binary `argocd` (opt-in — not in the default set) |
| SOPS | 3.13.2 | `SOPS_VERSION` | single binary `sops` (verified against upstream `checksums.txt`) |

> "argo-cli" is ambiguous: the Argo **Workflows** CLI is the binary named `argo`, the Argo **CD** CLI is `argocd`. Both exist at 3.4.4 and both are supported here; the default set contains `argo` (Workflows). Add `argocd` to the tool list if you meant Argo CD.

---

## `fetch-cli-tools.sh`

Downloads the pinned tools into `--output-dir` (default `./cli-tools`), verifies upstream checksums where published (helm `.sha256`, sops `checksums.txt`; for the AWS zip a local `.sha256.local` is recorded since AWS ships only a GPG `.sig`), unpacks archives down to the bare binary (helm from tar.gz, argo from gz), and with `--embed` immediately runs `embed-cli.sh` on every artifact.

Already-present files are skipped unless `--force` — re-running after a partial failure only fetches what is missing.

### Usage

```bash
# everything (aws, helm, argo, sops), see what would happen first
./scripts/fetch-cli-tools.sh --dry-run

# fetch the default set into ./cli-tools/
./scripts/fetch-cli-tools.sh

# fetch AND embed in one go — ready-to-transfer .sh files
./scripts/fetch-cli-tools.sh --embed

# subset only
./scripts/fetch-cli-tools.sh helm sops

# Argo CD CLI instead of / in addition to Argo Workflows
./scripts/fetch-cli-tools.sh argocd
./scripts/fetch-cli-tools.sh argo argocd

# different versions without editing the script
HELM_VERSION=3.22.0 SOPS_VERSION=3.14.0 ./scripts/fetch-cli-tools.sh helm sops

# custom output dir
./scripts/fetch-cli-tools.sh -d /tmp/rhel-tools --embed
```

### Options

| Flag | Description |
|---|---|
| `-d, --output-dir DIR` | Where to put the downloads (default `./cli-tools`) |
| `-e, --embed` | After download, run `embed-cli.sh` on each artifact |
| `-n, --dry-run` | Print what would be downloaded, fetch nothing |
| `-f, --force` | Re-download / re-embed even if files exist |
| `-h, --help` | Help |
| `[tool ...]` | Subset selection: `aws`, `helm`, `argo`, `argocd`, `sops` (default: `aws helm argo sops`) |

### Result (`--embed`)

```
cli-tools/
  awscli-exe-linux-x86_64.zip                  # AWS installer, latest (not a single binary)
  awscli-exe-linux-x86_64.7z                   # same contents, 7z-recompressed (~40M vs ~70M zip)
  awscli-exe-linux-x86_64.zip.sha256.local
  awscli-exe-linux-x86_64.zip.sh               # self-extracting
  helm                                          # ELF x86-64 binary
  helm.sh                                       # self-extracting
  argo
  argo.sh
  sops
  sops.sh
```

Note: base64 inflates size by ~33% (e.g. 56M `helm` → 75M `helm.sh`). The AWS zip is ~65M → ~87M embedded.

---

## `embed-cli.sh`

Generalized `embed-tgz.sh`: accepts **any** file (binary, zip, whatever), and records + restores the executable bit. Same CLI, same generated-script contract (`NAME`/`SHA256` header, `__PAYLOAD_BELOW__` marker, base64 payload); generated scripts additionally carry `EXEC="0|1"`.

### Embed

```bash
./scripts/embed-cli.sh embed cli-tools/helm cli-tools/sops
# -> cli-tools/helm.sh, cli-tools/sops.sh

./scripts/embed-cli.sh embed -d ./embedded -f cli-tools/*
```

### Restore — two equivalent ways

```bash
# a) run the generated script directly (needs only POSIX sh + base64)
sh helm.sh                    # restores ./helm, verifies sha256, chmod +x
sh helm.sh /usr/local/bin/helm-3.21.2   # explicit output path
FORCE=1 sh helm.sh            # overwrite existing

# b) via the tool
./scripts/embed-cli.sh extract helm.sh
./scripts/embed-cli.sh extract -d ./restored *.sh
./scripts/embed-cli.sh extract -o /tmp/x helm.sh    # single input only
```

Checksum is verified after every restore; on mismatch the output is deleted and exit code is non-zero. Existing files are never overwritten unless forced (`--force` for the tool, `FORCE=1` for the generated script).

### Options

| Flag | Mode | Description |
|---|---|---|
| `-d, --output-dir DIR` | both | Write generated/restored files into `DIR` |
| `-o, --output FILE` | extract | Explicit output path (only with a single input) |
| `-f, --force` | both | Overwrite existing output files |
| `-h, --help` | — | Help |

---

## End-to-end: getting the tools onto a RHEL host

On a machine **with** internet:

```bash
./scripts/fetch-cli-tools.sh --embed -d ./cli-tools
# transfer cli-tools/*.sh to the RHEL host (mail, ticket, copy-paste, git — all text-safe)
```

On the RHEL host (**no** internet, no tools from this repo needed):

```bash
# restore everything
for f in *.sh; do sh "$f"; done

# helm / argo / sops are ready-to-run binaries
sudo install -m 0755 helm /usr/local/bin/helm
sudo install -m 0755 argo /usr/local/bin/argo
sudo install -m 0755 sops /usr/local/bin/sops

# aws-cli v2 is an installer zip, not a single binary
unzip -q awscli-exe-linux-x86_64-2.35.14.zip
sudo ./aws/install                     # installs to /usr/local/aws-cli, symlink /usr/local/bin/aws
# upgrade over an existing install:
# sudo ./aws/install --update

# verify
helm version --short      # v3.21.2+...
argo version --short      # argo: v3.4.4
sops --version            # sops 3.13.2
aws --version             # aws-cli/2.35.14 ...
```

RHEL notes:

- All four are statically linked (Go) or self-contained (aws bundles its own Python) — no extra RPM dependencies beyond `glibc`, which every RHEL has. Works on RHEL 8/9.
- `unzip` may be missing on minimal installs: `sudo dnf install -y unzip` (only needed for aws-cli).
- If `/usr/local/bin` is not in a service user's `PATH`, use `/usr/bin` or extend `PATH`.
- Restoring needs only POSIX `sh`, `base64`, `sed`, and `sha256sum` — all present in RHEL's coreutils.
