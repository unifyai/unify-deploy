# init2024.ps1 - Office LTSC 2024 Installation Script
# Run this manually on a Windows VM to install Excel + Office components
# Or use as GCP startup script with metadata for auto-configuration
#
# Manual Usage:
#   powershell -ExecutionPolicy Bypass -File init2024.ps1
#   powershell -ExecutionPolicy Bypass -File init2024.ps1 "MAK-KEY" "vncpassword"
#
# GCP Metadata Keys (optional):
#   windows-username  - Windows user to create/use for auto-logon
#   windows-password  - Password for Windows user
#   vnc-password      - VNC password (default: unify123)
#   office-mak-key    - Office MAK activation key
#   hostname          - DNS hostname for Caddy HTTPS (e.g., assistant-123.vm.unify.ai)
#   github-token      - GitHub PAT for cloning private repos
#   anthropic-api-key - Anthropic API key for agent service
#   unify-key         - Unify API key for agent service
#   unify-base-url    - Unify API base URL for agent service

Write-Host "=========================================="
Write-Host "  Office LTSC 2024 Installation Script"
Write-Host "=========================================="
Write-Host ""

# =============================================================================
# GCP Metadata Helper
# =============================================================================

function Get-GCPMetadata {
    param([string]$Key)
    try {
        $metadataUrl = "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$Key"
        $headers = @{"Metadata-Flavor" = "Google"}
        $value = Invoke-RestMethod -Uri $metadataUrl -Headers $headers -TimeoutSec 5 -ErrorAction Stop
        return $value
    } catch {
        return $null
    }
}

# =============================================================================
# Helper Functions
# =============================================================================

function Setup-WindowsUser {
    param(
        [string]$Username,
        [string]$Password
    )
    
    if (-not $Username -or -not $Password) {
        Write-Host "No Windows user credentials provided, skipping user setup" -ForegroundColor Yellow
        return $false
    }
    
    Write-Host ""
    Write-Host "=== Setting up Windows User ===" -ForegroundColor Cyan
    
    $securePassword = ConvertTo-SecureString $Password -AsPlainText -Force
    
    # Check if user exists
    $existingUser = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
    
    if ($existingUser) {
        Write-Host "User '$Username' already exists, updating password..." -ForegroundColor Yellow
        Set-LocalUser -Name $Username -Password $securePassword
    } else {
        Write-Host "Creating user '$Username'..."
        New-LocalUser -Name $Username -Password $securePassword -PasswordNeverExpires -UserMayNotChangePassword
        Add-LocalGroupMember -Group "Administrators" -Member $Username -ErrorAction SilentlyContinue
        Write-Host "User '$Username' created and added to Administrators" -ForegroundColor Green
    }
    
    return $true
}

function Configure-AutoLogon {
    param(
        [string]$Username,
        [string]$Password
    )
    
    if (-not $Username -or -not $Password) {
        Write-Host "No auto-logon credentials provided, skipping" -ForegroundColor Yellow
        return
    }
    
    Write-Host ""
    Write-Host "=== Configuring Auto-Logon ===" -ForegroundColor Cyan
    
    $regPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    
    Set-ItemProperty -Path $regPath -Name 'AutoAdminLogon' -Value '1' -Type String
    Set-ItemProperty -Path $regPath -Name 'DefaultUserName' -Value $Username -Type String
    Set-ItemProperty -Path $regPath -Name 'DefaultPassword' -Value $Password -Type String
    Set-ItemProperty -Path $regPath -Name 'DefaultDomainName' -Value '.' -Type String
    
    Write-Host "Auto-logon configured for user: $Username" -ForegroundColor Green
}

function Setup-FirstLogonTask {
    param([string]$TargetUser)
    
    if (-not $TargetUser) {
        Write-Host "No target user specified, skipping first-logon task" -ForegroundColor Yellow
        return
    }
    
    Write-Host ""
    Write-Host "=== Setting up First Logon Task ===" -ForegroundColor Cyan
    
    # Create a PowerShell script that runs at first logon to configure HKCU settings
    $firstLogonScript = @'
# First Logon Setup Script - Runs once as the auto-logon user
# Configures HKCU settings that must be set in the user's context

$logFile = "C:\novnc\first-logon.log"
"First logon script started at $(Get-Date)" | Out-File -FilePath $logFile -Encoding UTF8

try {
    # Configure Excel for headless automation (Protected View settings)
    $regPath = 'HKCU:\Software\Microsoft\Office\16.0\Excel\Security\ProtectedView'
    if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
    Set-ItemProperty -Path $regPath -Name 'DisableInternetFilesInPV' -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name 'DisableAttachementsInPV' -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name 'DisableUnsafeLocationsInPV' -Value 1 -Type DWord
    "Configured Excel Protected View settings" | Out-File -FilePath $logFile -Append -Encoding UTF8

    # Disable Office startup screen
    $regPath = 'HKCU:\Software\Microsoft\Office\16.0\Common\General'
    if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
    Set-ItemProperty -Path $regPath -Name 'ShownFirstRunOptin' -Value 1 -Type DWord
    Set-ItemProperty -Path $regPath -Name 'DisableBootToOfficeStart' -Value 1 -Type DWord
    "Configured Office startup settings" | Out-File -FilePath $logFile -Append -Encoding UTF8

    # Mark first-logon setup as complete
    "First logon setup completed successfully at $(Get-Date)" | Out-File -FilePath $logFile -Append -Encoding UTF8
    
    # Delete the scheduled task (self-cleanup)
    Unregister-ScheduledTask -TaskName "FirstLogonSetup" -Confirm:$false -ErrorAction SilentlyContinue
    "Scheduled task removed" | Out-File -FilePath $logFile -Append -Encoding UTF8
} catch {
    "ERROR: $_" | Out-File -FilePath $logFile -Append -Encoding UTF8
}
'@
    
    $scriptPath = 'C:\novnc\first-logon-setup.ps1'
    New-Item -ItemType Directory -Force -Path 'C:\novnc' | Out-Null
    $firstLogonScript | Out-File -FilePath $scriptPath -Encoding UTF8
    Write-Host "Created first-logon script at $scriptPath" -ForegroundColor Green
    
    # Create scheduled task
    $taskName = "FirstLogonSetup"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for first-logon setup..."
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
    $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Write-Host "Scheduled task '$taskName' created for user: $TargetUser" -ForegroundColor Green
}

