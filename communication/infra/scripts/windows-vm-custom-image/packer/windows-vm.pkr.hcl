# windows-vm.pkr.hcl - Packer template for Unity Windows VM base image
#
# This creates a custom Windows image with pre-installed software:
# - Office LTSC 2024 (Word, Excel, PowerPoint)
# - Git, Python 3.12, Node.js v22, Bun
# - noVNC + websockify, Caddy
# - Firewall rules
#
# NOT included (handled by startup script):
# - TightVNC (needs password at install)
# - Magnitude/Agent Service repos (needs github-token)
# - Windows user setup, API keys, hostname config
#
# RDP: Preserved from GCP Windows image (untouched)
#
# Usage:
#   packer init .
#   packer build -var "project_id=YOUR_PROJECT" windows-vm.pkr.hcl
#
# With service account credentials:
#   packer build -var "project_id=YOUR_PROJECT" -var "credentials_file=/path/to/sa.json" windows-vm.pkr.hcl
#
# Required GCP firewall rule for WinRM (create once):
#   gcloud compute firewall-rules create allow-winrm \
#     --allow tcp:5986 --target-tags allow-winrm --project YOUR_PROJECT

packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = "~> 1.1"
    }
  }
}

# =============================================================================
# Variables
# =============================================================================

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "credentials_file" {
  type        = string
  default     = ""
  description = "Path to GCP service account JSON credentials file. Leave empty to use application default credentials."
}

variable "zone" {
  type        = string
  default     = "us-central1-a"
  description = "GCP zone for building the image"
}

variable "machine_type" {
  type        = string
  default     = "e2-standard-4"
  description = "Machine type for the build VM (needs decent CPU for Office install)"
}

variable "disk_size" {
  type        = number
  default     = 100
  description = "Boot disk size in GB"
}

variable "image_family" {
  type        = string
  default     = "unity-windows-vm"
  description = "Image family name for the output image"
}

variable "network" {
  type        = string
  default     = "default"
  description = "VPC network to use"
}

variable "subnetwork" {
  type        = string
  default     = ""
  description = "Subnetwork to use (leave empty for auto)"
}

# =============================================================================
# Source: GCP Windows Server 2022
# =============================================================================

source "googlecompute" "windows-vm" {
  project_id       = var.project_id
  credentials_file = var.credentials_file != "" ? var.credentials_file : null
  zone             = var.zone
  machine_type     = var.machine_type

  # Base Windows image from GCP
  source_image_family     = "windows-2025"
  source_image_project_id = ["windows-cloud"]

  # Output image configuration
  image_name        = "${var.image_family}-{{timestamp}}"
  image_family      = var.image_family
  image_description = "Unity Windows VM with Office, Git, Python, Node.js, noVNC, Caddy. RDP enabled."
  image_labels = {
    "managed-by" = "packer"
    "purpose"    = "unity-windows-vm"
  }

  # Disk configuration
  disk_size = var.disk_size
  disk_type = "pd-ssd"

  # Network configuration
  network    = var.network
  subnetwork = var.subnetwork != "" ? var.subnetwork : null
  tags       = ["allow-winrm"]

  # Windows Remote Management (WinRM) for Packer provisioning
  communicator   = "winrm"
  winrm_username = "packer_build"
  winrm_insecure = true
  winrm_use_ssl  = true
  winrm_timeout  = "15m"

  # Metadata to bootstrap WinRM access for Packer
  # This runs before Packer connects
  metadata = {
    windows-startup-script-ps1 = <<-EOF
      # Enable WinRM for Packer provisioning
      Set-ExecutionPolicy Bypass -Scope Process -Force
      
      # Configure WinRM
      winrm quickconfig -q
      winrm set winrm/config/service '@{AllowUnencrypted="true"}'
      winrm set winrm/config/service/auth '@{Basic="true"}'
      winrm set winrm/config/winrs '@{MaxMemoryPerShellMB="1024"}'
      
      # Allow WinRM through firewall
      netsh advfirewall firewall add rule name="WinRM-HTTPS" dir=in localport=5986 protocol=TCP action=allow
      
      # Restart WinRM service
      Restart-Service WinRM
    EOF
  }

  # Service account (optional - uses default if not specified)
  # service_account_email = "packer@YOUR_PROJECT.iam.gserviceaccount.com"
  
  # Scopes needed for the build VM
  scopes = [
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/devstorage.read_only",
  ]
}

# =============================================================================
# Build
# =============================================================================

build {
  sources = ["source.googlecompute.windows-vm"]

  # Upload the base installation script
  provisioner "file" {
    source      = "scripts/install-base.ps1"
    destination = "C:\\temp\\install-base.ps1"
  }

  # Run the base installation script
  # This installs Office, Git, Python, Node.js, noVNC, Caddy
  provisioner "powershell" {
    inline = [
      "Set-ExecutionPolicy Bypass -Scope Process -Force",
      "Write-Host 'Starting base image provisioning...'",
      "& C:\\temp\\install-base.ps1",
      "Write-Host 'Base image provisioning complete.'"
    ]
    
    # Increase timeout for Office installation (~40 min)
    timeout = "30m"
  }

  # Clean up temporary files
  provisioner "powershell" {
    inline = [
      "Write-Host 'Cleaning up...'",
      "Remove-Item -Recurse -Force C:\\temp -ErrorAction SilentlyContinue",
      "Remove-Item -Recurse -Force C:\\odt -ErrorAction SilentlyContinue",
      "Write-Host 'Cleanup complete.'"
    ]
  }

  # Clear the startup script metadata (so it doesn't run on instances)
  # The actual startup script will be provided at instance creation time
  provisioner "powershell" {
    inline = [
      "Write-Host 'Base image build complete!'",
      "Write-Host ''",
      "Write-Host 'Pre-installed software:'",
      "Write-Host '  - Office LTSC 2024 (Word, Excel, PowerPoint)'",
      "Write-Host '  - Git for Windows'", 
      "Write-Host '  - Python 3.12'",
      "Write-Host '  - Node.js v22 + npm + Bun'",
      "Write-Host '  - noVNC + websockify'",
      "Write-Host '  - Caddy'",
      "Write-Host ''",
      "Write-Host 'RDP: Enabled (inherited from GCP Windows)'",
      "Write-Host ''",
      "Write-Host 'Use windows-vm-startup.ps1 at instance creation for:'",
      "Write-Host '  - TightVNC, repos, user setup, services'"
    ]
  }
}

