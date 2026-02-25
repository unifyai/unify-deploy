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
#   office-mak-key    - Office MAK activation key
#   github-token      - GitHub PAT for cloning private repos
#   unify-key         - Unify API key for agent service
#   orchestra-url     - Orchestra API base URL for agent service
#   comms-url         - Communication service base URL for agent service
#   staging           - Use staging branch (any value = true)
#   ssh-public-key    - SSH public key for file sync (Ed25519)
#                       Note: SSH uses windows-username for authentication
#
# Boot Modes:
#   FAST MODE  - Everything pre-installed, just update repos & start services (~15-20s)
#   NORMAL MODE - Full installation/configuration (~45-60s with baked image)

$script:StartTime = Get-Date

Write-Host "=========================================="
Write-Host "  Windows VM Setup Script"
Write-Host "=========================================="
Write-Host ""

# =============================================================================
# Fast Mode Detection - Check if everything is already installed
# =============================================================================

function Test-FastMode {
    # Check critical paths that indicate full installation is complete
    $checks = @(
        @{ Path = 'C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE'; Name = 'Office' },
        @{ Path = 'C:\Program Files\Git\bin\git.exe'; Name = 'Git' },
        @{ Path = 'C:\Program Files\Python312\python.exe'; Name = 'Python' },
        @{ Path = 'C:\Program Files\TightVNC\tvnserver.exe'; Name = 'TightVNC' },
        @{ Path = 'C:\novnc\vnc.html'; Name = 'noVNC' },
        @{ Path = 'C:\magnitude\package.json'; Name = 'Magnitude' },
        @{ Path = 'C:\agent-service\package.json'; Name = 'AgentService' },
        @{ Path = 'C:\caddy\caddy.exe'; Name = 'Caddy' }
    )

    $allPresent = $true
    foreach ($check in $checks) {
        if (-not (Test-Path $check.Path)) {
            Write-Host "  Missing: $($check.Name)" -ForegroundColor Yellow
            $allPresent = $false
        }
    }

    return $allPresent
}

function Get-PackageJsonHash {
    param([string]$Dir)
    $pkgFile = "$Dir\package.json"
    if (Test-Path $pkgFile) {
        return (Get-FileHash $pkgFile -Algorithm MD5).Hash.Substring(0, 8)
    }
    return $null
}

function Test-DependenciesInstalled {
    param([string]$Dir)
    # Check if node_modules exists and has content
    $nodeModules = "$Dir\node_modules"
    if (-not (Test-Path $nodeModules)) { return $false }

    # Check hash file matches current package.json
    $hashFile = "$Dir\.pkg-hash"
    if (-not (Test-Path $hashFile)) { return $false }

    $savedHash = Get-Content $hashFile -ErrorAction SilentlyContinue
    $currentHash = Get-PackageJsonHash -Dir $Dir

    return ($savedHash -eq $currentHash)
}

function Save-DependenciesHash {
    param([string]$Dir)
    $hash = Get-PackageJsonHash -Dir $Dir
    if ($hash) {
        $hash | Out-File -FilePath "$Dir\.pkg-hash" -Encoding UTF8 -NoNewline
    }
}

# =============================================================================
# Commit Hash Tracking - For detecting code changes in repos without .git
# =============================================================================

function Get-RemoteCommitHash {
    param(
        [string]$RepoUrl,
        [string]$Branch
    )
    try {
        # Use git ls-remote (lightweight, no clone needed)
        $output = git ls-remote $RepoUrl "refs/heads/$Branch" 2>&1
        if ($output -match '^([a-f0-9]+)\s') {
            return $matches[1].Substring(0, 12)  # Short hash
        }
    } catch {
        Write-Host "  WARNING: Failed to get remote commit hash - $_" -ForegroundColor Yellow
    }
    return $null
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
    param(
        [string]$Dir,
        [string]$Hash
    )
    if ($Hash) {
        $Hash | Out-File -FilePath "$Dir\.commit-hash" -Encoding UTF8 -NoNewline
    }
}

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
# SSH File Sync Setup (OpenSSH Server on port 2222)
# =============================================================================

