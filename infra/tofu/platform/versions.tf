terraform {
  required_version = ">= 1.13.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "3.5.0"
    }
  }

  # The rehearsal uses a private working copy with an explicit local backend.
  # TF_DATA_DIR isolates plugins, not state. Real deployments need a private,
  # encrypted and backed-up backend; see ../README.md.
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.context
}
