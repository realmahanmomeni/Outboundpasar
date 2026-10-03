# Task 2 Security Report — Resource Isolation

## 1. Scope

Audited and tested tenant isolation for:

- Users (GET/UPDATE/bulk remove, operator OWN-scope)
- Groups (GET/UPDATE, `tenant_id` on create)
- Hosts (GET/UPDATE/CREATE `tenant_id`)
- OC Panels (GET/PATCH via `/api/panels`)
- Admins (list/get-by-id modify/remove, bulk validation, `get_admins_simple`)
- `validate_all_groups` (user create/modify and bulk group ops)
- Platform Owner global access
- Tenant Operators (native RBAC preserved)
- Migration `db17ab7db94a_tenant_isolation.py`
- `build_admin_details` / JWT auth tenant propagation

Not exhaustively exercised in automated tests: user DELETE, group DELETE, host DELETE, panel sync OC child mutation paths (integration wizard remains owner-only), non-owner panel import.

## 2. Findings

| # | Severity | Issue |
|---|----------|--------|
| F1 | Critical | `build_admin_details()` omitted `tenant_id`, so authenticated non-owner admins had `tenant_id=None` and tenant filters never applied in operations. |
| F2 | High | `get_validated_admin_by_id` / `get_validated_admin` had no tenant check — cross-tenant admin IDOR on modify/remove/usage/bulk admin ops. |
| F3 | High | `_get_validated_bulk_admins` called `get_admins()` without `tenant_id` — cross-tenant bulk admin mutations. |
| F4 | High | `get_admins_simple()` lacked `tenant_id` filter — cross-tenant admin enumeration. |
| F5 | High | `validate_all_groups()` fetched groups by ID without `tenant_id` — attach other tenant's groups on user create/modify/bulk. |
| F6 | Medium | `HostOperation.modify_hosts()` used `get_host_by_id()` without tenant scope — cross-tenant host update via bulk host API. |
| F7 | Medium | `set_owner` / bulk set owner resolved target admin without tenant scope. |
| F8 | Low | `app/routers/panel.py` used `AdminDetails` type without import (fixed). |
| F9 | Info | Migration adds nullable `tenant_id` on groups/hosts/oc_panels; only `oc_panels` backfilled from `purchaser_identity` → `admins.username`. Groups/hosts remain NULL until set by app logic on create or manual ops. |

## 3. Fixes

| File | Change |
|------|--------|
| `app/db/crud/admin.py` | `build_admin_details`: set `tenant_id`; `get_admins_simple`: optional `tenant_id` filter |
| `app/operation/__init__.py` | `ensure_admin_tenant_access`; tenant-aware `get_validated_admin*`; `validate_all_groups` passes `tenant_id` |
| `app/operation/admin.py` | Tenant on `create_admin`; all admin-by-id paths use `current_admin`; bulk admins tenant-scoped; `get_admins_simple` tenant filter |
| `app/operation/host.py` | `modify_hosts` tenant-scoped `get_host_by_id` |
| `app/operation/user.py` | Tenant-scoped admin resolution for set-owner and expired-user cleanup |
| `app/routers/panel.py` | Import `AdminDetails`; tenant checks on panel routes (pre-existing partial impl) |
| Prior Task 2 diff | User/group/host CRUD `tenant_id`; panel/integration routers; models/migrations |

## 4. Migration Audit

**File:** `app/db/migrations/versions/db17ab7db94a_tenant_isolation.py`

| Step | Behavior |
|------|----------|
| Upgrade | Adds nullable `tenant_id` + FK to `tenants` on `groups`, `hosts`, `oc_panels` (no ON DELETE CASCADE on column; default FK behavior). |
| Backfill | `UPDATE oc_panels SET tenant_id = admins.tenant_id FROM admins WHERE oc_panels.purchaser_identity = admins.username` — only rows with matching admin username; unmatched panels stay `NULL`. |
| Groups/hosts | No backfill — existing rows keep `tenant_id IS NULL` (global until assigned). |
| Downgrade | Drops FK + column on panels, hosts, groups (does not delete tenant rows). |

**Safety:** No truncates/drops of data tables. Deterministic backfill only where purchaser username matches an admin.

## 5. Test Matrix

