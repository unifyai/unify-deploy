# =============================================================================
# Windows VM Startup Script (Pool VMs)
#
# Runs on every boot via GCP startup-script metadata. Handles pool-level
# infrastructure only - per-assistant configuration (SSH keys, VNC password,
# API keys, disk mount, Agent Service start) is handled by the pool watcher.
#
# Responsibilities:
#   1. Ensure Bun is installed for SYSTEM profile
#   2. Update pool watcher script from metadata
#   3. Update Magnitude and Agent Service code
#   4. Configure Caddy reverse proxy (hostname + TLS)
#   5. Ensure websockify scheduled task exists
#   6. Start services (TightVNC, websockify, Caddy)
#   7. Mark pool VM as idle
#
# GCP Metadata Keys (set at pool creation):
#   hostname, github-token, orchestra-url, comms-url, unity-environment,
#   staging, preview,
#   tls-fullchain, tls-privkey, pool-watcher-script
#
# GCP Metadata Keys (set at assignment, handled by pool watcher):
#   unify-key, vnc-password, ssh-public-key, disk-device, assistant-id,
#   office-mak-key
# =============================================================================

$ErrorActionPreference = 'Continue'
$script:StartTime = Get-Date
$script:GitExe = 'C:\Program Files\Git\bin\git.exe'
$script:CmdExe = 'C:\Windows\System32\cmd.exe'

Write-Host "=========================================="
Write-Host "  Windows VM Startup Script"
Write-Host "=========================================="
Write-Host ""

# =============================================================================
# Helpers
# =============================================================================

function Invoke-Git {
    $argStr = $args -join ' '
    & $script:CmdExe /c "`"$script:GitExe`" $argStr 2>&1"
}

function Get-GCPMetadata {
    param([string]$Key)
    try {
        $headers = @{"Metadata-Flavor" = "Google"}
        return (Invoke-RestMethod -Uri "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$Key" -Headers $headers -TimeoutSec 5 -ErrorAction Stop)
    } catch {
        return $null
    }
}

function Get-RemoteCommitHash {
    param([string]$RepoUrl, [string]$Branch)
    try {
        $output = Invoke-Git ls-remote $RepoUrl "refs/heads/$Branch"
        if ($output -match '^([a-f0-9]+)\s') {
            return $matches[1].Substring(0, 12)
        }
    } catch {}
    return $null
}

function Get-DeployEnv {
    $envName = Get-GCPMetadata -Key "unity-environment"
    if ($envName) { return $envName }
    if (Get-GCPMetadata -Key "preview") { return "preview" }
    if (Get-GCPMetadata -Key "staging") { return "staging" }
    return "production"
}

function Get-SavedCommitHash {
    param([string]$Dir)
    $hashFile = "$Dir\.commit-hash"
    if (Test-Path $hashFile) {
        return (Get-Content $hashFile -ErrorAction SilentlyContinue).Trim()
    }
    return $null
}

function Save-CommitHash {
    param([string]$Dir, [string]$Hash)
    if ($Hash) {
        $Hash | Out-File -FilePath "$Dir\.commit-hash" -Encoding UTF8 -NoNewline
    }
}

function Update-GitRepo {
    param(
        [string]$RepoPath,
        [string]$Branch,
        [string]$GithubToken,
        [string]$RepoName
    )

    Push-Location $RepoPath
    try {
        if ($GithubToken) {
            $remoteUrl = Invoke-Git remote get-url origin
            if ($remoteUrl -notlike "*$GithubToken*") {
                Invoke-Git remote set-url origin "https://$GithubToken@github.com/unifyai/$RepoName.git" | Out-Null
            }
        }

        Invoke-Git fetch --depth 1 origin $Branch | Out-Null
        Invoke-Git reset --hard origin/$Branch | Out-Null

        $commit = Invoke-Git rev-parse --short=12 HEAD
        Write-Host "  Updated to commit: $commit" -ForegroundColor Green
        if ($commit) {
            $commit | Out-File -FilePath "$RepoPath\.commit-hash" -Encoding UTF8 -NoNewline
        }
        Pop-Location
        return $true
    } catch {
        Write-Host "  WARNING: Failed to update - $_" -ForegroundColor Yellow
        Pop-Location
        return $false
    }
}

function Scrub-GitTokens {
    foreach ($dir in @('C:\magnitude', 'C:\agent-service')) {
        if (Test-Path "$dir\.git") {
            try {
                $url = (& $script:CmdExe /c "`"$script:GitExe`" -C `"$dir`" remote get-url origin 2>&1")
                if ($url -match '@github\.com') {
                    $clean = $url -replace 'https://[^@]+@', 'https://'
                    & $script:CmdExe /c "`"$script:GitExe`" -C `"$dir`" remote set-url origin $clean 2>&1" | Out-Null
                }
            } catch {}
        }
    }
}

