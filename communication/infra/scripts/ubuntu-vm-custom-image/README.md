# Ubuntu VM Custom Image (Packer)

Creates a custom GCP Ubuntu image with pre-installed software for Unity Ubuntu VMs.

This is equivalent to the Windows VM approach using `windows-vm-custom-image/`.

## What's Pre-installed (Packer Image)

- XFCE4 Desktop (full)
- TigerVNC Server
- noVNC + websockify
- Node.js 22 + npm
- Bun
- Playwright + Chromium
- Caddy
- supervisord
- Git, Python 3

## What's Configured at Runtime (Startup Script)

- VNC password
- Magnitude repository
- Agent Service repository
- API keys (Unify)
- Caddy hostname/HTTPS

## Building the Image

### Prerequisites

1. Install Packer: https://developer.hashicorp.com/packer/downloads
2. Authenticate with GCP:
   ```bash
   gcloud auth application-default login
   ```

### Build Commands

```bash
cd packer

# Initialize Packer plugins
packer init .

# Build the image
packer build -var "project_id=YOUR_PROJECT_ID" ubuntu-vm.pkr.hcl

# With custom zone
packer build \
  -var "project_id=YOUR_PROJECT_ID" \
  -var "zone=us-west1-a" \
  ubuntu-vm.pkr.hcl

# With service account
packer build \
  -var "project_id=YOUR_PROJECT_ID" \
  -var "credentials_file=/path/to/sa.json" \
  ubuntu-vm.pkr.hcl
```

### Build Output

The image is created in your project with:
- **Image family**: `unity-ubuntu-vm`
- **Image name**: `unity-ubuntu-vm-{timestamp}`

## Creating a VM from the Image

### Using gcloud CLI

```bash
# Reserve static IP (optional)
gcloud compute addresses create ubuntu-vm-ip \
  --region=us-central1 \
  --network-tier=PREMIUM

# Create VM
gcloud compute instances create ubuntu-vm-test \
  --project=YOUR_PROJECT_ID \
  --zone=us-central1-a \
  --machine-type=e2-standard-2 \
  --image-family=unity-ubuntu-vm \
  --image-project=YOUR_PROJECT_ID \
  --address=ubuntu-vm-ip \
  --tags=http-server,https-server \
  --metadata-from-file=startup-script=../ubuntu-vm-startup.sh \
  --metadata=^::^vnc-password=YOUR_VNC_PASSWORD::hostname=vm.example.com::github-token=ghp_xxx::unify-key=xxx
```

### Using Python API

```python
from google.cloud import compute_v1

# Create disk from image family
disk = compute_v1.AttachedDisk()
disk.initialize_params = compute_v1.AttachedDiskInitializeParams()
disk.initialize_params.source_image = f"projects/{project_id}/global/images/family/unity-ubuntu-vm"
disk.initialize_params.disk_size_gb = 50
disk.initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-ssd"
disk.boot = True
disk.auto_delete = True

# Set metadata
metadata = compute_v1.Metadata()
metadata.items = [
    {"key": "startup-script", "value": open("ubuntu-vm-startup.sh").read()},
    {"key": "vnc-password", "value": "xxx"},
    {"key": "hostname", "value": "vm.example.com"},
    {"key": "github-token", "value": "ghp_xxx"},
    {"key": "unify-key", "value": "xxx"},
    {"key": "orchestra-url", "value": "https://api.unify.ai"},
]
```

## GCP Metadata Keys

| Key | Description | Default |
|-----|-------------|---------|
| `vnc-password` | VNC access password | `unify123` |
| `hostname` | DNS hostname for Caddy HTTPS | (none - no HTTPS) |
| `github-token` | GitHub PAT for private repos | (none) |
| `unify-key` | Unify API key | (none) |
| `orchestra-url` | Orchestra API base URL | (none) |
| `staging` | Use staging branch | (none = main) |

## Ports

| Port | Service | Description |
|------|---------|-------------|
| 80 | Caddy | HTTP (redirects to HTTPS) |
| 443 | Caddy | HTTPS |
| 3000 | Agent Service | API |
| 5901 | TigerVNC | Direct VNC (display :1) |
| 6080 | websockify | noVNC web client |

## URL Routes (via Caddy)

| URL | Service |
|-----|---------|
| `https://hostname/desktop/` | noVNC (web desktop) |
| `https://hostname/api/` | Agent Service API |

## Directory Structure

```
ubuntu-vm-custom-image/
├── packer/
│   ├── ubuntu-vm.pkr.hcl          # Packer template
│   ├── scripts/
│   │   └── install-base.sh        # Pre-install script
│   └── files/
│       ├── supervisord.conf       # Process manager config
│       └── novnc-custom.html      # Custom noVNC wrapper
└── README.md

../ubuntu-vm-startup.sh             # Runtime startup script
```

## Firewall Rules

Ensure these firewall rules exist in your project:

```bash
# Allow HTTPS (Caddy reverse proxy — the only public-facing port)
gcloud compute firewall-rules create allow-https --allow=tcp:443 --target-tags=https-server

# Allow SFTP file sync
gcloud compute firewall-rules create allow-2222 --allow=tcp:2222 --target-tags=allow-2222
```

Ports 6080 (noVNC) and 3000 (Agent Service) are **not** exposed directly — they are only reachable via Caddy's reverse proxy on port 443. Port 80 is not needed since VMs use a pre-provisioned wildcard TLS cert.

## Logs

```bash
# SSH into VM
gcloud compute ssh ubuntu-vm-test --zone=us-central1-a

# View startup script output
sudo journalctl -u google-startup-scripts.service

# View service logs
sudo tail -f /var/log/supervisor/supervisord.log
sudo tail -f /var/log/supervisor/xvnc.log
sudo tail -f /var/log/supervisor/agent-service.log
sudo tail -f /var/log/caddy/access.log
```

## Comparison: Windows vs Ubuntu

| Feature | Windows | Ubuntu |
|---------|---------|--------|
| Base Image | `windows-2025` | `ubuntu-2204-lts` |
| Image Family | `unity-windows-vm` | `unity-ubuntu-vm` |
| Startup Script Key | `windows-startup-script-ps1` | `startup-script` |
| Desktop | Windows Explorer | XFCE4 |
| VNC Server | TightVNC | TigerVNC |
| Communicator | WinRM | SSH |
| Build Time | ~60 min (Office) | ~15 min |
| Image Size | ~15GB | ~5GB |
