<#
.SYNOPSIS
  Upload Helm charts to a JFrog Artifactory Helm repository (Windows/PowerShell
  port of upload-helm-charts.sh).

.DESCRIPTION
  Accepts any mix of packaged charts (*.tgz) and unpacked chart directories
  (containing Chart.yaml); directories are packaged with `helm package` into a
  temp dir first. Each .tgz is PUT to
    <Url>/artifactory/<Repo>/[<PathPrefix>/]<name>-<version>.tgz
  Artifactory indexes Helm local repos automatically — no reindex call needed.

  Already-existing versions are skipped (HEAD probe) unless -Force is given,
  so re-running after a partial failure only uploads what is missing.

  Authentication (first match wins) — environment variables only, never
  parameters, so credentials stay out of shell history:
    $env:ARTIFACTORY_TOKEN                             -> Authorization: Bearer
    $env:ARTIFACTORY_API_KEY                           -> X-JFrog-Art-Api header
    $env:ARTIFACTORY_USER + $env:ARTIFACTORY_PASSWORD  -> HTTP basic auth

  Works on Windows PowerShell 5.1 and PowerShell 7+ (-Insecure uses
  -SkipCertificateCheck on 7+, a ServicePointManager callback on 5.1).

.PARAMETER Charts
  One or more *.tgz files or unpacked chart directories.

.PARAMETER Url
  Artifactory base URL, e.g. https://artifactory.example.com
  (default: $env:ARTIFACTORY_URL).

.PARAMETER Repo
  Target Helm repo key, e.g. helm-local (default: $env:ARTIFACTORY_REPO).

.PARAMETER PathPrefix
  Optional path prefix inside the repo.

.PARAMETER Force
  Overwrite versions that already exist in the repo.

.PARAMETER DryRun
  Show what would be uploaded, upload nothing.

.PARAMETER Insecure
  Skip TLS verification (self-signed Artifactory).

.EXAMPLE
  $env:ARTIFACTORY_URL   = "https://artifactory.example.com"
  $env:ARTIFACTORY_REPO  = "helm-local"
  $env:ARTIFACTORY_TOKEN = "<token>"
  .\upload-helm-charts.ps1 -DryRun .\mychart-1.2.3.tgz .\helm\mychart\
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0, ValueFromRemainingArguments)]
    [string[]]$Charts,
    [string]$Url = $env:ARTIFACTORY_URL,
    [string]$Repo = $env:ARTIFACTORY_REPO,
    [string]$PathPrefix = "",
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Insecure
)

$ErrorActionPreference = "Stop"

if (-not $Url)  { throw "Artifactory URL not set (-Url / `$env:ARTIFACTORY_URL)" }
if (-not $Repo) { throw "target repo not set (-Repo / `$env:ARTIFACTORY_REPO)" }
$Url = $Url.TrimEnd('/')
$PathPrefix = $PathPrefix.Trim('/')

# --- auth --------------------------------------------------------------------
$headers = @{}
if ($env:ARTIFACTORY_TOKEN) {
    $headers["Authorization"] = "Bearer $($env:ARTIFACTORY_TOKEN)"
}
elseif ($env:ARTIFACTORY_API_KEY) {
    $headers["X-JFrog-Art-Api"] = $env:ARTIFACTORY_API_KEY
}
elseif ($env:ARTIFACTORY_USER -and $env:ARTIFACTORY_PASSWORD) {
    $pair = "$($env:ARTIFACTORY_USER):$($env:ARTIFACTORY_PASSWORD)"
    $headers["Authorization"] = "Basic " +
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
}
else {
    throw ("no credentials. Set ARTIFACTORY_TOKEN, ARTIFACTORY_API_KEY, " +
           "or ARTIFACTORY_USER + ARTIFACTORY_PASSWORD.")
}

# --- TLS handling ------------------------------------------------------------
$isPwsh7 = $PSVersionTable.PSVersion.Major -ge 6
$extra = @{}
if ($Insecure) {
    if ($isPwsh7) {
        $extra["SkipCertificateCheck"] = $true
    }
    else {
        # Windows PowerShell 5.1 has no -SkipCertificateCheck; disable
        # validation process-wide for this run.
        [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
    }
}
if (-not $isPwsh7) {
    # 5.1 can default to old TLS; force TLS 1.2.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

# --- collect .tgz files (package directories on the fly) ---------------------
$tmpPkgDir = $null
$tgzs = @()
foreach ($arg in $Charts) {
    if ((Test-Path $arg -PathType Leaf) -and $arg -like "*.tgz") {
        $tgzs += (Resolve-Path $arg).Path
    }
    elseif ((Test-Path $arg -PathType Container) -and
            (Test-Path (Join-Path $arg "Chart.yaml"))) {
        if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
            throw "helm not on PATH but '$arg' is an unpacked chart"
        }
        if (-not $tmpPkgDir) {
            $tmpPkgDir = Join-Path ([IO.Path]::GetTempPath()) `
                ("helm-upload-" + [IO.Path]::GetRandomFileName())
            New-Item -ItemType Directory -Path $tmpPkgDir | Out-Null
        }
        Write-Host "packaging $arg"
        $out = helm package $arg --destination $tmpPkgDir
        if ($LASTEXITCODE -ne 0) { throw "helm package failed for '$arg'" }
        $tgzs += ($out -replace '^.*saved it to: ', '')
    }
    else {
        throw "'$arg' is neither a .tgz nor a chart directory (Chart.yaml missing)"
    }
}

# --- upload ------------------------------------------------------------------
$failed = 0
try {
    foreach ($tgz in $tgzs) {
        $base = Split-Path $tgz -Leaf
        $dest = if ($PathPrefix) { "$Url/artifactory/$Repo/$PathPrefix/$base" }
                else             { "$Url/artifactory/$Repo/$base" }

        if (-not $Force) {
            $exists = $false
            try {
                Invoke-WebRequest -Uri $dest -Method Head -Headers $headers `
                    -UseBasicParsing @extra | Out-Null
                $exists = $true
            } catch { }   # 404 -> not there yet; upload proceeds
            if ($exists) {
                Write-Host "skip   $base (already in repo; use -Force to overwrite)"
                continue
            }
        }

        if ($DryRun) {
            Write-Host "would upload $base -> $dest"
            continue
        }

        Write-Host "upload $base -> $dest"
        # Checksum header lets Artifactory verify the transfer server-side.
        $sha256 = (Get-FileHash -Algorithm SHA256 -Path $tgz).Hash.ToLower()
        $h = $headers.Clone()
        $h["X-Checksum-Sha256"] = $sha256
        try {
            Invoke-WebRequest -Uri $dest -Method Put -Headers $h -InFile $tgz `
                -UseBasicParsing @extra | Out-Null
            Write-Host "ok     $base"
        }
        catch {
            Write-Error "FAILED $base : $_" -ErrorAction Continue
            $failed = 1
        }
    }
}
finally {
    if ($tmpPkgDir) { Remove-Item -Recurse -Force $tmpPkgDir -ErrorAction SilentlyContinue }
}

exit $failed
