# install-base.ps1 - Base image provisioning for Packer
#
# This script installs static software that doesn't need secrets or instance-specific config.
# It runs during Packer image build, NOT at VM startup.
#
# INSTALLED BY THIS SCRIPT:
#   - Microsoft Office LTSC 2024 (Word, Excel, PowerPoint) - NOT activated
#   - Git for Windows
#   - Python 3.12
#   - Chocolatey
#   - Node.js v22 + npm
#   - Bun
#   - noVNC + websockify (pip package)
#   - Caddy (binary only, no Caddyfile)
#   - Firewall rules for Droid services
#
# NOT INSTALLED (handled by startup script at instance boot):
#   - TightVNC (needs password at install time)
#   - Magnitude / Agent Service repos (needs github-token, staging flag)
#   - Windows user setup / auto-logon
#   - Caddyfile (needs hostname)
#   - Agent Service .env (needs API keys)
#   - Display resolution setup
#   - Service startup
#
# RDP: Already enabled on GCP Windows images - NOT TOUCHED by this script
#
# Usage (via Packer):
#   This script is uploaded and executed by Packer during image build.
#   See windows-vm.pkr.hcl for the Packer configuration.

Write-Host "=========================================="
Write-Host "  Windows VM Base Image Provisioning"
Write-Host "=========================================="
Write-Host ""
Write-Host "Start time: $(Get-Date)"
Write-Host ""

# =============================================================================
# Executable Paths (use full paths to avoid PATH issues during Packer build)
# =============================================================================

$script:GitExe = 'C:\Program Files\Git\bin\git.exe'
$script:PythonExe = 'C:\Program Files\Python312\python.exe'
$script:ChocoExe = 'C:\ProgramData\chocolatey\bin\choco.exe'
$script:NodeExe = 'C:\Program Files\nodejs\node.exe'
$script:NpmCmd = 'C:\Program Files\nodejs\npm.cmd'
$script:NpxCmd = 'C:\Program Files\nodejs\npx.cmd'
$script:BunExe = "$env:USERPROFILE\.bun\bin\bun.exe"

# =============================================================================
# Helper Functions
# =============================================================================

function Install-Git {
    Write-Host ""
    Write-Host "=== Installing Git CLI ===" -ForegroundColor Cyan

    # Check using full path
    if (Test-Path $script:GitExe) {
        $gitVersion = & $script:GitExe --version
        Write-Host "Git already installed - $gitVersion" -ForegroundColor Green
        return
    }

    Write-Host "Git not found. Installing Git for Windows..."

    $gitInstallerUrl = 'https://github.com/git-for-windows/git/releases/download/v2.43.0.windows.1/Git-2.43.0-64-bit.exe'
    $gitInstallerPath = 'C:\temp\git-installer.exe'

    New-Item -ItemType Directory -Force -Path C:\temp | Out-Null

    Write-Host "Downloading Git for Windows..."
    Write-Host "URL: $gitInstallerUrl"
    Invoke-WebRequest -Uri $gitInstallerUrl -OutFile $gitInstallerPath

    Write-Host "Installing Git (silent install)..."
    $gitProcess = Start-Process -FilePath $gitInstallerPath -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS="icons,ext\reg\shellhere,assoc,assoc_sh"' -PassThru -Wait -NoNewWindow

    Write-Host "Git installation exit code: $($gitProcess.ExitCode)"

    # Verify Git installation using full path
    if (Test-Path $script:GitExe) {
        $gitVersion = & $script:GitExe --version
        Write-Host "SUCCESS: Git installed - $gitVersion" -ForegroundColor Green
        Write-Host "  Path: $script:GitExe" -ForegroundColor Gray
    } else {
        Write-Host "ERROR: Git not found at expected location: $script:GitExe" -ForegroundColor Red
        throw "Git installation failed"
    }

    # Cleanup installer
    Remove-Item $gitInstallerPath -Force -ErrorAction SilentlyContinue
}

