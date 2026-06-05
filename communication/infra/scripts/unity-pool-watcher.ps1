# =============================================================================
# Unity Pool Watcher (Windows)
#
# Runs as a Windows service (via NSSM). Watches GCE instance metadata for
# assignment/release signals. When unify-key changes from empty to non-empty,
# the VM is assigned; when it clears, the VM is released.
#
# Install via NSSM:
#   nssm install UnityPoolWatcher powershell.exe -ExecutionPolicy Bypass -File C:\unity-pool-watcher.ps1
#   nssm set UnityPoolWatcher Start SERVICE_AUTO_START
# =============================================================================

param(
    [switch]$SkipMain
)

$ErrorActionPreference = "Continue"

$MetadataUrl = "http://metadata.google.internal/computeMetadata/v1"
$MetadataHeaders = @{"Metadata-Flavor" = "Google"}
$Etag = ""
$PrevUnifyKey = ""
$PrevTlsHash = ""
$ReleaseStateDir = "C:\ProgramData\UnityPoolWatcher"
$LastReleaseTokenPath = Join-Path $ReleaseStateDir "last-release-token.txt"

function Get-Metadata($key) {
    try {
        $response = Invoke-RestMethod -Uri "$MetadataUrl/instance/attributes/$key" `
            -Headers $MetadataHeaders -TimeoutSec 5 -ErrorAction SilentlyContinue
        return [string]$response
    } catch {
        return ""
    }
}

function Get-DeployEnv {
    $envName = Get-Metadata "unity-environment"
    if ($envName) { return $envName }
    if (Get-Metadata "preview") { return "preview" }
    if (Get-Metadata "staging") { return "staging" }
    return "production"
}

function Write-Log($message) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Write-Host "[$ts] $message"
}

function Ensure-ReleaseStateDir {
    New-Item -ItemType Directory -Force -Path $ReleaseStateDir -ErrorAction SilentlyContinue | Out-Null
}

function Get-LastReleaseToken {
    if (Test-Path $LastReleaseTokenPath) {
        return (Get-Content $LastReleaseTokenPath -Raw).Trim()
    }
    return ""
}

function Save-LastReleaseToken($token) {
    Ensure-ReleaseStateDir
    if ($token) {
        Set-Content -Path $LastReleaseTokenPath -Value $token -Encoding ASCII -NoNewline
    } else {
        Remove-Item $LastReleaseTokenPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-CurrentReleaseToken {
    $bindingId = Get-Metadata "binding-id"
    $releaseGeneration = Get-Metadata "release-generation"
    if ($bindingId -and $releaseGeneration) {
        return "$bindingId`:$releaseGeneration"
    }
    return ""
}

function Should-TriggerRelease($PreviousUnifyKey, $CurrentUnifyKey, $CurrentReleaseToken, $LastHandledReleaseToken) {
    if ($CurrentUnifyKey -ne $PreviousUnifyKey -and -not $CurrentUnifyKey -and -not $CurrentReleaseToken) {
        return $true
    }

    return (-not $CurrentUnifyKey -and $CurrentReleaseToken -and $CurrentReleaseToken -ne $LastHandledReleaseToken)
}

function Get-UnityUserAuthorizedKeysPath {
    return "C:\Users\unityuser\.ssh\authorized_keys"
}

function Ensure-UnityUserSshConfig {
    $sshProgramDataDir = "C:\ProgramData\ssh"
    $sshDir = "C:\Users\unityuser\.ssh"
    New-Item -ItemType Directory -Force -Path $sshProgramDataDir | Out-Null
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null

    $sshdConfigPath = Join-Path $sshProgramDataDir "sshd_config"
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
    C:\Windows\System32\icacls.exe $sshDir /inheritance:r /grant "unityuser:(OI)(CI)F" /grant "SYSTEM:F" /grant "Administrators:F" 2>$null | Out-Null
    Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
    return Get-UnityUserAuthorizedKeysPath
}

# ─── Code update helpers ─────────────────────────────────────────────────

function Get-RemoteCommitHash($repoUrl, $branch) {
    try {
        $output = git ls-remote $repoUrl "refs/heads/$branch" 2>$null
        if ($output) { return ($output -split "\s")[0].Substring(0, 12) }
    } catch {}
    return ""
}

function Get-SavedCommitHash($dir) {
    $hashFile = Join-Path $dir ".commit-hash"
    if (Test-Path $hashFile) { return (Get-Content $hashFile -Raw).Trim() }
    return ""
}

function Save-CommitHash($dir, $hash) {
    if ($hash) { $hash | Out-File -FilePath (Join-Path $dir ".commit-hash") -Encoding UTF8 -NoNewline }
}

function Scrub-GitTokens {
    foreach ($dir in @('C:\magnitude', 'C:\agent-service')) {
        if (Test-Path "$dir\.git") {
            try {
                $url = git -C $dir remote get-url origin 2>$null
                if ($url -match '@github\.com') {
                    $clean = $url -replace 'https://[^@]+@', 'https://'
                    git -C $dir remote set-url origin $clean 2>$null
                }
            } catch {}
        }
    }
}