# =============================================================================
# Service User Hardening (converge on every boot)
# =============================================================================
Remove-LocalGroupMember -Group "Administrators" -Member "unityuser" -ErrorAction SilentlyContinue
Write-Host "Ensured unityuser is not in Administrators group" -ForegroundColor Green

# =============================================================================
# SSHD config convergence (unityuser is intentionally non-admin)
# =============================================================================
$sshdConfigPath = "C:\ProgramData\ssh\sshd_config"
$unityUserSshDir = "C:\Users\unityuser\.ssh"
New-Item -ItemType Directory -Force -Path "C:\ProgramData\ssh" | Out-Null
New-Item -ItemType Directory -Force -Path $unityUserSshDir | Out-Null
$sshdConfig = @"
# Unity File Sync - OpenSSH Server Configuration

Port 2222
PasswordAuthentication no
PubkeyAuthentication yes

# unityuser is intentionally non-admin, so use its per-user authorized_keys
AuthorizedKeysFile C:/Users/unityuser/.ssh/authorized_keys

# Subsystem for SFTP
Subsystem sftp sftp-server.exe
"@
Set-Content -Path $sshdConfigPath -Value $sshdConfig -Encoding UTF8
C:\Windows\System32\icacls.exe $unityUserSshDir /inheritance:r /grant "unityuser:(OI)(CI)F" /grant "SYSTEM:F" /grant "Administrators:F" 2>$null | Out-Null
Remove-Item "C:\ProgramData\ssh\administrators_authorized_keys" -Force -ErrorAction SilentlyContinue
Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Restart-Service sshd -ErrorAction SilentlyContinue
Write-Host "Converged SSHD for unityuser authorized_keys" -ForegroundColor Green

# =============================================================================
# Read Configuration
# =============================================================================
Write-Host "Reading GCP metadata..."
$gcpHostname = Get-GCPMetadata -Key "hostname"
$gcpGithubToken = (Get-GCPMetadata -Key "github-token")
if ($gcpGithubToken) { $gcpGithubToken = $gcpGithubToken.Trim() }
$gcpDeployEnv = Get-DeployEnv
$gcpTlsFullchain = Get-GCPMetadata -Key "tls-fullchain"
$gcpTlsPrivkey = Get-GCPMetadata -Key "tls-privkey"

Write-Host "  Hostname:       $(if ($gcpHostname) { $gcpHostname } else { '(not configured)' })"
Write-Host "  GitHub Token:   $(if ($gcpGithubToken) { '(set)' } else { '(not provided)' })"
Write-Host "  Deploy Env:     $gcpDeployEnv"
Write-Host "  TLS Wildcard:   $(if ($gcpTlsFullchain) { '(set)' } else { '(not provided)' })"
Write-Host ""

# Unconditionally prepend known baked tool paths - the SYSTEM context often
# cannot resolve them from Machine/User PATH alone during early boot.
$env:Path = @(
    "C:\Program Files\Git\bin",
    "C:\Program Files\Git\cmd",
    "C:\Program Files\nodejs",
    "C:\Program Files\Python312",
    "C:\Program Files\Python312\Scripts",
    "C:\ProgramData\chocolatey\bin",
    "$env:USERPROFILE\.bun\bin",
    [System.Environment]::GetEnvironmentVariable("Path","Machine"),
    [System.Environment]::GetEnvironmentVariable("Path","User")
) -join ";"

# =============================================================================
# Ensure Bun is installed for SYSTEM profile
# Packer installs bun to the interactive user profile, but the startup script
# and pool watcher run as SYSTEM - install here so both contexts have it.
# =============================================================================
$systemBunExe = "C:\Windows\System32\config\systemprofile\.bun\bin\bun.exe"
if (-not (Test-Path $systemBunExe)) {
    Write-Host "Installing Bun for SYSTEM profile..." -ForegroundColor Cyan
    try {
        $env:BUN_INSTALL = "C:\Windows\System32\config\systemprofile\.bun"
        Invoke-RestMethod -Uri "https://bun.sh/install.ps1" | Invoke-Expression
        $env:Path = "C:\Windows\System32\config\systemprofile\.bun\bin;$env:Path"
        if (Test-Path $systemBunExe) {
            Write-Host "  Bun installed for SYSTEM profile" -ForegroundColor Green
        }
    } catch {
        Write-Host "  WARNING: Bun install failed - $_" -ForegroundColor Yellow
    }
} else {
    $env:Path = "C:\Windows\System32\config\systemprofile\.bun\bin;$env:Path"
    Write-Host "Bun available for SYSTEM profile" -ForegroundColor Green
}

