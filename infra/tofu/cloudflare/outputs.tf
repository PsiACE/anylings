output "tunnel_token" {
  description = "Token for the remotely managed cloudflared tunnel. Sensitive; never commit it."
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.this.token
  sensitive   = true
}

output "tunnel_id" {
  description = "cloudflared tunnel UUID."
  value       = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

output "r2_bucket_names" {
  description = "Created R2 bucket names."
  value       = sort([for bucket in cloudflare_r2_bucket.this : bucket.name])
}
