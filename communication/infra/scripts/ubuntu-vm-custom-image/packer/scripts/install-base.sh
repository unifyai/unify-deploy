#!/bin/bash
# =============================================================================
# install-base.sh - Packer provisioner script for Unity Ubuntu VM
#
# This script pre-installs all software into the base image:
# - XFCE4 Desktop (full)
# - TigerVNC Server
# - noVNC + websockify
# - Node.js 22 + Bun
# - Playwright + Chromium
# - Caddy
# - supervisord
#
# NOT included (runtime configuration):
# - VNC password
# - Magnitude/Agent Service (needs github-token)
# - API keys, hostname config
# =============================================================================

set -e

# Versions
NODEJS_VERSION=22
PLAYWRIGHT_VERSION="1.52.0"
CADDY_VERSION="2.7.6"

echo "=========================================="
echo "  Unity Ubuntu VM Base Image Build"
echo "=========================================="
echo ""

export DEBIAN_FRONTEND=noninteractive

# =============================================================================
# System packages + XFCE4 Desktop
# =============================================================================
echo "=== Installing system packages and XFCE4 ==="

apt-get update
apt-get install -y --no-install-recommends \
    locales \
    tzdata \
    ca-certificates \
    curl \
    wget \
    git \
    gnupg \
    xfce4 \
    xfce4-goodies \
    xfce4-terminal \
    tigervnc-standalone-server \
    tigervnc-common \
    dbus-x11 \
    x11-utils \
    x11-xserver-utils \
    python3 \
    python3-pip \
    python3-venv \
    supervisor \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-color-emoji \
    sudo \
    vim \
    htop \
    procps \
    net-tools \
    unzip

# Configure locale
sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen
locale-gen

echo "System packages installed"

# =============================================================================
# noVNC + websockify
# =============================================================================
echo ""
echo "=== Installing noVNC + websockify ==="

git clone --depth 1 https://github.com/novnc/noVNC.git /novnc
rm -rf /novnc/.git

pip3 install --no-cache-dir websockify pycryptodome

# Copy custom noVNC wrapper
if [[ -f /tmp/novnc-custom.html ]]; then
    cp /tmp/novnc-custom.html /novnc/index.html
    cp /tmp/novnc-custom.html /novnc/custom.html
fi

echo "noVNC installed at /novnc"

# =============================================================================
# Node.js 22
# =============================================================================
echo ""
echo "=== Installing Node.js ${NODEJS_VERSION} ==="

curl -fsSL https://deb.nodesource.com/setup_${NODEJS_VERSION}.x | bash -
apt-get install -y nodejs
npm install -g npm@latest

node --version
npm --version

echo "Node.js installed"

# =============================================================================
# Bun
# =============================================================================
echo ""
echo "=== Installing Bun ==="

curl -fsSL https://bun.sh/install | bash

# Add to system-wide profile
echo 'export BUN_INSTALL="/root/.bun"' >> /etc/profile.d/bun.sh
echo 'export PATH="$BUN_INSTALL/bin:$PATH"' >> /etc/profile.d/bun.sh
chmod +x /etc/profile.d/bun.sh

# Source for current session
export BUN_INSTALL="/root/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"

echo "Bun installed"

# =============================================================================
# Caddy
# =============================================================================
echo ""
echo "=== Installing Caddy ${CADDY_VERSION} ==="

curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
    | tar -xzC /usr/local/bin caddy
chmod +x /usr/local/bin/caddy

caddy version

echo "Caddy installed"

# =============================================================================
# Playwright + Chromium
# =============================================================================
echo ""
echo "=== Installing Playwright ${PLAYWRIGHT_VERSION} + Chromium ==="

# Install Playwright with all system dependencies for Chromium
npx playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium

echo "Playwright + Chromium installed"

# =============================================================================
# Configure Chromium as Default Browser
# =============================================================================
echo ""
echo "=== Configuring Chromium as default browser ==="

# Create wrapper script that finds and launches Playwright's Chromium
cat > /usr/local/bin/chromium-browser << 'CHROMEWRAPPER'
#!/bin/bash
# Wrapper script for Playwright Chromium
# Finds the Chromium binary in Playwright's cache and launches it
CHROME_PATH=$(find /root/.cache/ms-playwright -name "chrome" -type f -executable 2>/dev/null | head -1)
if [[ -z "$CHROME_PATH" ]]; then
    echo "Error: Chromium not found in Playwright cache" >&2
    exit 1
fi
exec "$CHROME_PATH" --no-sandbox "$@"
CHROMEWRAPPER
chmod +x /usr/local/bin/chromium-browser

echo "  Created /usr/local/bin/chromium-browser wrapper"

# Create .desktop file for Chromium
cat > /usr/share/applications/chromium-browser.desktop << 'DESKTOPFILE'
[Desktop Entry]
Version=1.0
Name=Chromium Web Browser
GenericName=Web Browser
Comment=Access the Internet
Exec=/usr/local/bin/chromium-browser %U
Terminal=false
Type=Application
Icon=web-browser
Categories=Network;WebBrowser;
MimeType=text/html;text/xml;application/xhtml+xml;x-scheme-handler/http;x-scheme-handler/https;
StartupNotify=true
DESKTOPFILE

echo "  Created chromium-browser.desktop"

# Register as x-www-browser alternative (Debian/Ubuntu standard)
update-alternatives --install /usr/bin/x-www-browser x-www-browser /usr/local/bin/chromium-browser 100
update-alternatives --set x-www-browser /usr/local/bin/chromium-browser

echo "  Registered as x-www-browser alternative"

# Update desktop database and set XDG defaults
update-desktop-database /usr/share/applications/ 2>/dev/null || true

