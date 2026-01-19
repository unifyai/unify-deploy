# init.ps1 - Windows VM Setup Script (Office + VNC + Caddy)
# Run this manually on a Windows VM to install Excel, VNC, and optional HTTPS proxy
#
# Manual Usage:
#   powershell -ExecutionPolicy Bypass -File init.ps1
#   powershell -ExecutionPolicy Bypass -File init.ps1 "MAK-KEY"
#   powershell -ExecutionPolicy Bypass -File init.ps1 "MAK-KEY" "vncpassword"
#
# GCP Metadata Keys (optional):
#   vnc-password      - VNC password (default: unify123)
#   hostname          - DNS hostname for Caddy HTTPS (e.g., vm.example.com)
#   windows-username  - Windows user to create for auto-logon
#   windows-password  - Password for Windows user
#   github-token      - GitHub PAT for cloning private repos
#   anthropic-api-key - Anthropic API key for agent service
#   unify-key         - Unify API key for agent service
#   unify-base-url    - Unify API base URL for agent service
#   staging           - Use staging branch (any value = true)

Write-Host "=========================================="
Write-Host "  Windows VM Setup Script"
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

function Setup-WindowsUser {
    param(
        [string]$Username,
        [string]$Password
    )
    
    if (-not $Username -or -not $Password) {
        Write-Host "No Windows user credentials provided, skipping user setup" -ForegroundColor Yellow
        return $null  # No user setup needed
    }
    
    Write-Host ""
    Write-Host "=== Setting up Windows User ===" -ForegroundColor Cyan
    
    $securePassword = ConvertTo-SecureString $Password -AsPlainText -Force
    
    # Check if user exists
    $existingUser = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
    
    if ($existingUser) {
        Write-Host "User '$Username' already exists, updating password..." -ForegroundColor Yellow
        Set-LocalUser -Name $Username -Password $securePassword
        return $false  # EXISTING user - no reboot needed
    } else {
        Write-Host "Creating user '$Username'..."
        New-LocalUser -Name $Username -Password $securePassword -PasswordNeverExpires -UserMayNotChangePassword
        Add-LocalGroupMember -Group "Administrators" -Member $Username -ErrorAction SilentlyContinue
        Write-Host "User '$Username' created and added to Administrators" -ForegroundColor Green
        return $true  # NEW user created - reboot needed
    }
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
    Write-Host "Note: Auto-logon will take effect after next reboot" -ForegroundColor Yellow
}

# =============================================================================
# Helper Functions
# =============================================================================

function Update-GitRepo {
    param(
        [string]$RepoPath,
        [string]$Branch,
        [string]$GithubToken,
        [string]$RepoName
    )
    
    Write-Host "Updating $RepoName at $RepoPath..." -ForegroundColor Yellow
    Push-Location $RepoPath
    
    try {
        # Update remote URL if token provided (in case it changed)
        if ($GithubToken) {
            $remoteUrl = git remote get-url origin 2>&1
            if ($remoteUrl -notlike "*$GithubToken*") {
                $newUrl = "https://$GithubToken@github.com/unifyai/$RepoName.git"
                git remote set-url origin $newUrl 2>&1 | Out-Null
            }
        }
        
        # Fetch latest from remote (works with shallow clones)
        Write-Host "  Fetching latest from origin/$Branch..."
        git fetch --depth 1 origin $Branch 2>&1 | Out-Null
        
        # Reset to latest remote state (discards local changes but keeps untracked files)
        Write-Host "  Resetting to origin/$Branch..."
        git reset --hard origin/$Branch 2>&1 | Out-Null
        
        # Get current commit for logging
        $commit = git rev-parse --short HEAD 2>&1
        Write-Host "  Updated to commit: $commit" -ForegroundColor Green
        
        Pop-Location
        return $true
    } catch {
        Write-Host "  WARNING: Failed to update - $_" -ForegroundColor Yellow
        Pop-Location
        return $false
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
    
    Write-Host "Installing Chocolatey package manager..."
    
    # Set execution policy for this process
    Set-ExecutionPolicy Bypass -Scope Process -Force
    
    # Install Chocolatey
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        $chocoVersion = choco --version
        Write-Host "SUCCESS: Chocolatey installed - v$chocoVersion" -ForegroundColor Green
    } else {
        Write-Host "WARNING: Chocolatey may not be in PATH yet" -ForegroundColor Yellow
    }
}

function Install-NodeJS {
    Write-Host ""
    Write-Host "=== Installing Node.js v22, Bun, and npx ===" -ForegroundColor Cyan
    
    # Check if Node.js is already installed
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $nodeVersion = node --version
        Write-Host "Node.js already installed - $nodeVersion" -ForegroundColor Green
    } else {
        Write-Host "Installing Node.js v22 via Chocolatey..."
        
        # Ensure Chocolatey is available
        if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
            Write-Host "ERROR: Chocolatey not found, cannot install Node.js" -ForegroundColor Red
            return
        }
        
        choco install nodejs --version=22.12.0 -y --no-progress
        
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        
        if (Get-Command node -ErrorAction SilentlyContinue) {
            $nodeVersion = node --version
            Write-Host "SUCCESS: Node.js installed - $nodeVersion" -ForegroundColor Green
        } else {
            Write-Host "WARNING: Node.js may not be in PATH yet" -ForegroundColor Yellow
        }
    }
    
    # Check npm
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        $npmVersion = npm --version
        Write-Host "npm available - v$npmVersion" -ForegroundColor Green
    }
    
    # Install Bun
    Write-Host ""
    Write-Host "Installing Bun..."
    if (Get-Command bun -ErrorAction SilentlyContinue) {
        $bunVersion = bun --version
        Write-Host "Bun already installed - v$bunVersion" -ForegroundColor Green
    } else {
        # Install Bun via PowerShell installer
        try {
            Invoke-RestMethod -Uri "https://bun.sh/install.ps1" | Invoke-Expression
            
            # Add Bun to PATH for current session
            $bunPath = "$env:USERPROFILE\.bun\bin"
            if (Test-Path $bunPath) {
                $env:Path = "$bunPath;$env:Path"
            }
            
            if (Get-Command bun -ErrorAction SilentlyContinue) {
                $bunVersion = bun --version
                Write-Host "SUCCESS: Bun installed - v$bunVersion" -ForegroundColor Green
            } else {
                Write-Host "WARNING: Bun installed but not in PATH" -ForegroundColor Yellow
                Write-Host "Expected location: $bunPath" -ForegroundColor Gray
            }
        } catch {
            Write-Host "WARNING: Failed to install Bun - $_" -ForegroundColor Yellow
        }
    }
    
    # Verify npx is available (comes with npm)
    if (Get-Command npx -ErrorAction SilentlyContinue) {
        Write-Host "npx available" -ForegroundColor Green
    } else {
        Write-Host "WARNING: npx not found (should come with npm)" -ForegroundColor Yellow
    }
}