function Install-Python {
    Write-Host ""
    Write-Host "=== Installing Python 3 ===" -ForegroundColor Cyan

    # Check using full path
    if (Test-Path $script:PythonExe) {
        $pythonVersion = & $script:PythonExe --version 2>&1
        Write-Host "Python already installed - $pythonVersion" -ForegroundColor Green
        return
    }

    Write-Host "Python not found. Installing Python 3.12.2..."

    $pythonInstallerUrl = 'https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe'
    $pythonInstallerPath = 'C:\temp\python-installer.exe'

    New-Item -ItemType Directory -Force -Path C:\temp | Out-Null

    Write-Host "Downloading Python 3.12.2..."
    Write-Host "URL: $pythonInstallerUrl"
    Invoke-WebRequest -Uri $pythonInstallerUrl -OutFile $pythonInstallerPath

    Write-Host "Installing Python (silent install with PATH)..."
    $pythonProcess = Start-Process -FilePath $pythonInstallerPath -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -PassThru -Wait -NoNewWindow

    Write-Host "Python installation exit code: $($pythonProcess.ExitCode)"

    # Verify Python installation using full path
    if (Test-Path $script:PythonExe) {
        $pythonVersion = & $script:PythonExe --version 2>&1
        Write-Host "SUCCESS: Python installed - $pythonVersion" -ForegroundColor Green
        Write-Host "  Path: $script:PythonExe" -ForegroundColor Gray

        # Upgrade pip using full path
        Write-Host "Upgrading pip..."
        & $script:PythonExe -m pip install --upgrade pip
    } else {
        Write-Host "ERROR: Python not found at expected location: $script:PythonExe" -ForegroundColor Red
        throw "Python installation failed"
    }

    # Cleanup installer
    Remove-Item $pythonInstallerPath -Force -ErrorAction SilentlyContinue
}

function Install-Chocolatey {
    Write-Host ""
    Write-Host "=== Installing Chocolatey ===" -ForegroundColor Cyan

    # Check using full path
    if (Test-Path $script:ChocoExe) {
        $chocoVersion = & $script:ChocoExe --version
        Write-Host "Chocolatey already installed - v$chocoVersion" -ForegroundColor Green
        return
    }

    Write-Host "Installing Chocolatey package manager..."

    # Set execution policy for this process
    Set-ExecutionPolicy Bypass -Scope Process -Force

    # Install Chocolatey
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

    # Verify Chocolatey installation using full path
    if (Test-Path $script:ChocoExe) {
        $chocoVersion = & $script:ChocoExe --version
        Write-Host "SUCCESS: Chocolatey installed - v$chocoVersion" -ForegroundColor Green
        Write-Host "  Path: $script:ChocoExe" -ForegroundColor Gray
    } else {
        Write-Host "ERROR: Chocolatey not found at expected location: $script:ChocoExe" -ForegroundColor Red
        throw "Chocolatey installation failed"
    }
}

function Install-NodeJS {
    Write-Host ""
    Write-Host "=== Installing Node.js v22, Bun, and npx ===" -ForegroundColor Cyan

    # Check if Node.js is already installed using full path
    if (Test-Path $script:NodeExe) {
        $nodeVersion = & $script:NodeExe --version
        Write-Host "Node.js already installed - $nodeVersion" -ForegroundColor Green
    } else {
        Write-Host "Installing Node.js v22 via Chocolatey..."

        # Ensure Chocolatey is available using full path
        if (-not (Test-Path $script:ChocoExe)) {
            Write-Host "ERROR: Chocolatey not found at $script:ChocoExe" -ForegroundColor Red
            throw "Chocolatey required for Node.js installation"
        }

        & $script:ChocoExe install nodejs --version=22.12.0 -y --no-progress

        # Verify Node.js installation using full path
        if (Test-Path $script:NodeExe) {
            $nodeVersion = & $script:NodeExe --version
            Write-Host "SUCCESS: Node.js installed - $nodeVersion" -ForegroundColor Green
            Write-Host "  Path: $script:NodeExe" -ForegroundColor Gray
        } else {
            Write-Host "ERROR: Node.js not found at expected location: $script:NodeExe" -ForegroundColor Red
            throw "Node.js installation failed"
        }
    }

    # Check npm using full path
    if (Test-Path $script:NpmCmd) {
        $npmVersion = & $script:NpmCmd --version
        Write-Host "npm available - v$npmVersion" -ForegroundColor Green
    } else {
        Write-Host "WARNING: npm not found at $script:NpmCmd" -ForegroundColor Yellow
    }

    # Check npx using full path
    if (Test-Path $script:NpxCmd) {
        Write-Host "npx available at $script:NpxCmd" -ForegroundColor Green
    } else {
        Write-Host "WARNING: npx not found at $script:NpxCmd" -ForegroundColor Yellow
    }

    # Install Bun
    Write-Host ""
    Write-Host "Installing Bun..."
    if (Test-Path $script:BunExe) {
        $bunVersion = & $script:BunExe --version
        Write-Host "Bun already installed - v$bunVersion" -ForegroundColor Green
    } else {
        # Install Bun via PowerShell installer (suppress errors about git not found)
        try {
            # Download and run bun installer, ignore non-critical errors
            $bunInstallScript = Invoke-RestMethod -Uri "https://bun.sh/install.ps1"
            # Run in a separate scope to isolate errors
            powershell -Command $bunInstallScript 2>&1 | ForEach-Object {
                if ($_ -notmatch 'git.*not recognized') {
                    Write-Host $_
                }
            }

            # Verify Bun installation using full path
            if (Test-Path $script:BunExe) {
                $bunVersion = & $script:BunExe --version
                Write-Host "SUCCESS: Bun installed - v$bunVersion" -ForegroundColor Green
                Write-Host "  Path: $script:BunExe" -ForegroundColor Gray
            } else {
                Write-Host "WARNING: Bun not found at $script:BunExe" -ForegroundColor Yellow
            }
        } catch {
            Write-Host "WARNING: Failed to install Bun - $_" -ForegroundColor Yellow
        }
    }
}

