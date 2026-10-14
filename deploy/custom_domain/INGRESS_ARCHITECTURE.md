# Custom-domain ingress architecture (Phase 3-B.3)

## Reported production constraints

- PasarGuard listens with TLS on `0.0.0.0:2053` inside `pasarguard-pasarguard-1`.
- Ports `80` and `443` belong to Outbound Center; do not rebind or reconfigure that ingress from this project.
- Caddy is not installed; NATS may be absent on `4222`.

Custom domains stay **inactive** until DNS verification, certificate readiness at the configured provider, and public HTTPS routing are all satisfied.

## Configuration

| Variable | Purpose |
|----------|---------|
| `CUSTOM_DOMAIN_DNS_ENABLED` | Enable live DNS TXT lookups for verification (default `false`). |
| `CUSTOM_DOMAIN_INGRESS_PROVIDER` | `none` (default), `caddy`, or `stub` (ignored outside `TESTING`). |
| `CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY` | Operator attestation that customer `https://host` (port 443) reaches the ingress path. Default `false`. |
| `CUSTOM_DOMAIN_CADDY_ADMIN_URL` | Required when provider is `caddy` (for example `http://127.0.0.1:2019`). |
| `CUSTOM_DOMAIN_CADDY_UPSTREAM_DIAL` | Origin dial string for Caddy reverse_proxy (default `127.0.0.1:8000`; use `127.0.0.1:2053` when terminating at the panel container). |

Setting `CUSTOM_DOMAIN_CADDY_ADMIN_URL` alone does **not** enable Caddy; set `CUSTOM_DOMAIN_INGRESS_PROVIDER=caddy` explicitly.

## Architecture options

1. **Dedicated public IP** — bind `:443` on a second IP for PasarGuard/Caddy without touching Outbound Center.
2. **External TLS** — CDN/managed custom hostnames terminating TLS and forwarding to `:2053` with origin auth.
3. **Shared ingress extension** — route via Outbound Center `:443`; requires separate authorization (not in this repo).
4. **Direct `:2053` TLS** — technical testing only; standard browsers use `:443`.

## Activation checklist (operations; not performed by application code)

1. Choose architecture and provision IP/proxy.
2. Point test customer DNS (TXT verification + public A/AAAA/CNAME as required).
3. Set `CUSTOM_DOMAIN_INGRESS_PROVIDER` and provider URLs; enable DNS when ready.
4. Set `CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY=true` only after smoke tests on port 443.
5. Configure trusted proxies (`UVICORN_PROXY_HEADERS`, `UVICORN_FORWARDED_ALLOW_IPS`).
6. Run migrations, restart PasarGuard only, smoke subscription URLs on default and custom hosts.
7. Monitor certificate renewal job and `/api/custom-domains/ingress-status`.

Rollback: set `CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY=false`, disable provider, disable domains via API; do not modify Outbound Center ingress without approval.
