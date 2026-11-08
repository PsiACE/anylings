output "kubeconfig" {
  description = "Kubeconfig for the new cluster. Sensitive; write it with mode 0600 outside Git. The platform root expects a kubeconfig_path plus an explicit context."
  value       = digitalocean_kubernetes_cluster.this.kube_config[0].raw_config
  sensitive   = true
}

output "kubeconfig_context" {
  description = "Actual current-context from the provider-issued kubeconfig, not an inferred cluster name."
  # Only the context name is declassified; the kubeconfig itself stays sensitive.
  value = nonsensitive(yamldecode(digitalocean_kubernetes_cluster.this.kube_config[0].raw_config)["current-context"])
}