function Setup-TightVNCTask {
    param([string]$TargetUser)
    
    Write-Host ""
    Write-Host "=== Setting up TightVNC scheduled task ===" -ForegroundColor Cyan
    
    $tvnExe = 'C:\Program Files\TightVNC\tvnserver.exe'
    if (-not (Test-Path $tvnExe)) { $tvnExe = 'C:\Program Files (x86)\TightVNC\tvnserver.exe' }
    
    if (-not (Test-Path $tvnExe)) {
        Write-Host "TightVNC not found, skipping task creation" -ForegroundColor Yellow
        return
    }
    
    # Create startup script for TightVNC
    # This script first copies password from HKLM to HKCU (in case it wasn't copied for this user)
    $startupScript = @"
@echo off
REM Wait for system to settle
timeout /t 5 /nobreak >nul

REM Copy VNC password from HKLM to HKCU (if not already present)
powershell -ExecutionPolicy Bypass -Command "& { `$hklm = 'HKLM:\SOFTWARE\TightVNC\Server'; `$hkcu = 'HKCU:\SOFTWARE\TightVNC\Server'; if (-not (Test-Path `$hkcu)) { New-Item -Path `$hkcu -Force | Out-Null }; try { `$pw = Get-ItemPropertyValue -Path `$hklm -Name 'Password' -ErrorAction Stop; Set-ItemProperty -Path `$hkcu -Name 'Password' -Value `$pw -Type Binary -Force } catch {}; try { `$cpw = Get-ItemPropertyValue -Path `$hklm -Name 'ControlPassword' -ErrorAction Stop; Set-ItemProperty -Path `$hkcu -Name 'ControlPassword' -Value `$cpw -Type Binary -Force } catch {}; Set-ItemProperty -Path `$hkcu -Name 'AllowLoopback' -Value 1 -Type DWord -Force; Set-ItemProperty -Path `$hkcu -Name 'AcceptRfbConnections' -Value 1 -Type DWord -Force; Set-ItemProperty -Path `$hkcu -Name 'UseVncAuthentication' -Value 1 -Type DWord -Force; Set-ItemProperty -Path `$hkcu -Name 'RfbPort' -Value 5900 -Type DWord -Force }"

REM Start TightVNC in app-mode
"$tvnExe" -run
REM Reload settings
timeout /t 2 /nobreak >nul
"$tvnExe" -controlapp -reload
"@
    $scriptPath = 'C:\novnc\start-tightvnc.bat'
    New-Item -ItemType Directory -Force -Path 'C:\novnc' | Out-Null
    $startupScript | Out-File -FilePath $scriptPath -Encoding ASCII
    
    # Create scheduled task
    $taskName = "StartTightVNC"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for TightVNC auto-start..."
    $action = New-ScheduledTaskAction -Execute $scriptPath -WorkingDirectory 'C:\novnc'
    
    # If target user specified, trigger at that user's logon; otherwise any user
    if ($TargetUser) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
        $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable) | Out-Null
        Write-Host "Scheduled task '$taskName' created for user: $TargetUser" -ForegroundColor Green
    } else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest | Out-Null
        Write-Host "Scheduled task '$taskName' created for any user at login" -ForegroundColor Green
    }
}

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

function Install-Chocolatey {
    Write-Host ""
    Write-Host "=== Installing Chocolatey ===" -ForegroundColor Cyan
    
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        $chocoVersion = choco --version
        Write-Host "Chocolatey already installed - v$chocoVersion" -ForegroundColor Green
        return
    }
    
    Write-Host "Chocolatey not found. Installing..."
    
    # Set execution policy and install Chocolatey
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    # Verify Chocolatey installation
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        $chocoVersion = choco --version
        Write-Host "SUCCESS: Chocolatey installed - v$chocoVersion" -ForegroundColor Green
    } else {
        Write-Host "ERROR: Chocolatey installation failed" -ForegroundColor Red
    }
}

