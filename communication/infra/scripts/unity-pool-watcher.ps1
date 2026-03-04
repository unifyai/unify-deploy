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

# ─── Assignment: configure VM for an assistant ───────────────────────────

function Invoke-Assign($unifyKey) {
    Write-Log "ASSIGN: configuring VM for assistant"

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
            $disk = Get-Disk | Where-Object { $_.FriendlyName -match $diskDevice -or $_.SerialNumber -match $diskDevice } | Select-Object -First 1
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
            Restart-Service "TightVNC Server" -ErrorAction SilentlyContinue
            Write-Log "VNC password updated"

            Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" `
                -Name "DefaultPassword" -Value $vncPassword -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Log "WARNING: VNC password update failed: $_"
    }

    # Agent Service: kill existing, write .env, start directly
    try {
        Get-Process -Name "node" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1

        $agentServiceDir = "C:\agent-service"
        $envContent = @"
PORT=3000
NODE_ENV=production
UNIFY_KEY=$unifyKey
ORCHESTRA_URL=$orchestraUrl
UNITY_COMMS_URL=$commsUrl
"@
        Set-Content -Path "$agentServiceDir\.env" -Value $envContent -Encoding UTF8
        Write-Log "Agent Service .env configured"

        # Ensure start script exists
        $startBat = @"
@echo off
set PLAYWRIGHT_BROWSERS_PATH=C:\ms-playwright
cd /d C:\agent-service
npx --yes ts-node src/index.ts >> C:\agent-service\agent.log 2>&1
"@
        Set-Content -Path "$agentServiceDir\start-agent.bat" -Value $startBat -Encoding ASCII

        if (Test-Path "$agentServiceDir\package.json") {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$agentServiceDir\start-agent.bat`"" `
                -WorkingDirectory $agentServiceDir -WindowStyle Hidden
            Write-Log "Agent Service started (direct)"
        }
    } catch {
        Write-Log "WARNING: Agent Service start failed: $_"
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
        Restart-Service "TightVNC Server" -ErrorAction SilentlyContinue
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

    Write-Log "RELEASE complete"
}

# ─── Main watcher loop ───────────────────────────────────────────────────

Write-Log "Unity Pool Watcher starting"

$PrevUnifyKey = Get-Metadata "unify-key"
Write-Log "Initial unify-key: $(if ($PrevUnifyKey) { '(set)' } else { '(empty)' })"

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

        if ($currentUnifyKey -and -not $PrevUnifyKey) {
            Invoke-Assign $currentUnifyKey
        } elseif (-not $currentUnifyKey -and $PrevUnifyKey) {
            Invoke-Release
        }

        $PrevUnifyKey = $currentUnifyKey
    } catch {
        Write-Log "ERROR in watcher loop: $_"
        Start-Sleep -Seconds 5
    }
}