function Install-NoVNC-Base {
    Write-Host ""
    Write-Host "=== Installing noVNC and websockify (base) ===" -ForegroundColor Cyan

    $novncDir = 'C:\novnc'

    # Clone noVNC if not present
    if (Test-Path "$novncDir\vnc.html") {
        Write-Host "noVNC already installed at: $novncDir" -ForegroundColor Green
    } else {
        Write-Host "Cloning noVNC repository..."
        if (Test-Path $novncDir) {
            Remove-Item -Recurse -Force $novncDir -ErrorAction SilentlyContinue
        }

        # Use full path to git
        if (-not (Test-Path $script:GitExe)) {
            Write-Host "ERROR: Git not found at $script:GitExe" -ForegroundColor Red
            throw "Git required for noVNC installation"
        }

        & $script:GitExe clone --depth 1 https://github.com/novnc/noVNC.git $novncDir

        if (Test-Path "$novncDir\vnc.html") {
            Write-Host "SUCCESS: noVNC cloned" -ForegroundColor Green
        } else {
            Write-Host "ERROR: noVNC clone failed" -ForegroundColor Red
            throw "noVNC installation failed"
        }
    }

    # Create custom.html (iframe wrapper for clean noVNC experience)
    if (Test-Path "$novncDir\vnc.html") {
        $customHtml = @'
<!DOCTYPE html>
<html>
<head>
    <title>Desktop</title>
    <style>
        body, html { margin: 0; padding: 0; overflow: hidden; background: #000; }
        iframe { width: 100vw; height: 100vh; border: none; }
    </style>
</head>
<body>
    <iframe id="vnc" src=""></iframe>
    <script>
        const params = new URLSearchParams(window.location.search);
        params.set('resize', 'scale');
        params.set('autoconnect', '1');
        params.set('reconnect', '1');
        params.set('show_dot', '1');
        document.getElementById('vnc').src = `vnc.html?${params}`;

        // Inject CSS to hide control bar, logo, and remote cursor
        document.getElementById('vnc').onload = function() {
            try {
                const style = this.contentDocument.createElement('style');
                style.textContent = `
                    #noVNC_control_bar,
                    #noVNC_control_bar_anchor,
                    #noVNC_control_bar_handle,
                    #noVNC_logo,
                    #noVNC_status { display: none !important; }

                    /* Hide remote cursor - use local browser cursor only */
                    .noVNC_cursor { display: none !important; }
                `;
                this.contentDocument.head.appendChild(style);
            } catch (e) {
                console.warn('Could not inject CSS (cross-origin)', e);
            }
        };
    </script>
</body>
</html>
'@
        $customHtml | Out-File -FilePath "$novncDir\custom.html" -Encoding UTF8
        Copy-Item "$novncDir\custom.html" "$novncDir\index.html" -Force
        Write-Host "Created/updated custom.html and set as index.html" -ForegroundColor Green
    }

    # Install websockify via pip using full Python path
    Write-Host "Installing websockify via pip..."
    if (Test-Path $script:PythonExe) {
        & $script:PythonExe -m pip install websockify --quiet
        Write-Host "SUCCESS: websockify installed" -ForegroundColor Green
    } else {
        Write-Host "ERROR: Python not found at $script:PythonExe" -ForegroundColor Red
        throw "Python required for websockify installation"
    }

    # NOTE: Scheduled task for websockify auto-start is created by startup script
    # (needs to run in the user's session after user is created)
}

function Install-Caddy-Base {
    Write-Host ""
    Write-Host "=== Installing Caddy Web Server (base) ===" -ForegroundColor Cyan

    $caddyDir = 'C:\caddy'
    $caddyExe = "$caddyDir\caddy.exe"

    if (Test-Path $caddyExe) {
        Write-Host "Caddy already installed at: $caddyExe" -ForegroundColor Green
        return
    }

    New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null

    # Download Caddy for Windows
    Write-Host "Downloading Caddy..."
    $caddyUrl = "https://github.com/caddyserver/caddy/releases/download/v2.7.6/caddy_2.7.6_windows_amd64.zip"
    $caddyZip = "$caddyDir\caddy.zip"

    try {
        Invoke-WebRequest -Uri $caddyUrl -OutFile $caddyZip -UseBasicParsing

        Write-Host "Extracting Caddy..."
        Expand-Archive -Path $caddyZip -DestinationPath $caddyDir -Force
        Remove-Item $caddyZip -Force -ErrorAction SilentlyContinue

        if (Test-Path $caddyExe) {
            Write-Host "SUCCESS: Caddy installed at $caddyExe" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Caddy extraction may have failed" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "ERROR: Failed to download Caddy - $_" -ForegroundColor Red
    }

    # NOTE: Caddyfile is created by startup script (needs hostname from metadata)
}

function Install-Office-Base {
    Write-Host ""
    Write-Host "=== Installing Office LTSC 2024 (base) ===" -ForegroundColor Cyan

    $excelPath = 'C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE'

    if (Test-Path $excelPath) {
        Write-Host "Excel already installed at: $excelPath" -ForegroundColor Green
        return
    }

    Write-Host "Excel not found. Proceeding with installation..." -ForegroundColor Yellow

    # Step 1: Download Office Deployment Tool
    Write-Host ""
    Write-Host "Downloading Office Deployment Tool..." -ForegroundColor Cyan

    New-Item -ItemType Directory -Force -Path C:\odt | Out-Null
    $odtUrl = 'https://download.microsoft.com/download/2/7/A/27AF1BE6-DD20-4CB4-B154-EBAB8A7D4A7E/officedeploymenttool_18129-20030.exe'
    Write-Host "Downloading from: $odtUrl"
    Invoke-WebRequest -Uri $odtUrl -OutFile C:\odt\odt.exe
    Write-Host "Extracting ODT..."
    Start-Process -FilePath C:\odt\odt.exe -ArgumentList '/quiet /extract:C:\odt' -Wait -NoNewWindow
    Write-Host "ODT ready at C:\odt" -ForegroundColor Green

    # Step 2: Create Office Configuration File
    Write-Host ""
    Write-Host "Creating Office configuration file..." -ForegroundColor Cyan

    $configXml = @"
<Configuration>
  <Add OfficeClientEdition="64" Channel="PerpetualVL2024">
    <Product ID="ProPlus2024Volume">
      <Language ID="en-us" />
      <ExcludeApp ID="Access" />
      <ExcludeApp ID="Groove" />
      <ExcludeApp ID="Lync" />
      <ExcludeApp ID="OneDrive" />
      <ExcludeApp ID="OneNote" />
      <ExcludeApp ID="Outlook" />
      <ExcludeApp ID="Publisher" />
      <ExcludeApp ID="Teams" />
    </Product>
  </Add>
  <Display Level="None" AcceptEULA="TRUE" />
  <Logging Level="Standard" Path="C:\odt\logs" />
  <Property Name="AUTOACTIVATE" Value="0" />
  <Property Name="FORCEAPPSHUTDOWN" Value="TRUE" />
</Configuration>
"@

    $configXml | Out-File -FilePath C:\odt\config.xml -Encoding UTF8
    Write-Host "Configuration saved to C:\odt\config.xml" -ForegroundColor Green
    Write-Host "Components to install: Word, Excel, PowerPoint"
    Write-Host "Excluded: Access, Groove, Lync, OneDrive, OneNote, Outlook, Publisher, Teams"

    # Step 3: Install Office LTSC 2024
    Write-Host ""
    Write-Host "Installing Office LTSC 2024..." -ForegroundColor Cyan
    Write-Host "WARNING: This takes 20-40 minutes for download + install!" -ForegroundColor Yellow
    Write-Host "Start time: $(Get-Date)"

    # Create logs directory
    New-Item -ItemType Directory -Force -Path C:\odt\logs | Out-Null

    # Run setup with logging
    $process = Start-Process -FilePath 'C:\odt\setup.exe' -ArgumentList '/configure C:\odt\config.xml' -PassThru -Wait -NoNewWindow

    Write-Host ""
    Write-Host "End time: $(Get-Date)"
    Write-Host "Exit code: $($process.ExitCode)"

    # Check logs if available
    Get-ChildItem 'C:\odt\logs\*.log' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "=== Log: $($_.Name) ===" -ForegroundColor Gray
        Get-Content $_.FullName | Select-Object -Last 20
    }

    # Verify installation
    if (Test-Path $excelPath) {
        Write-Host ""
        Write-Host "SUCCESS: Excel installed at $excelPath" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "ERROR: Excel not found at expected path!" -ForegroundColor Red
        Write-Host "Checking alternative locations..."
        Get-ChildItem 'C:\Program Files\Microsoft Office' -Recurse -Filter 'EXCEL.EXE' -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "Found: $($_.FullName)" }
        Get-ChildItem 'C:\Program Files (x86)\Microsoft Office' -Recurse -Filter 'EXCEL.EXE' -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "Found: $($_.FullName)" }
        throw 'Excel not found!'
    }

    # Step 4: Configure Excel for headless automation
    Write-Host ""
    Write-Host "Configuring Excel for headless automation..." -ForegroundColor Cyan

    # Disable first-run dialogs and protected view
    $regPath = 'HKCU:\Software\Microsoft\Office\16.0\Excel\Security\ProtectedView'
    New-Item -Path $regPath -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'DisableInternetFilesInPV' -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name 'DisableAttachementsInPV' -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name 'DisableUnsafeLocationsInPV' -Value 1 -Type DWord

    # Disable startup screen
    $regPath = 'HKCU:\Software\Microsoft\Office\16.0\Common\General'
    New-Item -Path $regPath -Force | Out-Null
    Set-ItemProperty -Path $regPath -Name 'ShownFirstRunOptin' -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name 'DisableBootToOfficeStart' -Value 1 -Type DWord

    Write-Host "Excel configured for headless automation" -ForegroundColor Green

    # NOTE: Office activation is done by startup script if MAK key is provided via metadata
    Write-Host ""
    Write-Host "Office installed but NOT activated (activation done at instance boot if MAK key provided)" -ForegroundColor Yellow
}