# =============================================================================
# Update Pool Watcher from Metadata
# =============================================================================
$poolWatcherScript = Get-GCPMetadata -Key "pool-watcher-script"
if ($poolWatcherScript) {
    Set-Content -Path "C:\unity-pool-watcher.ps1" -Value $poolWatcherScript -Encoding UTF8
    & 'C:\ProgramData\chocolatey\bin\nssm.exe' restart UnityPoolWatcher 2>&1 | Out-Null
    Write-Host "Pool watcher updated from metadata" -ForegroundColor Green
} else {
    Write-Host "No pool-watcher-script metadata, using baked-in version" -ForegroundColor Yellow
}

# =============================================================================
# Build Repo URLs
# =============================================================================
$magnitudeUrl = if ($gcpGithubToken) {
    "https://$gcpGithubToken@github.com/unifyai/magnitude.git"
} else {
    "https://github.com/unifyai/magnitude.git"
}
$unityUrl = if ($gcpGithubToken) {
    "https://$gcpGithubToken@github.com/unifyai/unity.git"
} else {
    "https://github.com/unifyai/unity.git"
}
$unityBranch = switch ($gcpDeployEnv) {
    "preview" { "preview" }
    "staging" { "staging" }
    default { "main" }
}

# =============================================================================
# Update Magnitude
# =============================================================================
Write-Host ""
Write-Host "=== Updating Magnitude ===" -ForegroundColor Cyan

$magnitudeDir = 'C:\magnitude'

if (Test-Path "$magnitudeDir\.git") {
    Update-GitRepo -RepoPath $magnitudeDir -Branch "unity-modifications" -GithubToken $gcpGithubToken -RepoName "magnitude"
} elseif (-not (Test-Path "$magnitudeDir\package.json")) {
    Write-Host "  Cloning Magnitude..."
    if (Test-Path $magnitudeDir) {
        & $script:CmdExe /c "rmdir /s /q `"$magnitudeDir`" 2>nul"
    }
    Invoke-Git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir
    if (Test-Path "$magnitudeDir\package.json") {
        Push-Location $magnitudeDir
        $commit = Invoke-Git rev-parse --short=12 HEAD
        Save-CommitHash -Dir $magnitudeDir -Hash $commit
        Write-Host "  Magnitude cloned (commit: $commit)" -ForegroundColor Green
        Pop-Location
    }
}

if (Test-Path "$magnitudeDir\package.json") {
    Write-Host "  Installing dependencies..." -ForegroundColor Yellow
    Push-Location $magnitudeDir
    if (Test-Path $systemBunExe) {
        & $script:CmdExe /c "`"$systemBunExe`" install 2>&1" | Out-Null
    } else {
        & $script:CmdExe /c "npm install 2>&1" | Out-Null
    }
    Pop-Location
    Write-Host "  Magnitude dependencies installed" -ForegroundColor Green

    # Install Patchright Chromium from magnitude-core
    $magCore = "$magnitudeDir\packages\magnitude-core"
    if (Test-Path "$magCore\package.json") {
        Write-Host "  Installing Patchright Chromium..." -ForegroundColor Yellow
        $env:PLAYWRIGHT_BROWSERS_PATH = "C:\ms-playwright"
        Push-Location $magCore
        & $script:CmdExe /c "npx --yes patchright install chromium 2>&1" | Out-Null
        Pop-Location
        Write-Host "  Patchright Chromium installed" -ForegroundColor Green
    }
}

# =============================================================================
# Update Agent Service
# =============================================================================
Write-Host ""
Write-Host "=== Updating Agent Service ===" -ForegroundColor Cyan

$agentServiceDir = 'C:\agent-service'

# Backup .env if exists (pool watcher may have written it)
$envBackup = $null
$envFile = "$agentServiceDir\.env"
if (Test-Path $envFile) {
    $envBackup = Get-Content $envFile -Raw
}

