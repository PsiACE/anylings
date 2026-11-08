# A small, fixed, non-HA cluster. The official provider owns API lifecycle;
# DigitalOcean supplies CNI, DNS and CSI. No Kubernetes objects belong here.
resource "digitalocean_kubernetes_cluster" "this" {
  name         = var.cluster_name
  region       = var.region
  version      = var.kubernetes_version
  ha           = false
  auto_upgrade = false

  node_pool {
    name       = "${var.cluster_name}-default"
    size       = "s-2vcpu-4gb"
    node_count = 2
    auto_scale = false
  }

  lifecycle {
    prevent_destroy = true

  }
}