function Disable-ServerManager-AutoStart {
    Write-Host ""
    Write-Host "=== Disabling Server Manager Auto-Start ===" -ForegroundColor Cyan

    # Disable the Server Manager scheduled task that launches it at logon
    $task = Get-ScheduledTask -TaskName "ServerManager" -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName "ServerManager" -ErrorAction SilentlyContinue | Out-Null
        Write-Host "Disabled ServerManager scheduled task" -ForegroundColor Green
    } else {
        Write-Host "ServerManager scheduled task not found (may not be Windows Server)" -ForegroundColor Yellow
    }

    # Also set registry key for all users to prevent Server Manager at logon
    # This applies to any user that logs in
    $regPath = 'HKLM:\SOFTWARE\Microsoft\ServerManager'
    if (-not (Test-Path $regPath)) {
        New-Item -Path $regPath -Force | Out-Null
    }
    Set-ItemProperty -Path $regPath -Name 'DoNotOpenServerManagerAtLogon' -Value 1 -Type DWord -Force
    Write-Host "Set DoNotOpenServerManagerAtLogon registry key (machine-wide)" -ForegroundColor Green

    # Set for default user profile (applies to newly created users)
    $defaultUserReg = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    # Note: The per-user setting would be at HKCU, but we can't set that for future users during Packer build
    # The machine-wide setting above and disabled scheduled task should handle it

    Write-Host "Server Manager auto-start disabled" -ForegroundColor Green
}

