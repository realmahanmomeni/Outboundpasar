from fastapi import APIRouter

from . import (
    admin,
    admin_role,
    api_key,
    client_template,
    core,
    group,
    home,
    host,
    hwid,
    node,
    settings,
    setup,
    subscription,
    system,
    user,
    user_template,
    customer_auth,
    panel,
    integration,
    user_panel_binding,
)

api_router = APIRouter()

routers = [
    home.router,
    admin.router,
    api_key.router,
    admin_role.router,
    setup.router,
    system.router,
    settings.router,
    group.router,
    core.router,
    client_template.router,
    host.router,
    node.router,
    user.router,
    subscription.router,
    user_template.router,
    hwid.router,
    customer_auth.router,
    panel.router,
    integration.router,
    user_panel_binding.router,
]

for router in routers:
    api_router.include_router(router)

__all__ = ["api_router"]
