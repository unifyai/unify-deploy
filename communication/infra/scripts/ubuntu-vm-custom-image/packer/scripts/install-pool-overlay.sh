#!/bin/bash
# =============================================================================
# Pool Overlay for Ubuntu VM
#
# Runs after install-base.sh to add pool-specific components:
# - unityuser (SFTP file sync on port 2222)
# - SSHD configuration for pool
# - Droid Pool Watcher systemd service
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
    useradd -m -d /Droid -s /bin/bash unityuser
    echo "  Created user unityuser with home /Droid"
else
    echo "  User unityuser already exists"
fi

mkdir -p /Droid/.ssh /Droid/Local /Droid/.config /Droid/.local /Droid/.cache
# /Droid must be root-owned for SSHD ChrootDirectory; subdirectories are user-owned
chown root:root /Droid
chmod 755 /Droid
chown -R unityuser:unityuser /Droid/.ssh /Droid/Local /Droid/.config /Droid/.local /Droid/.cache
chmod 700 /Droid/.ssh
chmod 755 /Droid/Local
ln -sfn /root/.cache/ms-playwright /Droid/.cache/ms-playwright
echo "  Created /Droid/.ssh, /Droid/Local, /Droid/.config, /Droid/.local, /Droid/.cache"

# Shell config for unityuser desktop terminal sessions
cat > /Droid/.bashrc << 'BASHRC'
if [[ -d /Droid ]] && [[ $- == *i* ]] && [[ -n "$DISPLAY" ]] && [[ -z "$DROID_SHELL_INIT" ]]; then
    export DROID_SHELL_INIT=1
    cd /Droid
fi
BASHRC
chown unityuser:unityuser /Droid/.bashrc
echo "  /Droid/.bashrc configured"

# =============================================================================
# SSHD: file sync on port 2222
# =============================================================================
echo ""
echo "=== Configuring SSHD for pool ==="

if ! grep -q "^Port 2222" /etc/ssh/sshd_config; then
    cat >> /etc/ssh/sshd_config << 'SSHEOF'

# =============================================================================
# Droid Pool: SFTP file sync for unityuser on port 2222
# =============================================================================
Port 22
Port 2222

Match User unityuser
    ForceCommand internal-sftp
    ChrootDirectory /Droid
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
echo "=== Installing Droid Pool Watcher ==="

if [[ -f /tmp/droid-pool-watcher.sh ]]; then
    cp /tmp/droid-pool-watcher.sh /usr/local/bin/droid-pool-watcher.sh
    chmod +x /usr/local/bin/droid-pool-watcher.sh
    echo "  Installed /usr/local/bin/droid-pool-watcher.sh"
else
    echo "  WARNING: /tmp/droid-pool-watcher.sh not found"
fi

cat > /etc/systemd/system/droid-pool-watcher.service << 'UNITEOF'
[Unit]
Description=Droid Pool Watcher - metadata-driven VM assignment
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/droid-pool-watcher.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=droid-pool-watcher

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable droid-pool-watcher.service
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
echo "  - Pool watcher: droid-pool-watcher.service (systemd)"
echo ""
