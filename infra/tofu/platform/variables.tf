variable "kubeconfig_path" {
  description = "Path to the kubeconfig file of the target cluster. The default kube context is never used implicitly."
  type        = string
  nullable    = false

  validation {
    condition     = length(trimspace(var.kubeconfig_path)) > 0
    error_message = "kubeconfig_path must not be empty; an empty value would fall back to an implicit kubeconfig."
  }
}

variable "context" {
  description = "Explicit kubeconfig context to select. Must match the cluster that owns the bootstrap Application."
  type        = string
  nullable    = false

  validation {
    condition     = length(trimspace(var.context)) > 0
    error_message = "context must not be empty; an empty value would fall back to the kubeconfig's current-context."
  }
}

variable "rendered_dir" {
  description = "Rendered output root as produced by `just configure platform` (ANYLINGS_OUTPUT_DIR). The bootstrap Application is read from kubernetes/bootstrap.yaml."
  type        = string
  nullable    = false

  validation {
    condition     = length(trimspace(var.rendered_dir)) > 0
    error_message = "rendered_dir must not be empty; point it at ANYLINGS_OUTPUT_DIR."
  }
}
