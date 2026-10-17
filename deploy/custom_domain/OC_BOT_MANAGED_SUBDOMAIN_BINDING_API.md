# Outbound Center bot → PasarGuard managed subdomain binding API (hardened)

## Trust model

- **Service identity:** `X-Integration-Token` (shared integration secret configured on both sides).
- **Account identity:** `X-OC-Connection-Token` (per-tenant credential issued when Telegram connects). PasarGuard resolves `tenant_id`, `workspace_id`, and `binding_admin_id` **only** from this token — never from JSON body fields.
- **Request integrity (production):** HMAC-SHA256 over `timestamp`, HTTP method, path, and SHA-256 of the raw body.

Body fields `tenant_id`, `workspace_id`, `binding_admin_id`, and `oc_account_id` are **not accepted** (removed from contract).

## Headers

| Header | Required | Purpose |
|--------|----------|---------|
| `X-Integration-Token` | Yes | Integration service authentication |
| `X-OC-Connection-Token` | Yes | Maps to active `TenantTelegramConnection` |
| `X-PG-Timestamp` | Yes (prod) | Unix seconds; max skew `MANAGED_SUBDOMAIN_BOT_REQUEST_MAX_AGE_SECONDS` (default 300) |
| `X-PG-Nonce` | Yes (prod) | Unique per request; replay rejected |
| `X-PG-Signature` | Yes (prod) | HMAC-SHA256 hex of canonical payload |

Canonical signing payload (UTF-8 lines):

```
{timestamp}
{METHOD}
{path}
{sha256_hex(raw_body)}
```

Secret: `MANAGED_SUBDOMAIN_BOT_HMAC_SECRET` (rotate by dual-write / overlap window).

In `TESTING=true` without HMAC secret, signature checks are skipped (development only).

## Create / refresh claim

`POST /api/integration/managed-subdomains/bindings`

```json
{
  "label": "alpha",
  "oc_subdomain_id": "stable-id-from-oc-registry",
  "idempotency_key": "optional-unique-key"
}
```

### Idempotency

| Case | HTTP | Behavior |
|------|------|----------|
| Same `idempotency_key` | 200 | Returns existing row |
| Same label + same workspace | 200/201 | Refreshes `pending_claim` |
| Label owned by another workspace | 409 | No transfer |
| Revoked label, different workspace | 409 | No silent transfer |

## Revoke

`DELETE /api/integration/managed-subdomains/bindings/{label}`

- Same auth headers; sign empty body.
- Idempotent: already revoked → `204`.
- Wrong workspace → `403`.

## Panel admin confirmation

`PUT /api/custom-domains/managed/settings` with `{ "label": "alpha" }` or `{ "label": null }`.

Server matches `pending_claim` for **derived** workspace/admin only.

## Ownership vs serving

| State | Meaning |
|-------|---------|
| `ownership_linked` | Bot claim exists or domain row linked |
| `serving_ready` | Public ingress flag + routable checks pass |

Subscription URLs use **only** `serving_ready`. Otherwise default `url_prefix`.

## Configuration

| Variable | Default |
|----------|---------|
| `MANAGED_SUBSCRIPTION_BASE_DOMAIN` | `outboundcenter.ir` |
| `MANAGED_SUBDOMAIN_BOT_HMAC_SECRET` | (empty; required in production) |
| `CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY` | `false` |

## Credential rotation

1. Generate new integration token / HMAC secret on PasarGuard.
2. Update Outbound Center bot configuration.
3. Revoke old secret after overlap window.
4. Never log tokens, secrets, or connection credentials.