if (Test-Path "$agentServiceDir\.git") {
    Update-GitRepo -RepoPath $agentServiceDir -Branch $unityBranch -GithubToken $gcpGithubToken -RepoName "unity"
} else {
    $savedHash = Get-SavedCommitHash -Dir $agentServiceDir
    $remoteHash = Get-RemoteCommitHash -RepoUrl $unityUrl -Branch $unityBranch
    $needsUpdate = $true

    if ((Test-Path "$agentServiceDir\package.json") -and $savedHash -and $remoteHash -and ($savedHash -eq $remoteHash)) {
        Write-Host "  Agent Service up-to-date (commit: $savedHash)" -ForegroundColor Green
        $needsUpdate = $false
    } elseif ($savedHash -and $remoteHash) {
        Write-Host "  Agent Service update available ($savedHash -> $remoteHash)" -ForegroundColor Yellow
    }

    if ($needsUpdate) {
        Write-Host "  Cloning Agent Service from Unity repo..."

        # Stop running agent-service to release file locks
        Stop-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
        Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500

        $unityRepoDir = 'C:\temp\unity-repo'
        New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
        if (Test-Path $unityRepoDir) {
            & $script:CmdExe /c "rmdir /s /q `"$unityRepoDir`" 2>nul"
        }

        Invoke-Git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $unityRepoDir

        if (Test-Path $unityRepoDir) {
            $commitHash = $null
            Push-Location $unityRepoDir
            $commitHash = Invoke-Git rev-parse --short=12 HEAD
            Invoke-Git sparse-checkout set agent-service | Out-Null
            Pop-Location

            if (Test-Path "$unityRepoDir\agent-service") {
                # Preserve node_modules to speed up npm install
                if (Test-Path "$agentServiceDir\node_modules") {
                    Move-Item "$agentServiceDir\node_modules" "$unityRepoDir\agent-service\node_modules" -Force -ErrorAction SilentlyContinue
                }
                if (Test-Path $agentServiceDir) {
                    & $script:CmdExe /c "rmdir /s /q `"$agentServiceDir`" 2>nul"
                }
                Move-Item "$unityRepoDir\agent-service" $agentServiceDir
                if ($commitHash) {
                    Save-CommitHash -Dir $agentServiceDir -Hash $commitHash
                }
                Write-Host "  Agent Service updated (commit: $commitHash)" -ForegroundColor Green
            }
            & $script:CmdExe /c "rmdir /s /q `"$unityRepoDir`" 2>nul"
        }
    }
}

if (Test-Path "$agentServiceDir\package.json") {
    Write-Host "  Installing dependencies..." -ForegroundColor Yellow
    Push-Location $agentServiceDir
    & $script:CmdExe /c "npm install 2>&1" | Out-Null
    Pop-Location
    Write-Host "  Agent Service dependencies installed" -ForegroundColor Green
}

# Restore .env file
if ($envBackup -and (Test-Path $agentServiceDir)) {
    $envBackup | Out-File -FilePath $envFile -Encoding UTF8 -NoNewline
}

Scrub-GitTokens

# Grant unityuser read+execute on code directories (installed by SYSTEM above)
foreach ($dir in @("C:\agent-service", "C:\magnitude", "C:\ms-playwright")) {
    if (Test-Path $dir) {
        C:\Windows\System32\icacls.exe $dir /grant "unityuser:(OI)(CI)RX" /T /Q 2>$null
    }
}
Write-Host "  unityuser ACLs set on service directories" -ForegroundColor Green

# =============================================================================
# Configure Caddy
# =============================================================================
Write-Host ""
Write-Host "=== Configuring Caddy ===" -ForegroundColor Cyan

# Write TLS certs if provided
if ($gcpTlsFullchain -and $gcpTlsPrivkey) {
    New-Item -ItemType Directory -Force -Path "C:\caddy\certs" | Out-Null
    [System.IO.File]::WriteAllText("C:\caddy\certs\fullchain.pem", $gcpTlsFullchain)
    [System.IO.File]::WriteAllText("C:\caddy\certs\privkey.pem", $gcpTlsPrivkey)
    Write-Host "  TLS cert written" -ForegroundColor Green
}

$caddyConfigured = $false
if ($gcpHostname) {
    $caddyDir = 'C:\caddy'
    $caddyfile = "$caddyDir\Caddyfile"
    New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null

    $tlsDirective = ""
    if (Test-Path "C:\caddy\certs\fullchain.pem") {
        $tlsDirective = "    tls C:\caddy\certs\fullchain.pem C:\caddy\certs\privkey.pem"
    }

    $caddyConfig = @"
$gcpHostname {
$tlsDirective
    @websocket {
        path /desktop/*
        header Connection *Upgrade*
        header Upgrade websocket
    }
    reverse_proxy @websocket localhost:6080

    @desktop_exact path /desktop
    handle @desktop_exact {
        redir /desktop/ permanent
    }

    handle_path /desktop/* {
        reverse_proxy localhost:6080
    }

    @api_exact path /api
    handle @api_exact {
        redir /api/ permanent
    }

    handle_path /api/* {
        reverse_proxy localhost:3000
    }

    handle {
        respond "Not Found" 404
    }

    log {
        output file C:\caddy\access.log {
            roll_size 10mb
            roll_keep 3
        }
        format console
    }
}
"@
    $caddyConfig | Out-File -FilePath $caddyfile -Encoding UTF8
    $caddyConfigured = $true
    Write-Host "  Caddy configured for https://$gcpHostname" -ForegroundColor Green
} else {
    Write-Host "  No hostname provided, Caddy disabled" -ForegroundColor Yellow
}

# =============================================================================
# Setup Websockify Scheduled Task
# =============================================================================
Write-Host ""
Write-Host "=== Setting up websockify ===" -ForegroundColor Cyan

$novncDir = 'C:\novnc'
$websockifyBat = "$novncDir\start-websockify.bat"
$websockifyTaskName = "StartWebsockify"

$existingTask = Get-ScheduledTask -TaskName $websockifyTaskName -ErrorAction SilentlyContinue
if ($existingTask -and (Test-Path $websockifyBat)) {
    Write-Host "  Websockify already configured" -ForegroundColor Green
} else {
    $pythonExe = 'C:\Program Files\Python312\python.exe'
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    }

    if ($pythonExe -and (Test-Path $pythonExe)) {
        $websockifyScript = @"
@echo off
cd /d C:\novnc
"$pythonExe" -m websockify --web C:\novnc 6080 localhost:5900
"@
        $websockifyScript | Out-File -FilePath $websockifyBat -Encoding ASCII

        if (-not $existingTask) {
            $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$websockifyBat'`"" -WorkingDirectory $novncDir
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User "unityuser"
            $principal = New-ScheduledTaskPrincipal -UserId "unityuser" -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
            Register-ScheduledTask -TaskName $websockifyTaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        }
        Write-Host "  Websockify configured" -ForegroundColor Green
    } else {
        Write-Host "  ERROR: Python not found" -ForegroundColor Red
    }
}

