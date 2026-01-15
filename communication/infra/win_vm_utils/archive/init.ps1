# init2024.ps1 - Office LTSC 2024 Installation Script
# Run this manually on a Windows VM to install Excel + Office components
# Usage: powershell -ExecutionPolicy Bypass -File init2024.ps1
# Usage with MAK: powershell -ExecutionPolicy Bypass -File init2024.ps1 "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX"

Write-Host "=========================================="
Write-Host "  Office LTSC 2024 Installation Script"
Write-Host "=========================================="
Write-Host ""

# =============================================================================
# Helper Functions
# =============================================================================

function Install-Git {
    Write-Host ""
    Write-Host "=== Installing Git CLI ===" -ForegroundColor Cyan
    
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $gitVersion = git --version
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
    
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    # Verify Git installation
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $gitVersion = git --version
        Write-Host "SUCCESS: Git installed - $gitVersion" -ForegroundColor Green
    } else {
        Write-Host "Git installed but not in PATH. You may need to restart your terminal." -ForegroundColor Yellow
        Write-Host "Expected location: C:\Program Files\Git\bin\git.exe"
        if (Test-Path 'C:\Program Files\Git\bin\git.exe') {
            Write-Host "Git executable found at expected location." -ForegroundColor Green
        }
    }
    
    # Cleanup installer
    Remove-Item $gitInstallerPath -Force -ErrorAction SilentlyContinue
}

function Install-Python {
    Write-Host ""
    Write-Host "=== Installing Python 3 ===" -ForegroundColor Cyan
    
    # Check if real Python is installed (not Windows Store stub)
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $checkPythonPath = (Get-Command python).Source
        if ($checkPythonPath -notlike '*\WindowsApps\*') {
            $pythonVersionOutput = python --version 2>&1
            if ($pythonVersionOutput -match '^Python \d+\.\d+') {
                Write-Host "Python already installed - $pythonVersionOutput" -ForegroundColor Green
                return
            }
        }
    }
    
    Write-Host "Python not found (or only Windows Store stub). Installing Python 3.12.2..."
    
    # Note about Windows Store alias
    $aliasPath = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
    if (Test-Path "$aliasPath\python.exe") {
        Write-Host "Note: Windows Store Python alias detected - real Python will take precedence after install." -ForegroundColor Gray
    }
    
    $pythonInstallerUrl = 'https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe'
    $pythonInstallerPath = 'C:\temp\python-installer.exe'
    
    New-Item -ItemType Directory -Force -Path C:\temp | Out-Null
    
    Write-Host "Downloading Python 3.12.2..."
    Write-Host "URL: $pythonInstallerUrl"
    Invoke-WebRequest -Uri $pythonInstallerUrl -OutFile $pythonInstallerPath
    
    Write-Host "Installing Python (silent install with PATH)..."
    $pythonProcess = Start-Process -FilePath $pythonInstallerPath -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -PassThru -Wait -NoNewWindow
    
    Write-Host "Python installation exit code: $($pythonProcess.ExitCode)"
    
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    # Verify Python installation
    $realPythonPath = 'C:\Program Files\Python312\python.exe'
    if (Test-Path $realPythonPath) {
        $pythonVersion = & $realPythonPath --version 2>&1
        Write-Host "SUCCESS: Python installed - $pythonVersion" -ForegroundColor Green
        
        # Upgrade pip
        Write-Host "Upgrading pip..."
        & $realPythonPath -m pip install --upgrade pip
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $checkPath = (Get-Command python).Source
        if ($checkPath -notlike '*\WindowsApps\*') {
            $pythonVersion = python --version 2>&1
            Write-Host "SUCCESS: Python installed - $pythonVersion" -ForegroundColor Green
            
            # Upgrade pip
            Write-Host "Upgrading pip..."
            python -m pip install --upgrade pip
        }
    } else {
        Write-Host "Python installed but not in PATH. You may need to restart your terminal." -ForegroundColor Yellow
        Write-Host "Expected location: C:\Program Files\Python312\python.exe"
        if (Test-Path $realPythonPath) {
            Write-Host "Python executable found at expected location." -ForegroundColor Green
        }
    }
    
    # Cleanup installer
    Remove-Item $pythonInstallerPath -Force -ErrorAction SilentlyContinue
}

