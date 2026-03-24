# Custom Windows VM Image (Packer)

This folder contains Hashicorp Packer configuration for building a custom Windows VM base image with pre-installed software.

## Architecture

| Layer | Tool | What's Included |
|-------|------|-----------------|
| **Base Image** | Packer | Office, Git, Python, Node.js, noVNC, Caddy, Firewall (443 only) |
| **Startup Script** | GCP metadata | TightVNC, repos, user setup, passwords, services |

## Access Methods

| Method | Port | Status |
|--------|------|--------|
| **RDP** | 3389 | ✅ Inherited from GCP Windows (untouched) |
| **HTTPS** | 443 | Caddy reverse proxy (noVNC + Agent API behind it) |

## Prerequisites

1. Install Packer: https://developer.hashicorp.com/packer/install
2. Authenticate with GCP: `gcloud auth application-default login`
3. Enable Compute Engine API in your project

## Build Base Image

```bash
cd custom-win/packer

# Initialize Packer plugins
packer init .

# Build the image (takes ~40 minutes due to Office install)
packer build \
  -var "project_id=YOUR_PROJECT_ID" \
  -var "zone=us-central1-a" \
  windows-vm.pkr.hcl
```

## Create VM from Custom Image

```bash
# Create VM with display device enabled
gcloud compute instances create my-windows-vm \
  --zone=us-central1-a \
  --machine-type=n2-standard-4 \
  --image-family=unity-windows-vm \
  --image-project=YOUR_PROJECT_ID \
  --boot-disk-size=100GB \
  --boot-disk-type=pd-ssd \
  --enable-display-device \
  --metadata-from-file=windows-startup-script-ps1=../communication/infra/scripts/windows-vm-startup.ps1 \
  --metadata=vnc-password=mypassword,hostname=vm.example.com,windows-username=unify,windows-password=SecurePass123,github-token=ghp_xxx,unify-key=xxx,orchestra-url=https://api.unify.ai
```

## Time Comparison

| Approach | VM Ready Time |
|----------|---------------|
| Fresh VM (no custom image) | ~40-45 minutes |
| Custom image + startup script | ~3-5 minutes |

## What's Pre-installed in Base Image

- Microsoft Office LTSC 2024 (Word, Excel, PowerPoint) - not activated
- Git for Windows
- Python 3.12
- Chocolatey
- Node.js v22 + npm
- Bun
- noVNC + websockify
- Caddy (binary only, no config)
- Firewall rules (port 443 only — 6080/3000 behind Caddy)

## What Startup Script Configures

- TightVNC Server (with password)
- Magnitude & Agent Service repos (with github-token)
- Windows user + auto-logon
- Agent Service .env (API keys)
- Caddyfile (hostname)
- Display resolution
- Start all services
- Office activation (if MAK key provided)

## RDP Access

RDP is always available on GCP Windows VMs:

```bash
# Using gcloud
gcloud compute rdp my-windows-vm --zone=us-central1-a

# Using mstsc (Windows)
mstsc /v:EXTERNAL_IP

# Using Remmina/FreeRDP (Linux/Mac)
xfreerdp /v:EXTERNAL_IP /u:USERNAME
```

## File Structure

```
custom-win/
├── README.md                    # This file
└── packer/
    ├── windows-vm.pkr.hcl       # Packer template
    └── scripts/
        └── install-base.ps1     # Base image provisioning script
```

The startup script remains at: `communication/infra/scripts/windows-vm-startup.ps1`
