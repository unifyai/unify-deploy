# =============================================================================
# Pool Overlay for Windows VM
#
# Runs after install-base.ps1 to add pool-specific components:
# - unityuser (auto-logon, standard user)
# - OpenSSH Server (port 2222 for file sync)
# - TightVNC Server (dummy password, updated at assignment)
# - Droid Pool Watcher (NSSM Windows service)
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
    New-LocalUser -Name "unityuser" -Password $poolPassword -PasswordNeverExpires -Description "Droid pool VM user"
    Write-Host "  Created user unityuser (standard user)"
} else {
    Write-Host "  User unityuser already exists"
}
Remove-LocalGroupMember -Group "Administrators" -Member "unityuser" -ErrorAction SilentlyContinue

# Configure auto-logon
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "AutoAdminLogon" -Value "1"
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultUserName" -Value "unityuser"
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultPassword" -Value "UnityPoolDefault1!"
Write-Host "  Auto-logon configured for unityuser"

# Create Droid directories
New-Item -ItemType Directory -Force -Path "C:\Droid" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\Droid\Local" | Out-Null
icacls "C:\Droid" /grant "unityuser:(OI)(CI)F" /T /Q 2>$null
Write-Host "  Created C:\Droid and C:\Droid\Local"

# Grant unityuser access to service directories
foreach ($dir in @("C:\agent-service", "C:\magnitude", "C:\ms-playwright", "C:\novnc")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    icacls $dir /grant "unityuser:(OI)(CI)RX" /T /Q 2>$null
}
Write-Host "  Granted unityuser read+execute on service directories"

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
# Droid File Sync - OpenSSH Server Configuration

Port 2222
PasswordAuthentication no
PubkeyAuthentication yes

# unityuser is intentionally non-admin, so use its per-user authorized_keys
AuthorizedKeysFile C:/Users/unityuser/.ssh/authorized_keys

# Subsystem for SFTP
Subsystem sftp sftp-server.exe
"@
Set-Content -Path $sshdConfigPath -Value $sshdConfig -Encoding UTF8
Write-Host "  Configured SSHD on port 2222 (full config)"

Set-Service -Name sshd -StartupType Automatic
Start-Service sshd -ErrorAction SilentlyContinue
Write-Host "  SSHD started and set to auto-start"

# Firewall rule for SSH port 2222
New-NetFirewallRule -DisplayName "Droid SSH 2222" -Direction Inbound -LocalPort 2222 -Protocol TCP -Action Allow -ErrorAction SilentlyContinue
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
$principal = New-ScheduledTaskPrincipal -UserId "unityuser" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Host "  Scheduled task '$taskName' created for unityuser (at logon)"

# =============================================================================
# Invisible Cursor (transparent cursor for clean VNC streaming)
# =============================================================================
Write-Host ""
Write-Host "=== Setting up Invisible Cursor ===" -ForegroundColor Cyan

$cursorDir = 'C:\Windows\Cursors'
$blankCursorPath = "$cursorDir\blank.cur"

# Create a minimal transparent 32x32 cursor file
$curHeader = [byte[]]@(
    0x00, 0x00,       # Reserved
    0x02, 0x00,       # Type (2 = cursor)
    0x01, 0x00        # Number of images (1)
)
$curDirEntry = [byte[]]@(
    0x20,             # Width (32)
    0x20,             # Height (32)
    0x00,             # Color count
    0x00,             # Reserved
    0x00, 0x00,       # Hotspot X
    0x00, 0x00,       # Hotspot Y
    0x30, 0x01, 0x00, 0x00,  # Size of image data (304 bytes)
    0x16, 0x00, 0x00, 0x00   # Offset to image data (22 bytes)
)
$bmpHeader = [byte[]]@(
    0x28, 0x00, 0x00, 0x00,  # Header size (40)
    0x20, 0x00, 0x00, 0x00,  # Width (32)
    0x40, 0x00, 0x00, 0x00,  # Height (64 = 32*2 for XOR+AND masks)
    0x01, 0x00,              # Planes (1)
    0x01, 0x00,              # Bits per pixel (1)
    0x00, 0x00, 0x00, 0x00,  # Compression (none)
    0x00, 0x01, 0x00, 0x00,  # Image size (256 bytes)
    0x00, 0x00, 0x00, 0x00,  # X pixels per meter
    0x00, 0x00, 0x00, 0x00,  # Y pixels per meter
    0x00, 0x00, 0x00, 0x00,  # Colors used
    0x00, 0x00, 0x00, 0x00   # Important colors
)
$colorTable = [byte[]]@(
    0x00, 0x00, 0x00, 0x00,  # Black (BGRX)
    0xFF, 0xFF, 0xFF, 0x00   # White (BGRX)
)
$xorMask = New-Object byte[] 128
$andMask = New-Object byte[] 128
for ($i = 0; $i -lt 128; $i++) { $andMask[$i] = 0xFF }