function Install-NodeJS {
    Write-Host ""
    Write-Host "=== Installing Node.js v22 ===" -ForegroundColor Cyan
    
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $nodeVersion = node --version
        Write-Host "Node.js already installed - $nodeVersion" -ForegroundColor Green
        return
    }
    
    # Ensure Chocolatey is installed
    Install-Chocolatey
    
    Write-Host "Installing Node.js v22 via Chocolatey..."
    choco install nodejs --version=22.13.0 -y --no-progress
    
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    # Verify Node.js installation
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $nodeVersion = node --version
        $npmVersion = npm --version
        Write-Host "SUCCESS: Node.js installed - $nodeVersion" -ForegroundColor Green
        Write-Host "npm version: $npmVersion" -ForegroundColor Green
    } else {
        Write-Host "Node.js installed but not in PATH. You may need to restart your terminal." -ForegroundColor Yellow
        Write-Host "Expected location: C:\Program Files\nodejs\node.exe"
        if (Test-Path 'C:\Program Files\nodejs\node.exe') {
            Write-Host "Node.js executable found at expected location." -ForegroundColor Green
        }
    }
    
    # Install Bun via npm
    Write-Host ""
    Write-Host "=== Installing Bun ===" -ForegroundColor Cyan
    
    if (Get-Command bun -ErrorAction SilentlyContinue) {
        $bunVersion = bun --version
        Write-Host "Bun already installed - v$bunVersion" -ForegroundColor Green
    } else {
        Write-Host "Installing Bun..."
        # Run installer in current process (not subprocess) for proper PATH handling
        Invoke-Expression (Invoke-RestMethod 'https://bun.sh/install.ps1')
        
        # Refresh PATH from registry
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        
        # Explicitly add Bun path to current session (in case registry isn't updated yet)
        
        # Verify Bun installation
        if (Get-Command bun -ErrorAction SilentlyContinue) {
            $bunVersion = bun --version
            Write-Host "SUCCESS: Bun installed - v$bunVersion" -ForegroundColor Green
        } else {
            Write-Host "Bun installed but not in PATH. You may need to restart your terminal." -ForegroundColor Yellow
        }
    }

    if (Get-Command npx -ErrorAction SilentlyContinue) {
        $npxVersion = npx --version
        Write-Host "SUCCESS: npx installed - v$npxVersion" -ForegroundColor Green
    } else {
        Write-Host "Installing npx..."
        npm i -g npx

        Write-Host "npx installed but not in PATH. You may need to restart your terminal." -ForegroundColor Yellow
    }
}

function Install-AgentService {
    param([string]$GithubToken, [string]$Staging)
    
    Write-Host ""
    Write-Host "=== Installing/Updating Agent Service ===" -ForegroundColor Cyan
    
    $magnitudeDir = 'C:\magnitude'
    $agentServiceDir = 'C:\agent-service'
    $tempRepoDir = 'C:\temp\unity'
    $branch = if ($Staging) { 'staging' } else { 'main' }
    
    # Build clone URL (with or without token)
    if ($GithubToken) {
        $magnitudeUrl = "https://${GithubToken}@github.com/unifyai/magnitude.git"
        $agentRepoUrl = "https://${GithubToken}@github.com/unifyai/unity.git"
        Write-Host "Using authenticated GitHub clone" -ForegroundColor Green
    } else {
        $magnitudeUrl = "https://github.com/unifyai/magnitude.git"
        $agentRepoUrl = "https://github.com/unifyai/unity.git"
        Write-Host "Using public GitHub clone (may fail for private repos)" -ForegroundColor Yellow
    }
    
    # === MAGNITUDE ===
    if (Test-Path "$magnitudeDir\.git") {
        # Update existing repo with git pull
        Write-Host "Updating magnitude (git pull)..."
        Push-Location $magnitudeDir
        
        # Configure remote URL (in case token changed)
        git remote set-url origin $magnitudeUrl
        git pull origin unity-modifications
        
        Write-Host "Installing magnitude dependencies..."
        bun install
        npm run build
        Pop-Location
        Write-Host "SUCCESS: Magnitude updated" -ForegroundColor Green
    } else {
        # Clone fresh
        Write-Host "Cloning unifyai/magnitude (branch: unity-modifications)..."
        if (Test-Path $magnitudeDir) { Remove-Item -Recurse -Force $magnitudeDir -ErrorAction SilentlyContinue }
        
        git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir
        
        if (Test-Path "$magnitudeDir\package.json") {
            Write-Host "Installing magnitude dependencies..."
            Push-Location $magnitudeDir
            bun install
            npm run build
            Pop-Location
            Write-Host "SUCCESS: Magnitude installed" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Magnitude may not have installed correctly" -ForegroundColor Yellow
        }
    }
    
    # === AGENT SERVICE ===
    # Stop any running Node processes to release file locks
    Write-Host "Stopping any running Node processes..."
    Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    
    # Backup .env if exists (contains API keys)
    $envBackup = $null
    $envFile = "$agentServiceDir\.env"
    if (Test-Path $envFile) {
        Write-Host "Backing up .env file..."
        $envBackup = Get-Content $envFile -Raw
    }
    
    # Clone unity.git to temp and extract agent-service
    Write-Host "Cloning unifyai/unity (branch: $branch)..."
    if (Test-Path $tempRepoDir) { Remove-Item -Recurse -Force $tempRepoDir -ErrorAction SilentlyContinue }
    
    git clone --depth 1 --branch $branch $agentRepoUrl $tempRepoDir
    
    # Extract agent-service subfolder
    if (Test-Path "$tempRepoDir\agent-service") {
        Write-Host "Extracting agent-service subfolder..."
        
        # Remove existing agent-service folder with retry logic
        if (Test-Path $agentServiceDir) {
            $retryCount = 0
            $maxRetries = 3
            while ((Test-Path $agentServiceDir) -and ($retryCount -lt $maxRetries)) {
                Write-Host "  Removing existing agent-service folder (attempt $($retryCount + 1))..."
                Remove-Item -Recurse -Force $agentServiceDir -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
                $retryCount++
            }
            
            if (Test-Path $agentServiceDir) {
                Write-Host "  WARNING: Could not fully remove existing folder, will try to overwrite" -ForegroundColor Yellow
            }
        }
        
        # Move new agent-service folder
        if (Test-Path $agentServiceDir) {
            # Folder still exists, use Copy instead of Move
            Write-Host "  Using copy (folder still exists)..."
            Copy-Item -Path "$tempRepoDir\agent-service\*" -Destination $agentServiceDir -Recurse -Force
        } else {
            Move-Item "$tempRepoDir\agent-service" $agentServiceDir
        }
        Remove-Item -Recurse -Force $tempRepoDir -ErrorAction SilentlyContinue
        
        # Restore .env if backed up
        if ($envBackup) {
            Write-Host "Restoring .env file..."
            $envBackup | Out-File -FilePath $envFile -Encoding UTF8 -NoNewline
        }
        
        if (Test-Path "$agentServiceDir\package.json") {
            Write-Host "Installing agent-service dependencies..."
            Push-Location $agentServiceDir
            npm install
            Pop-Location
            Write-Host "SUCCESS: Agent service installed/updated" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Agent service may not have installed correctly" -ForegroundColor Yellow
        }
    } else {
        Write-Host "ERROR: agent-service subfolder not found in repo" -ForegroundColor Red
        Remove-Item -Recurse -Force $tempRepoDir -ErrorAction SilentlyContinue
    }
}

