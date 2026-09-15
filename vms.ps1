<#
.SYNOPSIS
  vms.ps1 — Windows/macOS PowerShell equivalent of ./vms (the Bash wrapper).

  ./vms does not run on Windows (it is Bash), so Docker-Desktop users had to
  copy .env, generate secrets, and remember the bridge overlay by hand. This
  wrapper does all of it, then passes every argument through to docker compose:

    .\vms.ps1 up -d        .\vms.ps1 ps        .\vms.ps1 logs -f api

  What it does before delegating:
    1. First run (or a .env still holding shipped placeholders while
       DEV_AUTH=false) -> scripts/gen-secrets.ps1 -Fresh creates .env and fills
       INTERNAL_API_KEY / DISCOVERY_SECRET_KEY / KEYCLOAK_ADMIN_PASSWORD. The API
       refuses to boot on the placeholders, so this is what makes `up` succeed.
    2. Always adds docker-compose.bridge.yml — on Docker Desktop host-networking
       is unreachable from the browser (see that file's header).

  Native-Linux servers should keep using ./vms (host networking, GPU/CDI
  preflight, boot-resilience) — this wrapper is the Docker-Desktop path only.

  NOTE: intentionally no param() / [CmdletBinding()] block. This is a pure
  pass-through wrapper, and CmdletBinding would let PowerShell swallow compose
  flags as common-parameter prefixes (-d -> -Debug, -v -> -Verbose), silently
  dropping `up -d`. Raw compose args are forwarded via the automatic $args.
#>
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot        # run from the repo root

$envFile     = Join-Path $PSScriptRoot '.env'
$exampleFile = Join-Path $PSScriptRoot '.env.example'
$genScript   = Join-Path $PSScriptRoot 'scripts/gen-secrets.ps1'

# Shipped placeholders the boot guard rejects (backend/main.py). If any remain
# in .env while real auth is on, `up` would crash-loop — so regenerate.
$placeholders = @(
  'INTERNAL_API_KEY=dev-internal-key-change-for-production',
  'DISCOVERY_SECRET_KEY=dev-discovery-secret-change-for-production',
  'KEYCLOAK_ADMIN_PASSWORD=admin'
)

function Test-NeedsSecrets {
  if (-not (Test-Path -LiteralPath $envFile)) { return $true }   # first run
  $content = Get-Content -LiteralPath $envFile
  # DEV_AUTH=true is a throwaway local box with no auth — placeholders are fine.
  if ($content | Where-Object { $_ -match '^\s*DEV_AUTH\s*=\s*true\s*$' }) { return $false }
  foreach ($p in $placeholders) {
    if ($content | Where-Object { $_ -eq $p }) { return $true }
  }
  return $false
}

if (Test-NeedsSecrets) {
  if (-not (Test-Path -LiteralPath $genScript)) {
    throw "vms: scripts/gen-secrets.ps1 is missing - cannot generate secrets"
  }
  Write-Host 'vms: generating secrets into .env (first run / placeholders present)...'
  # Clear first: $LASTEXITCODE is set only by NATIVE commands and persists across
  # PowerShell calls, so a leftover non-zero from any earlier native command in
  # this session would be misread below as "gen-secrets failed". gen-secrets.ps1
  # ends in an explicit `exit`, so after this the value is genuinely its own.
  $global:LASTEXITCODE = 0
  & $genScript -Fresh -EnvFile $envFile
  if ($LASTEXITCODE -ne 0) {
    # gen-secrets refuses -Fresh only when an existing install has stored camera
    # credentials; it still rotated the other two. Surface and stop.
    throw "vms: secret generation reported a problem (exit $LASTEXITCODE) - see above."
  }
}

# Docker Desktop always uses the bridge overlay.
$files = @('-f', 'docker-compose.yml', '-f', 'docker-compose.bridge.yml')

# OVERLAYS FOLLOW THE PROFILES, so one command does the whole job.
#
# Passing these by hand is where deployments go wrong, and both failures are
# silent. Layer the broker overlay but forget the analytics one and analytics
# detects with nowhere to send, so the index stops growing while every
# container reports healthy. Forget them on `down` and containers are left
# behind holding the network, which breaks the next `up` with
# "network ... not found".
#
# COMPOSE_PROFILES in .env decides which services exist; these decide how they
# are wired, so reading the profiles is the honest way to pick them. Set
# VMS_NO_OVERLAYS=1 to opt out and pass -f yourself.
if (-not $env:VMS_NO_OVERLAYS) {
  $profiles = ''
  if (Test-Path -LiteralPath $envFile) {
    $line = Select-String -LiteralPath $envFile -Pattern '^\s*COMPOSE_PROFILES\s*=' |
            Select-Object -First 1
    if ($line) { $profiles = ($line.Line -split '=', 2)[1].Trim() }
  }
  if ($env:COMPOSE_PROFILES) { $profiles = $env:COMPOSE_PROFILES }
  $active = @($profiles -split ',' | ForEach-Object { $_.Trim() })

  # The broker overlay points the motion service at vms_frames instead of its
  # own decoder. Only meaningful when the frames service is running.
  if ($active -contains 'frames' -and (Test-Path -LiteralPath 'docker-compose.broker.yml')) {
    $files += @('-f', 'docker-compose.broker.yml')
  }
  # The analytics overlay hands analytics a sink URL. WITHOUT IT ANALYTICS
  # PRODUCES NOTHING, which is the outage this guard exists to prevent.
  if ($active -contains 'analytics' -and (Test-Path -LiteralPath 'docker-compose.analytics.yml')) {
    $files += @('-f', 'docker-compose.analytics.yml')
  }
}

# Testability / advanced override: VMS_DOCKER lets tests stub the docker CLI.
$docker = if ($env:VMS_DOCKER) { $env:VMS_DOCKER } else { 'docker' }

& $docker compose @files @args
exit $LASTEXITCODE
