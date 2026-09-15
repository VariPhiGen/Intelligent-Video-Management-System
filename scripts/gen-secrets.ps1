<#
.SYNOPSIS
  gen-secrets.ps1 — Windows/macOS PowerShell port of scripts/gen-secrets.sh.

  Generates strong production secrets into .env (never committed). Exists because
  ./vms (and gen-secrets.sh) are Bash and do not run in Windows cmd/PowerShell,
  so first-run secret generation had to be done by hand.

  Two modes, matching the Bash script:

    (default)  Rotate INTERNAL_API_KEY and KEYCLOAK_ADMIN_PASSWORD only.
               DISCOVERY_SECRET_KEY is deliberately NOT touched: it derives the
               Fernet key for camera credentials at rest (backend/crypto.py);
               overwriting it makes every stored credential silently
               undecryptable. On an appliance with cameras use rotate-secrets.sh.

    -Fresh     Also generate DISCOVERY_SECRET_KEY. Safe ONLY on an installation
               with no encrypted camera credentials yet — nothing to orphan.
               Checked, not assumed (Test-FreshInstall); refuses otherwise.

  -Fresh is the usual choice on a brand-new box: backend/main.py refuses to boot
  when DEV_AUTH=false and any of the three secrets is still at a shipped default,
  and .env.example ships all three as placeholders.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\gen-secrets.ps1 -Fresh
#>
[CmdletBinding()]
param(
  [switch]$Fresh,
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
# Resolve repo root (this script lives in scripts/) so it works from any cwd.
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot '.env' }
$ExampleFile = Join-Path $RepoRoot '.env.example'

# Turnkey: create .env from the template if it does not exist yet (./vms does
# this on first run; the Bash gen-secrets.sh assumes it already exists).
if (-not (Test-Path -LiteralPath $EnvFile)) {
  if (-not (Test-Path -LiteralPath $ExampleFile)) {
    throw "No $EnvFile and no .env.example to copy from - run from the project root."
  }
  Copy-Item -LiteralPath $ExampleFile -Destination $EnvFile
  Write-Host "Created $EnvFile from .env.example"
}

# URL/env-safe random string: base64, stripped of shell-special chars, cut to
# length. Mirrors the Bash gen(): openssl rand -base64 N | tr -d '\n/+=' | cut.
function New-Secret {
  param([int]$Length = 48)
  $bytes = New-Object 'System.Byte[]' 96
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  $s = ([Convert]::ToBase64String($bytes)) -replace '[/+=\r\n]', ''
  if ($s.Length -lt $Length) { throw "entropy short ($($s.Length) < $Length)" }
  $s.Substring(0, $Length)
}

# Replace ^KEY=... in place, or append if absent. Preserves the rest of .env.
function Set-EnvKV {
  param([string]$Key, [string]$Value)
  $pattern = "^$([regex]::Escape($Key))="
  $lines = @(Get-Content -LiteralPath $EnvFile)
  if ($lines -match $pattern) {
    # -replace on an array rewrites each matching line; $Value is literal
    # (base64 stripped of +/=), so no replacement-token escaping needed.
    $lines = $lines -replace "$pattern.*", "$Key=$Value"
    Set-Content -LiteralPath $EnvFile -Value $lines
  } else {
    Add-Content -LiteralPath $EnvFile -Value "$Key=$Value"
  }
}

# Read a KEY=value from .env (falling back to a default) — used for the
# fresh-install DB probe below.
function Get-EnvValue {
  param([string]$Key, [string]$Default)
  $line = (Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "^$([regex]::Escape($Key))=" } | Select-Object -First 1)
  if ($line) { ($line -split '=', 2)[1].Trim() } else { $Default }
}

# Run `docker` and hand back its stdout, with stderr discarded and the exit
# code in $script:DockerExit.
#
# Why this wrapper exists: this script runs under $ErrorActionPreference='Stop',
# and in Windows PowerShell 5.1 redirecting a NATIVE command's stderr (`2>$null`)
# wraps every stderr line in an ErrorRecord (NativeCommandError) — which 'Stop'
# then promotes to a TERMINATING error. On a clean box `docker inspect
# vms_postgres` legitimately writes "no such object" to stderr, so the freshness
# probe below used to throw instead of answering "fresh": DISCOVERY_SECRET_KEY
# was never generated, .env kept the shipped placeholder, and `vms up -d` died on
# the api's boot guard. Dropping to 'SilentlyContinue' for the duration of the
# call keeps the redirect silent AND non-terminating; $LASTEXITCODE is still set.
$script:DockerExit = 0
function Invoke-Docker {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$DockerArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try {
    $out = & docker @DockerArgs 2>$null
    $script:DockerExit = $LASTEXITCODE
    return ($out | Out-String)
  } finally { $ErrorActionPreference = $prev }
}

