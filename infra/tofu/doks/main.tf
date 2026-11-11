# A small, fixed, non-HA cluster. The official provider owns API lifecycle;
# DigitalOcean supplies CNI, DNS and CSI. No Kubernetes objects belong here.
# The single pool is deliberate: no autoscaling controller, no second pool.
resource "digitalocean_kubernetes_cluster" "this" {
  name    = var.cluster_name
  region  = var.region
  version = var.kubernetes_version

  # Both stay explicit: DOKS 1.36+ enables HA when `ha` is omitted, and an
  # implicit upgrade would replace workers without review.
  ha           = false
  auto_upgrade = false

  node_pool {
    name       = "${var.cluster_name}-default"
    size       = var.node_size
    node_count = var.node_count
    auto_scale = false
  }

  lifecycle {
    prevent_destroy = true
  }
}
