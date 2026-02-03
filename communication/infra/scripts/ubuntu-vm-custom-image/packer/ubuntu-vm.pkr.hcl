# ubuntu-vm.pkr.hcl - Packer template for Unity Ubuntu VM base image
#
# This creates a custom Ubuntu image with pre-installed software:
# - XFCE4 Desktop (full)
# - TigerVNC Server
# - noVNC + websockify
# - Git, Python 3, Node.js v22, Bun
# - Playwright + Chromium
# - Caddy
# - supervisord
#
# NOT included (handled by startup script):
# - VNC password (needs to be set at runtime)
# - Magnitude/Agent Service repos (needs github-token)
# - API keys, hostname config
#
# Usage:
#   packer init .
#   packer build -var "project_id=YOUR_PROJECT" ubuntu-vm.pkr.hcl
#
# With service account credentials:
#   packer build -var "project_id=YOUR_PROJECT" -var "credentials_file=/path/to/sa.json" ubuntu-vm.pkr.hcl

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
  description = "Machine type for the build VM"
}

variable "disk_size" {
  type        = number
  default     = 50
  description = "Boot disk size in GB"
}

variable "image_family" {
  type        = string
  default     = "unity-ubuntu-vm"
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
# Source: Ubuntu 22.04 LTS
# =============================================================================

source "googlecompute" "ubuntu-vm" {
  project_id       = var.project_id
  credentials_file = var.credentials_file != "" ? var.credentials_file : null
  zone             = var.zone
  machine_type     = var.machine_type

  # Base Ubuntu image from GCP
  source_image_family     = "ubuntu-2204-lts"
  source_image_project_id = ["ubuntu-os-cloud"]

  # Output image configuration
  image_name        = "${var.image_family}-{{timestamp}}"
  image_family      = var.image_family
  image_description = "Unity Ubuntu VM with XFCE4, TigerVNC, noVNC, Node.js, Playwright, Caddy. SSH enabled."
  image_labels = {
    "managed-by" = "packer"
    "purpose"    = "unity-ubuntu-vm"
  }

  # Disk configuration
  disk_size = var.disk_size
  disk_type = "pd-ssd"

  # Network configuration
  network    = var.network
  subnetwork = var.subnetwork != "" ? var.subnetwork : null

  # SSH configuration for Packer provisioning
  communicator = "ssh"
  ssh_username = "packer"
  ssh_timeout  = "10m"

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
  sources = ["source.googlecompute.ubuntu-vm"]

  # Upload the base installation script
  provisioner "file" {
    source      = "scripts/install-base.sh"
    destination = "/tmp/install-base.sh"
  }

  # Upload supervisord config
  provisioner "file" {
    source      = "files/supervisord.conf"
    destination = "/tmp/supervisord.conf"
  }

  # Upload noVNC custom HTML
  provisioner "file" {
    source      = "files/novnc-custom.html"
    destination = "/tmp/novnc-custom.html"
  }

  # Run the base installation script
  provisioner "shell" {
    inline = [
      "chmod +x /tmp/install-base.sh",
      "sudo /tmp/install-base.sh"
    ]

    # Increase timeout for installations
    timeout = "30m"
  }

  # Clean up
  provisioner "shell" {
    inline = [
      "echo 'Cleaning up...'",
      "sudo rm -rf /tmp/*.sh /tmp/*.html /tmp/*.conf",
      "sudo apt-get clean",
      "sudo rm -rf /var/lib/apt/lists/*",
      "echo 'Cleanup complete.'"
    ]
  }

  # Summary
  provisioner "shell" {
    inline = [
      "echo ''",
      "echo '=========================================='",
      "echo '  Base image build complete!'",
      "echo '=========================================='",
      "echo ''",
      "echo 'Pre-installed software:'",
      "echo '  - XFCE4 Desktop (full)'",
      "echo '  - TigerVNC Server'",
      "echo '  - noVNC + websockify'",
      "echo '  - Git'",
      "echo '  - Python 3'",
      "echo '  - Node.js v22 + npm'",
      "echo '  - Bun'",
      "echo '  - Playwright + Chromium'",
      "echo '  - Caddy'",
      "echo '  - supervisord'",
      "echo ''",
      "echo 'Use ubuntu-vm-startup.sh at instance creation for:'",
      "echo '  - VNC password, repos, API keys, services'"
    ]
  }
}
