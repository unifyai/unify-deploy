#!/bin/bash
# =============================================================================
# Pool Overlay for Ubuntu VM
#
# Runs after install-base.sh to add pool-specific components:
# - unityuser (SFTP file sync on port 2222)
# - SSHD configuration for pool
# - Unity Pool Watcher systemd service
# =============================================================================

set -euo pipefail

echo ""
echo "=========================================="
echo "  Pool Overlay: Starting"
echo "=========================================="

# =============================================================================
# Pool User: unityuser (for SFTP file sync)
# =============================================================================
echo ""
echo "=== Creating pool user: unityuser ==="

if ! id "unityuser" &>/dev/null; then
    useradd -m -d /Unity -s /bin/bash unityuser
    echo "  Created user unityuser with home /Unity"
else
    echo "  User unityuser already exists"
fi

mkdir -p /Unity/.ssh /Unity/Local /Unity/.config /Unity/.local /Unity/.cache
# /Unity must be root-owned for SSHD ChrootDirectory; subdirectories are user-owned
chown root:root /Unity
chmod 755 /Unity
chown -R unityuser:unityuser /Unity/.ssh /Unity/Local /Unity/.config /Unity/.local /Unity/.cache
chmod 700 /Unity/.ssh
chmod 755 /Unity/Local
echo "  Created /Unity/.ssh, /Unity/Local, /Unity/.config, /Unity/.local, /Unity/.cache"

# Shell config for unityuser desktop terminal sessions
cat > /Unity/.bashrc << 'BASHRC'
if [[ -d /Unity ]] && [[ $- == *i* ]] && [[ -n "$DISPLAY" ]] && [[ -z "$UNITY_SHELL_INIT" ]]; then
    export UNITY_SHELL_INIT=1
    cd /Unity
fi
BASHRC
chown unityuser:unityuser /Unity/.bashrc
echo "  /Unity/.bashrc configured"

# =============================================================================
# SSHD: file sync on port 2222
# =============================================================================
echo ""
echo "=== Configuring SSHD for pool ==="

if ! grep -q "^Port 2222" /etc/ssh/sshd_config; then
    cat >> /etc/ssh/sshd_config << 'SSHEOF'

# =============================================================================
# Unity Pool: SFTP file sync for unityuser on port 2222
# =============================================================================
Port 22
Port 2222

Match User unityuser
    ForceCommand internal-sftp
    ChrootDirectory /Unity
    AllowTcpForwarding no
    X11Forwarding no
SSHEOF
    echo "  SSHD configured for unityuser on port 2222"
else
    echo "  SSHD already configured for port 2222"
fi

# =============================================================================
# Pool Watcher: systemd service for metadata-driven assignment/release
# =============================================================================
echo ""
echo "=== Installing Unity Pool Watcher ==="

if [[ -f /tmp/unity-pool-watcher.sh ]]; then
    cp /tmp/unity-pool-watcher.sh /usr/local/bin/unity-pool-watcher.sh
    chmod +x /usr/local/bin/unity-pool-watcher.sh
    echo "  Installed /usr/local/bin/unity-pool-watcher.sh"
else
    echo "  WARNING: /tmp/unity-pool-watcher.sh not found"
fi

cat > /etc/systemd/system/unity-pool-watcher.service << 'UNITEOF'
[Unit]
Description=Unity Pool Watcher - metadata-driven VM assignment
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/unity-pool-watcher.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=unity-pool-watcher

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable unity-pool-watcher.service
echo "  Systemd service installed and enabled"

# =============================================================================
# Patchright Chromium (pre-installed for faster first assign)
# =============================================================================
echo ""
echo "=== Installing Patchright Chromium ==="

npx --yes patchright install --with-deps chromium 2>&1 || true
echo "  Patchright Chromium installed"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=========================================="
echo "  Pool Overlay: Complete"
echo "=========================================="
echo ""
echo "Added:"
echo "  - Pool user: unityuser (SFTP on port 2222)"
echo "  - Patchright Chromium"
echo "  - Pool watcher: unity-pool-watcher.service (systemd)"
echo ""
