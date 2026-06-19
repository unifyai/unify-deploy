# ubuntu-pool-vm.pkr.hcl - Packer template for Droid Pool Ubuntu VM image
#
# Extends the base Ubuntu VM image with pool-specific components:
# - unityuser (SFTP file sync on port 2222)
# - Droid Pool Watcher (systemd service)
#
# Outputs to image family "droid-pool-ubuntu-vm" (separate from legacy "droid-ubuntu-vm")
#
# Usage:
#   packer init .
#   packer build -var "project_id=YOUR_PROJECT" ubuntu-pool-vm.pkr.hcl

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
  default     = 50
  description = "Boot disk size in GB"
}

variable "image_family" {
  type        = string
  default     = "droid-pool-ubuntu-vm"
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

source "googlecompute" "ubuntu-pool-vm" {
  project_id       = var.project_id
  credentials_file = var.credentials_file != "" ? var.credentials_file : null
  zone             = var.zone
  machine_type     = var.machine_type

  source_image_family     = "ubuntu-2204-lts"
  source_image_project_id = ["ubuntu-os-cloud"]

  image_name        = "${var.image_family}-{{timestamp}}"
  image_family      = var.image_family
  image_description = "Droid Pool Ubuntu VM with XFCE4, TigerVNC, noVNC, Node.js, Playwright, Caddy, pool watcher."
  image_labels = {
    "managed-by" = "packer"
    "purpose"    = "droid-pool-ubuntu-vm"
  }

  disk_size = var.disk_size
  disk_type = "pd-ssd"

  network    = var.network
  subnetwork = var.subnetwork != "" ? var.subnetwork : null

  communicator = "ssh"
  ssh_username = "packer"
  ssh_timeout  = "10m"

  scopes = [
    "https://www.googleapis.com/auth/compute",
    "https://www.googleapis.com/auth/devstorage.read_only",
  ]
}

# =============================================================================
# Build
# =============================================================================

build {
  sources = ["source.googlecompute.ubuntu-pool-vm"]

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

  # Upload pool overlay script
  provisioner "file" {
    source      = "scripts/install-pool-overlay.sh"
    destination = "/tmp/install-pool-overlay.sh"
  }

  # Upload pool watcher script
  provisioner "file" {
    source      = "../../droid-pool-watcher.sh"
    destination = "/tmp/droid-pool-watcher.sh"
  }

  # Run the base installation script
  provisioner "shell" {
    inline = [
      "chmod +x /tmp/install-base.sh",
      "sudo /tmp/install-base.sh"
    ]
    timeout = "30m"
  }

  # Run the pool overlay
  provisioner "shell" {
    inline = [
      "chmod +x /tmp/install-pool-overlay.sh",
      "sudo /tmp/install-pool-overlay.sh"
    ]
    timeout = "10m"
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

  provisioner "shell" {
    inline = [
      "echo ''",
      "echo '=========================================='",
      "echo '  Pool image build complete!'",
      "echo '=========================================='",
      "echo ''",
      "echo 'Base + Pool overlay:'",
      "echo '  - XFCE4, TigerVNC, noVNC, Node.js, Caddy, supervisord'",
      "echo '  - Pool user: unityuser (SFTP on port 2222)'",
      "echo '  - Pool watcher: droid-pool-watcher.service'",
    ]
  }
}
