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
New-Item -ItemType Directory -Force -Path "C:\ProgramData\ssh" | Out-Null
$sshdConfig = @"
# Unity File Sync - OpenSSH Server Configuration

Port 2222
PasswordAuthentication no
PubkeyAuthentication yes

# Use administrators_authorized_keys for all users
AuthorizedKeysFile C:/ProgramData/ssh/administrators_authorized_keys

# Subsystem for SFTP
Subsystem sftp sftp-server.exe
"@
Set-Content -Path $sshdConfigPath -Value $sshdConfig -Encoding UTF8
Write-Host "  Configured SSHD on port 2222 (full config)"

Set-Service -Name sshd -StartupType Automatic
Start-Service sshd -ErrorAction SilentlyContinue
Write-Host "  SSHD started and set to auto-start"

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
# Display Resolution (1920x1080 scheduled task for unityuser)
# =============================================================================
Write-Host ""
Write-Host "=== Setting up Display Resolution ===" -ForegroundColor Cyan

$novncDir = "C:\novnc"
New-Item -ItemType Directory -Force -Path $novncDir | Out-Null

$resolutionScript = @'
$code = @"
using System;
using System.Runtime.InteropServices;

public class DisplaySettings {
    [DllImport("user32.dll")]
    public static extern int EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);

    [DllImport("user32.dll")]
    public static extern int ChangeDisplaySettings(ref DEVMODE devMode, int flags);

    public const int ENUM_CURRENT_SETTINGS = -1;
    public const int CDS_UPDATEREGISTRY = 0x01;
    public const int CDS_TEST = 0x02;
    public const int DISP_CHANGE_SUCCESSFUL = 0;
    public const int DM_PELSWIDTH = 0x80000;
    public const int DM_PELSHEIGHT = 0x100000;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct DEVMODE {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string dmDeviceName;
        public short dmSpecVersion;
        public short dmDriverVersion;
        public short dmSize;
        public short dmDriverExtra;
        public int dmFields;
        public int dmPositionX;
        public int dmPositionY;
        public int dmDisplayOrientation;
        public int dmDisplayFixedOutput;
        public short dmColor;
        public short dmDuplex;
        public short dmYResolution;
        public short dmTTOption;
        public short dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel;
        public int dmPelsWidth;
        public int dmPelsHeight;
        public int dmDisplayFlags;
        public int dmDisplayFrequency;
        public int dmICMMethod;
        public int dmICMIntent;
        public int dmMediaType;
        public int dmDitherType;
        public int dmReserved1;
        public int dmReserved2;
        public int dmPanningWidth;
        public int dmPanningHeight;
    }

    public static int SetResolution(int width, int height) {
        DEVMODE dm = new DEVMODE();
        dm.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
        if (EnumDisplaySettings(null, ENUM_CURRENT_SETTINGS, ref dm) == 0) {
            return -1;
        }
        dm.dmPelsWidth = width;
        dm.dmPelsHeight = height;
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT;
        int testResult = ChangeDisplaySettings(ref dm, CDS_TEST);
        if (testResult != DISP_CHANGE_SUCCESSFUL) {
            return testResult;
        }
        return ChangeDisplaySettings(ref dm, CDS_UPDATEREGISTRY);
    }
}
"@

try {
    Add-Type -TypeDefinition $code -Language CSharp -ErrorAction Stop
} catch {}

$result = [DisplaySettings]::SetResolution(1920, 1080)
if ($result -eq 0) {
    "Resolution set to 1920x1080" | Out-File -FilePath "C:\novnc\resolution.log" -Append -Encoding UTF8
} else {
    "Failed to set resolution, error code: $result" | Out-File -FilePath "C:\novnc\resolution.log" -Append -Encoding UTF8
}
'@

$resolutionScript | Out-File -FilePath "$novncDir\set-resolution.ps1" -Encoding UTF8
Write-Host "  Created resolution script at $novncDir\set-resolution.ps1"

$taskName = "SetDisplayResolution"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"Start-Sleep -Seconds 3; & 'C:\novnc\set-resolution.ps1'`"" `
    -WorkingDirectory $novncDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "unityuser"
$principal = New-ScheduledTaskPrincipal -UserId "unityuser" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Host "  Scheduled task '$taskName' created for unityuser (at logon)"

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
Write-Host "  - Display resolution (1920x1080 at logon)"
Write-Host "  - Pool watcher: UnityPoolWatcher service (NSSM)"
Write-Host ""