function Setup-AgentServiceEnv {
    param(
        [string]$AnthropicApiKey,
        [string]$UnifyKey,
        [string]$UnifyBaseUrl
    )
    
    Write-Host ""
    Write-Host "=== Setting up Agent Service Environment ===" -ForegroundColor Cyan
    
    $agentServiceDir = 'C:\agent-service'
    $envFile = "$agentServiceDir\.env"
    
    if (-not (Test-Path $agentServiceDir)) {
        Write-Host "Agent service directory not found, skipping .env creation" -ForegroundColor Yellow
        return
    }
    
    # Build .env content
    $envContent = @()
    
    if ($AnthropicApiKey) {
        $envContent += "ANTHROPIC_API_KEY=$AnthropicApiKey"
        Write-Host "  Added ANTHROPIC_API_KEY" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: No ANTHROPIC_API_KEY provided" -ForegroundColor Yellow
    }
    
    if ($UnifyKey) {
        $envContent += "UNIFY_KEY=$UnifyKey"
        Write-Host "  Added UNIFY_KEY" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: No UNIFY_KEY provided" -ForegroundColor Yellow
    }
    
    if ($UnifyBaseUrl) {
        $envContent += "UNIFY_BASE_URL=$UnifyBaseUrl"
        Write-Host "  Added UNIFY_BASE_URL=$UnifyBaseUrl" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: No UNIFY_BASE_URL provided" -ForegroundColor Yellow
    }
    
    if ($envContent.Count -gt 0) {
        $envContent -join "`n" | Out-File -FilePath $envFile -Encoding UTF8 -NoNewline
        Write-Host "Created .env file at: $envFile" -ForegroundColor Green
    } else {
        Write-Host "No environment variables to write" -ForegroundColor Yellow
    }
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
        
        # Create custom.html with iframe wrapper for clean noVNC experience
        # Hides control bar, logo, and remote cursor via CSS injection
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
        Write-Host "Created custom.html and set as index.html (default)" -ForegroundColor Green
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
    param([string]$TargetUser)
    
    Write-Host ""
    Write-Host "=== Setting up websockify ===" -ForegroundColor Cyan

    $novncDir = 'C:\novnc'

    # Find pythonw.exe (no-console Python) for hidden execution
    $pythonwExe = 'C:\Program Files\Python312\pythonw.exe'
    if (-not (Test-Path $pythonwExe)) {
        # Fallback to python.exe directory
        $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
        if ($pythonExe) {
            $pythonwExe = Join-Path (Split-Path $pythonExe) "pythonw.exe"
    }
    }
    if (-not $pythonwExe -or -not (Test-Path $pythonwExe)) {
        Write-Host "ERROR: pythonw.exe not found" -ForegroundColor Red
        return
    }
    Write-Host "Using pythonw.exe: $pythonwExe (no console window)" -ForegroundColor Green

    # Create a scheduled task to run websockify at login (for persistence)
    # Using pythonw.exe directly - no .bat file needed, no console window
    $taskName = "StartWebsockify"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }

    Write-Host "Creating scheduled task for websockify auto-start (hidden)..."
    $action = New-ScheduledTaskAction -Execute $pythonwExe -Argument "-m websockify --web $novncDir 6080 localhost:5900" -WorkingDirectory $novncDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    # If target user specified, trigger at that user's logon; otherwise any user
    if ($TargetUser) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
        $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "Scheduled task '$taskName' created for user: $TargetUser" -ForegroundColor Green
    } else {
        # Fallback: use current user or run for any user
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest | Out-Null
        Write-Host "Scheduled task '$taskName' created for any user at login" -ForegroundColor Green
    }
}

