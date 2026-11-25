terraform {
  backend "local" {}

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.29.0"
    }
  }
}

# The provider reads CLOUDFLARE_API_TOKEN from the environment.
provider "cloudflare" {}

resource "cloudflare_zero_trust_access_policy" "admin" {
  account_id = var.account_id
  name       = "Allow anylings admin"
  decision   = "allow"
  include = [{
    email = { email = var.admin_email }
  }]
}

resource "cloudflare_zero_trust_access_application" "anylings" {
  account_id = var.account_id
  name       = "anylings"
  type       = "self_hosted"
  domain     = var.hostname
  destinations = [{
    type = "public"
    uri  = var.hostname
  }]
  policies = [{
    id         = cloudflare_zero_trust_access_policy.admin.id
    precedence = 1
  }]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "anylings" {
  account_id = var.account_id
  name       = "anylings"
  config_src = "cloudflare"
}

locals {
  protected_origin = {
    access = {
      required  = true
      aud_tag   = [cloudflare_zero_trust_access_application.anylings.aud]
      team_name = var.team_name
    }
  }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "anylings" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.anylings.id
  config = {
    ingress = [
      {
        hostname       = var.hostname
        path           = "^/argocd(/|$)"
        service        = "http://argocd-server.argocd.svc.cluster.local:80"
        origin_request = local.protected_origin
      },
      {
        hostname       = var.hostname
        service        = "http://glance.glance.svc.cluster.local:8080"
        origin_request = local.protected_origin
      },
      { service = "http_status:404" },
    ]
  }
}

resource "cloudflare_dns_record" "anylings" {
  zone_id = var.zone_id
  name    = var.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.anylings.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1

  # Publish only after the Access-protected ingress is configured.
  depends_on = [cloudflare_zero_trust_tunnel_cloudflared_config.anylings]
}

output "tunnel_id" {
  description = "ID of the remotely managed tunnel."
  value       = cloudflare_zero_trust_tunnel_cloudflared.anylings.id
}