# True only when nothing would be lost by changing DISCOVERY_SECRET_KEY.
# Conservative: anything we cannot verify counts as NOT fresh, because the
# failure mode (orphaned camera credentials) is silent and unrecoverable.
$script:CredCount = $null
function Test-FreshInstall {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $true }
  # No database container at all -> nothing has ever been stored.
  Invoke-Docker inspect vms_postgres | Out-Null
  if ($script:DockerExit -ne 0) { return $true }
  $status = (Invoke-Docker inspect -f '{{.State.Status}}' vms_postgres).Trim()
  if ($status -ne 'running') { return $true }

  $pgUser = Get-EnvValue 'POSTGRES_USER' 'rtsp'
  $pgDb   = Get-EnvValue 'POSTGRES_DB'   'rtsp_relay'
  # The arguments are passed as ONE named array, never as bare tokens: with
  # ValueFromRemainingArguments, a bare `-d` prefix-binds to -DockerArgs itself
  # (declared parameters outrank the common -Debug), eats the db name, and the
  # unbindable 'exec' throws a terminating ParameterBindingException — so this
  # probe crashed on exactly the installs it exists to protect (a running
  # vms_postgres), after two secrets were already rotated.
  $n = Invoke-Docker -DockerArgs @(
        'exec', 'vms_postgres', 'psql', '-U', $pgUser, '-d', $pgDb, '-tAc',
        "select count(*) from cameras where coalesce(enc_password,'')<>'';")
  if ($script:DockerExit -ne 0) { return $true }   # table absent (fresh schema) => fresh
  $n = "$n".Trim()
  if ([string]::IsNullOrWhiteSpace($n) -or $n -eq '0') { return $true }
  $script:CredCount = $n
  return $false
}

# ── Generate & write ─────────────────────────────────────────────────────────
Set-EnvKV 'INTERNAL_API_KEY'        (New-Secret 64)
$kcPass = New-Secret 32
Set-EnvKV 'KEYCLOAK_ADMIN_PASSWORD' $kcPass

$dskNote = "  DISCOVERY_SECRET_KEY     left untouched (see -Fresh, and rotate-secrets.sh)"
if ($Fresh) {
  if (Test-FreshInstall) {
    Set-EnvKV 'DISCOVERY_SECRET_KEY' (New-Secret 48)
    $dskNote = "  DISCOVERY_SECRET_KEY     generated (no stored camera credentials to orphan)"
  } else {
    Write-Host ""
    Write-Warning @"
Refusing -Fresh: this installation already has $($script:CredCount) camera(s) with
stored credentials. Generating a new DISCOVERY_SECRET_KEY would make them
permanently undecryptable, and the failure would be silent.

INTERNAL_API_KEY and KEYCLOAK_ADMIN_PASSWORD were still rotated.

To rotate the discovery key safely, re-encrypting as it goes, use the Linux
appliance tooling: scripts/rotate-secrets.sh --rotate
"@
    exit 1
  }
}

Write-Host @"

Secrets written to ${EnvFile}:
  INTERNAL_API_KEY         (trusted machine-to-machine callers) - rotated
  KEYCLOAK_ADMIN_PASSWORD  = $kcPass
$dskNote

Next steps:
  1. Start (or restart) the stack:
        docker compose up -d
  2. KEYCLOAK_ADMIN_PASSWORD only takes effect on a FRESH Keycloak (bootstrap).
     If the keycloak DB already exists, either change it in the Keycloak console,
     or reset (LOCAL/eval only - wipes Keycloak + app DB):
        docker compose down -v; docker compose up -d

Note: keep DEV_AUTH=false to use real login (admin/admin via Keycloak). Set it to
true only for a throwaway local box with no auth at all.

Note: POSTGRES_PASSWORD and SEARCHDB_PASSWORD (Smart Search's pgvector DB,
started by default) are NOT rotated here — both are loopback-bound, and database
rotation needs an ALTER ROLE plus matching URL updates. Same follow-up as the
.sh script (noted 2026-09-02): add them when a database-rotation step lands.
"@

# Explicit success exit. Required, not cosmetic: `& gen-secrets.ps1` from vms.ps1
# is a POWERSHELL invocation, and $LASTEXITCODE is only ever set by NATIVE
# commands — so without this it keeps whatever the last `docker` call in
# Test-FreshInstall left behind (`docker inspect` on a clean box exits 1, which
# is the correct "no such container" answer). vms.ps1 would then read that stale
# 1 as "secret generation failed" and refuse to start the stack.
exit 0