function Setup-AgentServiceTask {
    param([string]$TargetUser)
    
    Write-Host ""
    Write-Host "=== Setting up Agent Service scheduled task ===" -ForegroundColor Cyan
    
    $agentServiceDir = 'C:\agent-service'
    
    if (-not (Test-Path "$agentServiceDir\package.json")) {
        Write-Host "Agent service not found, skipping task creation" -ForegroundColor Yellow
        return
    }
    
    # Create scheduled task using PowerShell with hidden window - no .bat file needed
    $taskName = "StartAgentService"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for Agent Service (hidden)..."
    # Use PowerShell with -WindowStyle Hidden to run npx without showing a console window
    $psCommand = "Set-Location '$agentServiceDir'; npx ts-node src/index.ts"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory $agentServiceDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    if ($TargetUser) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
        $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "Scheduled task '$taskName' created for user: $TargetUser" -ForegroundColor Green
    } else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest | Out-Null
        Write-Host "Scheduled task '$taskName' created for any user at login" -ForegroundColor Green
    }
}

function Setup-DisplayResolution {
    param(
        [string]$TargetUser,
        [int]$Width = 1920,
        [int]$Height = 1080
    )
    
    Write-Host ""
    Write-Host "=== Setting up Display Resolution ($Width x $Height) ===" -ForegroundColor Cyan
    
    $novncDir = 'C:\novnc'
    New-Item -ItemType Directory -Force -Path $novncDir | Out-Null
    
    # Create PowerShell script that sets display resolution using Windows API
    $resolutionScript = @"
# Set display resolution to ${Width}x${Height}
# This script uses the Windows ChangeDisplaySettings API

`$code = @'
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
        
        // Get current settings first
        if (EnumDisplaySettings(null, ENUM_CURRENT_SETTINGS, ref dm) == 0) {
            return -1;
        }
        
        dm.dmPelsWidth = width;
        dm.dmPelsHeight = height;
        dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT;
        
        // Test if the resolution is valid
        int testResult = ChangeDisplaySettings(ref dm, CDS_TEST);
        if (testResult != DISP_CHANGE_SUCCESSFUL) {
            return testResult;
        }
        
        // Apply the resolution
        return ChangeDisplaySettings(ref dm, CDS_UPDATEREGISTRY);
    }
}
'@

try {
    Add-Type -TypeDefinition `$code -Language CSharp -ErrorAction Stop
} catch {
    # Type may already be loaded
}

`$result = [DisplaySettings]::SetResolution($Width, $Height)
if (`$result -eq 0) {
    "Resolution set to ${Width}x${Height}" | Out-File -FilePath "C:\novnc\resolution.log" -Append -Encoding UTF8
} else {
    "Failed to set resolution, error code: `$result" | Out-File -FilePath "C:\novnc\resolution.log" -Append -Encoding UTF8
}
"@
    
    $scriptPath = "$novncDir\set-resolution.ps1"
    $resolutionScript | Out-File -FilePath $scriptPath -Encoding UTF8
    Write-Host "Created resolution script at $scriptPath" -ForegroundColor Green
    
    # Create batch wrapper for scheduled task
    $batchScript = @"
@echo off
REM Wait for desktop to initialize
timeout /t 3 /nobreak >nul
powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File "$scriptPath"
"@
    $batchPath = "$novncDir\set-resolution.bat"
    $batchScript | Out-File -FilePath $batchPath -Encoding ASCII
    Write-Host "Created resolution batch script at $batchPath" -ForegroundColor Green
    
    # Create scheduled task to run resolution script at login (before TightVNC starts)
    $taskName = "SetDisplayResolution"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for display resolution..."
    $action = New-ScheduledTaskAction -Execute $batchPath -WorkingDirectory $novncDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    if ($TargetUser) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
        $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "Scheduled task '$taskName' created for user: $TargetUser" -ForegroundColor Green
    } else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest | Out-Null
        Write-Host "Scheduled task '$taskName' created for any user at login" -ForegroundColor Green
    }
}

function Configure-Firewall {
    Write-Host ""
    Write-Host "=== Configuring Firewall ===" -ForegroundColor Cyan

    # Remove old rules if they exist (to update them)
    Remove-NetFirewallRule -DisplayName "Unity-noVNC" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-HTTPS" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-HTTP" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-VNC-Local" -ErrorAction SilentlyContinue

    # Allow HTTPS (443) - Caddy reverse proxy
    New-NetFirewallRule -DisplayName "Unity-HTTPS" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow HTTPS (443)" -ForegroundColor Green

    # Allow HTTP (80) - Let's Encrypt ACME challenge
    New-NetFirewallRule -DisplayName "Unity-HTTP" -Direction Inbound -LocalPort 80 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow HTTP (80)" -ForegroundColor Green

    # Allow noVNC/websockify (6080) - for direct access / fallback
    New-NetFirewallRule -DisplayName "Unity-noVNC" -Direction Inbound -LocalPort 6080 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow noVNC (6080)" -ForegroundColor Green

    Write-Host "Firewall configured" -ForegroundColor Green
}