$cursorData = $curHeader + $curDirEntry + $bmpHeader + $colorTable + $xorMask + $andMask
[System.IO.File]::WriteAllBytes($blankCursorPath, $cursorData)
Write-Host "  Created blank cursor at $blankCursorPath"

# Script that applies invisible cursor in the user's interactive session
$cursorScript = @'
$blankCursorPath = 'C:\Windows\Cursors\blank.cur'
if (-not (Test-Path $blankCursorPath)) {
    "Blank cursor not found: $blankCursorPath" | Out-File -FilePath "C:\novnc\cursor.log" -Append -Encoding UTF8
    exit 1
}

$cursorTypes = @(
    'Arrow', 'Help', 'AppStarting', 'Wait', 'NWPen', 'No',
    'SizeNS', 'SizeWE', 'Crosshair', 'IBeam', 'SizeNWSE',
    'SizeNESW', 'SizeAll', 'UpArrow', 'Hand'
)

$regPath = 'HKCU:\Control Panel\Cursors'
if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }

foreach ($type in $cursorTypes) {
    Set-ItemProperty -Path $regPath -Name $type -Value $blankCursorPath -ErrorAction SilentlyContinue
}

Set-ItemProperty -Path $regPath -Name 'ContactVisualization' -Value 0 -Type DWord -ErrorAction SilentlyContinue
Set-ItemProperty -Path $regPath -Name 'GestureVisualization' -Value 0 -Type DWord -ErrorAction SilentlyContinue

$cursorHelperCode = @"
using System;
using System.Runtime.InteropServices;

public class CursorHelperLogon {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SystemParametersInfo(int uAction, int uParam, int lpvParam, int fuWinIni);

    public const int SPI_SETCURSORS = 0x0057;
    public const int SPIF_UPDATEINIFILE = 0x01;
    public const int SPIF_SENDCHANGE = 0x02;

    public static bool ApplyCursors() {
        return SystemParametersInfo(SPI_SETCURSORS, 0, 0, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE);
    }
}
"@

try { Add-Type -TypeDefinition $cursorHelperCode -Language CSharp -ErrorAction SilentlyContinue } catch {}

$result = [CursorHelperLogon]::ApplyCursors()
"$(Get-Date): Cursor applied, result: $result" | Out-File -FilePath "C:\novnc\cursor.log" -Append -Encoding UTF8
'@

$cursorScriptPath = "$novncDir\set-invisible-cursor.ps1"
$cursorScript | Out-File -FilePath $cursorScriptPath -Encoding UTF8
Write-Host "  Created cursor script at $cursorScriptPath"

$cursorTaskName = "SetInvisibleCursor"
$cursorAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"Start-Sleep -Seconds 2; & '$cursorScriptPath'`"" `
    -WorkingDirectory $novncDir
$cursorTrigger = New-ScheduledTaskTrigger -AtLogOn -User "unityuser"
$cursorPrincipal = New-ScheduledTaskPrincipal -UserId "unityuser" -LogonType Interactive -RunLevel Limited
$cursorSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $cursorTaskName -Action $cursorAction -Trigger $cursorTrigger -Principal $cursorPrincipal -Settings $cursorSettings | Out-Null
Write-Host "  Scheduled task '$cursorTaskName' created for unityuser (at logon)"

# =============================================================================
# Agent Service (scheduled task for interactive session)
# =============================================================================
Write-Host ""
Write-Host "=== Setting up Agent Service Scheduled Task ===" -ForegroundColor Cyan