function Install-AgentService {
    param(
        [string]$GithubToken,
        [string]$Staging
    )
    
    Write-Host ""
    Write-Host "=== Installing/Updating Magnitude & Agent Service ===" -ForegroundColor Cyan
    
    $magnitudeDir = 'C:\magnitude'
    $agentServiceDir = 'C:\agent-service'
    $unityRepoDir = 'C:\temp\unity-repo'
    
    # Determine branch for unity repo (magnitude always uses unity-modifications)
    $unityBranch = if ($Staging) { "staging" } else { "main" }
    Write-Host "Magnitude branch: unity-modifications (fixed)" -ForegroundColor Cyan
    Write-Host "Unity branch: $unityBranch" -ForegroundColor Cyan
    
    # Build repo URLs
    $magnitudeUrl = if ($GithubToken) { 
        "https://$GithubToken@github.com/unifyai/magnitude.git" 
    } else { 
        "https://github.com/unifyai/magnitude.git" 
    }
    $unityUrl = if ($GithubToken) { 
        "https://$GithubToken@github.com/unifyai/unity.git" 
    } else { 
        "https://github.com/unifyai/unity.git" 
    }
    
    # =========================================================================
    # MAGNITUDE: Update if exists (has .git), otherwise clone fresh
    # =========================================================================
    if (Test-Path "$magnitudeDir\.git") {
        # Existing git repo - update it
        $updated = Update-GitRepo -RepoPath $magnitudeDir -Branch "unity-modifications" -GithubToken $GithubToken -RepoName "magnitude"
        
        if ($updated) {
            # Reinstall dependencies (in case package.json changed)
            Write-Host "Reinstalling Magnitude dependencies..."
            Push-Location $magnitudeDir
            if (Get-Command bun -ErrorAction SilentlyContinue) {
                bun install 2>&1 | Out-Null
                Write-Host "Magnitude dependencies installed" -ForegroundColor Green
            } else {
                npm install 2>&1 | Out-Null
            }
            Pop-Location
        }
    } else {
        # No git repo - clone fresh
        Write-Host "Cloning Magnitude repository (unity-modifications branch)..."
        
        if (Test-Path $magnitudeDir) {
            Remove-Item -Recurse -Force $magnitudeDir -ErrorAction SilentlyContinue
        }
        
        git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir 2>&1
        
        if (Test-Path "$magnitudeDir\package.json") {
            Write-Host "SUCCESS: Magnitude cloned (unity-modifications)" -ForegroundColor Green
            
            # Install dependencies
            Write-Host "Installing Magnitude dependencies (bun install)..."
            Push-Location $magnitudeDir
            if (Get-Command bun -ErrorAction SilentlyContinue) {
                bun install 2>&1
                Write-Host "Dependencies installed" -ForegroundColor Green
            } else {
                Write-Host "WARNING: Bun not found, trying npm..." -ForegroundColor Yellow
                npm install 2>&1
            }
            Pop-Location
        } else {
            Write-Host "WARNING: Magnitude may not have cloned correctly" -ForegroundColor Yellow
        }
    }
    
    # =========================================================================
    # AGENT-SERVICE: Backup .env, delete, re-clone, restore .env
    # (Cannot git update - it's extracted from sparse checkout, not a git repo)
    # =========================================================================
    
    # Step 1: Backup .env if it exists
    $envBackup = $null
    $envFile = "$agentServiceDir\.env"
    if (Test-Path $envFile) {
        Write-Host "Backing up .env file..." -ForegroundColor Yellow
        $envBackup = Get-Content $envFile -Raw
    }
    
    # Step 2: Check if we need to update or install
    $needsClone = $true
    if (Test-Path "$agentServiceDir\package.json") {
        Write-Host "Agent Service exists - updating..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $agentServiceDir -ErrorAction SilentlyContinue
    } else {
        Write-Host "Agent Service not found - installing..." -ForegroundColor Cyan
    }
    
    # Step 3: Clone and extract agent-service
    Write-Host "Cloning Unity repository ($unityBranch branch) for agent-service..."
    
    New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
    
    if (Test-Path $unityRepoDir) {
        Remove-Item -Recurse -Force $unityRepoDir -ErrorAction SilentlyContinue
    }
    
    # Sparse checkout to get only agent-service (from dynamic branch)
    git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $unityRepoDir 2>&1
    
    if (Test-Path $unityRepoDir) {
        Push-Location $unityRepoDir
        git sparse-checkout set agent-service 2>&1
        Pop-Location
        
        # Move agent-service to final location
        if (Test-Path "$unityRepoDir\agent-service") {
            if (Test-Path $agentServiceDir) {
                Remove-Item -Recurse -Force $agentServiceDir -ErrorAction SilentlyContinue
            }
            Move-Item "$unityRepoDir\agent-service" $agentServiceDir
            
            if (Test-Path "$agentServiceDir\package.json") {
                Write-Host "SUCCESS: Agent Service extracted" -ForegroundColor Green
                
                # Install dependencies
                Write-Host "Installing Agent Service dependencies..."
                Push-Location $agentServiceDir
                if (Get-Command bun -ErrorAction SilentlyContinue) {
                    bun install 2>&1
                    Write-Host "Dependencies installed" -ForegroundColor Green
                } else {
                    Write-Host "WARNING: Bun not found, trying npm..." -ForegroundColor Yellow
                    npm install 2>&1
                }
                Pop-Location
            } else {
                Write-Host "WARNING: Agent Service package.json not found" -ForegroundColor Yellow
            }
        } else {
            Write-Host "WARNING: agent-service folder not found in unity repo" -ForegroundColor Yellow
        }
        
        # Cleanup temp repo
        Remove-Item -Recurse -Force $unityRepoDir -ErrorAction SilentlyContinue
    } else {
        Write-Host "WARNING: Failed to clone unity repository" -ForegroundColor Yellow
    }
    
    # Step 4: Restore .env file if we had a backup
    if ($envBackup -and (Test-Path $agentServiceDir)) {
        Write-Host "Restoring .env file..." -ForegroundColor Green
        $envBackup | Out-File -FilePath $envFile -Encoding UTF8 -NoNewline
        Write-Host ".env file restored" -ForegroundColor Green
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
        Write-Host "Agent Service not installed, skipping .env setup" -ForegroundColor Yellow
        return
    }
    
    # Create .env file
    $envContent = @"
# Agent Service Environment Configuration
# Generated: $(Get-Date)

PORT=3000
NODE_ENV=production
"@
    
    if ($AnthropicApiKey) {
        $envContent += "`nANTHROPIC_API_KEY=$AnthropicApiKey"
        Write-Host "  ANTHROPIC_API_KEY: (set)" -ForegroundColor Green
    } else {
        Write-Host "  ANTHROPIC_API_KEY: (not provided)" -ForegroundColor Yellow
    }
    
    if ($UnifyKey) {
        $envContent += "`nUNIFY_KEY=$UnifyKey"
        Write-Host "  UNIFY_KEY: (set)" -ForegroundColor Green
    } else {
        Write-Host "  UNIFY_KEY: (not provided)" -ForegroundColor Yellow
    }
    
    if ($UnifyBaseUrl) {
        $envContent += "`nUNIFY_BASE_URL=$UnifyBaseUrl"
        Write-Host "  UNIFY_BASE_URL: $UnifyBaseUrl" -ForegroundColor Green
    }
    
    $envContent | Out-File -FilePath $envFile -Encoding UTF8
    Write-Host ".env file created at: $envFile" -ForegroundColor Green
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
    
    # Enable TightVNC service for automatic startup
    # Service mode runs in Session 0 - works well with websockify connecting via localhost
    Write-Host "Enabling TightVNC service for automatic startup..."
    Set-Service -Name "tvnserver" -StartupType Automatic -ErrorAction SilentlyContinue
    Write-Host "TightVNC service configured for automatic startup" -ForegroundColor Green
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
        } else {
            Write-Host "WARNING: noVNC may not have installed correctly" -ForegroundColor Yellow
        }
    }
    
    # Always create/update custom.html (iframe wrapper for clean noVNC experience)
    # Hides control bar, logo, and remote cursor via CSS injection
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
    
    # Routing: /desktop -> noVNC (6080), /api -> Agent Service (3000)
    # Root path (/) returns 404
    $caddyConfig = @"