# =============================================================================
# Enforce Firewall Rules (converge on every boot)
# =============================================================================
Write-Host ""
Write-Host "=== Enforcing firewall rules ===" -ForegroundColor Cyan

# Remove all Unity-* rules to ensure clean slate
Get-NetFirewallRule -DisplayName "Unity-*" -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

# Inbound: HTTPS only (6080/3000 behind Caddy, RDP handled by Windows/GCP defaults)
New-NetFirewallRule -DisplayName "Unity-HTTPS" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow -Profile Any | Out-Null
Write-Host "  Inbound: port 443 allowed" -ForegroundColor Green

# Outbound: block metadata server (169.254.169.254) for unityuser
try {
    $sid = (New-Object System.Security.Principal.NTAccount("unityuser")).Translate(
        [System.Security.Principal.SecurityIdentifier]).Value
    New-NetFirewallRule -DisplayName "Unity-BlockMetadata" -Direction Outbound `
        -RemoteAddress 169.254.169.254 -Action Block `
        -LocalUser "D:(A;;CC;;;$sid)" -Profile Any | Out-Null
    Write-Host "  Outbound: metadata server blocked for unityuser" -ForegroundColor Green
} catch {
    Write-Host "  WARNING: could not create metadata block rule: $_" -ForegroundColor Yellow
}

# =============================================================================
# Start Services (TightVNC, websockify, Caddy - NOT Agent Service)
# =============================================================================
Write-Host ""
Write-Host "=== Starting Services ===" -ForegroundColor Cyan