function Configure-Firewall-Base {
    Write-Host ""
    Write-Host "=== Configuring Firewall (base) ===" -ForegroundColor Cyan

    # ==========================================================================
    # RDP (port 3389) is already configured by Windows/GCP — untouched.
    # Ports 6080 (noVNC) and 3000 (Agent Service) are NOT exposed directly;
    # they are only reachable via Caddy's reverse proxy on port 443.
    # ==========================================================================

    # Remove old Droid rules if they exist (to update them)
    Remove-NetFirewallRule -DisplayName "Droid-noVNC" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Droid-HTTPS" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Droid-HTTP" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Droid-VNC-Local" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Droid-AgentService" -ErrorAction SilentlyContinue

    New-NetFirewallRule -DisplayName "Droid-HTTPS" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow HTTPS (443)" -ForegroundColor Green

    Write-Host ""
    Write-Host "RDP (3389): Preserved from Windows/GCP defaults (not modified)" -ForegroundColor Cyan
    Write-Host "Firewall configured" -ForegroundColor Green
}

# =============================================================================
# Main Installation Flow
# =============================================================================

Write-Host "Starting base image provisioning..." -ForegroundColor Cyan
Write-Host ""

# Run installations in order
# Each function checks if software is already installed and skips if so
Install-Git
Install-Python
Install-Chocolatey
Install-NodeJS
Install-NoVNC-Base      # noVNC repo + websockify pip (no scheduled task - that's done at startup)
Install-Caddy-Base      # Caddy binary only (no Caddyfile - that's created at startup with hostname)
Install-Office-Base     # Office without activation (activation done at startup with MAK key)
Configure-Firewall-Base # Droid firewall rules (RDP untouched)
Disable-ServerManager-AutoStart  # Prevent Server Manager GUI from opening at logon