function Setup-SSHFileSync {
    param(
        [string]$WindowsUsername,
        [string]$SshPublicKey
    )

    if (-not $WindowsUsername -or -not $SshPublicKey) {
        Write-Host "SSH file sync not configured (missing Windows username or public key)" -ForegroundColor Yellow
        return $false
    }

    Write-Host ""
    Write-Host "=== Configuring SSH File Sync ===" -ForegroundColor Cyan
    Write-Host "  Username: $WindowsUsername (Windows user)"

    # 1. Install OpenSSH Server if not present
    $sshServerCapability = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
    if ($sshServerCapability.State -ne 'Installed') {
        Write-Host "  Installing OpenSSH Server..."
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
        Write-Host "  OpenSSH Server installed" -ForegroundColor Green
    } else {
        Write-Host "  OpenSSH Server already installed" -ForegroundColor Green
    }

    # 2. Create C:\Unity\Local directory for file sync
    # Note: Subdirectories (Downloads, user_files, etc.) are created by sync process
    $unityDir = "C:\Unity"
    $syncDir = "C:\Unity\Local"
    if (-not (Test-Path $unityDir)) {
        New-Item -ItemType Directory -Force -Path $unityDir | Out-Null
    }
    if (-not (Test-Path $syncDir)) {
        New-Item -ItemType Directory -Force -Path $syncDir | Out-Null
    }
    Write-Host "  Created C:\Unity\Local sync directory"

    # 3. Setup authorized_keys for the Windows user
    # For administrators on Windows, keys go in C:\ProgramData\ssh\administrators_authorized_keys
    # For regular users, keys go in C:\Users\{username}\.ssh\authorized_keys
    $userProfileDir = "C:\Users\$WindowsUsername"
    $userSshDir = "$userProfileDir\.ssh"

    # Create user's .ssh directory
    if (-not (Test-Path $userSshDir)) {
        New-Item -ItemType Directory -Force -Path $userSshDir | Out-Null
    }

    # Write public key to user's authorized_keys
    $authorizedKeysPath = "$userSshDir\authorized_keys"
    Set-Content -Path $authorizedKeysPath -Value $SshPublicKey -Encoding UTF8

    # Set proper permissions on .ssh directory and authorized_keys
    # Windows OpenSSH is picky about permissions
    icacls $userSshDir /inheritance:r /grant "${WindowsUsername}:F" /grant "SYSTEM:F" /grant "Administrators:F" | Out-Null
    icacls $authorizedKeysPath /inheritance:r /grant "${WindowsUsername}:F" /grant "SYSTEM:F" /grant "Administrators:F" | Out-Null
    Write-Host "  Configured authorized_keys for $WindowsUsername"

    # Also set up administrators_authorized_keys for admin users (Windows OpenSSH special handling)
    $adminKeysPath = "$env:ProgramData\ssh\administrators_authorized_keys"
    Set-Content -Path $adminKeysPath -Value $SshPublicKey -Encoding UTF8
    icacls $adminKeysPath /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" | Out-Null
    Write-Host "  Configured administrators_authorized_keys"

    # 4. Configure SSHD to listen on port 2222
    $sshdConfigPath = "$env:ProgramData\ssh\sshd_config"

    # Ensure ssh directory exists
    if (-not (Test-Path "$env:ProgramData\ssh")) {
        New-Item -ItemType Directory -Force -Path "$env:ProgramData\ssh" | Out-Null
    }

    # Check if we already configured port 2222
    $configContent = ""
    if (Test-Path $sshdConfigPath) {
        $configContent = Get-Content $sshdConfigPath -Raw -ErrorAction SilentlyContinue
    }

    if ($configContent -notmatch "Port 2222") {
        # Create or update sshd_config
        # Note: Using simplified config without Match blocks - they cause issues with Windows OpenSSH
        $sshdConfig = @"
# Unity File Sync - OpenSSH Server Configuration
# Generated by windows-vm-startup.ps1

Port 2222
PasswordAuthentication no
PubkeyAuthentication yes

# Use administrators_authorized_keys for all users
# Note: Match blocks can cause silent auth failures with Windows OpenSSH
AuthorizedKeysFile C:/ProgramData/ssh/administrators_authorized_keys

# Subsystem for SFTP
Subsystem sftp sftp-server.exe
"@
        Set-Content -Path $sshdConfigPath -Value $sshdConfig -Encoding UTF8
        Write-Host "  Configured SSHD for port 2222"
    } else {
        Write-Host "  SSHD already configured for port 2222"
    }

    # 5. Configure firewall rule for port 2222
    $firewallRuleName = "Unity SSH File Sync (Port 2222)"
    $existingRule = Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue
    if (-not $existingRule) {
        New-NetFirewallRule -DisplayName $firewallRuleName -Direction Inbound -Protocol TCP -LocalPort 2222 -Action Allow | Out-Null
        Write-Host "  Added firewall rule for port 2222"
    } else {
        Write-Host "  Firewall rule for port 2222 already exists"
    }

    # 6. Start and enable SSH service
    Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service sshd -ErrorAction SilentlyContinue
    Write-Host "  Started SSH service"

    Write-Host "SSH file sync configured:" -ForegroundColor Green
    Write-Host "  User: $WindowsUsername"
    Write-Host "  Port: 2222"
    Write-Host "  Sync Path: C:\Unity\Local"

    return $true
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

        # Get current commit for logging and save hash
        $commit = git rev-parse --short=12 HEAD 2>&1
        Write-Host "  Updated to commit: $commit" -ForegroundColor Green

        # Save commit hash for future update checks (works for repos without .git too)
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
        [string]$Staging,
        [switch]$FastMode
    )

    Write-Host ""
    Write-Host "=== Installing/Updating Magnitude & Agent Service ===" -ForegroundColor Cyan

    $magnitudeDir = 'C:\magnitude'
    $agentServiceDir = 'C:\agent-service'
    $unityRepoDir = 'C:\temp\unity-repo'

    # Determine branch for unity repo (magnitude always uses unity-modifications)
    $unityBranch = if ($Staging) { "staging" } else { "main" }
    Write-Host "Magnitude branch: unity-modifications | Unity branch: $unityBranch" -ForegroundColor Gray

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
    # MAGNITUDE: Fast update if exists, otherwise clone fresh
    # =========================================================================
    if (Test-Path "$magnitudeDir\.git") {
        # Existing git repo - update via fetch+reset
        Write-Host "Updating Magnitude (git fetch)..." -ForegroundColor Yellow
        Update-GitRepo -RepoPath $magnitudeDir -Branch "unity-modifications" -GithubToken $GithubToken -RepoName "magnitude"
    } else {
        # No git repo - clone fresh
        Write-Host "Cloning Magnitude repository..."

        if (Test-Path $magnitudeDir) {
            cmd /c "rmdir /s /q `"$magnitudeDir`"" 2>&1 | Out-Null
        }

        git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir 2>&1 | Out-Null

        if (Test-Path "$magnitudeDir\package.json") {
            Push-Location $magnitudeDir
            $commitHash = (git rev-parse --short=12 HEAD 2>&1)
            if ($commitHash) {
                Save-CommitHash -Dir $magnitudeDir -Hash $commitHash
            }
            Write-Host "  Magnitude cloned (commit: $commitHash)" -ForegroundColor Green
            Pop-Location
        }
    }

    # Always run bun install to ensure node_modules matches the current code
    if (Test-Path "$magnitudeDir\package.json") {
        Write-Host "  Installing Magnitude dependencies..." -ForegroundColor Yellow
        Push-Location $magnitudeDir
        if (Get-Command bun -ErrorAction SilentlyContinue) {
            bun install 2>&1 | Out-Null
        } else {
            npm install 2>&1 | Out-Null
        }
        Pop-Location
        Write-Host "  Magnitude dependencies installed" -ForegroundColor Green
    }

    # =========================================================================
    # AGENT-SERVICE: Use git repo for updates if possible
    # =========================================================================

    # Backup .env if exists
    $envBackup = $null
    $envFile = "$agentServiceDir\.env"
    if (Test-Path $envFile) {
        $envBackup = Get-Content $envFile -Raw
    }

    # Check if we have a proper git repo (created by previous runs with this optimization)
    if (Test-Path "$agentServiceDir\.git") {
        # Update via git fetch+reset
        Write-Host "Updating Agent Service (git fetch)..." -ForegroundColor Yellow
        Update-GitRepo -RepoPath $agentServiceDir -Branch $unityBranch -GithubToken $GithubToken -RepoName "unity"
    } elseif (Test-Path "$agentServiceDir\package.json") {
        # Have agent-service but no git - check remote commit to detect code changes
        $savedHash = Get-SavedCommitHash -Dir $agentServiceDir
        $remoteHash = Get-RemoteCommitHash -RepoUrl $unityUrl -Branch $unityBranch

        $needsUpdate = $true
        if ($FastMode -and $savedHash -and $remoteHash -and ($savedHash -eq $remoteHash)) {
            Write-Host "Agent Service up-to-date (commit: $savedHash)" -ForegroundColor Green
            $needsUpdate = $false
        } elseif ($savedHash -and $remoteHash) {
            Write-Host "Agent Service update available ($savedHash -> $remoteHash)" -ForegroundColor Yellow
        }

        if ($needsUpdate) {
            # Re-clone to get updates
            Write-Host "Cloning Agent Service from Unity repo..." -ForegroundColor Yellow

            # Stop running agent-service to release file locks before deletion
            Stop-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
            Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500

            # cmd /c rmdir handles long node_modules paths that Remove-Item chokes on
            if (Test-Path $agentServiceDir) {
                cmd /c "rmdir /s /q `"$agentServiceDir`"" 2>&1 | Out-Null
            }

            New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
            if (Test-Path $unityRepoDir) {
                cmd /c "rmdir /s /q `"$unityRepoDir`"" 2>&1 | Out-Null
            }

            # Sparse checkout to get only agent-service
            git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $unityRepoDir 2>&1 | Out-Null

            if (Test-Path $unityRepoDir) {
                $commitHash = $null
                Push-Location $unityRepoDir
                $commitHash = (git rev-parse --short=12 HEAD 2>&1)
                git sparse-checkout set agent-service 2>&1 | Out-Null
                Pop-Location

                if (Test-Path "$unityRepoDir\agent-service") {
                    # Move-Item nests into existing dirs, so ensure destination is gone
                    if (Test-Path $agentServiceDir) {
                        cmd /c "rmdir /s /q `"$agentServiceDir`"" 2>&1 | Out-Null
                    }
                    Move-Item "$unityRepoDir\agent-service" $agentServiceDir
                    if ($commitHash) {
                        Save-CommitHash -Dir $agentServiceDir -Hash $commitHash
                    }
                    Write-Host "  Agent Service cloned (commit: $commitHash)" -ForegroundColor Green
                }
                cmd /c "rmdir /s /q `"$unityRepoDir`"" 2>&1 | Out-Null
            }
        }
    } else {
        # Fresh install
        Write-Host "Installing Agent Service..." -ForegroundColor Cyan

        New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
        if (Test-Path $unityRepoDir) {
            cmd /c "rmdir /s /q `"$unityRepoDir`"" 2>&1 | Out-Null
        }

        git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $unityRepoDir 2>&1 | Out-Null

        if (Test-Path $unityRepoDir) {
            $commitHash = $null
            Push-Location $unityRepoDir
            $commitHash = (git rev-parse --short=12 HEAD 2>&1)
            git sparse-checkout set agent-service 2>&1 | Out-Null
            Pop-Location

            if (Test-Path "$unityRepoDir\agent-service") {
                # Move-Item nests into existing dirs, so ensure destination is gone
                if (Test-Path $agentServiceDir) {
                    cmd /c "rmdir /s /q `"$agentServiceDir`"" 2>&1 | Out-Null
                }
                Move-Item "$unityRepoDir\agent-service" $agentServiceDir
                if ($commitHash) {
                    Save-CommitHash -Dir $agentServiceDir -Hash $commitHash
                }
                Write-Host "  Agent Service cloned (commit: $commitHash)" -ForegroundColor Green
            }
            cmd /c "rmdir /s /q `"$unityRepoDir`"" 2>&1 | Out-Null
        }
    }

    # Always run npm install to ensure node_modules matches the current code
    if (Test-Path "$agentServiceDir\package.json") {
        Write-Host "  Installing Agent Service dependencies..." -ForegroundColor Yellow
        Push-Location $agentServiceDir
        npm install 2>&1 | Out-Null
        Pop-Location
        Write-Host "  Agent Service dependencies installed" -ForegroundColor Green
    }

    # Install Patchright Chromium to shared location (matches Ubuntu pattern)
    if (Test-Path "$magnitudeDir\packages\magnitude-core\package.json") {
        Write-Host "  Installing Patchright Chromium..." -ForegroundColor Yellow
        [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', 'C:\ms-playwright', 'Machine')
        $env:PLAYWRIGHT_BROWSERS_PATH = 'C:\ms-playwright'
        Push-Location "$magnitudeDir\packages\magnitude-core"
        npx --yes patchright install chromium 2>&1 | Out-Null
        Pop-Location
        Write-Host "  Patchright Chromium installed" -ForegroundColor Green
    }

    # Restore .env file
    if ($envBackup -and (Test-Path $agentServiceDir)) {
        $envBackup | Out-File -FilePath $envFile -Encoding UTF8 -NoNewline
    }
}

function Setup-AgentServiceEnv {
    param(
        [string]$UnifyKey,
        [string]$UnifyBaseUrl,
        [string]$CommsUrl
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

    if ($UnifyKey) {
        $envContent += "`nUNIFY_KEY=$UnifyKey"
        Write-Host "  UNIFY_KEY: (set)" -ForegroundColor Green
    } else {
        Write-Host "  UNIFY_KEY: (not provided)" -ForegroundColor Yellow
    }

    if ($UnifyBaseUrl) {
        $envContent += "`nORCHESTRA_URL=$UnifyBaseUrl"
        Write-Host "  ORCHESTRA_URL: $UnifyBaseUrl" -ForegroundColor Green
    }

    if ($CommsUrl) {
        $envContent += "`nUNITY_COMMS_URL=$CommsUrl"
        Write-Host "  UNITY_COMMS_URL: $CommsUrl" -ForegroundColor Green
    }

    # Patchright/Playwright browsers are installed to a shared location
    $envContent += "`nPLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright"
    Write-Host "  PLAYWRIGHT_BROWSERS_PATH: C:\ms-playwright" -ForegroundColor Green

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

    # Check if password is already in HKCU (from previous run)
    $hklmPath = 'HKLM:\SOFTWARE\TightVNC\Server'
    $hkcuPath = 'HKCU:\SOFTWARE\TightVNC\Server'
    $passwordExists = (Get-ItemProperty -Path $hkcuPath -Name 'Password' -ErrorAction SilentlyContinue).Password

    if (-not $passwordExists) {
        # First time setup - start service to initialize password
        Write-Host "Initializing TightVNC password..."
        Set-Service -Name "tvnserver" -StartupType Manual -ErrorAction SilentlyContinue
        Start-Service -Name "tvnserver" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1  # Reduced from 3s

        # Copy encrypted password from HKLM to HKCU
        if (-not (Test-Path $hkcuPath)) {
            New-Item -Path $hkcuPath -Force | Out-Null
        }

        try {
            $passwordBytes = Get-ItemPropertyValue -Path $hklmPath -Name 'Password' -ErrorAction Stop
            Set-ItemProperty -Path $hkcuPath -Name 'Password' -Value $passwordBytes -Type Binary -Force
            $controlPwdBytes = Get-ItemPropertyValue -Path $hklmPath -Name 'ControlPassword' -ErrorAction Stop
            Set-ItemProperty -Path $hkcuPath -Name 'ControlPassword' -Value $controlPwdBytes -Type Binary -Force
            Write-Host "  Password configured" -ForegroundColor Green
        } catch {
            Write-Host "  WARNING: Password copy failed - $_" -ForegroundColor Yellow
        }
    }

    # Enable TightVNC service for automatic startup
    Set-Service -Name "tvnserver" -StartupType Automatic -ErrorAction SilentlyContinue
    Write-Host "TightVNC configured" -ForegroundColor Green
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
    $caddyDir = 'C:\caddy'
    $caddyExe = "$caddyDir\caddy.exe"
    $caddyfile = "$caddyDir\Caddyfile"

    if (-not (Test-Path $caddyExe) -or -not (Test-Path $caddyfile)) {
        return
    }

    # Check if Caddy is already running
    $caddyProcess = Get-Process -Name "caddy" -ErrorAction SilentlyContinue
    if ($caddyProcess) {
        Write-Host "  Caddy: Already running" -ForegroundColor Green
        return
    }

    Write-Host "  Starting Caddy..." -ForegroundColor Gray
    $psCommand = "Set-Location '$caddyDir'; & '$caddyExe' run --config '$caddyfile'"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory $caddyDir

    # Quick check (reduced from 5s to 1s)
    Start-Sleep -Milliseconds 1000

    if (Test-PortListening -Port 443 -TimeoutMs 500) {
        Write-Host "  Caddy: Running (HTTPS)" -ForegroundColor Green
    } else {
        Write-Host "  Caddy: Starting (TLS cert may take a moment)..." -ForegroundColor Yellow
    }
}

function Setup-Websockify {
    param(
        [switch]$Force,
        [string]$TargetUser
    )

    Write-Host ""
    Write-Host "=== Setting up websockify ===" -ForegroundColor Cyan

    $novncDir = 'C:\novnc'
    $batFile = "$novncDir\start-websockify.bat"
    $taskName = "StartWebsockify"

    # Check if already configured (but always recreate if TargetUser set, to fix principal)
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask -and $TargetUser) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        $existingTask = $null
    }
    if ($existingTask -and (Test-Path $batFile) -and -not $Force) {
        Write-Host "  Websockify already configured" -ForegroundColor Green
        return
    }

    # Find Python executable
    $pythonExe = 'C:\Program Files\Python312\python.exe'
    if (-not (Test-Path $pythonExe)) {
        $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
    if (-not $pythonExe -or -not (Test-Path $pythonExe)) {
        Write-Host "  ERROR: Python not found" -ForegroundColor Red
        return
    }

    # Create websockify startup script
    $websockifyScript = @"
@echo off
cd /d C:\novnc
"$pythonExe" -m websockify --web C:\novnc 6080 localhost:5900
"@
    $websockifyScript | Out-File -FilePath $batFile -Encoding ASCII

    # Create scheduled task (launch via PowerShell hidden to avoid visible cmd window)
    if (-not $existingTask) {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$batFile'`"" -WorkingDirectory $novncDir
        if ($TargetUser) {
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
            $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
        } else {
            $trigger = New-ScheduledTaskTrigger -AtLogOn
            $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
        }
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    }

    Write-Host "  Websockify configured" -ForegroundColor Green
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
    param([switch]$Force)

    Write-Host ""
    Write-Host "=== Configuring Firewall ===" -ForegroundColor Cyan

    # Check if rules already exist
    $existingRules = Get-NetFirewallRule -DisplayName "Unity-*" -ErrorAction SilentlyContinue
    if ($existingRules -and -not $Force) {
        Write-Host "  Firewall rules already configured ($($existingRules.Count) rules)" -ForegroundColor Green
        return
    }

    # Define required rules
    $rules = @(
        @{ Name = "Unity-HTTPS"; Port = 443; Desc = "HTTPS" },
        @{ Name = "Unity-HTTP"; Port = 80; Desc = "HTTP" },
        @{ Name = "Unity-noVNC"; Port = 6080; Desc = "noVNC" },
        @{ Name = "Unity-AgentService"; Port = 3000; Desc = "Agent Service" }
    )

    foreach ($rule in $rules) {
        $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
        if (-not $existing) {
            New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -LocalPort $rule.Port -Protocol TCP -Action Allow -Profile Any | Out-Null
            Write-Host "  Created: $($rule.Desc) ($($rule.Port))" -ForegroundColor Green
        }
    }

    Write-Host "  Firewall configured" -ForegroundColor Green
}

function Test-PortListening {
    param([int]$Port, [int]$TimeoutMs = 2000)
    $start = Get-Date
    while (((Get-Date) - $start).TotalMilliseconds -lt $TimeoutMs) {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($conn) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

function Start-AllServices {
    param(
        [switch]$FastMode,
        [string]$TargetUser
    )

    Write-Host ""
    Write-Host "=== Starting Services ===" -ForegroundColor Cyan

    # Start TightVNC service (non-blocking)
    $tvnService = Get-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    if ($tvnService -and $tvnService.Status -ne 'Running') {
        Write-Host "Starting TightVNC service..." -ForegroundColor Gray
        Start-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    }

    # Start websockify in background (don't wait)
    $novncDir = 'C:\novnc'
    $websockifyBat = "$novncDir\start-websockify.bat"
    $port6080 = Get-NetTCPConnection -LocalPort 6080 -State Listen -ErrorAction SilentlyContinue

    if (-not $port6080 -and (Test-Path $websockifyBat)) {
        Write-Host "Starting websockify..." -ForegroundColor Gray
        Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$websockifyBat'`"" -WorkingDirectory $novncDir -WindowStyle Hidden
    }

    # Start Agent Service in background
    $agentServiceDir = 'C:\agent-service'
    if (Test-Path "$agentServiceDir\package.json") {
        $port3000 = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue

        if (-not $port3000) {
            Write-Host "Starting Agent Service..." -ForegroundColor Gray

            # Ensure startup script exists
            $agentStartScript = @"
@echo off
set PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
cd /d C:\agent-service
npx --yes ts-node src/index.ts >> C:\agent-service\agent.log 2>&1
"@
            $agentStartScript | Out-File -FilePath "$agentServiceDir\start-agent.bat" -Encoding ASCII

            # Ensure scheduled task exists with correct user
            $taskName = "StartAgentService"
            $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($existingTask -and $TargetUser) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
                $existingTask = $null
            }
            if (-not $existingTask) {
                $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$agentServiceDir\start-agent.bat'`"" -WorkingDirectory $agentServiceDir
                if ($TargetUser) {
                    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TargetUser
                    $principal = New-ScheduledTaskPrincipal -UserId $TargetUser -LogonType Interactive -RunLevel Highest
                } else {
                    $trigger = New-ScheduledTaskTrigger -AtLogOn
                    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
                }
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
                Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
            }

            # Start via scheduled task to run in user's session, or direct Start-Process for manual runs
            if ($TargetUser) {
                Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            } else {
                Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$agentServiceDir\start-agent.bat'`"" -WorkingDirectory $agentServiceDir -WindowStyle Hidden
            }
        }
    }

    # Quick verification with minimal wait (only 2s total instead of 10+)
    Start-Sleep -Milliseconds 1500

    # Report status
    $tvnService = Get-Service -Name "tvnserver" -ErrorAction SilentlyContinue
    if ($tvnService -and $tvnService.Status -eq 'Running') {
        Write-Host "  TightVNC: Running" -ForegroundColor Green
    } else {
        Write-Host "  TightVNC: Starting..." -ForegroundColor Yellow
    }

    if (Test-PortListening -Port 6080 -TimeoutMs 500) {
        Write-Host "  websockify: Running (port 6080)" -ForegroundColor Green
    } else {
        Write-Host "  websockify: Starting..." -ForegroundColor Yellow
    }

    if (Test-Path "$agentServiceDir\package.json") {
        if (Test-PortListening -Port 3000 -TimeoutMs 500) {
            Write-Host "  Agent Service: Running (port 3000)" -ForegroundColor Green
        } else {
            Write-Host "  Agent Service: Starting..." -ForegroundColor Yellow
        }
    }
}

function Install-Office {
    param(
        [string]$MakKey,
        [switch]$FastMode
    )

    $excelPath = 'C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE'

    # Fast mode: just verify Office exists, skip activation check
    if ($FastMode -and (Test-Path $excelPath)) {
        Write-Host "Office: Installed" -ForegroundColor Green
        return
    }

    Write-Host ""
    Write-Host "=== Installing Office LTSC 2024 ===" -ForegroundColor Cyan

    if (Test-Path $excelPath) {
        Write-Host "Excel already installed at: $excelPath" -ForegroundColor Green
        if ($MakKey) {
            Activate-Office -MakKey $MakKey
        }
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
    if ($script:sshConfigured) {
        Write-Host "  - SSH File Sync (port 2222, uses Windows user)"
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
$gcpUnifyKey = Get-GCPMetadata -Key "unify-key"
$gcpUnifyBaseUrl = Get-GCPMetadata -Key "orchestra-url"
$gcpCommsUrl = Get-GCPMetadata -Key "comms-url"
$gcpStaging = Get-GCPMetadata -Key "staging"
$gcpSshPublicKey = Get-GCPMetadata -Key "ssh-public-key"
$gcpMakKey = Get-GCPMetadata -Key "office-mak-key"
# Note: SSH uses windows-username for authentication (no separate ssh-username needed)

if ($gcpHostname) {
    Write-Host "Found hostname metadata: $gcpHostname" -ForegroundColor Cyan
}
if ($gcpWindowsUser) {
    Write-Host "Found windows-username metadata: $gcpWindowsUser" -ForegroundColor Cyan
}
if ($gcpGithubToken) {
    Write-Host "Found github-token metadata: (set)" -ForegroundColor Cyan
}
if ($gcpCommsUrl) {
    Write-Host "Found comms-url metadata: $gcpCommsUrl" -ForegroundColor Cyan
}
if ($gcpStaging) {
    Write-Host "Found staging metadata: enabled" -ForegroundColor Cyan
}
if ($gcpSshPublicKey) {
    Write-Host "Found ssh-public-key metadata: (set, will use windows-username for SSH)" -ForegroundColor Cyan
}
if ($gcpMakKey) {
    Write-Host "Found office-mak-key metadata: (set)" -ForegroundColor Cyan
}

# Parse arguments (GCP metadata takes precedence)
$makKey = if ($gcpMakKey) { $gcpMakKey } elseif ($args[0]) { $args[0] } else { $null }
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
Write-Host "  SSH Public Key:  $(if ($gcpSshPublicKey) { '(set, uses Windows user)' } else { '(not provided)' })"
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
# Detect Boot Mode
# =============================================================================

Write-Host "Checking installation status..."
$fastMode = Test-FastMode

if ($fastMode) {
    Write-Host ""
    Write-Host ">>> FAST MODE: All software pre-installed <<<" -ForegroundColor Green
    Write-Host "    Updating repos and starting services only" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host ">>> NORMAL MODE: Running full installation <<<" -ForegroundColor Yellow
}

# =============================================================================
# Phase 2: Software Installations (with parallelization)
# =============================================================================

Write-Host ""
Write-Host "=== Phase 2: Software Installations ===" -ForegroundColor Cyan

# Office check (fast mode just verifies, normal mode installs if needed)
Install-Office -MakKey $makKey -FastMode:$fastMode

# -----------------------------------------------------------------------------
# PARALLEL GROUP 1: Foundation tools (no dependencies between them)
# Git, Python, Chocolatey, TightVNC, Caddy can all download/install simultaneously
# Using Start-Job with self-contained script blocks for reliability
# -----------------------------------------------------------------------------

# Ensure PATH is always refreshed
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
$bunPath = "$env:USERPROFILE\.bun\bin"
if (Test-Path $bunPath) { $env:Path = "$bunPath;$env:Path" }

# In fast mode, skip parallel installs - everything is already there
if ($fastMode) {
    Write-Host ""
    Write-Host "=== Foundation tools: Pre-installed ===" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "=== Installing foundation tools in parallel ===" -ForegroundColor Cyan
}

$parallelStartTime = Get-Date
$jobs = @()

if (-not $fastMode) {
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

} # End of: if (-not $fastMode)

# Refresh PATH again after Node.js install
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Add Bun to PATH if installed
$bunPath = "$env:USERPROFILE\.bun\bin"
if (Test-Path $bunPath) {
    $env:Path = "$bunPath;$env:Path"
}

# -----------------------------------------------------------------------------
# PARALLEL GROUP 2 / Fast Mode Update: Services (noVNC + Agent Service)
# In fast mode: just update repos. In normal mode: full parallel install.
# -----------------------------------------------------------------------------

if ($fastMode) {
    # Fast mode: Update repos sequentially (faster than spawning jobs)
    Write-Host ""
    Write-Host "=== Updating services ===" -ForegroundColor Cyan
    Install-AgentService -GithubToken $gcpGithubToken -Staging $gcpStaging -FastMode
} else {
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

    # Job: Install AgentService (full install in job for parallelism)
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
            if (Test-Path $magnitudeDir) { cmd /c "rmdir /s /q `"$magnitudeDir`"" 2>&1 | Out-Null }
            git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir 2>&1 | Out-Null

            if (Test-Path "$magnitudeDir\package.json") {
                Push-Location $magnitudeDir
                $magCommitHash = (git rev-parse --short=12 HEAD 2>&1)
                if ($magCommitHash) {
                    $magCommitHash | Out-File -FilePath "$magnitudeDir\.commit-hash" -Encoding UTF8 -NoNewline
                }
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

        # cmd /c rmdir handles long node_modules paths that Remove-Item chokes on
        if (Test-Path "$agentServiceDir\package.json") {
            cmd /c "rmdir /s /q `"$agentServiceDir`"" 2>&1 | Out-Null
        }

        New-Item -ItemType Directory -Force -Path 'C:\temp' | Out-Null
        if (Test-Path $unityRepoDir) { cmd /c "rmdir /s /q `"$unityRepoDir`"" 2>&1 | Out-Null }

        git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $unityRepoDir 2>&1 | Out-Null

        if (Test-Path $unityRepoDir) {
            # Get commit hash before extracting
            $commitHash = $null
            Push-Location $unityRepoDir
            $commitHash = (git rev-parse --short=12 HEAD 2>&1)
            git sparse-checkout set agent-service 2>&1 | Out-Null
            Pop-Location

            if (Test-Path "$unityRepoDir\agent-service") {
                # Move-Item nests into existing dirs, so ensure destination is gone
                if (Test-Path $agentServiceDir) {
                    cmd /c "rmdir /s /q `"$agentServiceDir`"" 2>&1 | Out-Null
                }
                Move-Item "$unityRepoDir\agent-service" $agentServiceDir -Force

                # Save commit hash for future update checks
                if ($commitHash) {
                    $commitHash | Out-File -FilePath "$agentServiceDir\.commit-hash" -Encoding UTF8 -NoNewline
                }
            }
            cmd /c "rmdir /s /q `"$unityRepoDir`"" 2>&1 | Out-Null
        }

        # Install agent-service dependencies
        if (Test-Path "$agentServiceDir\package.json") {
            Push-Location $agentServiceDir
            npm install 2>&1 | Out-Null
            Pop-Location
        }

        # Install Patchright Chromium to shared location
        if (Test-Path "$magnitudeDir\packages\magnitude-core\package.json") {
            [System.Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', 'C:\ms-playwright', 'Machine')
            $env:PLAYWRIGHT_BROWSERS_PATH = 'C:\ms-playwright'
            Push-Location "$magnitudeDir\packages\magnitude-core"
            npx --yes patchright install chromium 2>&1 | Out-Null
            Pop-Location
        }

        # Restore .env
        if ($envBackup -and (Test-Path $agentServiceDir)) {
            $envBackup | Out-File -FilePath "$agentServiceDir\.env" -Encoding UTF8 -NoNewline
        }

        if (Test-Path "$agentServiceDir\package.json") {
            $savedHash = if (Test-Path "$agentServiceDir\.commit-hash") { Get-Content "$agentServiceDir\.commit-hash" } else { "unknown" }
            return "Agent Service installed successfully (commit: $savedHash)"
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
            $jobError = $job.ChildJobs[0].JobStateInfo.Reason
            Write-Host "  $($job.Name): FAILED - $jobError" -ForegroundColor Red
        }
        Remove-Job -Job $job -Force
    }

    $parallelElapsed2 = (Get-Date) - $parallelStartTime2
    Write-Host "  Parallel group 2 completed in $([math]::Round($parallelElapsed2.TotalSeconds, 1))s" -ForegroundColor Magenta
}

# -----------------------------------------------------------------------------
# SEQUENTIAL: Configuration steps (fast, most skip if already done)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Running configuration ===" -ForegroundColor Cyan

# Create noVNC custom.html only (Install-NoVNC was already run in parallel group)
$novncDir = 'C:\novnc'
if (Test-Path "$novncDir\vnc.html") {
    # Just ensure custom.html exists
    $customHtmlPath = "$novncDir\custom.html"
    if (-not (Test-Path $customHtmlPath)) {
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
        document.getElementById('vnc').onload = function() {
            try {
                const style = this.contentDocument.createElement('style');
                style.textContent = `
                    #noVNC_control_bar, #noVNC_control_bar_anchor, #noVNC_control_bar_handle,
                    #noVNC_logo, #noVNC_status { display: none !important; }
                    .noVNC_cursor { display: none !important; }
                `;
                this.contentDocument.head.appendChild(style);
            } catch (e) {}
        };
    </script>
</body>
</html>
'@
        $customHtml | Out-File -FilePath $customHtmlPath -Encoding UTF8
        Copy-Item $customHtmlPath "$novncDir\index.html" -Force
    }
}

# Run config functions (they now skip if already configured)
Setup-AgentServiceEnv -UnifyKey $gcpUnifyKey -UnifyBaseUrl $gcpUnifyBaseUrl -CommsUrl $gcpCommsUrl
$caddyConfigured = Setup-Caddyfile -Hostname $hostname
Setup-Websockify -TargetUser $windowsUser

# Display resolution and cursor only need setup if not already done
if (-not $fastMode) {
    if ($newUserCreated -ne $true -and $windowsUser) {
        Setup-DisplayResolution -TargetUser $windowsUser
    } elseif (-not $windowsUser) {
        Setup-DisplayResolution
    }

    if ($windowsUser) {
        Setup-InvisibleCursor -TargetUser $windowsUser
    } else {
        Setup-InvisibleCursor
    }
}

# Configure firewall (skips if rules exist)
Configure-Firewall

# Configure SSH file sync (uses Windows username for SSH auth)
$sshConfigured = $false
if ($gcpWindowsUser -and $gcpSshPublicKey) {
    $sshConfigured = Setup-SSHFileSync -WindowsUsername $gcpWindowsUser -SshPublicKey $gcpSshPublicKey
}

# -----------------------------------------------------------------------------
# FINAL: Start all services (must be sequential, after all installs)
# -----------------------------------------------------------------------------

Start-AllServices -FastMode:$fastMode -TargetUser $windowsUser

# Start Caddy if configured
if ($caddyConfigured) {
    Start-Caddy
}

# Calculate total elapsed time
$totalElapsed = (Get-Date) - $script:StartTime

Show-Summary -MakKey $makKey -VncPassword $vncPassword -Hostname $hostname

Write-Host ""
Write-Host "Total startup time: $([math]::Round($totalElapsed.TotalSeconds, 1)) seconds" -ForegroundColor Magenta
if ($fastMode) {
    Write-Host "Boot mode: FAST" -ForegroundColor Green
} else {
    Write-Host "Boot mode: NORMAL (full install)" -ForegroundColor Yellow
}

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
t "Existing user detected, continuing with installations..." -ForegroundColor Green
}
