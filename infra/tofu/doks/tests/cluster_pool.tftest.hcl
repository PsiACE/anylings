# Offline contract for the single worker pool. Mocked provider: no token, no
# DigitalOcean API call and no state. The resource address stays fixed so an
# existing tracked cluster is never replaced by this change.
mock_provider "digitalocean" {
  mock_resource "digitalocean_kubernetes_cluster" {
    defaults = {
      kube_config = [{
        client_certificate     = "synthetic"
        client_key             = "synthetic"
        cluster_ca_certificate = "synthetic"
        expires_at             = "2030-01-01T00:00:00Z"
        host                   = "https://synthetic"
        raw_config             = "apiVersion: v1\nkind: Config\ncurrent-context: synthetic\n"
        token                  = "synthetic"
      }]
    }
  }
}

run "baseline_defaults" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.node_pool[0].size == "s-2vcpu-4gb"
    error_message = "The baseline worker size must stay s-2vcpu-4gb."
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.node_pool[0].node_count == 2
    error_message = "The baseline worker count must stay two."
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.region == "sgp1"
    error_message = "The default region must stay sgp1."
  }

  # DOKS 1.36+ enables HA when `ha` is omitted, which adds $40/month. These
  # stay explicit, so a provider default change cannot silently alter the plan.
  assert {
    condition     = digitalocean_kubernetes_cluster.this.ha == false
    error_message = "The control plane must remain explicit non-HA."
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.auto_upgrade == false
    error_message = "Automatic Kubernetes upgrade must stay off."
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.node_pool[0].auto_scale == false
    error_message = "Node autoscaling must stay off; elastic capacity is separate work."
  }
}

run "single_node_economy_profile" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_count         = 1
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.node_pool[0].node_count == 1
    error_message = "The economy profile is one worker."
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.node_pool[0].size == "s-2vcpu-4gb"
    error_message = "The economy profile keeps the baseline size."
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.ha == false
    error_message = "The economy profile remains non-HA."
  }
}

run "explicit_size_change" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_size          = "s-2vcpu-2gb"
  }

  assert {
    condition     = digitalocean_kubernetes_cluster.this.node_pool[0].size == "s-2vcpu-2gb"
    error_message = "An explicit size slug must reach the pool unchanged."
  }
}

run "reject_zero_count" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_count         = 0
  }

  expect_failures = [var.node_count]
}

run "reject_negative_count" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_count         = -1
  }

  expect_failures = [var.node_count]
}

run "reject_fractional_count" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_count         = 1.5
  }

  expect_failures = [var.node_count]
}

run "reject_empty_size" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_size          = ""
  }

  expect_failures = [var.node_size]
}

run "reject_blank_size" {
  command = plan

  variables {
    kubernetes_version = "1.36.3-do.0"
    node_size          = "s 2vcpu 4gb"
  }

  expect_failures = [var.node_size]
}
