<#
Forge stack orchestration.

1. STOP phase: tears down the docker compose stack and kills orphaned
   llama-server.exe processes (llama.cpp workers Ollama's own `ollama ps`
   has lost track of -- observed on this box holding GPU memory that then
   starved a fresh model load) to reclaim memory before a clean start.
2. START phase: brings services up one at a time, in dependency order --
   Ollama (native host process; llm/embeddings/code_model's fallback chain
   all need it reachable) -> Phoenix (container; forge's tracing endpoint)
   -> Forge (container; depends on both of the above). Each step is
   verified before the next one starts: if a dependency didn't actually
   come up, starting what depends on it would just fail with a more
   confusing error later, so the chain stops and reports why.

Deliberately avoids `2>&1` on docker/ollama (native exes): under PowerShell
5.1 that wraps every stderr line -- including docker compose's normal
progress narration -- in a terminating NativeCommandError. Success/failure
is read from $LASTEXITCODE instead.

Usage (from repo root):  powershell -File scripts\run-stack.ps1
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$OllamaUrl = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "http://localhost:11434" }
$PhoenixUrl = "http://localhost:6006"
$ForgeUrl = "http://localhost:8010"

function Write-Section($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)      { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Write-Skip($msg)    { Write-Host "[SKIP] $msg" -ForegroundColor Yellow }
function Write-Fail($msg)    { Write-Host "[FAIL] $msg" -ForegroundColor Red }

function Test-HttpUp($url, $timeoutSec = 5) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec | Out-Null
        return $true
    } catch {
        # Any HTTP response (even a 4xx) means something is listening --
        # only a connection-level failure means "not up".
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

# ---------------------------------------------------------------------------
# 1. STOP -- reclaim memory before starting anything fresh.
# ---------------------------------------------------------------------------
Write-Section "Stopping running stack"

docker compose down
if ($LASTEXITCODE -eq 0) {
    Write-Ok "docker compose down (forge, phoenix)"
} else {
    Write-Fail "docker compose down exited with code $LASTEXITCODE"
}

$loadedModels = ollama ps 2>$null | Select-Object -Skip 1
if ($loadedModels -and (($loadedModels | Out-String).Trim().Length -gt 0)) {
    Write-Skip "orphaned-process cleanup: 'ollama ps' shows a model currently loaded -- not touching llama-server.exe to avoid killing active work"
} else {
    $orphans = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
    if (-not $orphans) {
        Write-Ok "no orphaned llama-server.exe processes found"
    } else {
        foreach ($p in $orphans) {
            try {
                Stop-Process -Id $p.Id -Force -ErrorAction Stop
                Write-Ok "killed orphaned llama-server.exe (PID $($p.Id))"
            } catch {
                Write-Fail "could not kill llama-server.exe PID $($p.Id): $($_.Exception.Message)"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 2. START -- one dependency at a time; stop the chain if one fails.
# ---------------------------------------------------------------------------
Write-Section "Starting stack (dependency order: Ollama -> Phoenix -> Forge)"

# --- Ollama (native host process) ------------------------------------------
# Checked with retries before concluding it's down: under heavy concurrent
# disk/CPU load (e.g. a docker build running alongside) a single short-timeout
# probe can false-negative on an Ollama that's actually up.
$up = $false
for ($i = 0; $i -lt 3 -and -not $up; $i++) {
    $up = Test-HttpUp $OllamaUrl
    if (-not $up) { Start-Sleep -Seconds 2 }
}

if ($up) {
    Write-Ok "Ollama already reachable at $OllamaUrl"
} else {
    $existing = Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue
    if ($existing) {
        # A process is running (tray app / server) but not answering yet --
        # don't spawn a second 'ollama serve' on top of it (port conflict);
        # just give it longer to come up.
        Write-Host "Ollama process running but not yet answering at $OllamaUrl -- waiting..."
    } else {
        Write-Host "Ollama not running -- attempting to start it..."
        try {
            Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
        } catch {
            Write-Fail "Ollama: could not launch 'ollama serve' -- $($_.Exception.Message) (is Ollama installed and on PATH?)"
            Write-Host "`nStack startup aborted: Phoenix/Forge would start but every LLM/embedding call would fail." -ForegroundColor Red
            exit 1
        }
    }
    for ($i = 0; $i -lt 15 -and -not $up; $i++) {
        Start-Sleep -Seconds 1
        $up = Test-HttpUp $OllamaUrl
    }
    if ($up) {
        Write-Ok "Ollama started, reachable at $OllamaUrl"
    } else {
        Write-Fail "Ollama: still unreachable at $OllamaUrl after waiting -- check for a port conflict or a crashed server process"
        Write-Host "`nStack startup aborted: Phoenix/Forge would start but every LLM/embedding call would fail." -ForegroundColor Red
        exit 1
    }
}

# --- Phoenix (container) ----------------------------------------------------
docker compose up -d phoenix
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Phoenix: 'docker compose up -d phoenix' exited with code $LASTEXITCODE"
    exit 1
}
$phoenixState = docker inspect --format '{{.State.Status}}' forge-phoenix-1 2>$null
if ($phoenixState -ne "running") {
    $logs = (docker logs --tail 20 forge-phoenix-1 2>$null | Out-String)
    Write-Fail "Phoenix: container state is '$phoenixState', not 'running'. Last log lines:`n$logs"
    Write-Host "`nStack startup aborted: Forge depends on Phoenix's tracing endpoint." -ForegroundColor Red
    exit 1
}
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    $up = Test-HttpUp $PhoenixUrl
}
if ($up) {
    Write-Ok "Phoenix started, reachable at $PhoenixUrl"
} else {
    Write-Fail "Phoenix: container is 'running' but $PhoenixUrl never answered within ~60s (DB migrations under heavy concurrent load observed taking 30s+ -- check 'docker logs forge-phoenix-1')"
    exit 1
}

# --- Forge (container; builds first) ----------------------------------------
docker compose up -d --build forge
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Forge: 'docker compose up -d --build forge' exited with code $LASTEXITCODE"
    exit 1
}
Start-Sleep -Seconds 3
$forgeState = docker inspect --format '{{.State.Status}}' forge-forge-1 2>$null
if ($forgeState -ne "running") {
    $logs = (docker logs --tail 30 forge-forge-1 2>$null | Out-String)
    Write-Fail "Forge: container state is '$forgeState', not 'running'. Last log lines:`n$logs"
    exit 1
}
$up = $false
for ($i = 0; $i -lt 45 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    $up = Test-HttpUp $ForgeUrl
    # A container can still be "running" but crash-looping (e.g. a Python
    # traceback right after startup) -- re-check state each iteration too.
    $forgeState = docker inspect --format '{{.State.Status}}' forge-forge-1 2>$null
    if ($forgeState -ne "running") { break }
}
if ($forgeState -ne "running") {
    $logs = (docker logs --tail 30 forge-forge-1 2>$null | Out-String)
    Write-Fail "Forge: container exited during startup. Last log lines:`n$logs"
    exit 1
} elseif ($up) {
    Write-Ok "Forge started, reachable at $ForgeUrl"
} else {
    Write-Fail "Forge: container is 'running' but $ForgeUrl never answered within ~45s -- check 'docker logs forge-forge-1' for a hang"
    exit 1
}

Write-Section "Stack up"
Write-Host "Ollama:  $OllamaUrl"
Write-Host "Phoenix: $PhoenixUrl"
Write-Host "Forge:   $ForgeUrl"