function Grant-ServiceDirectoryAccess {
    foreach ($dir in @("C:\agent-service", "C:\magnitude", "C:\ms-playwright")) {
        if (Test-Path $dir) {
            C:\Windows\System32\icacls.exe $dir /grant "unityuser:(OI)(CI)RX" /T /Q 2>$null
        }
    }
    Write-Log "unityuser ACLs set on service directories"
}

function Scrub-Filesystem {
    Write-Log "SCRUB: cleaning session artifacts from filesystem"

    # C:\Unity\ — remove everything except structural dirs
    if (Test-Path "C:\Unity") {
        Get-ChildItem "C:\Unity" -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notin @('.ssh', 'Local') } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }

    # unityuser profile — wipe session data from known directories
    $userProfile = "C:\Users\unityuser"
    if (Test-Path $userProfile) {
        foreach ($subdir in @('Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos', 'Music', 'Favorites', '.cache')) {
            $path = Join-Path $userProfile $subdir
            if (Test-Path $path) {
                Get-ChildItem $path -Force -ErrorAction SilentlyContinue |
                    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        # User temp
        $userTemp = Join-Path $userProfile "AppData\Local\Temp"
        if (Test-Path $userTemp) {
            Get-ChildItem $userTemp -Force -ErrorAction SilentlyContinue |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # SYSTEM profile — preserve .bun and .npm, wipe the rest
    $sysProfile = "C:\Windows\System32\config\systemprofile"
    if (Test-Path $sysProfile) {
        Get-ChildItem $sysProfile -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notin @('.bun', '.npm', 'AppData') } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Application logs
    Remove-Item "C:\agent-service\agent.log" -Force -ErrorAction SilentlyContinue
    if (Test-Path "C:\caddy\access.log") {
        Clear-Content "C:\caddy\access.log" -ErrorAction SilentlyContinue
    }

    # SYSTEM temp
    Get-ChildItem $env:TEMP -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # PowerShell history
    foreach ($histPath in @(
        "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt",
        "C:\Users\unityuser\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    )) {
        Remove-Item $histPath -Force -ErrorAction SilentlyContinue
    }

    Write-Log "SCRUB complete"
}

function Wipe-MetadataKey($key) {
    try {
        $commsUrl = Get-Metadata "comms-url"
        $idToken = Get-VMIdentityToken
        if (-not $commsUrl -or -not $idToken) { return }

        $body = @{ key = $key } | ConvertTo-Json
        Invoke-RestMethod -Uri "$commsUrl/infra/vm/wipe-metadata-key" `
            -Method POST -ContentType "application/json" `
            -Headers @{ Authorization = "Bearer $idToken" } `
            -Body $body -TimeoutSec 10 | Out-Null
        Write-Log "Wiped metadata key: $key"
    } catch {
        Write-Log "WARNING: failed to wipe metadata key $key via Comms API - $_"
    }
}

function Get-VMIdentityToken {
    try {
        return Invoke-RestMethod -Uri "$MetadataUrl/instance/service-accounts/default/identity?audience=unity-comms-vm&format=full" `
            -Headers $MetadataHeaders -TimeoutSec 5 -ErrorAction Stop
    } catch {
        return ""
    }
}

function Get-HttpResponseDetails($response) {
    $details = @{
        status = ""
        body = ""
    }
    if (-not $response) {
        return $details
    }

    try {
        $details.status = [int]$response.StatusCode
    } catch {}

    try {
        $stream = $response.GetResponseStream()
        if ($stream) {
            $reader = New-Object System.IO.StreamReader($stream)
            $details.body = $reader.ReadToEnd()
            $reader.Close()
            $stream.Close()
        }
    } catch {}

    return $details
}

function Invoke-JsonPostWithRetry {
    param(
        [string]$Uri,
        [hashtable]$Headers,
        [hashtable]$Payload,
        [string]$SuccessPrefix,
        [string]$FailurePrefix,
        [int]$Attempts = 10,
        [int]$DelaySeconds = 5
    )

    $body = $Payload | ConvertTo-Json -Compress
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Uri `
                -Method POST -ContentType "application/json" `
                -Headers $Headers -Body $body -TimeoutSec 10 `
                -UseBasicParsing -ErrorAction Stop
            $statusCode = ""
            try {
                $statusCode = [int]$response.StatusCode
            } catch {
                $statusCode = 200
            }
            $responseBody = [string]$response.Content
            if ($responseBody) {
                Write-Log "$SuccessPrefix (attempt $attempt): status=$statusCode body=$responseBody"
            } else {
                Write-Log "$SuccessPrefix (attempt $attempt): status=$statusCode"
            }
            return $true
        } catch {
            $statusCode = "request_error"
            $responseBody = ""
            if ($_.Exception.Response) {
                $failure = Get-HttpResponseDetails $_.Exception.Response
                if ($failure.status) {
                    $statusCode = $failure.status
                }
                $responseBody = [string]$failure.body
            } elseif ($_.Exception.Message) {
                $responseBody = [string]$_.Exception.Message
            }

            if ($responseBody) {
                if ($attempt -lt $Attempts) {
                    Write-Log "$FailurePrefix attempt $attempt failed: status=$statusCode body=$responseBody, retrying in ${DelaySeconds}s..."
                } else {
                    Write-Log "$FailurePrefix attempt $attempt failed: status=$statusCode body=$responseBody"
                }
            } else {
                if ($attempt -lt $Attempts) {
                    Write-Log "$FailurePrefix attempt $attempt failed: status=$statusCode, retrying in ${DelaySeconds}s..."
                } else {
                    Write-Log "$FailurePrefix attempt $attempt failed: status=$statusCode"
                }
            }

            if ($attempt -lt $Attempts) {
                Start-Sleep -Seconds $DelaySeconds
            }
        }
    }

    return $false
}

function Notify-ReleaseComplete {
    $bindingId = Get-Metadata "binding-id"
    $releaseGeneration = Get-Metadata "release-generation"
    $commsUrl = Get-Metadata "comms-url"
    $idToken = Get-VMIdentityToken
    $missing = @()
    if (-not $bindingId) { $missing += "binding-id" }
    if (-not $commsUrl) { $missing += "comms-url" }
    if (-not $idToken) { $missing += "identity-token" }
    if ($missing.Count -gt 0) {
        Write-Log "WARNING: missing release completion metadata ($($missing -join '/'))"
        return $false
    }

    $payload = @{ binding_id = $bindingId }
    if ($releaseGeneration) {
        $payload.release_generation = [int]$releaseGeneration
    }

    $reported = Invoke-JsonPostWithRetry `
        -Uri "$commsUrl/infra/vm/release-complete" `
        -Headers @{ Authorization = "Bearer $idToken" } `
        -Payload $payload `
        -SuccessPrefix "Reported release completion to Comms" `
        -FailurePrefix "Release completion" `
        -Attempts 10 `
        -DelaySeconds 3
    if (-not $reported) {
        Write-Log "WARNING: failed to report release completion to Comms"
    }
    return $reported
}

function Stop-AgentService {
    Stop-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
    Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    for ($i = 1; $i -le 10; $i++) {
        $listener = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
        if (-not $listener) { return }
        if ($i -ge 3) {
            $pids = $listener | Select-Object -ExpandProperty OwningProcess -Unique
            foreach ($p in $pids) {
                Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 1
    }
    Write-Log "WARNING: port 3000 still in use after Stop-AgentService"
}

function Invoke-Update {
    Write-Log "UPDATE: checking for code updates"

    Stop-AgentService

    $githubToken = (Get-Metadata "github-token").Trim()
    $deployEnv = Get-DeployEnv

    if ($githubToken) {
        $magnitudeUrl = "https://${githubToken}@github.com/unifyai/magnitude.git"
        $unityUrl = "https://${githubToken}@github.com/unifyai/unity.git"
    } else {
        $magnitudeUrl = "https://github.com/unifyai/magnitude.git"
        $unityUrl = "https://github.com/unifyai/unity.git"
    }
    $unityBranch = switch ($deployEnv) {
        "preview" { "preview" }
        "staging" { "staging" }
        default { "main" }
    }

    # ── Magnitude ──
    $magnitudeDir = "C:\magnitude"
    $magSaved = Get-SavedCommitHash $magnitudeDir
    $magRemote = Get-RemoteCommitHash $magnitudeUrl "unity-modifications"

    if ($magSaved -and $magRemote -and ($magSaved -eq $magRemote)) {
        Write-Log "Magnitude up-to-date ($magSaved)"

        if (Test-Path "$magnitudeDir\package.json") {
            Write-Log "Installing Magnitude dependencies..."
            Push-Location $magnitudeDir
            $bunExe = "C:\Windows\System32\config\systemprofile\.bun\bin\bun.exe"
            if (Test-Path $bunExe) {
                $bunDir = Split-Path $bunExe
                $env:Path = "$bunDir;$env:Path"
                & $bunExe install 2>&1
            } else {
                npm install 2>&1
            }
            Pop-Location
        }
        Write-Log "Magnitude dependencies installed"
    } else {
        Write-Log "Magnitude updating ($magSaved -> $magRemote)"
        if (Test-Path "$magnitudeDir\.git") {
            Push-Location $magnitudeDir
            if ($githubToken) { git remote set-url origin $magnitudeUrl 2>$null }
            git fetch --depth 1 origin unity-modifications 2>&1
            git reset --hard origin/unity-modifications 2>&1
            $commit = (git rev-parse --short=12 HEAD 2>&1)
            Save-CommitHash $magnitudeDir $commit
            Pop-Location
        } else {
            Write-Log "Magnitude not found, cloning fresh..."
            if (Test-Path $magnitudeDir) {
                cmd /c "rmdir /s /q `"$magnitudeDir`"" 2>&1 | Out-Null
            }
            git clone --depth 1 --branch unity-modifications $magnitudeUrl $magnitudeDir 2>&1
            if (Test-Path "$magnitudeDir\package.json") {
                Push-Location $magnitudeDir
                $commit = (git rev-parse --short=12 HEAD 2>&1)
                Save-CommitHash $magnitudeDir $commit
                Pop-Location
                Write-Log "Magnitude cloned (commit: $commit)"
            } else {
                Write-Log "WARNING: Magnitude clone failed"
            }
        }
        if (Test-Path "$magnitudeDir\package.json") {
            Write-Log "Installing Magnitude dependencies..."
            Push-Location $magnitudeDir
            $bunExe = "C:\Windows\System32\config\systemprofile\.bun\bin\bun.exe"
            if (Test-Path $bunExe) {
                $bunDir = Split-Path $bunExe
                $env:Path = "$bunDir;$env:Path"
                & $bunExe install 2>&1
            } else {
                npm install 2>&1
            }
            Pop-Location
        }

        # Install Patchright Chromium from magnitude-core
        $magCore = "$magnitudeDir\packages\magnitude-core"
        if (Test-Path "$magCore\package.json") {
            Write-Log "Installing Patchright Chromium..."
            $env:PLAYWRIGHT_BROWSERS_PATH = "C:\ms-playwright"
            Push-Location $magCore
            npx --yes patchright install chromium 2>&1 | Out-Null
            Pop-Location
            Write-Log "Patchright Chromium installed"
        }
        Write-Log "Magnitude updated"
    }

    # ── Agent Service (sparse checkout from unity monorepo) ──
    $agentServiceDir = "C:\agent-service"
    $asSaved = Get-SavedCommitHash $agentServiceDir
    $asRemote = Get-RemoteCommitHash $unityUrl $unityBranch

    if ($asSaved -and $asRemote -and ($asSaved -eq $asRemote)) {
        Write-Log "Agent Service up-to-date ($asSaved)"

        # Install dependencies
        if (Test-Path "$agentServiceDir\package.json") {
            Write-Log "Installing Agent Service dependencies..."
            Push-Location $agentServiceDir
            npm install 2>&1
            Pop-Location
        }
        Write-Log "Agent Service dependencies installed"
    } else {
        Write-Log "Agent Service updating ($asSaved -> $asRemote)"

        # Backup .env if exists
        $envBackup = $null
        if (Test-Path "$agentServiceDir\.env") {
            $envBackup = Get-Content "$agentServiceDir\.env" -Raw
        }

        $tmpDir = Join-Path $env:TEMP "unity-repo-$(Get-Random)"
        Write-Log "Cloning unity repo (sparse) to $tmpDir..."
        git clone --depth 1 --branch $unityBranch --filter=blob:none --sparse $unityUrl $tmpDir 2>&1
        if (Test-Path $tmpDir) {
            Push-Location $tmpDir
            git sparse-checkout set agent-service 2>&1
            $commit = (git rev-parse --short=12 HEAD 2>&1)
            Pop-Location
        }

        if (Test-Path "$tmpDir\agent-service") {
            # Remove old dir (rmdir + fallback to Remove-Item if locks prevented it)
            if (Test-Path $agentServiceDir) {
                cmd /c "rmdir /s /q `"$agentServiceDir`"" 2>&1 | Out-Null
            }
            if (Test-Path $agentServiceDir) {
                Remove-Item $agentServiceDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            Move-Item "$tmpDir\agent-service" $agentServiceDir
            Save-CommitHash $agentServiceDir $commit
            Write-Log "Agent Service cloned (commit: $commit)"
        } else {
            Write-Log "WARNING: Agent Service clone failed (check github-token metadata)"
        }
        if (Test-Path $tmpDir) {
            cmd /c "rmdir /s /q `"$tmpDir`"" 2>&1 | Out-Null
        }

        # Install dependencies
        if (Test-Path "$agentServiceDir\package.json") {
            Write-Log "Installing Agent Service dependencies..."
            Push-Location $agentServiceDir
            npm install 2>&1
            Pop-Location
        }

        # Restore .env
        if ($envBackup) {
            $envBackup | Out-File -FilePath "$agentServiceDir\.env" -Encoding UTF8 -NoNewline
        }
        Write-Log "Agent Service updated"
    }

    Scrub-GitTokens
    Grant-ServiceDirectoryAccess
    Write-Log "UPDATE complete"
}

# ─── Assignment: configure VM for an assistant ───────────────────────────

function Invoke-Assign($unifyKey) {
    Write-Log "ASSIGN: configuring VM for assistant"

    # Clean up any previous assignment (handles re-assignment without explicit release)
    Stop-AgentService
    if (Test-Path "C:\Unity\Local") {
        cmd /c rmdir "C:\Unity\Local" 2>$null
        Get-Disk | Where-Object { $_.Number -gt 0 } |
            Set-Disk -IsOffline $true -ErrorAction SilentlyContinue
        Write-Log "Unmounted previous disk"
    }

    # Update code before configuring
    Invoke-Update

    $vncPassword = Get-Metadata "vnc-password"
    $sshPublicKey = Get-Metadata "ssh-public-key"
    $diskDevice = Get-Metadata "disk-device"
    $assistantId = Get-Metadata "assistant-id"
    $bindingId = Get-Metadata "binding-id"
    $hostname = Get-Metadata "hostname"
    $orchestraUrl = Get-Metadata "orchestra-url"
    $commsUrl = Get-Metadata "comms-url"

    # Mount persistent disk
    if ($diskDevice) {
        $maxWait = 30
        $disk = $null
        for ($i = 0; $i -lt $maxWait; $i++) {
            Update-HostStorageCache -ErrorAction SilentlyContinue
            $disk = Get-Disk | Where-Object {
                $_.SerialNumber -and $_.SerialNumber -match $diskDevice
            } | Select-Object -First 1
            if (-not $disk) {
                $disk = Get-Disk | Where-Object { $_.Number -gt 0 } | Select-Object -First 1
            }
            if ($disk) { break }
            Start-Sleep -Seconds 1
        }

        if ($disk) {
            if ($disk.PartitionStyle -eq "RAW") {
                Write-Log "Formatting new disk"
                $disk | Initialize-Disk -PartitionStyle GPT -ErrorAction SilentlyContinue
                $partition = $disk | New-Partition -UseMaximumSize -AssignDriveLetter
                $partition | Format-Volume -FileSystem NTFS -NewFileSystemLabel "UnityData" -Confirm:$false
                $driveLetter = $partition.DriveLetter
            } else {
                if ($disk.IsOffline) {
                    $disk | Set-Disk -IsOffline $false
                }
                $partition = $disk | Get-Partition | Where-Object { $_.Type -ne "Reserved" } | Select-Object -First 1
                if (-not $partition.DriveLetter) {
                    $partition | Add-PartitionAccessPath -AssignDriveLetter
                    $partition = $disk | Get-Partition | Where-Object { $_.Type -ne "Reserved" } | Select-Object -First 1
                }
                $driveLetter = $partition.DriveLetter
            }

            # Create junction from C:\Unity\Local to the data drive
            if (Test-Path "C:\Unity\Local") {
                cmd /c rmdir "C:\Unity\Local" 2>$null
                Remove-Item "C:\Unity\Local" -Force -Recurse -ErrorAction SilentlyContinue
            }
            New-Item -ItemType Directory -Force -Path "C:\Unity" | Out-Null
            cmd /c mklink /J "C:\Unity\Local" "${driveLetter}:\"
            # Grant unityuser full control on both the junction and the mounted volume directly
            # (icacls through a junction modifies the reparse point, not the target)
            icacls "C:\Unity\Local" /grant "unityuser:(OI)(CI)F" /T /Q
            icacls "${driveLetter}:\" /grant "unityuser:(OI)(CI)F" /T /Q
            Write-Log "Mounted disk at C:\Unity\Local (drive $driveLetter)"
        } else {
            Write-Log "WARNING: disk device $diskDevice not found after ${maxWait}s"
        }
    }

    # Restore from GCS archive if disk is empty
    if ($diskDevice -and (Test-Path "C:\Unity\Local")) {
        $items = Get-ChildItem "C:\Unity\Local" -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne 'System Volume Information' -and $_.Name -ne '$RECYCLE.BIN' }
        if (-not $items) {
            $archiveBucket = Get-Metadata "archive-bucket"
            if ($archiveBucket -and $assistantId) {
                $archivePath = "gs://${archiveBucket}/${assistantId}.tar.gz"
                Write-Log "Empty disk detected, checking for GCS archive at $archivePath"
                try {
                    $statResult = gsutil -q stat $archivePath 2>&1
                    if ($LASTEXITCODE -eq 0) {
                        $tempArchive = Join-Path $env:TEMP "unity-restore-$(Get-Random).tar.gz"
                        gsutil -q cp $archivePath $tempArchive 2>$null
                        tar xzf $tempArchive -C "C:\Unity\Local"
                        Remove-Item $tempArchive -Force -ErrorAction SilentlyContinue
                        icacls "C:\Unity\Local" /grant "unityuser:(OI)(CI)F" /T /Q
                        Write-Log "Restored filesystem from GCS archive"
                    } else {
                        Write-Log "No GCS archive found, starting with empty filesystem"
                    }
                } catch {
                    Write-Log "WARNING: archive restore failed, starting with empty filesystem"
                }
            }
        }
    }

    # SSH authorized_keys + restart SSHD
    try {
        if ($sshPublicKey) {
            $authorizedKeysPath = Ensure-UnityUserSshConfig
            Remove-Item "C:\ProgramData\ssh\administrators_authorized_keys" -Force -ErrorAction SilentlyContinue
            Set-Content -Path $authorizedKeysPath -Value $sshPublicKey -Encoding UTF8
            icacls $authorizedKeysPath /inheritance:r /grant "unityuser:F" /grant "SYSTEM:F" /grant "Administrators:F" 2>$null | Out-Null
            Restart-Service sshd -ErrorAction SilentlyContinue
            Write-Log "SSH authorized_keys configured, SSHD restarted"
        }
    } catch {
        Write-Log "WARNING: SSH setup failed: $_"
    }

    # TightVNC password via registry
    try {
        if ($vncPassword) {
            $vncKey = [System.Text.Encoding]::ASCII.GetBytes(($vncPassword + "`0`0`0`0`0`0`0`0").Substring(0, 8))
            $desKey = [byte[]]@(0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0)
            $des = [System.Security.Cryptography.DES]::Create()
            $des.Mode = [System.Security.Cryptography.CipherMode]::ECB
            $des.Padding = [System.Security.Cryptography.PaddingMode]::None
            $des.Key = $desKey
            $encryptor = $des.CreateEncryptor()
            $encrypted = $encryptor.TransformFinalBlock($vncKey, 0, 8)
            Set-ItemProperty -Path "HKLM:\SOFTWARE\TightVNC\Server" -Name "Password" -Value $encrypted -Type Binary
            Set-ItemProperty -Path "HKLM:\SOFTWARE\TightVNC\Server" -Name "ControlPassword" -Value $encrypted -Type Binary
            Restart-Service "tvnserver" -ErrorAction SilentlyContinue
            Write-Log "VNC password updated"
        }
    } catch {
        Write-Log "WARNING: VNC password update failed: $_"
    }

    # Agent Service: kill existing, write .env, start via scheduled task (interactive session)
    try {
        Stop-AgentService

        $agentServiceDir = "C:\agent-service"
        $envContent = @"
PORT=3000
NODE_ENV=production
UNIFY_KEY=$unifyKey
ORCHESTRA_URL=$orchestraUrl
UNITY_COMMS_URL=$commsUrl
PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
DISPLAY=:1
"@
        Set-Content -Path "$agentServiceDir\.env" -Value $envContent -Encoding UTF8
        icacls "$agentServiceDir\.env" /inheritance:r /grant "unityuser:R" /grant "SYSTEM:F" /grant "Administrators:F" 2>$null
        Write-Log "Agent Service .env configured"

        $startBat = @"
@echo off
set PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
cd /d C:\agent-service
npx --yes ts-node src/index.ts >> C:\Unity\agent-service.log 2>&1
"@
        Set-Content -Path "$agentServiceDir\start-agent.bat" -Value $startBat -Encoding ASCII

        if (Test-Path "$agentServiceDir\package.json") {
            Enable-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
            Start-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
            Write-Log "Agent Service started (interactive session via scheduled task)"
        }
    } catch {
        Write-Log "WARNING: Agent Service start failed: $_"
    }

    # Wait for Agent Service to be listening
    Write-Log "Waiting for Agent Service on port 3000..."
    for ($i = 1; $i -le 60; $i++) {
        $listener = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            Write-Log "Agent Service is listening on port 3000 (after ${i}s)"
            break
        }
        if ($i -eq 60) {
            Write-Log "WARNING: Agent Service not listening on port 3000 after 60s, proceeding anyway"
        }
        Start-Sleep -Seconds 1
    }

    try {
        $resScript = "C:\novnc\set-resolution.ps1"
        if (Test-Path $resScript) {
            Start-ScheduledTask -TaskName "SetDisplayResolution" -ErrorAction SilentlyContinue
            Write-Log "Display resolution task triggered"
        }
    } catch {
        Write-Log "WARNING: Display resolution trigger failed: $_"
    }

    $makKey = Get-Metadata "office-mak-key"
    if ($makKey) {
        $osppPath = "C:\Program Files\Microsoft Office\root\Office16\OSPP.VBS"
        if (Test-Path $osppPath) {
            $status = cscript //nologo $osppPath /dstatus 2>$null | Out-String
            if ($status -notmatch "---LICENSED---") {
                cscript //nologo $osppPath /inpkey:$makKey 2>$null | Out-Null
                cscript //nologo $osppPath /act 2>$null | Out-Null
                Write-Log "Office activated with MAK key"
            } else {
                Write-Log "Office already activated, skipping"
            }
        }
    }

    # Wait for Caddy on port 443 before notifying (the /vm/ready endpoint probes HTTPS back)
    Write-Log "Waiting for Caddy on port 443..."
    $caddyReady = $false
    for ($i = 1; $i -le 30; $i++) {
        $listener = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            Write-Log "Caddy is listening on port 443 (after ${i}s)"
            $caddyReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $caddyReady) {
        Write-Log "Caddy not listening after 30s, restarting..."
        Stop-Process -Name "caddy" -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        $caddyExe = "C:\caddy\caddy.exe"
        $caddyfile = "C:\caddy\Caddyfile"
        if ((Test-Path $caddyExe) -and (Test-Path $caddyfile)) {
            $psCommand = "Set-Location 'C:\caddy'; & '$caddyExe' run --config '$caddyfile'"
            Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory "C:\caddy"
        }
        for ($i = 1; $i -le 30; $i++) {
            $listener = Get-NetTCPConnection -LocalPort 443 -State Listen -ErrorAction SilentlyContinue
            if ($listener) {
                Write-Log "Caddy is listening on port 443 (after restart, ${i}s)"
                $caddyReady = $true
                break
            }
            if ($i -eq 30) {
                Write-Log "WARNING: Caddy not listening on port 443 after restart, proceeding anyway"
            }
            Start-Sleep -Seconds 1
        }
    }

    # Send ready notification
    if ($commsUrl -and $hostname -and $unifyKey -and $assistantId -and $bindingId) {
        $readySent = Invoke-JsonPostWithRetry `
            -Uri "$commsUrl/infra/vm/ready" `
            -Headers @{ Authorization = "Bearer $unifyKey" } `
            -Payload @{
                assistant_id = $assistantId
                binding_id = $bindingId
                vm_type = "windows"
                hostname = $hostname
            } `
            -SuccessPrefix "VM ready notification sent" `
            -FailurePrefix "VM ready notification" `
            -Attempts 10 `
            -DelaySeconds 5
        if (-not $readySent) {
            Write-Log "WARNING: failed to send VM ready notification to Comms"
        }
    } else {
        $missing = @()
        if (-not $assistantId) { $missing += "assistant-id" }
        if (-not $bindingId) { $missing += "binding-id" }
        if (-not $hostname) { $missing += "hostname" }
        if (-not $commsUrl) { $missing += "comms-url" }
        if (-not $unifyKey) { $missing += "unify-key" }
        Write-Log "WARNING: skipping VM ready notification because required metadata is missing ($($missing -join '/'))"
    }

    Write-Log "ASSIGN complete"
}

# ─── Release: clean up VM for return to pool ─────────────────────────────

function Invoke-Release {
    $releaseToken = Get-CurrentReleaseToken
    if ($releaseToken) {
        Write-Log "RELEASE: cleaning up VM (token=$releaseToken)"
    } else {
        Write-Log "RELEASE: cleaning up VM"
    }

    # Stop Agent Service
    Stop-AgentService
    Disable-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
    Write-Log "Agent Service stopped"

    # Clear .env
    Remove-Item "C:\agent-service\.env" -Force -ErrorAction SilentlyContinue
    Write-Log "Agent Service .env cleared"

    # Clear SSH keys
    Remove-Item (Get-UnityUserAuthorizedKeysPath) -Force -ErrorAction SilentlyContinue
    Remove-Item "C:\ProgramData\ssh\administrators_authorized_keys" -Force -ErrorAction SilentlyContinue
    Write-Log "SSH authorized_keys cleared"

    # Reset VNC password
    try {
        $deadPw = [System.Text.Encoding]::ASCII.GetBytes("disabled")
        $desKey = [byte[]]@(0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0)
        $des = [System.Security.Cryptography.DES]::Create()
        $des.Mode = [System.Security.Cryptography.CipherMode]::ECB
        $des.Padding = [System.Security.Cryptography.PaddingMode]::None
        $des.Key = $desKey
        $encryptor = $des.CreateEncryptor()
        $encrypted = $encryptor.TransformFinalBlock($deadPw, 0, 8)
        Set-ItemProperty -Path "HKLM:\SOFTWARE\TightVNC\Server" -Name "Password" -Value $encrypted -Type Binary -ErrorAction SilentlyContinue
        Set-ItemProperty -Path "HKLM:\SOFTWARE\TightVNC\Server" -Name "ControlPassword" -Value $encrypted -Type Binary -ErrorAction SilentlyContinue
        Restart-Service "tvnserver" -ErrorAction SilentlyContinue
        Write-Log "VNC password reset"
    } catch {
        Write-Log "WARNING: failed to reset VNC password: $_"
    }

    # Archive filesystem to GCS before unmount
    if (Test-Path "C:\Unity\Local") {
        $assistantId = Get-Metadata "assistant-id"
        $archiveBucket = Get-Metadata "archive-bucket"
        if ($assistantId -and $archiveBucket) {
            $archivePath = "gs://${archiveBucket}/${assistantId}.tar.gz"
            Write-Log "Archiving C:\Unity\Local to $archivePath"
            try {
                $tempArchive = Join-Path $env:TEMP "unity-archive-$(Get-Random).tar.gz"
                tar czf $tempArchive -C "C:\Unity\Local" .
                gsutil -q cp $tempArchive $archivePath 2>$null
                Remove-Item $tempArchive -Force -ErrorAction SilentlyContinue
                Write-Log "Archive uploaded successfully"
            } catch {
                Write-Log "WARNING: archive upload failed, PD data will be preserved as fallback"
            }
        }
    }

    # Unmount persistent disk
    if (Test-Path "C:\Unity\Local") {
        cmd /c rmdir "C:\Unity\Local" 2>$null
        # Set disk offline
        Get-Disk | Where-Object { $_.FriendlyName -notmatch "Google" -or $_.Number -gt 0 } |
            Where-Object { $_.Number -gt 0 } |
            Set-Disk -IsOffline $true -ErrorAction SilentlyContinue
        Write-Log "Unmounted persistent disk"
    }

    Scrub-Filesystem

    # Update code while VM is idle so next assignment starts with latest
    Invoke-Update

    Wipe-MetadataKey "github-token"
    if (Notify-ReleaseComplete) {
        Save-LastReleaseToken $releaseToken
    } else {
        Write-Log "WARNING: release completion callback did not succeed; token remains unacked"
    }

    Write-Log "RELEASE complete"
}

# ─── TLS cert refresh: update Caddy when cert metadata changes ────────────

function Invoke-RefreshTls {
    $tlsCert = Get-Metadata "tls-fullchain"
    $tlsKey = Get-Metadata "tls-privkey"
    if (-not $tlsCert -or -not $tlsKey) { return }

    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($tlsCert)
    $hash = [BitConverter]::ToString($md5.ComputeHash($bytes)).Replace("-", "").ToLower()

    if ($hash -eq $script:PrevTlsHash) { return }

    Write-Log "TLS cert changed, updating Caddy certs"
    $certDir = "C:\caddy\certs"
    New-Item -ItemType Directory -Force -Path $certDir -ErrorAction SilentlyContinue | Out-Null
    [System.IO.File]::WriteAllText("$certDir\fullchain.pem", $tlsCert)
    [System.IO.File]::WriteAllText("$certDir\privkey.pem", $tlsKey)

    $caddyProc = Get-Process -Name "caddy" -ErrorAction SilentlyContinue
    if ($caddyProc) {
        $caddyDir = "C:\caddy"
        $caddyExe = "$caddyDir\caddy.exe"
        $caddyfile = "$caddyDir\Caddyfile"
        try {
            & $caddyExe reload --config $caddyfile 2>$null
            Write-Log "Caddy reloaded with new cert"
        } catch {
            Write-Log "WARNING: Caddy reload failed, restarting"
            Stop-Process -Name "caddy" -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            $psCommand = "Set-Location '$caddyDir'; & '$caddyExe' run --config '$caddyfile'"
            Start-Process -FilePath "powershell.exe" -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$psCommand`"" -WorkingDirectory $caddyDir
            Write-Log "Caddy restarted with new cert"
        }
    }

    $script:PrevTlsHash = $hash
}

function Start-Watcher {
    Write-Log "Unity Pool Watcher starting"

    # Seed TLS hash to avoid unnecessary reload on first loop iteration
    $initTls = Get-Metadata "tls-fullchain"
    if ($initTls) {
        $md5 = [System.Security.Cryptography.MD5]::Create()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($initTls)
        $PrevTlsHash = [BitConverter]::ToString($md5.ComputeHash($bytes)).Replace("-", "").ToLower()
    }

    # Pre-fetch etag so the first long-poll has a valid value and won't block
    # on already-set metadata. Also check current state immediately to handle
    # assignments that happened before the watcher started.
    try {
        $initResponse = Invoke-WebRequest -Uri "$MetadataUrl/instance/attributes/?recursive=true" `
            -Headers $MetadataHeaders -TimeoutSec 10 -UseBasicParsing
        $Etag = $initResponse.Headers["ETag"]
    } catch {
        Write-Log "WARNING: Initial metadata fetch failed: $_"
    }

    $currentUnifyKey = Get-Metadata "unify-key"
    $currentReleaseToken = Get-CurrentReleaseToken
    $lastHandledReleaseToken = Get-LastReleaseToken
    if ($currentUnifyKey -ne $PrevUnifyKey -and $currentUnifyKey) {
        Invoke-Assign $currentUnifyKey
    }
    if (Should-TriggerRelease $PrevUnifyKey $currentUnifyKey $currentReleaseToken $lastHandledReleaseToken) {
        Invoke-Release
        $lastHandledReleaseToken = Get-LastReleaseToken
    }
    $PrevUnifyKey = $currentUnifyKey

    while ($true) {
        try {
            # Long-poll for metadata changes (Etag is always valid here)
            $uri = "$MetadataUrl/instance/attributes/?recursive=true&wait_for_change=true"
            $uri += "&last_etag=$Etag"

            try {
                $response = Invoke-WebRequest -Uri $uri -Headers $MetadataHeaders -TimeoutSec 0 -UseBasicParsing
                $Etag = $response.Headers["ETag"]
            } catch {
                Write-Log "Metadata poll failed, retrying in 5s"
                Start-Sleep -Seconds 5
                continue
            }

            $currentUnifyKey = Get-Metadata "unify-key"
            $currentReleaseToken = Get-CurrentReleaseToken
            $lastHandledReleaseToken = Get-LastReleaseToken

            if ($currentUnifyKey -ne $PrevUnifyKey -and $currentUnifyKey) {
                Invoke-Assign $currentUnifyKey
            }

            if (Should-TriggerRelease $PrevUnifyKey $currentUnifyKey $currentReleaseToken $lastHandledReleaseToken) {
                Invoke-Release
                $lastHandledReleaseToken = Get-LastReleaseToken
            }

            $PrevUnifyKey = $currentUnifyKey

            # Refresh TLS cert if metadata changed (handles renewal pushes)
            Invoke-RefreshTls
        } catch {
            Write-Log "ERROR in watcher loop: $_"
            Start-Sleep -Seconds 5
        }
    }
}

if (-not $SkipMain) {
    Start-Watcher
}