# Set XDG MIME type handlers for http/https URLs
xdg-mime default chromium-browser.desktop x-scheme-handler/http 2>/dev/null || true
xdg-mime default chromium-browser.desktop x-scheme-handler/https 2>/dev/null || true
xdg-mime default chromium-browser.desktop text/html 2>/dev/null || true

echo "  Configured MIME type handlers"

# Create XFCE helper definition for Chromium
# This is required for XFCE's exo-open to recognize Chromium as a browser option
mkdir -p /usr/share/xfce4/helpers
cat > /usr/share/xfce4/helpers/chromium-browser.desktop << 'HELPERFILE'
[Desktop Entry]
Version=1.0
Type=X-XFCE-Helper
Name=Chromium Web Browser
Icon=web-browser
X-XFCE-Binaries=chromium-browser;/usr/local/bin/chromium-browser;
X-XFCE-Category=WebBrowser
X-XFCE-Commands=%B;
X-XFCE-CommandsWithParameter=%B %s;
HELPERFILE

echo "  Created XFCE helper definition"
echo "Chromium configured as default browser"

# =============================================================================
# Create directories
# =============================================================================
echo ""
echo "=== Creating directories ==="

mkdir -p /root/.vnc
mkdir -p /etc/caddy
mkdir -p /var/log/caddy
mkdir -p /var/log/supervisor
mkdir -p /magnitude
mkdir -p /agent-service
mkdir -p /Unity

# =============================================================================
# supervisord configuration
# =============================================================================
echo ""
echo "=== Configuring supervisord ==="

if [[ -f /tmp/supervisord.conf ]]; then
    cp /tmp/supervisord.conf /etc/supervisor/conf.d/unity-vm.conf
    echo "supervisord config installed"
else
    echo "WARNING: supervisord.conf not found"
fi

# =============================================================================
# XFCE Configuration
# =============================================================================
echo ""
echo "=== Configuring XFCE ==="

# Create default XFCE config to suppress first-run dialogs
mkdir -p /root/.config/xfce4/xfconf/xfce-perchannel-xml

# Disable screensaver and power management (important for VNC)
cat > /root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>
    <property name="dpms-on-ac-off" type="uint" value="0"/>
  </property>
</channel>
EOF

# Disable screensaver
cat > /root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-screensaver.xml << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
  </property>
  <property name="lock" type="empty">
    <property name="enabled" type="bool" value="false"/>
  </property>
</channel>
EOF

# Configure XFCE preferred applications (helpers.rc)
cat > /root/.config/xfce4/helpers.rc << 'EOF'
WebBrowser=chromium-browser
TerminalEmulator=xfce4-terminal
EOF

echo "  Configured XFCE preferred applications"

# Configure xfce4-terminal to start in /Unity by default
mkdir -p /root/.config/xfce4/terminal
cat > /root/.config/xfce4/terminal/terminalrc << 'EOF'
[Configuration]
MiscDefaultWorkingDir=/Unity
MiscDefaultWorkingDirSpec=TERMINAL_DEFAULT_WORKING_DIR_CUSTOM
EOF

echo "  Configured terminal default directory: /Unity"

# Create a wrapper script that ensures terminal starts in /Unity
# This is a robust fallback that works regardless of config file state
cat > /usr/local/bin/xfce4-terminal-unity << 'TERMWRAPPER'
#!/bin/bash
# Wrapper to ensure terminal starts in /Unity
exec /usr/bin/xfce4-terminal --default-working-directory=/Unity "$@"
TERMWRAPPER
chmod +x /usr/local/bin/xfce4-terminal-unity

echo "  Created terminal wrapper script"

# Create custom .desktop launcher for Terminal Emulator that starts in /Unity
# Files in ~/.local/share/applications/ override system .desktop files
mkdir -p /root/.local/share/applications
cat > /root/.local/share/applications/xfce4-terminal.desktop << 'EOF'
[Desktop Entry]
Version=1.0
Name=Terminal Emulator
GenericName=Terminal Emulator
Comment=Use the command line
Exec=/usr/local/bin/xfce4-terminal-unity
Icon=org.xfce.terminalemulator
Terminal=false
Type=Application
Categories=System;TerminalEmulator;X-XFCE;
StartupNotify=true
Keywords=shell;prompt;command;commandline;cmd;
EOF

# Also override the system .desktop file to ensure panel launchers use it
cp /root/.local/share/applications/xfce4-terminal.desktop /usr/share/applications/xfce4-terminal.desktop

echo "  Created custom terminal launcher (starts in /Unity)"

echo "XFCE configured"

# =============================================================================
# Environment variables
# =============================================================================
echo ""
echo "=== Setting up environment ==="

# Create system-wide environment file
cat > /etc/profile.d/unity-vm.sh << 'EOF'
export DISPLAY=:1
export VNC_GEOMETRY=1920x1080
export VNC_DEPTH=24
export HOME=/root
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
EOF
chmod +x /etc/profile.d/unity-vm.sh

echo "Environment configured"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=========================================="
echo "  Base installation complete!"
echo "=========================================="
echo ""
echo "Installed:"
echo "  - XFCE4 Desktop"
echo "  - TigerVNC: $(Xvnc -version 2>&1 | head -1 || echo 'installed')"
echo "  - noVNC: /novnc"
echo "  - Node.js: $(node --version)"
echo "  - npm: $(npm --version)"
echo "  - Bun: $($BUN_INSTALL/bin/bun --version 2>/dev/null || echo 'installed')"
echo "  - Caddy: $(caddy version)"
echo "  - Playwright: ${PLAYWRIGHT_VERSION}"
echo "  - Chromium: (default browser)"
echo "  - supervisord: $(supervisord --version)"
echo ""
echo "Configuration:"
echo "  - Default browser: Chromium (Playwright)"
echo "  - Terminal starts in: /Unity"
echo ""