function Install-TightVNC {
    param([string]$Password = "unify123")
    
    Write-Host ""
    Write-Host "=== Installing TightVNC Server ===" -ForegroundColor Cyan
    
    $tvnServerPath = 'C:\Program Files\TightVNC\tvnserver.exe'
    
    if (Test-Path $tvnServerPath) {
        Write-Host "TightVNC already installed at: $tvnServerPath" -ForegroundColor Green
    } else {
        Write-Host "Using VNC password: $Password" -ForegroundColor Yellow
        
        $vncInstallerUrl = 'https://www.tightvnc.com/download/2.8.81/tightvnc-2.8.81-gpl-setup-64bit.msi'
        $vncInstallerPath = 'C:\temp\tightvnc.msi'
        
        New-Item -ItemType Directory -Force -Path C:\temp | Out-Null
        
        Write-Host "Downloading TightVNC..."
        Write-Host "URL: $vncInstallerUrl"
        Invoke-WebRequest -Uri $vncInstallerUrl -OutFile $vncInstallerPath -UseBasicParsing
        
        Write-Host "Installing TightVNC (silent)..."
        $vncArgs = @(
            "/i", $vncInstallerPath,
            "/quiet", "/norestart",
            "ADDLOCAL=Server",
            "SET_USEVNCAUTHENTICATION=1",
            "VALUE_OF_USEVNCAUTHENTICATION=1",
            "SET_PASSWORD=1",
            "VALUE_OF_PASSWORD=$Password",
            "SET_USECONTROLAUTHENTICATION=1",
            "VALUE_OF_USECONTROLAUTHENTICATION=1",
            "SET_CONTROLPASSWORD=1",
            "VALUE_OF_CONTROLPASSWORD=$Password"
        )
        $vncProcess = Start-Process msiexec.exe -ArgumentList $vncArgs -Wait -NoNewWindow -PassThru
        
        Write-Host "TightVNC installation exit code: $($vncProcess.ExitCode)"
        
        if (Test-Path $tvnServerPath) {
            Write-Host "SUCCESS: TightVNC installed" -ForegroundColor Green
        } else {
            Write-Host "WARNING: TightVNC may not have installed correctly" -ForegroundColor Yellow
        }
        
        # Cleanup
        Remove-Item $vncInstallerPath -Force -ErrorAction SilentlyContinue
    }
    
    # Configure TightVNC registry settings (required for proper operation)
    # Note: App-mode may read from HKCU, service mode reads from HKLM
    Write-Host "Configuring TightVNC settings..."
    $regPaths = @(
        'HKLM:\SOFTWARE\TightVNC\Server',
        'HKLM:\SOFTWARE\WOW6432Node\TightVNC\Server',
        'HKCU:\SOFTWARE\TightVNC\Server',
        'HKCU:\SOFTWARE\WOW6432Node\TightVNC\Server'
    )
    foreach ($regPath in $regPaths) {
        # Create the path if it doesn't exist (especially for HKCU)
        if (-not (Test-Path $regPath)) {
            New-Item -Path $regPath -Force | Out-Null
        }
        # Enable loopback connections (required for websockify to connect via localhost)
        Set-ItemProperty -Path $regPath -Name 'AllowLoopback' -Value 1 -Type DWord -Force
        # Accept RFB connections (required to transmit screen data)
        Set-ItemProperty -Path $regPath -Name 'AcceptRfbConnections' -Value 1 -Type DWord -Force
        # Use VNC authentication
        Set-ItemProperty -Path $regPath -Name 'UseVncAuthentication' -Value 1 -Type DWord -Force
        # Don't query if no password
        Set-ItemProperty -Path $regPath -Name 'QueryIfNoPassword' -Value 0 -Type DWord -Force
        # Set RFB port
        Set-ItemProperty -Path $regPath -Name 'RfbPort' -Value 5900 -Type DWord -Force
        Write-Host "  Configured: $regPath" -ForegroundColor Green
    }
    Write-Host "TightVNC settings configured" -ForegroundColor Green
    
    # Start TightVNC service briefly to ensure password is written to HKLM registry
    # The MSI installer writes the encrypted password to HKLM when the service starts
    Write-Host "Starting TightVNC service (Session 0) to initialize password..."
    Set-Service -Name "tvnserver" -StartupType Manual -ErrorAction SilentlyContinue
    Start-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    
    # Copy encrypted password from HKLM to HKCU (app-mode reads from HKCU)
    Write-Host "Copying encrypted password from HKLM to HKCU..."
    $hklmPath = 'HKLM:\SOFTWARE\TightVNC\Server'
    $hkcuPath = 'HKCU:\SOFTWARE\TightVNC\Server'
    
    # Ensure HKCU path exists
    if (-not (Test-Path $hkcuPath)) {
        New-Item -Path $hkcuPath -Force | Out-Null
    }
    
    # Copy Password (encrypted binary value) - use Get-ItemPropertyValue for proper binary handling
    try {
        $passwordBytes = Get-ItemPropertyValue -Path $hklmPath -Name 'Password' -ErrorAction Stop
        Set-ItemProperty -Path $hkcuPath -Name 'Password' -Value $passwordBytes -Type Binary -Force
        Write-Host "  Password copied" -ForegroundColor Green
    } catch {
        Write-Host "  WARNING: No Password found in HKLM - $_" -ForegroundColor Yellow
    }
    
    # Copy ControlPassword (encrypted binary value)
    try {
        $controlPwdBytes = Get-ItemPropertyValue -Path $hklmPath -Name 'ControlPassword' -ErrorAction Stop
        Set-ItemProperty -Path $hkcuPath -Name 'ControlPassword' -Value $controlPwdBytes -Type Binary -Force
        Write-Host "  ControlPassword copied" -ForegroundColor Green
    } catch {
        Write-Host "  WARNING: No ControlPassword found in HKLM - $_" -ForegroundColor Yellow
    }
    
    Start-Sleep -Seconds 2
    
    # Stop and disable the TightVNC service (we'll run in app-mode instead)
    # App-mode runs in the user's session and can capture the interactive desktop
    # Service mode runs in Session 0 and cannot see the user's desktop
    Write-Host "Stopping TightVNC service (will use app-mode instead)..."
    Stop-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    Set-Service -Name "tvnserver" -StartupType Disabled -ErrorAction SilentlyContinue
    Write-Host "TightVNC service disabled" -ForegroundColor Green
}