# Note: Cleanup is done by Packer after this script completes
# See windows-vm.pkr.hcl for the cleanup provisioner

Write-Host ""
Write-Host "=========================================="
Write-Host "  Base Image Provisioning Complete!"
Write-Host "=========================================="
Write-Host ""
Write-Host "End time: $(Get-Date)"
Write-Host ""
Write-Host "Installed components:" -ForegroundColor Cyan
Write-Host "  - Git for Windows"
Write-Host "  - Python 3.12 + pip"
Write-Host "  - Chocolatey"
Write-Host "  - Node.js v22 + npm"
Write-Host "  - Bun"
Write-Host "  - noVNC + websockify"
Write-Host "  - Caddy (binary only)"
Write-Host "  - Microsoft Office LTSC 2024 (Word, Excel, PowerPoint) - NOT activated"
Write-Host "  - Firewall rules (port 443 only — 6080/3000 behind Caddy)"
Write-Host "  - Server Manager auto-start disabled"
Write-Host ""
Write-Host "Preserved from GCP Windows image:" -ForegroundColor Cyan
Write-Host "  - RDP (port 3389) - always available for remote access"
Write-Host ""
Write-Host "NOT installed (handled by startup script at instance boot):" -ForegroundColor Yellow
Write-Host "  - TightVNC Server (needs password)"
Write-Host "  - Magnitude / Agent Service repos (needs github-token)"
Write-Host "  - Windows user / auto-logon (needs credentials)"
Write-Host "  - Caddyfile (needs hostname)"
Write-Host "  - Agent Service .env (needs API keys)"
Write-Host "  - Service startup"
Write-Host "  - Office activation (needs MAK key)"
Write-Host ""
Write-Host "Use the existing windows-vm-startup.ps1 script at instance creation." -ForegroundColor Cyan
Write-Host ""