$agentServiceDir = "C:\agent-service"
$startBat = @"
@echo off
set PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
cd /d C:\agent-service
npx --yes ts-node src/index.ts >> C:\Droid\agent-service.log 2>&1
"@
New-Item -ItemType Directory -Force -Path $agentServiceDir | Out-Null
$startBat | Out-File -FilePath "$agentServiceDir\start-agent.bat" -Encoding ASCII

$agentTaskName = "StartAgentService"
$agentAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$agentServiceDir\start-agent.bat'`"" `
    -WorkingDirectory $agentServiceDir
$agentTrigger = New-ScheduledTaskTrigger -AtLogOn -User "unityuser"
$agentPrincipal = New-ScheduledTaskPrincipal -UserId "unityuser" -LogonType Interactive -RunLevel Limited
$agentSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $agentTaskName -Action $agentAction -Trigger $agentTrigger -Principal $agentPrincipal -Settings $agentSettings | Out-Null
Disable-ScheduledTask -TaskName $agentTaskName | Out-Null
Write-Host "  Scheduled task '$agentTaskName' created for unityuser (disabled until assign)"

# =============================================================================
# Patchright Chromium (pre-installed for faster first assign)
# =============================================================================
Write-Host ""
Write-Host "=== Installing Patchright Chromium ===" -ForegroundColor Cyan

[System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', 'C:\ms-playwright', 'Machine')
$env:PLAYWRIGHT_BROWSERS_PATH = 'C:\ms-playwright'
New-Item -ItemType Directory -Force -Path 'C:\ms-playwright' | Out-Null
$npxCmd = 'C:\Program Files\nodejs\npx.cmd'
if (Test-Path $npxCmd) {
    Write-Host "  Running patchright install..."
    try {
        $output = cmd /c "`"$npxCmd`" --yes patchright install chromium 2>&1"
        $output | ForEach-Object { Write-Host "  $_" }
        Write-Host "  Patchright Chromium installed at C:\ms-playwright"
    } catch {
        Write-Host "  WARNING: Patchright install failed: $_" -ForegroundColor Yellow
        Write-Host "  Chromium will be installed on first assignment instead" -ForegroundColor Yellow
    }
} else {
    Write-Host "  WARNING: npx not found, skipping Patchright install" -ForegroundColor Yellow
}

# =============================================================================
# Pool Watcher (NSSM Windows service)
# =============================================================================
Write-Host ""
Write-Host "=== Installing Droid Pool Watcher ===" -ForegroundColor Cyan

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    choco install nssm -y --no-progress 2>$null
    if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
        throw "NSSM installation failed - pool watcher service cannot be registered without it"
    }
    Write-Host "  NSSM installed"
}

if (Test-Path "C:\temp\droid-pool-watcher.ps1") {
    Copy-Item "C:\temp\droid-pool-watcher.ps1" "C:\droid-pool-watcher.ps1" -Force
    Write-Host "  Watcher script installed at C:\droid-pool-watcher.ps1"
}

$nssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if ($nssmPath) {
    & $nssmPath install UnityPoolWatcher powershell.exe "-ExecutionPolicy Bypass -File C:\droid-pool-watcher.ps1"
    & $nssmPath set UnityPoolWatcher Start SERVICE_AUTO_START
    & $nssmPath set UnityPoolWatcher AppStdout "C:\Droid\pool-watcher.log"
    & $nssmPath set UnityPoolWatcher AppStderr "C:\Droid\pool-watcher.log"
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
Write-Host "  - Pool user: unityuser (auto-logon, standard user)"
Write-Host "  - OpenSSH Server (port 2222)"
Write-Host "  - TightVNC Server (dummy password, updated at assignment)"
Write-Host "  - Display resolution (1920x1080 at logon)"
Write-Host "  - Invisible cursor (transparent for clean VNC streaming)"
Write-Host "  - Agent Service scheduled task (interactive, disabled until assign)"
Write-Host "  - Patchright Chromium (C:\ms-playwright)"
Write-Host "  - Pool watcher: UnityPoolWatcher service (NSSM)"
Write-Host ""
