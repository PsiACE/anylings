# Official Cloudflare resources: protected host routes and optional buckets.
data "cloudflare_zones" "this" {
  name = var.zone_name
}

locals {
  zone = one(data.cloudflare_zones.this.result)

  tunnel_origin = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"

  # An Access-protected route must name at least one allowed identity. There is
  # deliberately no "everyone" fallback and no implicit public admin route.
  access_identities_configured = length(var.access_include_emails) > 0 || length(var.access_include_group_ids) > 0

  protected_routes = { for hostname, route in var.routes : hostname => route if route.access }

  access_include = concat(
    [for email in var.access_include_emails : { email = { email = email } }],
    [for id in var.access_include_group_ids : { group = { id = id } }],
  )

  ingress = concat(
    [
      for hostname, route in var.routes : merge(
        {
          hostname = hostname
          service  = route.service
        },
        route.path == "" ? {} : { path = route.path },
      )
    ],
    [{ service = "http_status:404" }],
  )
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.cloudflare_account_id
  name       = var.tunnel_name
  # Routes live in this configuration, so the tunnel is managed remotely and the
  # in-cluster cloudflared runs with only the token.
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
  source     = "cloudflare"

  config = {
    ingress = local.ingress
  }

  # Policy first, then route activation. Existing connectors can serve new
  # routes immediately; creating these concurrently risks an unprotected gap.
  depends_on = [cloudflare_zero_trust_access_application.route]
}

resource "cloudflare_dns_record" "route" {
  for_each = var.routes

  zone_id = local.zone.id
  name    = each.key
  type    = "CNAME"
  content = local.tunnel_origin
  proxied = true
  ttl     = 1
  comment = "anylings tunnel route"

  # Publish DNS only after the policy-gated tunnel configuration is accepted.
  # Destroy reverses this order: DNS -> routes -> Access policies.
  depends_on = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

resource "cloudflare_zero_trust_access_application" "route" {
  for_each = local.protected_routes

  name = each.key
  type = "self_hosted"

  # self_hosted domain applications use zone scope; do not also set account_id.
  zone_id = local.zone.id
  domain  = each.key

  session_duration = var.session_duration

  policies = [
    {
      name       = "allowed-identities"
      decision   = "allow"
      precedence = 1
      include    = local.access_include
    },
  ]

  lifecycle {
    precondition {
      condition     = local.access_identities_configured
      error_message = "Route ${each.key} requests Cloudflare Access but no access_include_emails or access_include_group_ids are configured. Explicitly set access = false to publish a route without Access."
    }
  }
}

resource "cloudflare_r2_bucket" "this" {
  for_each = var.r2_buckets

  account_id = var.cloudflare_account_id
  name       = each.key
  location   = "apac"

  # Persistent data has a separate lifecycle from ingress resources.
  lifecycle {
    prevent_destroy = true
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "this" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
}