# Unity Windows VM - Caddy Configuration
# Hostname: $Hostname
# Generated: $(Get-Date)

$Hostname {
    # Handle WebSocket upgrade for noVNC (only for /desktop paths)
    @websocket {
        path /desktop/*
        header Connection *Upgrade*
        header Upgrade websocket
    }
    reverse_proxy @websocket localhost:6080

    # Exact match for /desktop (redirect to /desktop/)
    @desktop_exact path /desktop
    handle @desktop_exact {
        redir /desktop/ permanent
    }

    # noVNC desktop sub-resources at /desktop/*
    handle_path /desktop/* {
        reverse_proxy localhost:6080
    }

    # Exact match for /api (redirect to /api/)
    @api_exact path /api
    handle @api_exact {
        redir /api/ permanent
    }

    # Agent Service API at /api/*
    handle_path /api/* {
        reverse_proxy localhost:3000
    }

    # Block all other paths (including root /)
    handle {
        respond "Not Found" 404
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
    Write-Host "  HTTPS route: https://$Hostname/desktop/ -> noVNC (localhost:6080)" -ForegroundColor Gray
    
    return $true
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

function Setup-DisplayResolution {
    param(
        [int]$Width = 1920,
        [int]$Height = 1080,
        [string]$TargetUser
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
    
    # Create scheduled task to run resolution script at login (before TightVNC starts)
    $taskName = "SetDisplayResolution"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for display resolution (hidden)..."
    # Execute PowerShell directly with hidden window (delay is handled via task settings)
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"Start-Sleep -Seconds 3; & '$scriptPath'`"" -WorkingDirectory $novncDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    # If TargetUser specified, schedule for that user's logon (runs in their interactive session)
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
    
    # Also run immediately in current session (only effective for manual/interactive runs)
    Write-Host "Applying display resolution now..."
    & powershell.exe -ExecutionPolicy Bypass -File $scriptPath
    Write-Host "Display resolution applied" -ForegroundColor Green
}

function Setup-InvisibleCursor {
    param(
        [string]$TargetUser
    )
    
    Write-Host ""
    Write-Host "=== Setting up Invisible Cursor ===" -ForegroundColor Cyan
    
    $cursorDir = 'C:\Windows\Cursors'
    $blankCursorPath = "$cursorDir\blank.cur"
    $novncDir = 'C:\novnc'
    
    New-Item -ItemType Directory -Force -Path $novncDir | Out-Null
    
    # Check if blank cursor already exists
    if (Test-Path $blankCursorPath) {
        Write-Host "Blank cursor already exists at: $blankCursorPath" -ForegroundColor Green
    } else {
        # Create a minimal transparent 32x32 cursor file
        # Minimal 32x32 1-bit transparent cursor (326 bytes)
        $curHeader = [byte[]]@(
            0x00, 0x00,       # Reserved (must be 0)
            0x02, 0x00,       # Type (2 = cursor)
            0x01, 0x00        # Number of images (1)
        )
        
        $curDirEntry = [byte[]]@(
            0x20,             # Width (32)
            0x20,             # Height (32)
            0x00,             # Color count (0 = more than 256)
            0x00,             # Reserved
            0x00, 0x00,       # Hotspot X (0)
            0x00, 0x00,       # Hotspot Y (0)
            0x30, 0x01, 0x00, 0x00,  # Size of image data (304 bytes)
            0x16, 0x00, 0x00, 0x00   # Offset to image data (22 bytes)
        )
        
        # BITMAPINFOHEADER for 32x32 1-bit cursor
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
        
        # Color table (2 entries for 1-bit: black and white)
        $colorTable = [byte[]]@(
            0x00, 0x00, 0x00, 0x00,  # Black (BGRX)
            0xFF, 0xFF, 0xFF, 0x00   # White (BGRX)
        )
        
        # XOR mask: 32x32 bits = 128 bytes (all zeros = use AND mask colors)
        $xorMask = New-Object byte[] 128
        
        # AND mask: 32x32 bits = 128 bytes (all 1s = fully transparent)
        $andMask = New-Object byte[] 128
        for ($i = 0; $i -lt 128; $i++) {
            $andMask[$i] = 0xFF
        }
        
        # Combine all parts
        $cursorData = $curHeader + $curDirEntry + $bmpHeader + $colorTable + $xorMask + $andMask
        
        try {
            [System.IO.File]::WriteAllBytes($blankCursorPath, $cursorData)
            Write-Host "Created blank cursor at: $blankCursorPath" -ForegroundColor Green
        } catch {
            Write-Host "WARNING: Could not create blank cursor - $_" -ForegroundColor Yellow
            return
        }
    }
    
    # Create PowerShell script that applies invisible cursor (runs in user's session)
    $cursorScript = @'
# Apply invisible cursor settings
# This script runs at user logon to set cursor to transparent

$blankCursorPath = 'C:\Windows\Cursors\blank.cur'

if (-not (Test-Path $blankCursorPath)) {
    "Blank cursor not found: $blankCursorPath" | Out-File -FilePath "C:\novnc\cursor.log" -Append -Encoding UTF8
    exit 1
}

# Set all cursor types to use the blank cursor
$cursorTypes = @(
    'Arrow', 'Help', 'AppStarting', 'Wait', 'NWPen', 'No',
    'SizeNS', 'SizeWE', 'Crosshair', 'IBeam', 'SizeNWSE',
    'SizeNESW', 'SizeAll', 'UpArrow', 'Hand'
)

$regPath = 'HKCU:\Control Panel\Cursors'
if (-not (Test-Path $regPath)) {
    New-Item -Path $regPath -Force | Out-Null
}

foreach ($type in $cursorTypes) {
    Set-ItemProperty -Path $regPath -Name $type -Value $blankCursorPath -ErrorAction SilentlyContinue
}

# Disable touch and gesture visualizations
Set-ItemProperty -Path $regPath -Name 'ContactVisualization' -Value 0 -Type DWord -ErrorAction SilentlyContinue
Set-ItemProperty -Path $regPath -Name 'GestureVisualization' -Value 0 -Type DWord -ErrorAction SilentlyContinue

# Apply cursor changes immediately using SystemParametersInfo
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

try {
    Add-Type -TypeDefinition $cursorHelperCode -Language CSharp -ErrorAction SilentlyContinue
} catch {
    # Type may already be loaded
}

$result = [CursorHelperLogon]::ApplyCursors()
"$(Get-Date): Cursor applied, result: $result" | Out-File -FilePath "C:\novnc\cursor.log" -Append -Encoding UTF8
'@
    
    $scriptPath = "$novncDir\set-invisible-cursor.ps1"
    $cursorScript | Out-File -FilePath $scriptPath -Encoding UTF8
    Write-Host "Created cursor script at $scriptPath" -ForegroundColor Green
    
    # Create scheduled task to run cursor script at login (runs in user's interactive session)
    $taskName = "SetInvisibleCursor"
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Write-Host "Removing existing scheduled task..."
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    
    Write-Host "Creating scheduled task for invisible cursor (hidden)..."
    # Execute PowerShell with hidden window, small delay to ensure desktop is ready
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"Start-Sleep -Seconds 2; & '$scriptPath'`"" -WorkingDirectory $novncDir
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    
    # If TargetUser specified, schedule for that user's logon (runs in their interactive session)
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
    
    # Also try to apply immediately in current session (only effective for interactive runs)
    # For existing logged-in users, we trigger the scheduled task to run now
    Write-Host "Applying cursor changes now..."
    
    # Try to run the task immediately for existing sessions
    try {
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Host "Triggered cursor task to run immediately" -ForegroundColor Green
    } catch {
        Write-Host "Cursor task scheduled (will apply at next logon)" -ForegroundColor Yellow
    }
    
    Write-Host "Note: Native Windows cursor will be invisible for VNC streaming" -ForegroundColor Gray
}

function Configure-Firewall {
    Write-Host ""
    Write-Host "=== Configuring Firewall ===" -ForegroundColor Cyan
    
    # Remove old rules if they exist (to update them)
    Remove-NetFirewallRule -DisplayName "Unity-noVNC" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-HTTPS" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-HTTP" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-VNC-Local" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "Unity-AgentService" -ErrorAction SilentlyContinue
    
    # Allow HTTPS (443) - Caddy reverse proxy
    New-NetFirewallRule -DisplayName "Unity-HTTPS" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow HTTPS (443)" -ForegroundColor Green
    
    # Allow HTTP (80) - Let's Encrypt ACME challenge
    New-NetFirewallRule -DisplayName "Unity-HTTP" -Direction Inbound -LocalPort 80 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow HTTP (80)" -ForegroundColor Green
    
    # Allow noVNC/websockify (6080) - for direct access / fallback
    New-NetFirewallRule -DisplayName "Unity-noVNC" -Direction Inbound -LocalPort 6080 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow noVNC (6080)" -ForegroundColor Green
    
    # Allow Agent Service (3000) - for direct API access / fallback
    New-NetFirewallRule -DisplayName "Unity-AgentService" -Direction Inbound -LocalPort 3000 -Protocol TCP -Action Allow -Profile Any
    Write-Host "Firewall rule created: Allow Agent Service (3000)" -ForegroundColor Green
    
    Write-Host "Firewall configured" -ForegroundColor Green
}

function Start-AllServices {
    Write-Host ""
    Write-Host "=== Starting Services ===" -ForegroundColor Cyan
    
    # Start TightVNC service
    Write-Host "Starting TightVNC service..."
    Start-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    
    # Check if TightVNC service is running
    $tvnService = Get-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    if ($tvnService -and $tvnService.Status -eq 'Running') {
        Write-Host "  TightVNC (service) : Running" -ForegroundColor Green
    } else {
        Write-Host "  TightVNC (service) : Not Running" -ForegroundColor Red
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
    
    # Start Agent Service
    $agentServiceDir = 'C:\agent-service'
    if (Test-Path "$agentServiceDir\package.json") {
        Write-Host "Starting Agent Service..."
        
        # Kill any existing agent service process on port 3000
        $existingProcess = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingProcess) {
            Stop-Process -Id $existingProcess.OwningProcess -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
        
        # Create startup script for agent service
        $agentStartScript = @"
@echo off
cd /d C:\agent-service
npx ts-node src/index.ts >> C:\agent-service\agent.log 2>&1
"@
        $agentStartScript | Out-File -FilePath "$agentServiceDir\start-agent.bat" -Encoding ASCII
        
        # Create scheduled task for auto-start at login (for persistence)
        $taskName = "StartAgentService"
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existingTask) {
            Write-Host "  Updating scheduled task for Agent Service..."
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        
        $action = New-ScheduledTaskAction -Execute "$agentServiceDir\start-agent.bat" -WorkingDirectory $agentServiceDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "  Scheduled task '$taskName' created for auto-start at login" -ForegroundColor Green
        
        # Start agent service now
        Start-Process -FilePath "$agentServiceDir\start-agent.bat" -WorkingDirectory $agentServiceDir -WindowStyle Hidden
        Start-Sleep -Seconds 5
        
        # Check if agent service is running
        $listening3000 = netstat -an | Select-String ":3000.*LISTENING"
        if ($listening3000) {
            Write-Host "  Agent Service : Running (port 3000 listening)" -ForegroundColor Green
        } else {
            Write-Host "  Agent Service : Not listening on port 3000 yet" -ForegroundColor Yellow
            Write-Host "  Check logs: C:\agent-service\agent.log" -ForegroundColor Gray
        }
    } else {
        Write-Host "  Agent Service : Not installed" -ForegroundColor Gray
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
    Write-Host "  - Node.js v22 + npm"
    Write-Host "  - Bun"
    Write-Host "  - TightVNC Server (port 5900, service mode)"
    Write-Host "  - noVNC + websockify (port 6080)"
    Write-Host "  - Invisible cursor (for clean VNC streaming)"
    if (Test-Path 'C:\magnitude\package.json') {
        Write-Host "  - Magnitude (unity-modifications)"
    }
    if (Test-Path 'C:\agent-service\package.json') {
        Write-Host "  - Agent Service (port 3000)"
    }
    if ($Hostname) {
        Write-Host "  - Caddy HTTPS reverse proxy (ports 80, 443)"
    }
    Write-Host ""
    Write-Host "Paths:"
    Write-Host "  Excel:         C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
    Write-Host "  Git:           C:\Program Files\Git\bin\git.exe"
    Write-Host "  Python:        C:\Program Files\Python312\python.exe"
    Write-Host "  Node.js:       C:\Program Files\nodejs\node.exe"
    Write-Host "  Bun:           $env:USERPROFILE\.bun\bin\bun.exe"
    Write-Host "  noVNC:         C:\novnc"
    Write-Host "  Magnitude:     C:\magnitude"
    Write-Host "  Agent Service: C:\agent-service"
    if ($Hostname) {
        Write-Host "  Caddy:         C:\caddy"
    }
    Write-Host ""
    Write-Host "Logs:"
    Write-Host "  websockify:    C:\novnc\websockify.log"
    Write-Host "  Agent Service: C:\agent-service\agent.log"
    if ($Hostname) {
        Write-Host "  Caddy:         C:\caddy\access.log"
    }
    Write-Host ""
    
    # Access information
    $ipAddress = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch "Loopback" } | Select-Object -First 1).IPAddress
    
    if ($Hostname) {
        Write-Host "HTTPS Access (via Caddy):" -ForegroundColor Cyan
        Write-Host "  Desktop: https://$Hostname/desktop/" -ForegroundColor Green
        Write-Host "  API:     https://$Hostname/api/" -ForegroundColor Green
        Write-Host ""
        Write-Host "Direct HTTP Access (fallback):" -ForegroundColor Cyan
        Write-Host "  Desktop: http://$ipAddress`:6080/vnc.html" -ForegroundColor Gray
        Write-Host "  API:     http://$ipAddress`:3000/" -ForegroundColor Gray
    } else {
        Write-Host "HTTP Access (no hostname configured):" -ForegroundColor Yellow
        Write-Host "  Desktop: http://$ipAddress`:6080/vnc.html" -ForegroundColor Green
        Write-Host "  API:     http://$ipAddress`:3000/" -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "VNC Password: $VncPassword" -ForegroundColor Cyan
    Write-Host ""
    
    if (-not $MakKey) {
        Write-Host "REMINDER: Office is not activated. Run with MAK key to activate:" -ForegroundColor Yellow
        Write-Host '  .\init.ps1 "MAK-KEY" "VNC-PASSWORD"' -ForegroundColor Gray
    }
    
    Write-Host ""
    Write-Host "Script usage:" -ForegroundColor Cyan
    Write-Host '  .\init.ps1                        # Defaults only'
    Write-Host '  .\init.ps1 "MAK-KEY"              # With Office activation'
    Write-Host '  .\init.ps1 "MAK-KEY" "mypassword" # Custom VNC password'
    Write-Host ""
}

# =============================================================================
# Main Installation Flow
# =============================================================================

# Try to get configuration from GCP metadata first, then fall back to arguments
Write-Host "Checking for GCP metadata..."
$gcpVncPassword = Get-GCPMetadata -Key "vnc-password"
$gcpHostname = Get-GCPMetadata -Key "hostname"
$gcpWindowsUser = Get-GCPMetadata -Key "windows-username"
$gcpWindowsPassword = Get-GCPMetadata -Key "windows-password"
$gcpGithubToken = Get-GCPMetadata -Key "github-token"
$gcpAnthropicKey = Get-GCPMetadata -Key "anthropic-api-key"
$gcpUnifyKey = Get-GCPMetadata -Key "unify-key"
$gcpUnifyBaseUrl = Get-GCPMetadata -Key "unify-base-url"
$gcpStaging = Get-GCPMetadata -Key "staging"

if ($gcpHostname) {
    Write-Host "Found hostname metadata: $gcpHostname" -ForegroundColor Cyan
}
if ($gcpWindowsUser) {
    Write-Host "Found windows-username metadata: $gcpWindowsUser" -ForegroundColor Cyan
}
if ($gcpGithubToken) {
    Write-Host "Found github-token metadata: (set)" -ForegroundColor Cyan
}
if ($gcpStaging) {
    Write-Host "Found staging metadata: enabled" -ForegroundColor Cyan
}

# Parse arguments (GCP metadata takes precedence for vnc-password)
$makKey = $args[0]
$vncPassword = if ($gcpVncPassword) { $gcpVncPassword } elseif ($args[1]) { $args[1] } else { "unify123" }
$hostname = if ($gcpHostname) { $gcpHostname } else { $null }
$windowsUser = if ($gcpWindowsUser) { $gcpWindowsUser } else { $null }
$windowsPassword = if ($gcpWindowsPassword) { $gcpWindowsPassword } else { $null }

Write-Host ""
Write-Host "Configuration:" -ForegroundColor Cyan
Write-Host "  VNC Password:    $vncPassword"
Write-Host "  Office MAK:      $(if ($makKey) { '(provided)' } else { '(not provided)' })"
Write-Host "  Hostname:        $(if ($hostname) { $hostname } else { '(not configured - no HTTPS)' })"
Write-Host "  Windows User:    $(if ($windowsUser) { $windowsUser } else { '(not configured)' })"
Write-Host "  GitHub Token:    $(if ($gcpGithubToken) { '(set)' } else { '(not provided)' })"
Write-Host "  Staging Branch:  $(if ($gcpStaging) { 'yes' } else { 'no' })"
Write-Host ""

# =============================================================================
# Phase 1: User Setup (may trigger reboot if NEW user created)
# =============================================================================

$newUserCreated = $null
if ($windowsUser -and $windowsPassword) {
    $newUserCreated = Setup-WindowsUser -Username $windowsUser -Password $windowsPassword
    Configure-AutoLogon -Username $windowsUser -Password $windowsPassword
}

# =============================================================================
# Phase 2: Software Installations (with parallelization)
# =============================================================================

Write-Host ""
Write-Host "=== Phase 2: Software Installations ===" -ForegroundColor Cyan

# Office must be installed first (longest, 20-40 min, and critical)
Install-Office -MakKey $makKey

# -----------------------------------------------------------------------------
# PARALLEL GROUP 1: Foundation tools (no dependencies between them)
# Git, Python, Chocolatey, TightVNC, Caddy can all download/install simultaneously
# Using Start-Job with self-contained script blocks for reliability
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Installing foundation tools in parallel ===" -ForegroundColor Cyan
$parallelStartTime = Get-Date

$jobs = @()

# Job 1: Install Git
$jobs += Start-Job -Name "Install-Git" -ScriptBlock {
    $gitInstallerUrl = 'https://github.com/git-for-windows/git/releases/download/v2.43.0.windows.1/Git-2.43.0-64-bit.exe'
    $gitInstallerPath = 'C:\temp\git-installer.exe'
    
    if (Get-Command git -ErrorAction SilentlyContinue) {
        return "Git already installed"
    }
    
    New-Item -ItemType Directory -Force -Path C:\temp | Out-Null
    Invoke-WebRequest -Uri $gitInstallerUrl -OutFile $gitInstallerPath
    $proc = Start-Process -FilePath $gitInstallerPath -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS="icons,ext\reg\shellhere,assoc,assoc_sh"' -PassThru -Wait -NoNewWindow
    Remove-Item $gitInstallerPath -Force -ErrorAction SilentlyContinue
    
    if (Test-Path 'C:\Program Files\Git\bin\git.exe') {
        return "Git installed successfully (exit code: $($proc.ExitCode))"
    } else {
        throw "Git installation failed"
    }
}

# Job 2: Install Python
$jobs += Start-Job -Name "Install-Python" -ScriptBlock {
    $pythonInstallerUrl = 'https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe'
    $pythonInstallerPath = 'C:\temp\python-installer.exe'
    $realPythonPath = 'C:\Program Files\Python312\python.exe'
    
    if (Test-Path $realPythonPath) {
        return "Python already installed"
    }
    
    New-Item -ItemType Directory -Force -Path C:\temp | Out-Null
    Invoke-WebRequest -Uri $pythonInstallerUrl -OutFile $pythonInstallerPath
    $proc = Start-Process -FilePath $pythonInstallerPath -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -PassThru -Wait -NoNewWindow
    Remove-Item $pythonInstallerPath -Force -ErrorAction SilentlyContinue
    
    if (Test-Path $realPythonPath) {
        # Upgrade pip
        & $realPythonPath -m pip install --upgrade pip 2>&1 | Out-Null
        return "Python installed successfully (exit code: $($proc.ExitCode))"
    } else {
        throw "Python installation failed"
    }
}

# Job 3: Install Chocolatey
$jobs += Start-Job -Name "Install-Chocolatey" -ScriptBlock {
    if (Test-Path 'C:\ProgramData\chocolatey\bin\choco.exe') {
        return "Chocolatey already installed"
    }
    
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    
    if (Test-Path 'C:\ProgramData\chocolatey\bin\choco.exe') {
        return "Chocolatey installed successfully"
    } else {
        throw "Chocolatey installation failed"
    }
}

# Job 4: Install TightVNC
$vncPwd = $vncPassword  # Capture for job scope
$jobs += Start-Job -Name "Install-TightVNC" -ScriptBlock {
    param($Password)
    
    $tvnServerPath = 'C:\Program Files\TightVNC\tvnserver.exe'
    
    if (Test-Path $tvnServerPath) {
        return "TightVNC already installed"
    }
    
    $vncInstallerUrl = 'https://www.tightvnc.com/download/2.8.81/tightvnc-2.8.81-gpl-setup-64bit.msi'
    $vncInstallerPath = 'C:\temp\tightvnc.msi'
    
    New-Item -ItemType Directory -Force -Path C:\temp | Out-Null
    Invoke-WebRequest -Uri $vncInstallerUrl -OutFile $vncInstallerPath -UseBasicParsing
    
    $vncArgs = @(
        "/i", $vncInstallerPath,
        "/quiet", "/norestart",
        "ADDLOCAL=Server",
        "SET_USEVNCAUTHENTICATION=1", "VALUE_OF_USEVNCAUTHENTICATION=1",
        "SET_PASSWORD=1", "VALUE_OF_PASSWORD=$Password",
        "SET_USECONTROLAUTHENTICATION=1", "VALUE_OF_USECONTROLAUTHENTICATION=1",
        "SET_CONTROLPASSWORD=1", "VALUE_OF_CONTROLPASSWORD=$Password"
    )
    $proc = Start-Process msiexec.exe -ArgumentList $vncArgs -Wait -NoNewWindow -PassThru
    Remove-Item $vncInstallerPath -Force -ErrorAction SilentlyContinue
    
    if (Test-Path $tvnServerPath) {
        return "TightVNC installed successfully (exit code: $($proc.ExitCode))"
    } else {
        throw "TightVNC installation failed"
    }
} -ArgumentList $vncPwd

# Job 5: Install Caddy
$jobs += Start-Job -Name "Install-Caddy" -ScriptBlock {
    $caddyDir = 'C:\caddy'
    $caddyExe = "$caddyDir\caddy.exe"
    
    if (Test-Path $caddyExe) {
        return "Caddy already installed"
    }
    
    New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null
    $caddyUrl = "https://github.com/caddyserver/caddy/releases/download/v2.7.6/caddy_2.7.6_windows_amd64.zip"
    $caddyZip = "$caddyDir\caddy.zip"
    
    Invoke-WebRequest -Uri $caddyUrl -OutFile $caddyZip -UseBasicParsing
    Expand-Archive -Path $caddyZip -DestinationPath $caddyDir -Force
    Remove-Item $caddyZip -Force -ErrorAction SilentlyContinue
    
    if (Test-Path $caddyExe) {
        return "Caddy installed successfully"
    } else {
        throw "Caddy installation failed"
    }
}

# Wait for all parallel jobs
Write-Host "  Started $($jobs.Count) parallel install jobs..." -ForegroundColor Gray
$jobs | Wait-Job | Out-Null

# Report results
foreach ($job in $jobs) {
    $result = Receive-Job -Job $job -ErrorAction SilentlyContinue
    $error = $job.ChildJobs[0].JobStateInfo.Reason
    
    if ($job.State -eq 'Completed') {
        Write-Host "  $($job.Name): $result" -ForegroundColor Green
    } else {
        Write-Host "  $($job.Name): FAILED - $error" -ForegroundColor Red
    }
    Remove-Job -Job $job -Force
}

$parallelElapsed = (Get-Date) - $parallelStartTime
Write-Host "  Parallel group 1 completed in $([math]::Round($parallelElapsed.TotalSeconds, 1))s" -ForegroundColor Magenta

# Refresh PATH after parallel installs (jobs run in separate processes)
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Run TightVNC configuration (needs to be in main process for registry access)
Write-Host ""
Write-Host "Configuring TightVNC settings..." -ForegroundColor Cyan
$regPaths = @(
    'HKLM:\SOFTWARE\TightVNC\Server',
    'HKLM:\SOFTWARE\WOW6432Node\TightVNC\Server',
    'HKCU:\SOFTWARE\TightVNC\Server',
    'HKCU:\SOFTWARE\WOW6432Node\TightVNC\Server'
)
foreach ($regPath in $regPaths) {
    if (-not (Test-Path $regPath)) {
        New-Item -Path $regPath -Force | Out-Null
    }
    Set-ItemProperty -Path $regPath -Name 'AllowLoopback' -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $regPath -Name 'AcceptRfbConnections' -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $regPath -Name 'UseVncAuthentication' -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $regPath -Name 'QueryIfNoPassword' -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $regPath -Name 'RfbPort' -Value 5900 -Type DWord -Force -ErrorAction SilentlyContinue
}
Set-Service -Name "tvnserver" -StartupType Automatic -ErrorAction SilentlyContinue
Write-Host "TightVNC configured" -ForegroundColor Green

# -----------------------------------------------------------------------------
# SEQUENTIAL: Node.js (depends on Chocolatey)
# -----------------------------------------------------------------------------
Install-NodeJS

# Refresh PATH again after Node.js install
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Add Bun to PATH if installed
$bunPath = "$env:USERPROFILE\.bun\bin"
if (Test-Path $bunPath) {
    $env:Path = "$bunPath;$env:Path"
}

# -----------------------------------------------------------------------------
# PARALLEL GROUP 2: Services that need Git/Python/Node (install in parallel)
# noVNC and AgentService can install simultaneously
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Installing services in parallel ===" -ForegroundColor Cyan
$parallelStartTime2 = Get-Date

$jobs2 = @()

# Job: Install noVNC
$jobs2 += Start-Job -Name "Install-NoVNC" -ScriptBlock {
    $novncDir = 'C:\novnc'
    $pythonExe = 'C:\Program Files\Python312\python.exe'
    
    # Refresh PATH in job
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    
    if (-not (Test-Path "$novncDir\vnc.html")) {
        if (Test-Path $novncDir) {
            Remove-Item -Recurse -Force $novncDir -ErrorAction SilentlyContinue
        }
        git clone --depth 1 https://github.com/novnc/noVNC.git $novncDir 2>&1 | Out-Null
    }
    
    # Install websockify
    if (Test-Path $pythonExe) {
        & $pythonExe -m pip install websockify --quiet 2>&1 | Out-Null
    }
    
    if (Test-Path "$novncDir\vnc.html") {
        return "noVNC installed successfully"
    } else {
        throw "noVNC installation failed"
    }
}

# Job: Install AgentService
$ghToken = $gcpGithubToken
$staging = $gcpStaging
$jobs2 += Start-Job -Name "Install-AgentService" -ScriptBlock {
    param($GithubToken, $Staging)
    
    $magnitudeDir = 'C:\magnitude'
    $agentServiceDir = 'C:\agent-service'
    $unityRepoDir = 'C:\temp\unity-repo'
    
    # Refresh PATH in job
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $bunPath = "$env:USERPROFILE\.bun\bin"
    if (Test-Path $bunPath) { $env:Path = "$bunPath;$env:Path" }
    
    $unityBranch = if ($Staging) { "staging" } else { "main" }
    
    # Build URLs
    $magnitudeUrl = if ($GithubToken) { "https://$GithubToken@github.com/unifyai/magnitude.git" } else { "https://github.com/unifyai/magnitude.git" }
    $unityUrl = if ($GithubToken) { "https://$GithubToken@github.com/unifyai/unity.git" } else { "https://github.com/unifyai/unity.git" }
    
    # Clone Magnitude
    if (-not (Test-Path "$magnitudeDir\.git")) {
        if (Test-Path $magnitudeDir) { Remove-Item -Recurse -Force $magnitudeDir -ErrorAction SilentlyContinue }
        git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir 2>&1 | Out-Null
        
        if (Test-Path "$magnitudeDir\package.json") {
            Push-Location $magnitudeDir
            if (Get-Command bun -ErrorAction SilentlyContinue) {
                bun install 2>&1 | Out-Null
            } else {
                npm install 2>&1 | Out-Null
            }
            Pop-Location
        }
    }
    
    # Clone and extract agent-service
    $envBackup = $null
    if (Test-Path "$agentServiceDir\.env") {
        $envBackup = Get-Content "$agentServiceDir\.env" -Raw
    }
    
    if (Test-Path "$agentServiceDir\package.json") {
        Remove-Item -Recurse -Force $agentServiceDir -ErrorAction SilentlyContinue
    }
    
    New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
    if (Test-Path $unityRepoDir) { Remove-Item -Recurse -Force $unityRepoDir -ErrorAction SilentlyContinue }
    
    git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $unityRepoDir 2>&1 | Out-Null
    
    if (Test-Path $unityRepoDir) {
        Push-Location $unityRepoDir
        git sparse-checkout set agent-service 2>&1 | Out-Null
        Pop-Location
        
        if (Test-Path "$unityRepoDir\agent-service") {
            Move-Item "$unityRepoDir\agent-service" $agentServiceDir -Force
            
            if (Test-Path "$agentServiceDir\package.json") {
                Push-Location $agentServiceDir
                if (Get-Command bun -ErrorAction SilentlyContinue) {
                    bun install 2>&1 | Out-Null
                } else {
                    npm install 2>&1 | Out-Null
                }
                Pop-Location
            }
        }
        Remove-Item -Recurse -Force $unityRepoDir -ErrorAction SilentlyContinue
    }
    
    # Restore .env
    if ($envBackup -and (Test-Path $agentServiceDir)) {
        $envBackup | Out-File -FilePath "$agentServiceDir\.env" -Encoding UTF8 -NoNewline
    }
    
    if (Test-Path "$agentServiceDir\package.json") {
        return "Agent Service installed successfully"
    } else {
        return "Agent Service install completed (may need manual verification)"
    }
} -ArgumentList $ghToken, $staging

# Wait for parallel jobs
Write-Host "  Started $($jobs2.Count) parallel service install jobs..." -ForegroundColor Gray
$jobs2 | Wait-Job | Out-Null

foreach ($job in $jobs2) {
    $result = Receive-Job -Job $job -ErrorAction SilentlyContinue
    if ($job.State -eq 'Completed') {
        Write-Host "  $($job.Name): $result" -ForegroundColor Green
    } else {
        $error = $job.ChildJobs[0].JobStateInfo.Reason
        Write-Host "  $($job.Name): FAILED - $error" -ForegroundColor Red
    }
    Remove-Job -Job $job -Force
}

$parallelElapsed2 = (Get-Date) - $parallelStartTime2
Write-Host "  Parallel group 2 completed in $([math]::Round($parallelElapsed2.TotalSeconds, 1))s" -ForegroundColor Magenta

# -----------------------------------------------------------------------------
# SEQUENTIAL: Configuration steps (fast, some have dependencies)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Running configuration ===" -ForegroundColor Cyan

# Create noVNC custom.html (needs noVNC installed)
Install-NoVNC  # This will skip install but create custom.html

Setup-AgentServiceEnv -AnthropicApiKey $gcpAnthropicKey -UnifyKey $gcpUnifyKey -UnifyBaseUrl $gcpUnifyBaseUrl
$caddyConfigured = Setup-Caddyfile -Hostname $hostname
Setup-Websockify

# Setup display resolution
if ($newUserCreated -ne $true -and $windowsUser) {
    Setup-DisplayResolution -TargetUser $windowsUser
} elseif (-not $windowsUser) {
    Setup-DisplayResolution
}

# Hide native Windows cursor (scheduled task for user session)
if ($windowsUser) {
    Setup-InvisibleCursor -TargetUser $windowsUser
} else {
    Setup-InvisibleCursor
}

# Configure firewall
Configure-Firewall

# -----------------------------------------------------------------------------
# FINAL: Start all services (must be sequential, after all installs)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Starting services ===" -ForegroundColor Cyan

Start-AllServices

# Start Caddy if configured
if ($caddyConfigured) {
    Start-Caddy
}

Show-Summary -MakKey $makKey -VncPassword $vncPassword -Hostname $hostname

# If NEW user was created, setup display resolution and reboot
# After reboot, GCP startup script runs again, user will exist, and we continue to Phase 2
if ($newUserCreated -eq $true) {
    Write-Host ""
    Write-Host "=== New User Created - Preparing for Reboot ===" -ForegroundColor Cyan
    
    # Setup display resolution task for the new user (will run at their logon after reboot)
    Setup-DisplayResolution -TargetUser $windowsUser
    
    # Setup invisible cursor task for the new user (will run at their logon after reboot)
    Setup-InvisibleCursor -TargetUser $windowsUser
    
    Write-Host ""
    Write-Host "Rebooting in 10 seconds to activate auto-logon..." -ForegroundColor Yellow
    Write-Host "After reboot, the script will resume with software installations." -ForegroundColor Yellow
    Write-Host ""
    
    Start-Sleep -Seconds 10
    Restart-Computer -Force
} else {
    Write-Host "Existing user detected, continuing with installations..." -ForegroundColor Green
}