# =============================================================================
# Caddy HTTPS Reverse Proxy
# =============================================================================

function Install-Caddy {
    Write-Host ""
    Write-Host "=== Installing Caddy Web Server ===" -ForegroundColor Cyan
    
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
}

function Setup-Caddyfile {
    param([string]$Hostname)
    
    Write-Host ""
    Write-Host "=== Creating Caddyfile ===" -ForegroundColor Cyan
    
    if (-not $Hostname) {
        Write-Host "No hostname provided, skipping Caddy configuration" -ForegroundColor Yellow
        Write-Host "Caddy will not provide HTTPS - use HTTP on port 6080 instead" -ForegroundColor Yellow
        return $false
    }
    
    $caddyDir = 'C:\caddy'
    $caddyfile = "$caddyDir\Caddyfile"
    
    New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null
    
    # Path-based routing: /desktop -> noVNC (6080), /api -> Agent Service (3000)
    $caddyConfig = @"
# Unity Windows VM - Caddy Configuration
# Hostname: $Hostname
# Generated: $(Get-Date)

$Hostname {
    # Handle WebSocket upgrade for noVNC
    @websocket {
        header Connection *Upgrade*
        header Upgrade websocket
    }
    reverse_proxy @websocket localhost:6080

    # API endpoint service (port 3000)
    handle_path /api/* {
        reverse_proxy localhost:3000
    }

    # Exact match for /desktop (no trailing slash)
    @desktop_exact path /desktop
    handle @desktop_exact {
        rewrite * /
        reverse_proxy localhost:6080
    }

    # noVNC desktop sub-resources at /desktop/*
    handle_path /desktop/* {
        reverse_proxy localhost:6080
    }

    # Logging
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
    Write-Host "Caddyfile created at: $caddyfile" -ForegroundColor Green
    Write-Host "  HTTPS routes:" -ForegroundColor Gray
    Write-Host "    https://$Hostname/desktop/ -> noVNC (localhost:6080)" -ForegroundColor Gray
    Write-Host "    https://$Hostname/api/*    -> Agent Service (localhost:3000)" -ForegroundColor Gray
    
    return $true
}

function Setup-CaddyTask {
    Write-Host ""
    Write-Host "=== Setting up Caddy scheduled task ===" -ForegroundColor Cyan
    
    $caddyDir = 'C:\caddy'
    $caddyExe = "$caddyDir\caddy.exe"
    $caddyfile = "$caddyDir\Caddyfile"
    
    if (-not (Test-Path $caddyExe)) {
        Write-Host "Caddy not found, skipping task creation" -ForegroundColor Yellow
        return
    }
    
    if (-not (Test-Path $caddyfile)) {
        Write-Host "Caddyfile not found, skipping task creation" -ForegroundColor Yellow
        return
    }
    
    $taskName = "StartCaddy"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing Caddy scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for Caddy (runs at system startup as SYSTEM)..."
    # Use PowerShell with hidden window to run Caddy
    $psCommand = "Set-Location '$caddyDir'; & '$caddyExe' run --config '$caddyfile'"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory $caddyDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    
    # Run at system startup as SYSTEM - gives Caddy time to acquire TLS certificate before user logon
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Write-Host "Scheduled task '$taskName' created (runs at system startup as SYSTEM)" -ForegroundColor Green
}

function Start-Caddy {
    Write-Host ""
    Write-Host "=== Starting Caddy ===" -ForegroundColor Cyan
    
    $caddyDir = 'C:\caddy'
    $caddyExe = "$caddyDir\caddy.exe"
    $caddyfile = "$caddyDir\Caddyfile"
    
    if (-not (Test-Path $caddyExe)) {
        Write-Host "Caddy not found, skipping" -ForegroundColor Yellow
        return
    }
    
    if (-not (Test-Path $caddyfile)) {
        Write-Host "Caddyfile not found, skipping" -ForegroundColor Yellow
        return
    }
    
    # Kill any existing Caddy process
    Get-Process -Name "caddy" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    
    Write-Host "Starting Caddy..."
    # Use PowerShell with hidden window to run Caddy
    $psCommand = "Set-Location '$caddyDir'; & '$caddyExe' run --config '$caddyfile'"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory $caddyDir
    Start-Sleep -Seconds 5
    
    # Check if Caddy is running
    $listening443 = netstat -an | Select-String ":443.*LISTENING"
    $listening80 = netstat -an | Select-String ":80.*LISTENING"
    
    if ($listening443) {
        Write-Host "  Caddy: Running (port 443 listening)" -ForegroundColor Green
    } else {
        Write-Host "  Caddy: Not listening on port 443 yet" -ForegroundColor Yellow
        Write-Host "  Note: TLS certificate acquisition may take a moment" -ForegroundColor Gray
    }
    
    if ($listening80) {
        Write-Host "  Caddy: HTTP redirect active (port 80 listening)" -ForegroundColor Green
    }
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
    
    # Start websockify directly using pythonw.exe (no console window)
Write-Host "Starting websockify..."
    $novncDir = 'C:\novnc'
    $pythonwExe = 'C:\Program Files\Python312\pythonw.exe'
    
    if (Test-Path $pythonwExe) {
        Start-Process -FilePath $pythonwExe -ArgumentList "-m websockify --web $novncDir 6080 localhost:5900" -WorkingDirectory $novncDir
Start-Sleep -Seconds 3

        # Check if websockify is running (check if port 6080 is listening)
        $listening = netstat -an | Select-String ":6080.*LISTENING"
        if ($listening) {
            Write-Host "  websockify : Running (port 6080 listening)" -ForegroundColor Green
        } else {
            Write-Host "  websockify : Not listening on port 6080" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  websockify : pythonw.exe not found at $pythonwExe" -ForegroundColor Red
    }
    
    # Start Agent Service using PowerShell with hidden window
    Write-Host "Starting Agent Service..."
    $agentServiceDir = 'C:\agent-service'
    if (Test-Path "$agentServiceDir\package.json") {
        $psCommand = "Set-Location '$agentServiceDir'; npx ts-node src/index.ts"
        Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory $agentServiceDir
        Start-Sleep -Seconds 5
        
        # Check if Agent Service is running (check if port 3000 is listening)
        $listening3000 = netstat -an | Select-String ":3000.*LISTENING"
        if ($listening3000) {
            Write-Host "  Agent Service : Running (port 3000 listening)" -ForegroundColor Green
        } else {
            Write-Host "  Agent Service : Not listening on port 3000 yet" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  Agent Service : package.json not found at $agentServiceDir" -ForegroundColor Yellow
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
    
    # Note: HKCU settings (Protected View, startup screen) are configured via
    # Setup-FirstLogonTask to ensure they apply to the correct user's registry
    Write-Host "Note: Excel HKCU settings will be configured at first user logon" -ForegroundColor Yellow
    
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
        [string]$VncPassword,
        [string]$Hostname
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
    Write-Host "  - Node.js + Bun"
    Write-Host "  - TightVNC Server (port 5900)"
    Write-Host "  - noVNC + websockify (port 6080)"
    Write-Host "  - Agent Service (port 3000)"
    if ($Hostname) {
        Write-Host "  - Caddy HTTPS reverse proxy (ports 80, 443)"
    }
    Write-Host ""
    Write-Host "Paths:"
    Write-Host "  Excel:      C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
    Write-Host "  Git:        C:\Program Files\Git\bin\git.exe"
    Write-Host "  Python:     C:\Program Files\Python312\python.exe"
    Write-Host "  Node.js:    C:\Program Files\nodejs\node.exe"
    Write-Host "  noVNC:      C:\novnc"
    Write-Host "  Magnitude:  C:\magnitude"
    Write-Host "  Agent:      C:\agent-service"
    if ($Hostname) {
        Write-Host "  Caddy:      C:\caddy"
    }
    Write-Host ""
    Write-Host "Logs:"
    Write-Host "  websockify: C:\novnc\websockify.log"
    if ($Hostname) {
        Write-Host "  Caddy:      C:\caddy\access.log"
    }
    Write-Host ""

    # Access information
    $ipAddress = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback" } | Select-Object -First 1).IPAddress
    
    if ($Hostname) {
        Write-Host "HTTPS Access (via Caddy):" -ForegroundColor Cyan
        Write-Host "  Desktop:   https://$Hostname/desktop/" -ForegroundColor Green
        Write-Host "  API:       https://$Hostname/api/*" -ForegroundColor Green
        Write-Host ""
        Write-Host "Direct HTTP Access (fallback):" -ForegroundColor Cyan
        Write-Host "  http://$ipAddress`:6080/vnc.html" -ForegroundColor Gray
        Write-Host "  http://$ipAddress`:3000/ (Agent Service)" -ForegroundColor Gray
    } else {
        Write-Host "HTTP Access (no hostname configured):" -ForegroundColor Yellow
        Write-Host "  http://$ipAddress`:6080/vnc.html" -ForegroundColor Green
        Write-Host "  http://$ipAddress`:3000/ (Agent Service)" -ForegroundColor Green
    }
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

# Try to get credentials from GCP metadata first, then fall back to arguments
Write-Host "Checking for GCP metadata..."
$gcpWindowsUser = Get-GCPMetadata -Key "windows-username"
$gcpWindowsPassword = Get-GCPMetadata -Key "windows-password"
$gcpVncPassword = Get-GCPMetadata -Key "vnc-password"
$gcpMakKey = Get-GCPMetadata -Key "office-mak-key"
$gcpHostname = Get-GCPMetadata -Key "hostname"
$gcpGithubToken = Get-GCPMetadata -Key "github-token"
$gcpAnthropicKey = Get-GCPMetadata -Key "anthropic-api-key"
$gcpUnifyKey = Get-GCPMetadata -Key "unify-key"
$gcpUnifyBaseUrl = Get-GCPMetadata -Key "unify-base-url"
$gcpStaging = Get-GCPMetadata -Key "staging"

if ($gcpWindowsUser) {
    Write-Host "Found GCP metadata - running in automated mode" -ForegroundColor Cyan
}
if ($gcpHostname) {
    Write-Host "Found hostname metadata: $gcpHostname" -ForegroundColor Cyan
}
if ($gcpGithubToken) {
    Write-Host "Found GitHub token in metadata" -ForegroundColor Cyan
}
if ($gcpAnthropicKey) {
    Write-Host "Found Anthropic API key in metadata" -ForegroundColor Cyan
}
if ($gcpStaging) {
    Write-Host "Found staging metadata: $gcpStaging" -ForegroundColor Cyan
}

# Parse arguments (GCP metadata takes precedence)
$makKey = if ($gcpMakKey) { $gcpMakKey } elseif ($args[0]) { $args[0] } else { $null }
$vncPassword = if ($gcpVncPassword) { $gcpVncPassword } elseif ($args[1]) { $args[1] } else { "unify123" }
$windowsUser = if ($gcpWindowsUser) { $gcpWindowsUser } elseif ($args[2]) { $args[2] } else { $null }
$windowsPassword = if ($gcpWindowsPassword) { $gcpWindowsPassword } elseif ($args[3]) { $args[3] } else { $null }
$hostname = if ($gcpHostname) { $gcpHostname } else { $null }

# Check if we're running in a user session or as startup script
$hasUserSession = [Environment]::UserInteractive -and ([Security.Principal.WindowsIdentity]::GetCurrent().Name -notmatch 'SYSTEM')

Write-Host ""
Write-Host "Configuration:" -ForegroundColor Cyan
Write-Host "  VNC Password:    $vncPassword"
Write-Host "  Windows User:    $(if ($windowsUser) { $windowsUser } else { '(not configured)' })"
Write-Host "  Office MAK:      $(if ($makKey) { '(provided)' } else { '(not provided)' })"
Write-Host "  Hostname:        $(if ($hostname) { $hostname } else { '(not configured - no HTTPS)' })"
Write-Host "  User Session:    $hasUserSession"
Write-Host ""

# Setup Windows user FIRST (before scheduled tasks, so user SID exists)
if ($windowsUser -and $windowsPassword) {
    Setup-WindowsUser -Username $windowsUser -Password $windowsPassword
    Configure-AutoLogon -Username $windowsUser -Password $windowsPassword
}

# Run installations in order (each function handles its own "already installed" check)
Install-Office -MakKey $makKey
Install-Git
Install-Python
Install-NodeJS
Install-AgentService -GithubToken $gcpGithubToken -Staging $gcpStaging
Setup-AgentServiceEnv -AnthropicApiKey $gcpAnthropicKey -UnifyKey $gcpUnifyKey -UnifyBaseUrl $gcpUnifyBaseUrl
Install-TightVNC -Password $vncPassword
Install-NoVNC

# Install Caddy for HTTPS reverse proxy
Install-Caddy
$caddyConfigured = Setup-Caddyfile -Hostname $hostname
if ($caddyConfigured) {
    Setup-CaddyTask
}

# Setup scheduled tasks (user must exist before this point)
Setup-DisplayResolution -TargetUser $windowsUser -Width 1920 -Height 1080
Setup-Websockify -TargetUser $windowsUser
Setup-TightVNCTask -TargetUser $windowsUser
Setup-AgentServiceTask -TargetUser $windowsUser
Setup-FirstLogonTask -TargetUser $windowsUser
Configure-Firewall

# Start services immediately only if we have a user session
if ($hasUserSession) {
    Write-Host ""
    Write-Host "User session detected - starting services now..." -ForegroundColor Cyan
    Start-AllServices
    if ($caddyConfigured) {
        Start-Caddy
    }
} else {
    Write-Host ""
    Write-Host "No user session (startup script mode) - services will start after reboot" -ForegroundColor Yellow
    Write-Host "Scheduled tasks created for: TightVNC, websockify, Agent Service (at logon)" -ForegroundColor Yellow
    if ($caddyConfigured) {
        Write-Host "Caddy will start at system startup (as SYSTEM, before user logon)" -ForegroundColor Yellow
        # Don't start Caddy now - let it start via scheduled task after reboot
        # This gives it time to acquire TLS certificate before user tests
    }
}

Show-Summary -MakKey $makKey -VncPassword $vncPassword -Hostname $hostname

# If auto-logon was configured and no user session, reboot (but only once!)
$setupCompleteFlag = 'C:\novnc\.setup-complete'

if ($windowsUser -and $windowsPassword -and -not $hasUserSession) {
    if (Test-Path $setupCompleteFlag) {
        Write-Host ""
        Write-Host "Setup already complete (found $setupCompleteFlag)" -ForegroundColor Green
        Write-Host "Skipping reboot - waiting for auto-logon to trigger scheduled tasks..."
    } else {
        # Create the flag file to prevent infinite reboot loop
        New-Item -ItemType Directory -Force -Path 'C:\novnc' | Out-Null
        "Setup completed at $(Get-Date)" | Out-File -FilePath $setupCompleteFlag -Encoding ASCII
        
        Write-Host ""
        Write-Host "=========================================="
        Write-Host "  REBOOT REQUIRED" -ForegroundColor Yellow
        Write-Host "=========================================="
        Write-Host "Auto-logon has been configured for user: $windowsUser"
        Write-Host "The VM will auto-login and start noVNC after reboot."
        Write-Host ""
        Write-Host "Rebooting in 10 seconds..." -ForegroundColor Yellow
        Start-Sleep -Seconds 10
        Restart-Computer -Force
    }
}