function Install-NoVNC {
    Write-Host ""
    Write-Host "=== Installing noVNC and websockify ===" -ForegroundColor Cyan
    
    $novncDir = 'C:\novnc'
    
    # Clone noVNC if not present
    if (Test-Path "$novncDir\vnc.html") {
        Write-Host "noVNC already installed at: $novncDir" -ForegroundColor Green
    } else {
        Write-Host "Cloning noVNC repository..."
        if (Test-Path $novncDir) {
            Remove-Item -Recurse -Force $novncDir -ErrorAction SilentlyContinue
        }
        
        git clone --depth 1 https://github.com/novnc/noVNC.git $novncDir
        
        if (Test-Path "$novncDir\vnc.html") {
            Write-Host "SUCCESS: noVNC installed" -ForegroundColor Green
            
            # Create index.html as a copy of vnc_lite.html for easier access
            Copy-Item "$novncDir\vnc_lite.html" "$novncDir\index.html" -Force
            Write-Host "Created index.html from vnc_lite.html" -ForegroundColor Green
        } else {
            Write-Host "WARNING: noVNC may not have installed correctly" -ForegroundColor Yellow
        }
    }
    
    # Install websockify via pip
    Write-Host "Installing websockify via pip..."
    $pythonExe = 'C:\Program Files\Python312\python.exe'
    if (Test-Path $pythonExe) {
        & $pythonExe -m pip install websockify --quiet
        Write-Host "SUCCESS: websockify installed" -ForegroundColor Green
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m pip install websockify --quiet
        Write-Host "SUCCESS: websockify installed" -ForegroundColor Green
    } else {
        Write-Host "ERROR: Python not found, cannot install websockify" -ForegroundColor Red
    }
}


