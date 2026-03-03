# =============================================================================
# Pool Overlay for Windows VM
#
# Runs after install-base.ps1 to add pool-specific components:
# - unityuser (auto-logon Administrator)
# - OpenSSH Server (port 2222 for file sync)
# - TightVNC Server (dummy password, updated at assignment)
# - Unity Pool Watcher (NSSM Windows service)
# =============================================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=========================================="
Write-Host "  Pool Overlay: Starting"
Write-Host "=========================================="

# =============================================================================
# Pool User: unityuser
# =============================================================================
Write-Host ""
Write-Host "=== Creating pool user: unityuser ===" -ForegroundColor Cyan

$poolPassword = ConvertTo-SecureString "UnityPoolDefault1!" -AsPlainText -Force
if (-not (Get-LocalUser -Name "unityuser" -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name "unityuser" -Password $poolPassword -PasswordNeverExpires -Description "Unity pool VM user"
    Add-LocalGroupMember -Group "Administrators" -Member "unityuser"
    Write-Host "  Created user unityuser (Administrator)"
} else {
    Write-Host "  User unityuser already exists"
}

# Configure auto-logon
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "AutoAdminLogon" -Value "1"
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultUserName" -Value "unityuser"
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultPassword" -Value "UnityPoolDefault1!"
Write-Host "  Auto-logon configured for unityuser"

# Create Unity directories
New-Item -ItemType Directory -Force -Path "C:\Unity" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\Unity\Local" | Out-Null
icacls "C:\Unity" /grant "unityuser:F" /T /Q 2>$null
Write-Host "  Created C:\Unity and C:\Unity\Local"

# =============================================================================
# OpenSSH Server (port 2222 for file sync)
# =============================================================================
Write-Host ""
Write-Host "=== Installing OpenSSH Server ===" -ForegroundColor Cyan

$sshCapability = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($sshCapability.State -ne "Installed") {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Write-Host "  OpenSSH Server installed"
} else {
    Write-Host "  OpenSSH Server already installed"
}

$sshdConfigPath = "C:\ProgramData\ssh\sshd_config"
if (Test-Path $sshdConfigPath) {
    $content = Get-Content $sshdConfigPath -Raw
    if ($content -notmatch "Port 2222") {
        $content = $content -replace "#Port 22", "Port 2222"
        if ($content -notmatch "Port 2222") {
            $content += "`nPort 2222`n"
        }
        Set-Content -Path $sshdConfigPath -Value $content
        Write-Host "  Configured SSHD on port 2222"
    }
}

Set-Service -Name sshd -StartupType Automatic
Write-Host "  SSHD set to auto-start"

# Firewall rule for SSH port 2222
New-NetFirewallRule -DisplayName "Unity SSH 2222" -Direction Inbound -LocalPort 2222 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
Write-Host "  Firewall rule added for port 2222"

# =============================================================================
# TightVNC Server (baked with dummy password, updated at assignment)
# =============================================================================
Write-Host ""
Write-Host "=== Installing TightVNC Server ===" -ForegroundColor Cyan

$tightVncInstaller = "C:\temp\tightvnc-2.8.84-gpl-setup-64bit.msi"
if (-not (Test-Path $tightVncInstaller)) {
    New-Item -ItemType Directory -Force -Path "C:\temp" | Out-Null
    Invoke-WebRequest -Uri "https://www.tightvnc.com/download/2.8.84/tightvnc-2.8.84-gpl-setup-64bit.msi" `
        -OutFile $tightVncInstaller -UseBasicParsing
}
Start-Process msiexec.exe -ArgumentList "/i `"$tightVncInstaller`" /quiet /norestart SET_USEVNCAUTHENTICATION=1 VALUE_OF_USEVNCAUTHENTICATION=1 SET_PASSWORD=1 VALUE_OF_PASSWORD=dummy123 SET_CONTROLPASSWORD=1 VALUE_OF_CONTROLPASSWORD=dummy123" -Wait
Write-Host "  TightVNC installed with dummy password"

# =============================================================================
# Pool Watcher (NSSM Windows service)
# =============================================================================
Write-Host ""
Write-Host "=== Installing Unity Pool Watcher ===" -ForegroundColor Cyan

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    choco install nssm -y --no-progress 2>$null
    Write-Host "  NSSM installed"
}

if (Test-Path "C:\temp\unity-pool-watcher.ps1") {
    Copy-Item "C:\temp\unity-pool-watcher.ps1" "C:\unity-pool-watcher.ps1" -Force
    Write-Host "  Watcher script installed at C:\unity-pool-watcher.ps1"
}

$nssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if ($nssmPath) {
    & $nssmPath install UnityPoolWatcher powershell.exe "-ExecutionPolicy Bypass -File C:\unity-pool-watcher.ps1"
    & $nssmPath set UnityPoolWatcher Start SERVICE_AUTO_START
    & $nssmPath set UnityPoolWatcher AppStdout "C:\Unity\pool-watcher.log"
    & $nssmPath set UnityPoolWatcher AppStderr "C:\Unity\pool-watcher.log"
    & $nssmPath set UnityPoolWatcher AppRotateFiles 1
    & $nssmPath set UnityPoolWatcher AppRotateBytes 10485760
    Write-Host "  UnityPoolWatcher service registered (auto-start)"
}

# =============================================================================
# Summary
# =============================================================================
Write-Host ""
Write-Host "=========================================="
Write-Host "  Pool Overlay: Complete"
Write-Host "=========================================="
Write-Host ""
Write-Host "Added:" -ForegroundColor Cyan
Write-Host "  - Pool user: unityuser (auto-logon, Administrator)"
Write-Host "  - OpenSSH Server (port 2222)"
Write-Host "  - TightVNC Server (dummy password, updated at assignment)"
Write-Host "  - Pool watcher: UnityPoolWatcher service (NSSM)"
Write-Host ""
