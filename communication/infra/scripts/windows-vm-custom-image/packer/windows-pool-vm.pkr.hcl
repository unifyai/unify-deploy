# windows-pool-vm.pkr.hcl - Packer template for Unity Pool Windows VM image
#
# Extends the base Windows VM image with pool-specific components:
# - unityuser (auto-logon Administrator)
# - OpenSSH Server (port 2222)
# - TightVNC Server (dummy password, updated at assignment)
# - Unity Pool Watcher (NSSM Windows service)
#
# Outputs to image family "unity-pool-windows-vm" (separate from legacy "unity-windows-vm")
#
# Usage:
#   packer init .
#   packer build -var "project_id=YOUR_PROJECT" windows-pool-vm.pkr.hcl

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
  description = "Path to GCP service account JSON credentials file."
}

variable "zone" {
  type        = string
  default     = "us-central1-a"
  description = "GCP zone for building the image"
}

variable "machine_type" {
  type        = string
  default     = "e2-standard-4"
  description = "Machine type for the build VM"
}

variable "disk_size" {
  type        = number
  default     = 100
  description = "Boot disk size in GB"
}

variable "image_family" {
  type        = string
  default     = "unity-pool-windows-vm"
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
# Source: GCP Windows Server 2025
# =============================================================================

source "googlecompute" "windows-pool-vm" {
  project_id       = var.project_id
  credentials_file = var.credentials_file != "" ? var.credentials_file : null
  zone             = var.zone
  machine_type     = var.machine_type

  source_image_family     = "windows-2025"
  source_image_project_id = ["windows-cloud"]

  image_name        = "${var.image_family}-{{timestamp}}"
  image_family      = var.image_family
  image_description = "Unity Pool Windows VM with Office, Git, Python, Node.js, noVNC, Caddy, TightVNC, pool watcher."
  image_labels = {
    "managed-by" = "packer"
    "purpose"    = "unity-pool-windows-vm"
  }

  disk_size = var.disk_size
  disk_type = "pd-ssd"

  network    = var.network
  subnetwork = var.subnetwork != "" ? var.subnetwork : null
  tags       = ["allow-winrm"]

  communicator   = "winrm"
  winrm_username = "packer_build"
  winrm_insecure = true
  winrm_use_ssl  = true
  winrm_timeout  = "15m"

  metadata = {
    windows-startup-script-ps1 = <<-EOF
      Set-ExecutionPolicy Bypass -Scope Process -Force
      winrm quickconfig -q
      winrm set winrm/config/service '@{AllowUnencrypted="true"}'
      winrm set winrm/config/service/auth '@{Basic="true"}'
      winrm set winrm/config/winrs '@{MaxMemoryPerShellMB="1024"}'
      netsh advfirewall firewall add rule name="WinRM-HTTPS" dir=in localport=5986 protocol=TCP action=allow
      Restart-Service WinRM
    EOF
  }

  scopes = [
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/devstorage.read_only",
  ]
}

# =============================================================================
# Build
# =============================================================================

build {
  sources = ["source.googlecompute.windows-pool-vm"]

  # Upload the base installation script
  provisioner "file" {
    source      = "scripts/install-base.ps1"
    destination = "C:\\temp\\install-base.ps1"
  }

  # Upload pool overlay script
  provisioner "file" {
    source      = "scripts/install-pool-overlay.ps1"
    destination = "C:\\temp\\install-pool-overlay.ps1"
  }

  # Upload pool watcher script
  provisioner "file" {
    source      = "../../unity-pool-watcher.ps1"
    destination = "C:\\temp\\unity-pool-watcher.ps1"
  }

  # Run the base installation script
  provisioner "powershell" {
    inline = [
      "Set-ExecutionPolicy Bypass -Scope Process -Force",
      "Write-Host 'Starting base image provisioning...'",
      "& C:\\temp\\install-base.ps1",
      "Write-Host 'Base image provisioning complete.'"
    ]
    timeout = "30m"
  }

  # Run the pool overlay
  provisioner "powershell" {
    inline = [
      "Set-ExecutionPolicy Bypass -Scope Process -Force",
      "Write-Host 'Starting pool overlay...'",
      "& C:\\temp\\install-pool-overlay.ps1",
      "Write-Host 'Pool overlay complete.'"
    ]
    timeout = "15m"
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

  provisioner "powershell" {
    inline = [
      "Write-Host 'Pool image build complete!'",
      "Write-Host ''",
      "Write-Host 'Base + Pool overlay:'",
      "Write-Host '  - Office, Git, Python, Node.js, noVNC, Caddy'",
      "Write-Host '  - Pool user: unityuser (auto-logon)'",
      "Write-Host '  - OpenSSH Server (port 2222)'",
      "Write-Host '  - TightVNC Server'",
      "Write-Host '  - Pool watcher: UnityPoolWatcher service'",
    ]
  }
}