function Setup-Websockify {
    Write-Host ""
    Write-Host "=== Setting up websockify ===" -ForegroundColor Cyan
    
    $novncDir = 'C:\novnc'
    
    # Find Python executable
    $pythonExe = 'C:\Program Files\Python312\python.exe'
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
    if (-not $pythonExe -or -not (Test-Path $pythonExe)) {
        Write-Host "ERROR: Python not found" -ForegroundColor Red
        return
    }
    Write-Host "Using Python: $pythonExe" -ForegroundColor Green
    
    # Create websockify startup script
    $websockifyScript = @"
@echo off
cd /d C:\novnc
"$pythonExe" -m websockify --web C:\novnc 6080 localhost:5900
"@
    $websockifyScript | Out-File -FilePath "$novncDir\start-websockify.bat" -Encoding ASCII
    Write-Host "Created websockify startup script at $novncDir\start-websockify.bat" -ForegroundColor Green
    
    # Create a scheduled task to run websockify at login (for persistence)
    $taskName = "StartWebsockify"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for websockify auto-start..."
    $action = New-ScheduledTaskAction -Execute "$novncDir\start-websockify.bat" -WorkingDirectory $novncDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Write-Host "Scheduled task '$taskName' created for auto-start at login" -ForegroundColor Green
}

function Configure-Firewall {
    Write-Host ""
    Write-Host "=== Configuring Firewall ===" -ForegroundColor Cyan
    
    # Remove old rules if they exist (to update them)
    Remove-NetFirewallRule -DisplayName "Unity-noVNC" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-HTTPS" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-HTTP" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-VNC-Local" -ErrorAction SilentlyContinue
    
    # Allow noVNC/websockify (6080)
    New-NetFirewallRule -DisplayName "Unity-noVNC" -Direction Inbound -LocalPort 6080 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow noVNC (6080)" -ForegroundColor Green
    
    Write-Host "Firewall configured" -ForegroundColor Green
}

function Start-AllServices {
    Write-Host ""
    Write-Host "=== Starting Services ===" -ForegroundColor Cyan
    
    # Stop TightVNC service if running (we use app-mode instead)
    Stop-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    
    # Kill any existing TightVNC app-mode process
    Get-Process -Name "tvnserver" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    
    # Start TightVNC in app-mode (runs in user session, can capture desktop)
    $tvnExe = 'C:\Program Files\TightVNC\tvnserver.exe'
    if (-not (Test-Path $tvnExe)) { $tvnExe = 'C:\Program Files (x86)\TightVNC\tvnserver.exe' }
    
    Write-Host "Starting TightVNC in app-mode..."
    Start-Process -FilePath $tvnExe -ArgumentList "-run" -WindowStyle Hidden
    Start-Sleep -Seconds 2
    
    # Reload TightVNC settings from registry
    Write-Host "Reloading TightVNC settings..."
    Start-Process -FilePath $tvnExe -ArgumentList "-controlapp", "-reload" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    
    # Check if TightVNC is running
    $tvnProcess = Get-Process -Name "tvnserver" -ErrorAction SilentlyContinue
    if ($tvnProcess) {
        Write-Host "  TightVNC (app-mode) : Running" -ForegroundColor Green
    } else {
        Write-Host "  TightVNC (app-mode) : Not Running" -ForegroundColor Red
    }
    
    # Kill any existing websockify process
    Get-Process -Name "python*" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*websockify*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    
    # Start websockify as background process
    Write-Host "Starting websockify..."
    $novncDir = 'C:\novnc'
    $websockifyBat = "$novncDir\start-websockify.bat"
    
    if (Test-Path $websockifyBat) {
        Start-Process -FilePath $websockifyBat -WorkingDirectory $novncDir -WindowStyle Hidden
        Start-Sleep -Seconds 3
        
        # Check if websockify is running (check if port 6080 is listening)
        $listening = netstat -an | Select-String ":6080.*LISTENING"
        if ($listening) {
            Write-Host "  websockify : Running (port 6080 listening)" -ForegroundColor Green
        } else {
            Write-Host "  websockify : Not listening on port 6080" -ForegroundColor Yellow
            Write-Host "  Check logs or try running manually: $websockifyBat" -ForegroundColor Gray
        }
    } else {
        Write-Host "  websockify : Startup script not found at $websockifyBat" -ForegroundColor Red
    }
}

