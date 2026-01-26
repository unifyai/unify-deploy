# Linux VM Docker Image

Docker image for Linux VMs with a graphical desktop accessible via web browser.

Equivalent to the Windows VM setup script (`windows-vm-startup.ps1`).

## Features

- **XFCE4 Desktop** - Full-featured Linux desktop environment
- **TigerVNC** - VNC server (port 5900)
- **noVNC** - Web-based VNC client (port 6080)
- **Caddy** - HTTPS reverse proxy with automatic TLS
- **Magnitude** - Browser automation framework
- **Agent Service** - API service (port 3000)
- **Playwright + Chromium** - Browser automation

## Quick Start

### Build the Image

```bash
docker build -t linux-vm:latest .
```

### Run Locally

```bash
# Basic run (no HTTPS)
docker run -d \
  --name linux-vm \
  -p 6080:6080 \
  -p 3000:3000 \
  -e VNC_PASSWORD=mypassword \
  linux-vm:latest

# Access at: http://localhost:6080
```

### Run with Full Configuration

```bash
docker run -d \
  --name linux-vm \
  -p 80:80 \
  -p 443:443 \
  -p 3000:3000 \
  -p 6080:6080 \
  -e VNC_PASSWORD=mypassword \
  -e HOSTNAME=vm.example.com \
  -e GITHUB_TOKEN=ghp_xxx \
  -e ANTHROPIC_API_KEY=sk-ant-xxx \
  -e UNIFY_KEY=xxx \
  -e UNIFY_BASE_URL=https://api.unify.ai \
  linux-vm:latest

# Access at: https://vm.example.com/desktop/
```

## GCP Deployment

### Option 1: Container-Optimized OS (Recommended)

```bash
# Build and push to Container Registry
docker build -t gcr.io/PROJECT_ID/linux-vm:latest .
docker push gcr.io/PROJECT_ID/linux-vm:latest

# Create VM with container
gcloud compute instances create-with-container linux-vm-1 \
  --zone=us-central1-a \
  --machine-type=e2-standard-2 \
  --container-image=gcr.io/PROJECT_ID/linux-vm:latest \
  --container-privileged \
  --tags=http-server,https-server \
  --metadata=\
vnc-password=secretpass,\
hostname=vm.example.com,\
github-token=ghp_xxx,\
anthropic-api-key=sk-ant-xxx,\
unify-key=xxx
```

### Option 2: Standard Ubuntu VM

```bash
gcloud compute instances create linux-vm-1 \
  --zone=us-central1-a \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --tags=http-server,https-server \
  --metadata-from-file=startup-script=startup-gce.sh \
  --metadata=\
vnc-password=secretpass,\
hostname=vm.example.com
```

## Configuration

### GCP Metadata Keys

| Key | Description | Default |
|-----|-------------|---------|
| `vnc-password` | VNC access password | `unify123` |
| `hostname` | DNS hostname for HTTPS | (none - no HTTPS) |
| `github-token` | GitHub PAT for private repos | (none) |
| `anthropic-api-key` | Anthropic API key | (none) |
| `unify-key` | Unify API key | (none) |
| `unify-base-url` | Unify API base URL | (none) |
| `staging` | Use staging branch | (none = main) |

### Environment Variables

All metadata keys can be set as environment variables instead (uppercase with underscores):

```bash
VNC_PASSWORD=xxx
HOSTNAME=vm.example.com
GITHUB_TOKEN=xxx
ANTHROPIC_API_KEY=xxx
UNIFY_KEY=xxx
UNIFY_BASE_URL=xxx
STAGING=true
```

## Ports

| Port | Service | Description |
|------|---------|-------------|
| 80 | Caddy | HTTP (redirects to HTTPS) |
| 443 | Caddy | HTTPS |
| 3000 | Agent Service | API |
| 5900 | TigerVNC | Direct VNC (optional) |
| 6080 | websockify | noVNC web client |

## URL Routes (via Caddy)

| URL | Service |
|-----|---------|
| `https://hostname/desktop/` | noVNC (web desktop) |
| `https://hostname/api/` | Agent Service API |

## Directory Structure

```
/
├── novnc/              # noVNC web client
├── magnitude/          # Magnitude framework
├── agent-service/      # Agent Service API
│   ├── .env           # Runtime configuration
│   └── src/           # Source code
├── etc/
│   ├── caddy/
│   │   └── Caddyfile  # Generated at runtime
│   └── supervisor/
│       └── conf.d/
│           └── supervisord.conf
└── var/log/
    ├── caddy/         # Caddy logs
    └── supervisor/    # Service logs
```

## Logs

```bash
# View all logs
docker logs linux-vm

# Individual service logs (inside container)
docker exec linux-vm tail -f /var/log/supervisor/xvnc.log
docker exec linux-vm tail -f /var/log/supervisor/agent-service.log
docker exec linux-vm tail -f /var/log/caddy/access.log
```

## Comparison: Windows vs Linux

| Feature | Windows | Linux |
|---------|---------|-------|
| Desktop | Windows Explorer | XFCE4 |
| VNC Server | TightVNC | TigerVNC |
| Process Manager | Services + Tasks | supervisord |
| Startup Script Key | `windows-startup-script-ps1` | `startup-script` |
| Image Size | ~15GB+ | ~3-4GB |
| Boot Time (fast) | ~15-20s | ~10-15s |

## Troubleshooting

### VNC Connection Failed

```bash
# Check if VNC is running
docker exec linux-vm pgrep Xvnc

# Check VNC logs
docker exec linux-vm cat /var/log/supervisor/xvnc.err
```

### Desktop Not Loading

```bash
# Check XFCE status
docker exec linux-vm pgrep xfce4-session

# Check XFCE logs
docker exec linux-vm cat /var/log/supervisor/xfce.err
```

### Agent Service Not Starting

```bash
# Check if service is running
docker exec linux-vm pgrep -f "ts-node"

# Check logs
docker exec linux-vm cat /var/log/supervisor/agent-service.err
```

### HTTPS Not Working

```bash
# Check Caddy logs
docker exec linux-vm cat /var/log/supervisor/caddy.err

# Verify Caddyfile
docker exec linux-vm cat /etc/caddy/Caddyfile
```

