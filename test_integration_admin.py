"""Shared AdminDetails builders for OC/integration pytest suites."""
from __future__ import annotations

import uuid

from app.db.models import Admin, AdminRole, Tenant, TenantStatus, Workspace
from app.models.admin import AdminDetails, AdminRoleData


def tenant_admin_details(
    *,
    admin_id: int = 1,
    tenant_id: int = 1,
    workspace_id: int | None = 1,
    username: str = "tenant-admin",
) -> AdminDetails:
    return AdminDetails(
        id=admin_id,
        username=username,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        role=AdminRoleData(id=2, name="administrator", is_owner=False),
    )


def owner_admin_details(
    *,
    admin_id: int = 1,
    username: str = "owner",
    tenant_id: int | None = None,
) -> AdminDetails:
    return AdminDetails(
        id=admin_id,
        username=username,
        tenant_id=tenant_id,
        role=AdminRoleData(id=1, name="owner", is_owner=True),
    )


async def seed_tenant_admin_workspace(
    session,
    *,
    username: str = "tenant-admin",
) -> tuple[int, int, int]:
    """Insert tenant, administrator role, admin, and workspace.

    Returns ``(tenant_id, workspace_id, admin_id)``.
    """
    tenant = Tenant(name=f"tenant_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
    session.add(tenant)
    await session.flush()
    role = AdminRole(
        name=f"administrator_{uuid.uuid4().hex[:12]}",
        is_owner=False,
        permissions={},
        limits={},
        features={},
        access={},
        hwid={},
    )
    session.add(role)
    await session.flush()
    admin = Admin(
        username=username,
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant.id,
    )
    session.add(admin)
    await session.flush()
    workspace = Workspace(tenant_id=tenant.id, owner_admin_id=admin.id)
    session.add(workspace)
    await session.flush()
    admin.workspace_id = workspace.id
    await session.flush()
    return tenant.id, workspace.id, admin.id
