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

$ErrorActionPreference = "Continue"

$MetadataUrl = "http://metadata.google.internal/computeMetadata/v1"
$MetadataHeaders = @{"Metadata-Flavor" = "Google"}
$Etag = ""
$PrevUnifyKey = ""
$PrevTlsHash = ""

function Get-Metadata($key) {
    try {
        $response = Invoke-RestMethod -Uri "$MetadataUrl/instance/attributes/$key" `
            -Headers $MetadataHeaders -TimeoutSec 5 -ErrorAction SilentlyContinue
        return [string]$response
    } catch {
        return ""
    }
}

function Write-Log($message) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Write-Host "[$ts] $message"
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

function Invoke-Update {
    Write-Log "UPDATE: checking for code updates"

    # Kill node processes upfront to release file locks on Magnitude's built files
    Stop-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
    Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    $githubToken = (Get-Metadata "github-token").Trim()
    $staging = Get-Metadata "staging"

    if ($githubToken) {
        $magnitudeUrl = "https://${githubToken}@github.com/unifyai/magnitude.git"
        $unityUrl = "https://${githubToken}@github.com/unifyai/unity.git"
    } else {
        $magnitudeUrl = "https://github.com/unifyai/magnitude.git"
        $unityUrl = "https://github.com/unifyai/unity.git"
    }
    $unityBranch = if ($staging) { "staging" } else { "main" }

    # ── Magnitude ──
    $magnitudeDir = "C:\magnitude"
    $magSaved = Get-SavedCommitHash $magnitudeDir
    $magRemote = Get-RemoteCommitHash $magnitudeUrl "unity-modifications"

    if ($magSaved -and $magRemote -and ($magSaved -eq $magRemote)) {
        Write-Log "Magnitude up-to-date ($magSaved)"
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
            # Preserve node_modules
            if (Test-Path "$agentServiceDir\node_modules") {
                Move-Item "$agentServiceDir\node_modules" "$tmpDir\agent-service\node_modules" -Force
            }
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

    Write-Log "UPDATE complete"
}

# ─── Assignment: configure VM for an assistant ───────────────────────────

function Invoke-Assign($unifyKey) {
    Write-Log "ASSIGN: configuring VM for assistant"

    # Clean up any previous assignment (handles re-assignment without explicit release)
    Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    if (Test-Path "C:\Unity\Local") {
        cmd /c rmdir "C:\Unity\Local" 2>$null
        Get-Disk | Where-Object { $_.Number -gt 0 } |
            Set-Disk -IsOffline $true -ErrorAction SilentlyContinue
        Write-Log "Unmounted previous disk"
    }

    # Update code before configuring (skips quickly if already up-to-date)
    Invoke-Update

    $vncPassword = Get-Metadata "vnc-password"
    $sshPublicKey = Get-Metadata "ssh-public-key"
    $diskDevice = Get-Metadata "disk-device"
    $assistantId = Get-Metadata "assistant-id"
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
            # Grant unityuser full control
            icacls "C:\Unity\Local" /grant "unityuser:F" /T /Q 2>$null
            Write-Log "Mounted disk at C:\Unity\Local (drive $driveLetter)"
        } else {
            Write-Log "WARNING: disk device $diskDevice not found after ${maxWait}s"
        }
    }

    # SSH authorized_keys + restart SSHD
    try {
        if ($sshPublicKey) {
            $sshDir = "C:\ProgramData\ssh"
            New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
            Set-Content -Path "$sshDir\administrators_authorized_keys" -Value $sshPublicKey -Encoding UTF8
            icacls "$sshDir\administrators_authorized_keys" /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" 2>$null
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
        Stop-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
        Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1

        $agentServiceDir = "C:\agent-service"
        $envContent = @"
PORT=3000
NODE_ENV=production
UNIFY_KEY=$unifyKey
ORCHESTRA_URL=$orchestraUrl
UNITY_COMMS_URL=$commsUrl
PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
"@
        Set-Content -Path "$agentServiceDir\.env" -Value $envContent -Encoding UTF8
        Write-Log "Agent Service .env configured"

        $startBat = @"
@echo off
set PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
cd /d C:\agent-service
npx --yes ts-node src/index.ts >> C:\agent-service\agent.log 2>&1
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

    # Display resolution: run the script directly in the console session
    try {
        $resScript = "C:\novnc\set-resolution.ps1"
        if (Test-Path $resScript) {
            Start-ScheduledTask -TaskName "SetDisplayResolution" -ErrorAction SilentlyContinue
            Write-Log "Display resolution task triggered"
        }
    } catch {
        Write-Log "WARNING: Display resolution trigger failed: $_"
    }

    # Activate Office if MAK key provided and not already licensed
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

    # Send ready notification
    if ($commsUrl -and $hostname -and $unifyKey -and $assistantId) {
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                $body = @{ assistant_id = $assistantId; vm_type = "windows"; hostname = $hostname } | ConvertTo-Json
                $result = Invoke-RestMethod -Uri "$commsUrl/infra/vm/ready" `
                    -Method POST -ContentType "application/json" `
                    -Headers @{ Authorization = "Bearer $unifyKey" } `
                    -Body $body -TimeoutSec 10
                Write-Log "VM ready notification sent (attempt $attempt)"
                break
            } catch {
                Write-Log "VM ready notification attempt $attempt failed, retrying in 5s..."
                Start-Sleep -Seconds 5
            }
        }
    }

    Write-Log "ASSIGN complete"
}