| Resource | Operation | Same Tenant | Cross Tenant | Result |
| -------- | --------- | ----------- | ------------ | ------ |
| User | GET | PASS | BLOCKED (404) | Executed |
| User | UPDATE | PASS | BLOCKED (404) | Executed |
| User | BULK DELETE | N/A | BLOCKED (404) | Executed |
| User | DELETE | Not run | Not run | Gap |
| Group | GET | PASS | BLOCKED (404) | Executed |
| Group | UPDATE | PASS | BLOCKED (404) | Executed |
| Group | DELETE | Not run | Not run | Gap |
| Host | GET | PASS | BLOCKED (404) | Executed |
| Host | UPDATE | PASS | BLOCKED (404) | Executed |
| Host | CREATE | `tenant_id` set | N/A | Executed |
| Host | DELETE | Not run | Not run | Gap |
| Panel | GET | PASS | BLOCKED (403) | Executed |
| Panel | UPDATE | PASS | BLOCKED (403) | Executed |
| Panel | DELETE | Not run | Not run | Gap |
| Admin | LIST | PASS (scoped) | Not listed | Executed |
| Admin | UPDATE | PASS | BLOCKED (404) | Executed |
| Admin | DELETE | N/A | BLOCKED (404) | Executed |
| Bulk user remove | BULK | N/A | BLOCKED | Executed |
| Group IDs on user | RELATIONSHIP | PASS | BLOCKED via `validate_all_groups` | Code fix + partial test |
| Panel import | CREATE | Owner-only endpoint | N/A | Owner sets tenant from purchaser admin |
| Host bulk modify | UPDATE | PASS | BLOCKED via tenant `get_host_by_id` | Code fix, not isolated test |
| Owner | GET users/hosts | PASS both tenants | PASS | Executed |
| Operator | GET own user | PASS | BLOCKED other tenant | Executed |
| Operator | GET tenant admin's user | BLOCKED (OWN scope) | BLOCKED | Executed (expected) |
| OC child (config/group) | Indirect | Panel gate | Panel gate | Owner-only integration routes |

## 6. Owner Verification

Owner (`AdminRole.is_owner`) retrieved users and hosts from Tenant A and Tenant B in `test_tenant_task2_isolation.py` without tenant filters.

## 7. Operator Verification

Tenant A operator:

- Can read user owned by same operator admin (OWN scope).
- Cannot read Tenant B user (tenant filter + 404).
- Cannot read Tenant A administrator's user (OWN scope preserved — not a tenant bypass).

No new operator permissions were added.

## 8. API Contract

- Clients do not send `tenant_id`; tenant derived from authenticated admin (`build_admin_details` + `TenantContext`).
- No URL or request schema changes required for isolation fixes.
- Panel routes: non-owner admins use `tenant_id`; customer JWT still uses `purchaser_identity` (unchanged).

## 9. Remaining Risks

- Legacy `groups`/`hosts` with `tenant_id IS NULL` may be visible to owner only in list APIs; non-owner filters use `tenant_id = X` and exclude NULL rows.
- `oc_panels` without backfilled `tenant_id` may be inaccessible to tenant admins (403) until aligned with purchaser admin.
- OC child resources (`OCPanelGroup`, `OCPanelConfig`, `OCUserMapping`, `OCSyncState`) rely on panel-level gates; non-owner integration wizard remains owner-only — tenant admins use `/api/panels` paths.
- DELETE operations for user/group/host/panel not in automated matrix.

## Phase 15 regression (final verification)

**Cause:** Stale test fixture only — `test_phase15_security.py` injected `user_context` as `(identity, is_owner)` while Task 2 panel routes require `(identity, is_owner, admin)` with `AdminDetails.tenant_id` for tenant checks. Not a production router regression.

**Fix:** Updated `test_phase15_security.py` with `_panel_user_context()` helper, `tenant_id` on test panels, same/cross-tenant and owner panel IDOR cases, and tenant cleanup after panels (FK-safe).

**Results (executed):**

| Command | Result |
|---------|--------|
| `test_tenant_foundation.py` | PASS |
| `test_tenant_task2_isolation.py` | PASS |
| `test_phase15_security.py` | PASS |

## 10. Final Status

READY FOR REVIEW
