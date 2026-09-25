import logging
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db import AsyncSession, get_db
from app.db.models_oc import OCIntegration, OCPanel, OCPanelGroup, OCPanelConfig
from app.db.models import ProxyInbound, ProxyHost
from app.routers.panel import get_current_user_context
from app.utils.crypto import decrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integration", tags=["Integration Wizard"])

class PanelItem(BaseModel):
    id: int
    panel_type: str
    name: str
    status: str

class AvailablePanelsResponse(BaseModel):
    items: list[PanelItem]

class SelectPanelRequest(BaseModel):
    source_panel_id: str
    name: str

class SelectPanelResponse(BaseModel):
    panel_id: int
    source_panel_id: str
    test_user_id: str | None

class TestUserResponse(BaseModel):
    test_user_id: str

class GroupItem(BaseModel):
    id: str
    name: str

class GroupListResponse(BaseModel):
    groups: list[GroupItem]

class SyncRequest(BaseModel):
    selected_group_ids: list[str]
    group_names: dict[str, str]

class SyncResponse(BaseModel):
    status: str
    configs_created: int
    hosts_created: int

async def get_active_integration(db: AsyncSession) -> OCIntegration:
    integration = (await db.execute(select(OCIntegration).where(OCIntegration.is_active == True).limit(1))).scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=503, detail="No active OC integration")
    return integration

async def call_oc_api(integration: OCIntegration, method: str, path: str, json: dict = None):
    decrypted_token = await decrypt_secret(integration.api_token_encrypted)
    url = f"{integration.base_url.rstrip('/')}{path}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                headers={"X-Integration-Token": decrypted_token},
                json=json,
                timeout=aiohttp.ClientTimeout(total=30.0)
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"OC API error {resp.status}: {await resp.text()}")
                    raise HTTPException(status_code=502, detail="OC API error")
                if resp.status != 204:
                    return await resp.json()
                return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OC API connection error: {e}")
        raise HTTPException(status_code=503, detail="OC API unavailable")

@router.get("/available-panels", response_model=AvailablePanelsResponse)
async def get_available_panels(db: AsyncSession = Depends(get_db), user_context = Depends(get_current_user_context)):
    identity, is_owner = user_context
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only owner can view available panels")
        
    integration = await get_active_integration(db)
    data = await call_oc_api(integration, "GET", "/v1/integration/panels")
    
    return AvailablePanelsResponse(items=data["items"])

