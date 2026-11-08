# One Autopilot-style bootstrap Application is the infrastructure/GitOps handoff.
# Argo owns its self-management Application, the projects root and ApplicationSet.
# Bootstrap has already installed Argo/CRDs and seeded Git access and the age key.
locals {
  bootstrap = yamldecode(file("${var.rendered_dir}/kubernetes/bootstrap.yaml"))
}

resource "kubernetes_manifest" "bootstrap" {
  manifest = local.bootstrap

  field_manager {
    name            = "anylings-platform"
    force_conflicts = false
  }

  # This confirms the bootstrap resources, NOT generated workload readiness.
  # Child Applications reconcile asynchronously; CI checks their real health.
  wait {
    fields = {
      "status.sync.status"   = "^Synced$"
      "status.health.status" = "^Healthy$"
    }
  }

  timeouts {
    create = "10m"
    update = "10m"
  }
}
