@echo off
rem ---------------------------------------------------------------------------
rem Upload Helm charts to a JFrog Artifactory Helm repository (Windows CMD port
rem of upload-helm-charts.sh). Needs curl.exe (built into Windows 10 1803+).
rem
rem Accepts packaged charts (*.tgz) and unpacked chart directories (containing
rem Chart.yaml — packaged via `helm package` into %TEMP%). Each .tgz is PUT to
rem   %ARTIFACTORY_URL%/artifactory/%ARTIFACTORY_REPO%/[%PREFIX%/]<name>-<ver>.tgz
rem Artifactory reindexes Helm local repos automatically.
rem
rem Already-existing versions are skipped (HEAD probe) unless /force is given.
rem
rem Usage:
rem   set ARTIFACTORY_URL=https://artifactory.example.com
rem   set ARTIFACTORY_REPO=helm-local
rem   set ARTIFACTORY_TOKEN=<identity-token>
rem   upload-helm-charts.cmd [/force] [/dryrun] [/insecure] [/path prefix] charts...
rem
rem Options:
rem   /force       overwrite versions that already exist in the repo
rem   /dryrun      show what would be uploaded, upload nothing
rem   /insecure    skip TLS verification (self-signed Artifactory)
rem   /path X      optional path prefix inside the repo
rem
rem Authentication (first match wins) — environment variables only:
rem   ARTIFACTORY_TOKEN                       -> Authorization: Bearer
rem   ARTIFACTORY_API_KEY                     -> X-JFrog-Art-Api header
rem   ARTIFACTORY_USER + ARTIFACTORY_PASSWORD -> HTTP basic auth
rem ---------------------------------------------------------------------------
setlocal EnableDelayedExpansion

set "FORCE=0"
set "DRYRUN=0"
set "PREFIX="
set "CURLOPTS=--silent --show-error --fail"
set "FAILED=0"
set "NCHARTS=0"
set "TMPPKG="

rem --- parse arguments -------------------------------------------------------
:parse
if "%~1"=="" goto donearg
if /i "%~1"=="/force"    ( set "FORCE=1" & shift & goto parse )
if /i "%~1"=="/dryrun"   ( set "DRYRUN=1" & shift & goto parse )
if /i "%~1"=="/insecure" ( set "CURLOPTS=!CURLOPTS! --insecure" & shift & goto parse )
if /i "%~1"=="/path"     ( set "PREFIX=%~2" & shift & shift & goto parse )
set /a NCHARTS+=1
set "CHART[!NCHARTS!]=%~1"
shift
goto parse
:donearg

if %NCHARTS%==0 ( echo error: no charts given & exit /b 1 )
if not defined ARTIFACTORY_URL  ( echo error: ARTIFACTORY_URL not set  & exit /b 1 )
if not defined ARTIFACTORY_REPO ( echo error: ARTIFACTORY_REPO not set & exit /b 1 )
if "%ARTIFACTORY_URL:~-1%"=="/" set "ARTIFACTORY_URL=%ARTIFACTORY_URL:~0,-1%"

rem --- auth ------------------------------------------------------------------
if defined ARTIFACTORY_TOKEN (
    set "AUTH=--header "Authorization: Bearer %ARTIFACTORY_TOKEN%""
) else if defined ARTIFACTORY_API_KEY (
    set "AUTH=--header "X-JFrog-Art-Api: %ARTIFACTORY_API_KEY%""
) else if defined ARTIFACTORY_USER (
    if not defined ARTIFACTORY_PASSWORD (
        echo error: ARTIFACTORY_USER set but ARTIFACTORY_PASSWORD missing & exit /b 1
    )
    set "AUTH=--user %ARTIFACTORY_USER%:%ARTIFACTORY_PASSWORD%"
) else (
    echo error: no credentials. Set ARTIFACTORY_TOKEN, ARTIFACTORY_API_KEY,
    echo        or ARTIFACTORY_USER + ARTIFACTORY_PASSWORD.
    exit /b 1
)

rem --- upload loop -----------------------------------------------------------
for /l %%i in (1,1,%NCHARTS%) do (
    set "SRC=!CHART[%%i]!"
    call :process "!SRC!"
)

if defined TMPPKG rd /s /q "%TMPPKG%" 2>nul
exit /b %FAILED%

rem ===========================================================================
:process  — one chart argument: package dir if needed, then upload
set "ITEM=%~1"

if exist "%ITEM%\Chart.yaml" (
    where helm >nul 2>nul || ( echo error: helm not on PATH but "%ITEM%" is an unpacked chart & set "FAILED=1" & goto :eof )
    if not defined TMPPKG (
        set "TMPPKG=%TEMP%\helm-upload-%RANDOM%%RANDOM%"
        mkdir "!TMPPKG!"
    )
    echo packaging %ITEM%
    for /f "tokens=* usebackq" %%o in (`helm package "%ITEM%" --destination "!TMPPKG!"`) do set "PKGLINE=%%o"
    rem helm prints: Successfully packaged chart and saved it to: <path>
    set "TGZ=!PKGLINE:*saved it to: =!"
) else if exist "%ITEM%" (
    set "TGZ=%ITEM%"
) else (
    echo error: "%ITEM%" not found & set "FAILED=1" & goto :eof
)

for %%f in ("!TGZ!") do set "BASE=%%~nxf"
if defined PREFIX (
    set "DEST=%ARTIFACTORY_URL%/artifactory/%ARTIFACTORY_REPO%/%PREFIX%/!BASE!"
) else (
    set "DEST=%ARTIFACTORY_URL%/artifactory/%ARTIFACTORY_REPO%/!BASE!"
)

if "%FORCE%"=="0" (
    curl %CURLOPTS% %AUTH% --head --output nul "!DEST!" >nul 2>nul
    if !errorlevel!==0 (
        echo skip   !BASE! ^(already in repo; use /force to overwrite^)
        goto :eof
    )
)

if "%DRYRUN%"=="1" (
    echo would upload !BASE! -^> !DEST!
    goto :eof
)

rem sha256 via certutil: hash is on the 2nd output line.
set "SHA="
for /f "skip=1 tokens=* usebackq" %%h in (`certutil -hashfile "!TGZ!" SHA256`) do (
    if not defined SHA set "SHA=%%h"
)
set "SHA=!SHA: =!"

echo upload !BASE! -^> !DEST!
curl %CURLOPTS% %AUTH% --header "X-Checksum-Sha256: !SHA!" --upload-file "!TGZ!" --output nul "!DEST!"
if !errorlevel!==0 (
    echo ok     !BASE!
) else (
    echo FAILED !BASE! 1>&2
    set "FAILED=1"
)
goto :eof