# TightVNC: ensure loopback connections are allowed (websockify connects via localhost)
$vncRegPath = "HKLM:\SOFTWARE\TightVNC\Server"
if (Test-Path $vncRegPath) {
    Set-ItemProperty -Path $vncRegPath -Name "AllowLoopback" -Value 1 -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $vncRegPath -Name "LoopbackOnly" -Value 0 -Type DWord -ErrorAction SilentlyContinue
}
$tvnService = Get-Service -Name "tvnserver" -ErrorAction SilentlyContinue
if ($tvnService) {
    Restart-Service -Name "tvnserver" -ErrorAction SilentlyContinue
}
if ((Get-Service -Name "tvnserver" -ErrorAction SilentlyContinue).Status -eq 'Running') {
    Write-Host "  TightVNC: Running" -ForegroundColor Green
} else {
    Write-Host "  TightVNC: Starting..." -ForegroundColor Yellow
}

# Websockify
$port6080 = Get-NetTCPConnection -LocalPort 6080 -State Listen -ErrorAction SilentlyContinue
if (-not $port6080 -and (Test-Path $websockifyBat)) {
    Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$websockifyBat'`"" -WorkingDirectory $novncDir -WindowStyle Hidden
    Start-Sleep -Milliseconds 1500
}
$port6080 = Get-NetTCPConnection -LocalPort 6080 -State Listen -ErrorAction SilentlyContinue
if ($port6080) {
    Write-Host "  Websockify: Running (port 6080)" -ForegroundColor Green
} else {
    Write-Host "  Websockify: Starting..." -ForegroundColor Yellow
}

# Caddy (runs as unityuser via scheduled task)
if ($caddyConfigured) {
    $caddyExe = "C:\caddy\caddy.exe"
    $caddyfileConfig = "C:\caddy\Caddyfile"

    # Grant unityuser read access to Caddy config + TLS certs
    C:\Windows\System32\icacls.exe "C:\caddy" /grant "unityuser:R" /T /Q 2>$null

    # Ensure scheduled task exists to run Caddy as unityuser (AtLogOn — auto-logon fires immediately)
    $taskName = "StartCaddy"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $existingTask -and (Test-Path $caddyExe) -and (Test-Path $caddyfileConfig)) {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$caddyExe' run --config '$caddyfileConfig'`"" `
            -WorkingDirectory "C:\caddy"
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "unityuser"
        $principal = New-ScheduledTaskPrincipal -UserId "unityuser" -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
    }

    $caddyProcess = Get-Process -Name "caddy" -ErrorAction SilentlyContinue
    if (-not $caddyProcess) {
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 1000
    }

    if (Get-Process -Name "caddy" -ErrorAction SilentlyContinue) {
        Write-Host "  Caddy: Running (as unityuser)" -ForegroundColor Green
    } else {
        Write-Host "  Caddy: Starting..." -ForegroundColor Yellow
    }
}

# =============================================================================
# Mark pool VM as idle + wipe github-token (via Comms API with GCP identity token)
# =============================================================================
$commsUrl = Get-GCPMetadata -Key "comms-url"
try {
    $metaHeaders = @{ "Metadata-Flavor" = "Google" }
    $idToken = Invoke-RestMethod -Uri "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=unity-comms-vm&format=full" -Headers $metaHeaders -TimeoutSec 5
} catch {
    $idToken = $null
}

if ($commsUrl -and $idToken) {
    try {
        Invoke-RestMethod -Uri "$commsUrl/infra/vm/mark-idle" `
            -Method POST -ContentType "application/json" `
            -Headers @{ Authorization = "Bearer $idToken" } `
            -TimeoutSec 10
        Write-Host "Pool VM marked as idle" -ForegroundColor Green
    } catch {
        Write-Host "WARNING: failed to mark VM as idle via Comms API: $_" -ForegroundColor Yellow
    }

    try {
        $wipeBody = @{ key = "github-token" } | ConvertTo-Json
        Invoke-RestMethod -Uri "$commsUrl/infra/vm/wipe-metadata-key" `
            -Method POST -ContentType "application/json" `
            -Headers @{ Authorization = "Bearer $idToken" } `
            -Body $wipeBody -TimeoutSec 10 | Out-Null
        Write-Host "Wiped github-token from metadata" -ForegroundColor Green
    } catch {
        Write-Host "WARNING: failed to wipe github-token via Comms API: $_" -ForegroundColor Yellow
    }
}

# =============================================================================
# Done
# =============================================================================
$totalElapsed = (Get-Date) - $script:StartTime
Write-Host ""
Write-Host "Startup complete in $([math]::Round($totalElapsed.TotalSeconds, 1))s" -ForegroundColor Magenta