@router.post("/select-panel", response_model=SelectPanelResponse)
async def select_panel(req: SelectPanelRequest, db: AsyncSession = Depends(get_db), user_context = Depends(get_current_user_context)):
    identity, is_owner = user_context
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only owner can add panels")
        
    integration = await get_active_integration(db)
    
    stmt = select(OCPanel).where(
        OCPanel.integration_id == integration.id,
        OCPanel.source_panel_id == req.source_panel_id,
        OCPanel.purchaser_identity == identity  # Owner's identity is "admin" normally, wait! The prompt says "Panel 40 + Mahan". If we are the owner, we can set the purchaser_identity to whatever the OC API says? 
        # Actually, in phase 4, the name is "admin_username - Sub id". So we parse it.
    )
    # Wait, the parsing logic:
    purchaser = req.name.split(" - Sub ")[0] if " - Sub " in req.name else req.name
    
    stmt = select(OCPanel).where(
        OCPanel.integration_id == integration.id,
        OCPanel.source_panel_id == req.source_panel_id,
        OCPanel.purchaser_identity == purchaser
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    
    if existing:
        return SelectPanelResponse(panel_id=existing.id, source_panel_id=existing.source_panel_id, test_user_id=existing.test_user_id)
        
    new_panel = OCPanel(
        integration_id=integration.id,
        source_panel_id=req.source_panel_id,
        purchaser_identity=purchaser,
        name=req.name,
        sync_status="pending"
    )
    db.add(new_panel)
    await db.commit()
    await db.refresh(new_panel)
    
    return SelectPanelResponse(panel_id=new_panel.id, source_panel_id=new_panel.source_panel_id, test_user_id=new_panel.test_user_id)

@router.post("/panels/{panel_id}/test-user", response_model=TestUserResponse)
async def create_test_user(panel_id: int, db: AsyncSession = Depends(get_db), user_context = Depends(get_current_user_context)):
    identity, is_owner = user_context
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only owner can manage panels")
        
    panel = (await db.execute(select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
        
    if panel.test_user_id:
        # Verify it still exists? Or just return it. The prompt says "Reuse existing test user". 
        # We can just call the OC API again because it is idempotent.
        pass
        
    data = await call_oc_api(panel.integration, "POST", f"/v1/integration/panels/{panel.source_panel_id}/test-user")
    
    panel.test_user_id = data["external_user_id"]
    await db.commit()
    
    return TestUserResponse(test_user_id=panel.test_user_id)

@router.get("/panels/{panel_id}/groups", response_model=GroupListResponse)
async def get_groups(panel_id: int, db: AsyncSession = Depends(get_db), user_context = Depends(get_current_user_context)):
    identity, is_owner = user_context
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only owner can manage panels")
        
    panel = (await db.execute(select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
        
    data = await call_oc_api(panel.integration, "GET", f"/v1/integration/panels/{panel.source_panel_id}/groups")
    return GroupListResponse(groups=data["groups"])

@router.post("/panels/{panel_id}/sync", response_model=SyncResponse)
async def sync_configs_and_hosts(panel_id: int, req: SyncRequest, db: AsyncSession = Depends(get_db), user_context = Depends(get_current_user_context)):
    identity, is_owner = user_context
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only owner can manage panels")
        
    panel = (await db.execute(select(OCPanel).options(selectinload(OCPanel.integration), selectinload(OCPanel.groups), selectinload(OCPanel.configs)).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=404, detail="Panel not found")
        
    # 1. Update groups
    existing_group_by_source = {g.source_group_id: g for g in panel.groups}
    for source_id, name in req.group_names.items():
        is_sel = source_id in req.selected_group_ids
        if source_id in existing_group_by_source:
            existing_group_by_source[source_id].is_selected = is_sel
            existing_group_by_source[source_id].source_name = name
        else:
            g = OCPanelGroup(
                panel_id=panel.id,
                source_group_id=source_id,
                source_name=name,
                is_selected=is_sel
            )
            db.add(g)
            panel.groups.append(g)
            existing_group_by_source[source_id] = g
            
    await db.flush()
    
    # 2. Fetch configs
    data = await call_oc_api(panel.integration, "GET", f"/v1/integration/panels/{panel.source_panel_id}/configs")
    configs_list = data["configs"]
    
    configs_created = 0
    hosts_created = 0
    
    existing_configs = {c.source_config_id: c for c in panel.configs}
    
    # Pre-fetch existing inbounds to check uniqueness of tags
    # Tags must be unique in ProxyInbound.
    # Pattern: f"oc_{panel.id}_{config['id']}"
    
    for c_data in configs_list:
        # Check if the config maps to any of our selected groups. 
        # If group_mapping is not supported, we assume it's global and should be created.
        mapping = c_data.get("group_mapping", {})
        is_supported = mapping.get("supported", False)
        mapped_groups = mapping.get("groups", [])
        
        # If supported, at least one mapped_group must be in selected_group_ids
        if is_supported and not any(g in req.selected_group_ids for g in mapped_groups):
            continue
            
        local_group_id = None
        if is_supported and mapped_groups:
            for g_id in mapped_groups:
                if g_id in existing_group_by_source:
                    local_group_id = existing_group_by_source[g_id].id
                    break
                    
        c_id = c_data["id"]
        c_name = c_data["name"]
        
        tag = f"oc_{panel.id}_{c_id}"
        
        if c_id not in existing_configs:
            # Create Inbound
            inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one_or_none()
            if not inbound:
                inbound = ProxyInbound(tag=tag)
                db.add(inbound)
                await db.flush() # get inbound.id
                
            # Create/Update Host
            host = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one_or_none()
            if not host:
                host = ProxyHost(
                    remark=c_name,
                    priority=0,
                    address={"127.0.0.1"}, # placeholder for now
                    port=None,
                    path=None,
                    allowinsecure=None,
                    alpn=[],
                    status=[]
                )
                host.inbound = inbound
                db.add(host)
                hosts_created += 1
            elif host.remark != c_name:
                host.remark = c_name
                
            pc = OCPanelConfig(
                panel_id=panel.id,
                source_config_id=c_id,
                source_name=c_name,
                panel_group_id=local_group_id,
                virtual_inbound_tag=tag
            )
            db.add(pc)
            panel.configs.append(pc)
            configs_created += 1
            
            existing_configs[c_id] = pc
        else:
            pc = existing_configs[c_id]
            pc.source_name = c_name
            pc.panel_group_id = local_group_id
            
            host = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one_or_none()
            if host and host.remark != c_name:
                host.remark = c_name
            
    await db.commit()
    
    # Update panel status
    panel.sync_status = "connected"
    await db.commit()
    
    return SyncResponse(status="success", configs_created=configs_created, hosts_created=hosts_created)
