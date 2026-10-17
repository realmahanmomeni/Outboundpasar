# Outbound Center managed subdomain API (required contract)

PasarGuard associates subscription hostnames such as `alpha.outboundcenter.ir` only after Outbound Center confirms the Telegram-connected account owns the label.

## Endpoint

`GET /v1/integration/pasarguard/subdomains/{label}`

### Request headers

| Header | Description |
|--------|-------------|
| `X-Integration-Token` | PasarGuard integration token configured on Outbound Center |
| `X-OC-Connection-Token` | Connection token issued when the tenant verified Telegram |

### Success (`200`)

```json
{
  "subdomain_id": "oc_sub_abc123",
  "label": "alpha",
  "fqdn": "alpha.outboundcenter.ir",
  "status": "active",
  "oc_account_id": 42
}
```

`oc_account_id` must match the account implied by `X-OC-Connection-Token`.

### Errors

| Status | Meaning |
|--------|---------|
| `404` | Label not registered or not visible to this account |
| `403` | Invalid connection token or banned account |
| `409` | Subdomain suspended or not eligible |

## PasarGuard configuration

- `OC_MANAGED_SUBDOMAIN_API_ENABLED=true` when Outbound Center ships the endpoint.
- `MANAGED_SUBSCRIPTION_BASE_DOMAIN=outboundcenter.ir`
- `CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY=true` only after OC ingress routes `*.outboundcenter.ir` to PasarGuard with a valid wildcard certificate.

## TLS / ingress (not deployed in this phase)

- Wildcard `*.outboundcenter.ir` terminates at the **Outbound Center** public HTTPS listener (`443`), not on PasarGuard `:2053` alone.
- DNS-01 for wildcard issuance is an infrastructure task; PasarGuard does not store private keys or run ACME in this phase.
