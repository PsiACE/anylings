output "kubeconfig" {
  description = "Issued admin kubeconfig, matching the DOKS root's output contract."
  value       = local.kubeconfig
  sensitive   = true
}

output "kubeconfig_context" {
  description = "Context parsed from the issued kubeconfig, never inferred from a name."
  value       = nonsensitive(yamldecode(local.kubeconfig)["current-context"])
}
