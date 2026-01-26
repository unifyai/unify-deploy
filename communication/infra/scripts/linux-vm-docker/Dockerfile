# =============================================================================
# Linux VM Docker Image
# Provides: XFCE4 desktop + TigerVNC + noVNC + Magnitude + Agent Service
# Equivalent to: windows-vm-startup.ps1
# =============================================================================

FROM ubuntu:22.04

# Build arguments
ARG NODEJS_VERSION=22
ARG PLAYWRIGHT_VERSION=1.52.0
ARG CADDY_VERSION=2.7.6

# Environment
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV TZ=UTC
ENV DISPLAY=:1
ENV VNC_GEOMETRY=1920x1080
ENV VNC_DEPTH=24
ENV HOME=/root

# =============================================================================
# Stage 1: System packages + XFCE4 Desktop
# =============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Locale & basics
    locales \
    tzdata \
    ca-certificates \
    curl \
    wget \
    git \
    gnupg \
    # XFCE4 full desktop
    xfce4 \
    xfce4-goodies \
    xfce4-terminal \
    # VNC server
    tigervnc-standalone-server \
    tigervnc-common \
    # Display utilities
    dbus-x11 \
    x11-utils \
    x11-xserver-utils \
    # Python for websockify
    python3 \
    python3-pip \
    python3-venv \
    # Process manager
    supervisor \
    # Fonts (for proper rendering)
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-color-emoji \
    # Utilities
    sudo \
    vim \
    htop \
    procps \
    net-tools \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Configure locale
RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen

# =============================================================================
# Stage 2: noVNC + websockify
# =============================================================================
RUN git clone --depth 1 https://github.com/novnc/noVNC.git /novnc \
    && rm -rf /novnc/.git \
    && pip3 install --no-cache-dir websockify

# Custom noVNC wrapper (hide controls, clean UI)
COPY config/novnc-custom.html /novnc/index.html
COPY config/novnc-custom.html /novnc/custom.html

# =============================================================================
# Stage 3: Node.js 22 + Bun
# =============================================================================
RUN curl -fsSL https://deb.nodesource.com/setup_${NODEJS_VERSION}.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g npm@latest \
    && rm -rf /var/lib/apt/lists/*

# Install Bun
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

# =============================================================================
# Stage 4: Caddy
# =============================================================================
RUN curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
    | tar -xzC /usr/local/bin caddy \
    && chmod +x /usr/local/bin/caddy

# =============================================================================
# Stage 5: Playwright + Chromium
# =============================================================================
# Install Playwright with all system dependencies for Chromium
RUN npx playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium

# =============================================================================
# Configuration & Directories
# =============================================================================

# Create required directories
RUN mkdir -p \
    /root/.vnc \
    /etc/caddy \
    /var/log/caddy \
    /var/log/supervisor

# Copy configuration files
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY config/Caddyfile.template /etc/caddy/Caddyfile.template
COPY startup.sh /startup.sh

# Make startup script executable
RUN chmod +x /startup.sh

# =============================================================================
# XFCE Configuration (optional customization)
# =============================================================================

# Create default XFCE config to suppress first-run dialogs
RUN mkdir -p /root/.config/xfce4/xfconf/xfce-perchannel-xml

# Disable screensaver and power management (important for VNC)
RUN mkdir -p /root/.config/xfce4/xfconf/xfce-perchannel-xml && \
    echo '<?xml version="1.0" encoding="UTF-8"?>\n\
<channel name="xfce4-power-manager" version="1.0">\n\
  <property name="xfce4-power-manager" type="empty">\n\
    <property name="dpms-enabled" type="bool" value="false"/>\n\
    <property name="blank-on-ac" type="int" value="0"/>\n\
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>\n\
    <property name="dpms-on-ac-off" type="uint" value="0"/>\n\
  </property>\n\
</channel>' > /root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml

# =============================================================================
# Expose ports
# =============================================================================
# 80   - HTTP (Caddy)
# 443  - HTTPS (Caddy)
# 3000 - Agent Service
# 5900 - VNC (direct)
# 6080 - noVNC (websockify)
EXPOSE 80 443 3000 5900 6080

# =============================================================================
# Entrypoint
# =============================================================================
CMD ["/bin/bash", "/startup.sh"]

