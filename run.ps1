<#
LLM gateway runner (Windows PowerShell 5.1+ / PowerShell 7). Same commands as run.sh.

  .\run.ps1              build + run end to end: validate config, fill .env, pull images,
                         start, wait until healthy, provision teams / keys / Langfuse projects
  .\run.ps1 test         end-to-end self-test of the running stack (temporary keys only)
  .\run.ps1 provision    re-apply config/teams.yaml + hook setup (no restart)
  .\run.ps1 render       only validate config/ and regenerate .build/
  .\run.ps1 status       container states
  .\run.ps1 logs [svc]   follow logs (e.g. litellm)
  .\run.ps1 compose ...  any docker compose command with the generated env, e.g. compose ps
  .\run.ps1 pull         pull the images of config/stack.yaml
  .\run.ps1 down         stop containers, KEEP data   (delete data: .\run.ps1 compose down -v)

Edit: config/*.yaml (settings, ports, models, teams, hooks) and .env (credentials).
If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
param(
    [ValidateSet('up', 'test', 'provision', 'render', 'status', 'logs', 'compose', 'pull', 'down')]
    [string]$Command = 'up',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

# Native tools write progress to stderr; with 'Stop' Windows PowerShell 5.1 would treat that as
# a failure. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot
$Root = $PSScriptRoot
$ComposeEnv = Join-Path $Root '.build\compose.env'

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Stop-Run([string]$Text) { Write-Host "ERROR: $Text" -ForegroundColor Red; exit 1 }

function Get-EnvValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $ComposeEnv)) { return '' }
    $line = Get-Content -LiteralPath $ComposeEnv | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line.Substring($Key.Length + 1)).Trim("'")
}

function Get-StackValue([string]$Section, [string]$Key) {
    # two-level keys of config/stack.yaml only (enough to find the image the renderer runs in)
    $cur = ''
    foreach ($raw in Get-Content -LiteralPath 'config\stack.yaml') {
        $line = ($raw -replace '(^|\s)#.*$', '').TrimEnd()
        if (-not $line.Trim()) { continue }
        if ($line -match '^([A-Za-z0-9_]+):') { $cur = $Matches[1]; continue }
        if ($cur -eq $Section -and $line -match "^\s+${Key}:\s*(.+)$") { return $Matches[1].Trim().Trim('"', "'") }
    }
    return ''
}

function Invoke-Compose {
    if (-not (Test-Path -LiteralPath $ComposeEnv)) { Stop-Run 'no .build/compose.env yet - run .\run.ps1 first' }
    docker compose --env-file $ComposeEnv -f (Join-Path $Root 'compose.yaml') @args
}

function Test-Image([string]$Image) { docker image inspect $Image *> $null; return ($LASTEXITCODE -eq 0) }

function Initialize-Image([string]$Image) {
    $need = ($Pull -eq 'always') -or (($Pull -eq 'missing') -and -not (Test-Image $Image))
    if ($need) {
        Write-Host "  pull  $Image"
        docker pull -q $Image | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-Run "could not pull $Image (registry reachable? tag exists?)" }
    } elseif (-not (Test-Image $Image)) {
        Stop-Run "image $Image is not on this machine and images.pull is 'never'"
    }
}

function Invoke-Render([string[]]$RenderArgs) {
    docker run --rm --pull never --user 0:0 --entrypoint python -v "${Root}:/w" -w /w $LiteImage -I tools/render.py @RenderArgs
}

# ------------------------------------------------------------------ preflight
docker info *> $null
if ($LASTEXITCODE -ne 0) { Stop-Run 'Docker is not running (start Docker Desktop).' }
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Stop-Run 'docker compose v2 is required.' }
New-Item -ItemType Directory -Force -Path (Join-Path $Root '.state') | Out-Null

switch ($Command) {
    'status'    { Invoke-Compose ps; exit $LASTEXITCODE }
    'logs'      { Invoke-Compose logs -f --tail 100 @Rest; exit $LASTEXITCODE }
    'compose'   { Invoke-Compose @Rest; exit $LASTEXITCODE }
    'down'      { Invoke-Compose down; Write-Host 'Stopped. Data kept in the volumes.'; exit $LASTEXITCODE }
    'provision' { Write-Step 'provision'; Invoke-Compose run --rm -T provision; exit $LASTEXITCODE }
    'test'      { Write-Step 'self-test'; Invoke-Compose run --rm -T selftest; exit $LASTEXITCODE }
}

$LiteImage = Get-StackValue 'images' 'litellm'
$Pull = Get-StackValue 'images' 'pull'
if (-not $Pull) { $Pull = 'missing' }
if (-not $LiteImage) { Stop-Run 'config/stack.yaml: images.litellm is missing' }

# ------------------------------------------------------------------ 1 config
Write-Step '1/6 config (config/*.yaml + .env -> .build/)'
Initialize-Image $LiteImage
Invoke-Render @()
if ($LASTEXITCODE -ne 0) { Stop-Run 'fix the above and run again' }
if ($Command -eq 'render') { exit 0 }

# ------------------------------------------------------------------ 2 images
Write-Step "2/6 images (images.pull: $Pull)"
$Images = @(Invoke-Render @('--images') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($img in $Images) { Initialize-Image $img }
$lock = @("# image digests in use - written by run.ps1 $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))")
foreach ($img in $Images) {
    $digest = docker image inspect --format '{{index .RepoDigests 0}}' $img 2> $null
    if (-not $digest) { $digest = '(local build)' }
    $lock += "$img  $digest"
}
[System.IO.File]::WriteAllLines((Join-Path $Root 'images.lock'), [string[]]$lock)
Write-Host "  $($Images.Count) images ready (digests in images.lock)"
if ($Command -eq 'pull') { exit 0 }

# Corporate TLS inspection: rebuild certs/ca-bundle.pem from the LiteLLM image in use (certs/README.md)
$corpRoot = Join-Path $Root 'certs\corp-root-ca.pem'
if ((Test-Path -LiteralPath $corpRoot) -and ((Get-Item -LiteralPath $corpRoot).Length -gt 0)) {
    docker run --rm --pull never --user 0:0 --entrypoint python -v "${Root}:/w" -w /w $LiteImage -I tools/build_ca_bundle.py
    if ($LASTEXITCODE -ne 0) { Stop-Run 'could not build certs/ca-bundle.pem' }
}

# ------------------------------------------------------------------ 3 up
Write-Step '3/6 start containers'
Invoke-Compose up -d --remove-orphans
if ($LASTEXITCODE -ne 0) { Stop-Run 'docker compose up failed - see above (.\run.ps1 logs <service>)' }
# LiteLLM reads its config + hooks only at start: restart so edits of config/ always apply.
Invoke-Compose restart litellm | Out-Null

# ------------------------------------------------------------------ 4 health
Write-Step '4/6 wait until healthy'
$HostName = Get-EnvValue 'BIND_ADDRESS'
if ($HostName -eq '0.0.0.0') { $HostName = '127.0.0.1' }

function Test-Endpoint([string]$Name, [string]$Url, [string]$Svc) {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) { Write-Host "  ok    $Name"; return $true }
        } catch { }
        Start-Sleep -Seconds 5
    }
    Write-Host "  FAIL  $Name did not answer at $Url (.\run.ps1 logs $Svc)" -ForegroundColor Red
    return $false
}

$ok = $true
$last = [int](Get-EnvValue 'PORT_LITELLM_LAST')
for ($p = [int](Get-EnvValue 'PORT_LITELLM'); $p -le $last; $p++) {
    if (-not (Test-Endpoint "LiteLLM :$p" "http://${HostName}:$p/health/liveliness" 'litellm')) { $ok = $false }
}
if (-not (Test-Endpoint 'Langfuse'   "http://${HostName}:$(Get-EnvValue 'PORT_LANGFUSE')/api/public/health" 'langfuse-web')) { $ok = $false }
if (-not (Test-Endpoint 'Grafana'    "http://${HostName}:$(Get-EnvValue 'PORT_GRAFANA')/api/health" 'grafana'))              { $ok = $false }
if (-not (Test-Endpoint 'Prometheus' "http://${HostName}:$(Get-EnvValue 'PORT_PROMETHEUS')/-/ready" 'prometheus'))           { $ok = $false }
$code = Invoke-Compose ps -a --format '{{.ExitCode}}' db-setup 2> $null | Select-Object -First 1
if ("$code".Trim() -eq '0') { Write-Host '  ok    db-setup' }
else { Write-Host "  FAIL  db-setup (exit $code): .\run.ps1 logs db-setup" -ForegroundColor Red; $ok = $false }

# ------------------------------------------------------------------ 5 provision
Write-Step '5/6 provision (teams, keys, Langfuse projects, metrics key)'
if ($ok) {
    Invoke-Compose run --rm -T provision
    if ($LASTEXITCODE -ne 0) { $ok = $false }
} else { Write-Host '  skipped (stack not healthy)' }

# ------------------------------------------------------------------ 6 ready
Write-Step '6/6 ready'
Invoke-Compose ps --format '  {{.Service}}: {{.State}} {{.Health}}' | Sort-Object -Unique
$lit = Get-EnvValue 'LITELLM_PUBLIC_URL'
Write-Host ''
Write-Host "  LiteLLM API  $lit          Bearer KEY_<TEAM>_<KEY> (apps) | LITELLM_MASTER_KEY (admin)"
Write-Host "  LiteLLM UI   $lit/ui       LITELLM_UI_USERNAME / LITELLM_UI_PASSWORD"
Write-Host "  Langfuse     $(Get-EnvValue 'LANGFUSE_PUBLIC_URL')          LANGFUSE_ADMIN_EMAIL / LANGFUSE_ADMIN_PASSWORD"
Write-Host "  Grafana      $(Get-EnvValue 'GRAFANA_PUBLIC_URL')          GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD"
Write-Host "  Prometheus   $(Get-EnvValue 'PROMETHEUS_PUBLIC_URL')"
Write-Host '  Credentials: .env (this folder).  Self-test: .\run.ps1 test'
if (-not $ok) { Write-Host ''; Stop-Run 'some checks failed - see above' }
