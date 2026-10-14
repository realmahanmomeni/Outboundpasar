# Outbound Center bot → PasarGuard managed subdomain binding API

When a user chooses **Connect to central panel** in the Outbound Center Telegram bot, the bot must register the subdomain claim on PasarGuard **before** the admin types the label in Settings.

## Authentication

| Header | Value |
|--------|--------|
| `X-Integration-Token` | Same shared secret as other PasarGuard ↔ Outbound Center integration calls |

Browser clients and end users must never receive this token.

## Create or refresh claim (idempotent)

`POST /api/integration/managed-subdomains/bindings`

### Request body

```json
{
  "tenant_id": 1,
  "workspace_id": 2,
  "binding_admin_id": 3,
  "label": "alpha",
  "oc_subdomain_id": "stable-id-from-oc-registry",
  "oc_account_id": 42,
  "idempotency_key": "optional-unique-key"
}
```

- `label` — single DNS label only (not a full hostname).
- `binding_admin_id` — PasarGuard admin who owns the workspace connection (same scope as Telegram OC binding).
- `idempotency_key` — when provided, repeats return the same row without duplicating.

### Success `201`

```json
{
  "id": 10,
  "label": "alpha",
  "fqdn": "alpha.outboundcenter.ir",
  "status": "pending_claim",
  "workspace_id": 2,
  "binding_admin_id": 3
}
```

### Errors

| Status | Meaning |
|--------|---------|
| `401` / `403` | Missing or invalid integration token |
| `409` | Label already bound to a different workspace |
| `422` | Invalid label |

## Revoke claim

`DELETE /api/integration/managed-subdomains/bindings/{label}`

- Marks the bot binding `revoked`.
- Disables the workspace managed subscription hostname if it was active for that FQDN.

## Panel admin confirmation (browser)

After the bot creates `pending_claim`, the admin opens **Settings → Subscriptions** and enters `alpha` in `[ x ].{MANAGED_SUBSCRIPTION_BASE_DOMAIN}`.

`PUT /api/custom-domains/managed/settings` with `{ "label": "alpha" }` succeeds only when a matching `pending_claim` exists for that workspace and admin.

## Configuration

| Variable | Purpose |
|----------|---------|
| `MANAGED_SUBSCRIPTION_BASE_DOMAIN` | Global suffix (default `outboundcenter.ir`) |
