# OpenTofu orchestration follows k0sproject/k0s/hack/ostests/modules/k0sctl:
# terraform_data invokes k0sctl; external reads the kubeconfig. Unlike the OS
# tests' disposable cloud hosts, existing hosts need an explicit reset lifecycle.
locals {
  config      = yamldecode(file(var.config_path))
  config_yaml = sensitive(yamlencode(local.config))
}

# Stable lifecycle owner. Configuration updates are in-place; only explicitly
# destroying/replacing this owner resets hosts. Keep this block and its state
# until the cluster has actually been removed.
resource "terraform_data" "cluster" {
  input = {
    config     = local.config_yaml
    executable = "k0sctl"
  }
  triggers_replace = [local.config.metadata.name]

  lifecycle {
    prevent_destroy = true
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-euo", "pipefail", "-c"]
    command     = "printf '%s' \"$K0SCTL_CONFIG\" | \"$K0SCTL_EXECUTABLE\" reset --force --config -"
    environment = {
      K0SCTL_CONFIG     = self.input.config
      K0SCTL_EXECUTABLE = self.input.executable
    }
  }
}

# Replacing an apply operation must NOT reset a running cluster. k0sctl handles
# the desired configuration/version update; destruction belongs only above.
resource "terraform_data" "apply" {
  triggers_replace = [sha256(local.config_yaml)]

  provisioner "local-exec" {
    interpreter = ["bash", "-euo", "pipefail", "-c"]
    command     = "printf '%s' \"$K0SCTL_CONFIG\" | k0sctl apply --config -"
    environment = {
      K0SCTL_CONFIG = local.config_yaml
    }
  }

  depends_on = [terraform_data.cluster]
}

data "external" "kubeconfig" {
  program = [
    "bash", "-euo", "pipefail", "-c",
    "jq -r '.config' | k0sctl kubeconfig --config - | jq -Rs '{kubeconfig: .}'"
  ]
  query = {
    config = local.config_yaml
  }

  depends_on = [terraform_data.apply]
}

locals {
  kubeconfig = sensitive(data.external.kubeconfig.result.kubeconfig)
}
