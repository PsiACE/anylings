output "bootstrap_application" {
  description = "The sole Application owned by OpenTofu; children are Argo-managed."
  value = {
    name      = kubernetes_manifest.bootstrap.manifest.metadata.name
    namespace = kubernetes_manifest.bootstrap.manifest.metadata.namespace
    path      = kubernetes_manifest.bootstrap.manifest.spec.source.path
  }
}

output "argo_namespace" {
  description = "Argo CD namespace derived from the bootstrap Application."
  value       = local.bootstrap.metadata.namespace
}
