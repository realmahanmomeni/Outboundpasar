from dataclasses import dataclass
from typing import Optional
from app.models.admin import AdminDetails

@dataclass
class TenantContext:
    admin: AdminDetails

    @property
    def tenant_id(self) -> Optional[int]:
        return self.admin.tenant_id

    @property
    def is_owner(self) -> bool:
        return self.admin.is_owner
    
    @property
    def role(self):
        return self.admin.role