# ─── Release: clean up VM for return to pool ─────────────────────────────

function Invoke-Release {
    Write-Log "RELEASE: cleaning up VM"

    # Stop Agent Service
    Stop-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName "StartAgentService" -ErrorAction SilentlyContinue
    Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Log "Agent Service stopped"

    # Clear .env
    Remove-Item "C:\agent-service\.env" -Force -ErrorAction SilentlyContinue
    Write-Log "Agent Service .env cleared"

    # Clear SSH keys
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

    # Unmount persistent disk
    if (Test-Path "C:\Unity\Local") {
        cmd /c rmdir "C:\Unity\Local" 2>$null
        # Set disk offline
        Get-Disk | Where-Object { $_.FriendlyName -notmatch "Google" -or $_.Number -gt 0 } |
            Where-Object { $_.Number -gt 0 } |
            Set-Disk -IsOffline $true -ErrorAction SilentlyContinue
        Write-Log "Unmounted persistent disk"
    }

    # Update code while VM is idle so next assignment starts with latest
    Invoke-Update

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

# ─── Main watcher loop ───────────────────────────────────────────────────

Write-Log "Unity Pool Watcher starting"

# $PrevUnifyKey starts empty (line 19) so the first loop iteration
# detects an already-set key and runs Invoke-Assign.  This handles the
# case where assign_pool_vm wrote metadata before the watcher started.

# Seed TLS hash to avoid unnecessary reload on first loop iteration
$initTls = Get-Metadata "tls-fullchain"
if ($initTls) {
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($initTls)
    $PrevTlsHash = [BitConverter]::ToString($md5.ComputeHash($bytes)).Replace("-", "").ToLower()
}

while ($true) {
    try {
        # Long-poll for metadata changes
        $uri = "$MetadataUrl/instance/attributes/?recursive=true&wait_for_change=true"
        if ($Etag) {
            $uri += "&last_etag=$Etag"
        }

        try {
            $response = Invoke-WebRequest -Uri $uri -Headers $MetadataHeaders -TimeoutSec 0 -UseBasicParsing
            $Etag = $response.Headers["ETag"]
        } catch {
            Write-Log "Metadata poll failed, retrying in 5s"
            Start-Sleep -Seconds 5
            continue
        }

        $currentUnifyKey = Get-Metadata "unify-key"

        if ($currentUnifyKey -ne $PrevUnifyKey) {
            if ($currentUnifyKey) {
                Invoke-Assign $currentUnifyKey
            } else {
                Invoke-Release
            }
        }

        $PrevUnifyKey = $currentUnifyKey

        # Refresh TLS cert if metadata changed (handles renewal pushes)
        Invoke-RefreshTls
    } catch {
        Write-Log "ERROR in watcher loop: $_"
        Start-Sleep -Seconds 5
    }
}