function Install-Office {
    param([string]$MakKey)
    
    Write-Host ""
    Write-Host "=== Installing Office LTSC 2024 ===" -ForegroundColor Cyan
    
    $excelPath = 'C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE'
    
    if (Test-Path $excelPath) {
        Write-Host "Excel already installed at: $excelPath" -ForegroundColor Green
        Activate-Office -MakKey $MakKey
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
    
    # Activate Office
    Activate-Office -MakKey $MakKey
    
    # Cleanup note
    Write-Host ""
    Write-Host "ODT files kept at C:\odt (delete manually if desired)" -ForegroundColor Gray
}

function Activate-Office {
    param([string]$MakKey)
    
    Write-Host ""
    Write-Host "=== Office Activation ===" -ForegroundColor Cyan
    
    $ospp = 'C:\Program Files\Microsoft Office\root\Office16\ospp.vbs'
    
    if ($MakKey) {
        Write-Host "Activating Office with MAK key..."
        if (Test-Path $ospp) {
            Write-Host "Installing product key..."
            cscript //nologo $ospp /inpkey:$MakKey
            Write-Host "Activating..."
            cscript //nologo $ospp /act
            Write-Host "Checking activation status..."
            cscript //nologo $ospp /dstatus
        } else {
            Write-Host "ERROR: ospp.vbs not found at $ospp" -ForegroundColor Red
        }
    } else {
        Write-Host "No MAK key provided. Checking current activation status..." -ForegroundColor Yellow
        if (Test-Path $ospp) {
            cscript //nologo $ospp /dstatus
        }
        Write-Host ""
        Write-Host "To activate later, run:" -ForegroundColor Yellow
        Write-Host '  cscript "C:\Program Files\Microsoft Office\root\Office16\ospp.vbs" /inpkey:YOUR-MAK-KEY' -ForegroundColor Gray
        Write-Host '  cscript "C:\Program Files\Microsoft Office\root\Office16\ospp.vbs" /act' -ForegroundColor Gray
    }
}

function Show-Summary {
    param(
        [string]$MakKey,
        [string]$VncPassword
    )
    
    Write-Host ""
    Write-Host "=========================================="
    Write-Host "  Installation Complete!"
    Write-Host "=========================================="
    Write-Host ""
    Write-Host "Installed components:"
    Write-Host "  - Microsoft Word"
    Write-Host "  - Microsoft Excel"
    Write-Host "  - Microsoft PowerPoint"
    Write-Host "  - Git CLI"
    Write-Host "  - Python 3"
    Write-Host "  - TightVNC Server (port 5900)"
    Write-Host "  - noVNC + websockify (port 6080)"
    Write-Host ""
    Write-Host "Paths:"
    Write-Host "  Excel:      C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
    Write-Host "  Git:        C:\Program Files\Git\bin\git.exe"
    Write-Host "  Python:     C:\Program Files\Python312\python.exe"
    Write-Host "  noVNC:      C:\novnc"
    Write-Host ""
    Write-Host "Logs:"
    Write-Host "  websockify: C:\novnc\websockify.log"
    Write-Host ""
    
    # Access information
    $ipAddress = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback" } | Select-Object -First 1).IPAddress
    Write-Host "Access noVNC Desktop:" -ForegroundColor Cyan
    Write-Host "  http://$ipAddress`:6080/vnc.html" -ForegroundColor Green
    Write-Host ""
    Write-Host "VNC Password: $VncPassword" -ForegroundColor Cyan
    Write-Host ""
    
    if (-not $MakKey) {
        Write-Host "REMINDER: Office is not activated. Run with MAK key to activate:" -ForegroundColor Yellow
        Write-Host '  .\init2024.ps1 "MAK-KEY" "VNC-PASSWORD"' -ForegroundColor Gray
    }
    
    Write-Host ""
    Write-Host "Script usage:" -ForegroundColor Cyan
    Write-Host '  .\init2024.ps1                        # Defaults only'
    Write-Host '  .\init2024.ps1 "MAK-KEY"              # With Office activation'
    Write-Host '  .\init2024.ps1 "MAK-KEY" "mypassword" # Custom VNC password'
    Write-Host ""
}

# =============================================================================
# Main Installation Flow
# =============================================================================

# Parse arguments
$makKey = $args[0]
$vncPassword = if ($args[1]) { $args[1] } else { "unify123" }

# Run installations in order (each function handles its own "already installed" check)
Install-Office -MakKey $makKey
Install-Git
Install-Python
Install-TightVNC -Password $vncPassword
Install-NoVNC
Setup-Websockify
Configure-Firewall
Start-AllServices
Show-Summary -MakKey $makKey -VncPassword $vncPassword
