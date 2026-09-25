import logging
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from aiogram.utils.web_app import safe_parse_webapp_init_data, WebAppInitData

from app.db import AsyncSession, get_db
from app.models.settings import Telegram
from app.settings import telegram_settings
from app.db.models_oc import OCIntegration
from app.utils.jwt import create_customer_token

router = APIRouter(prefix="/api/customer/auth", tags=["Customer Auth"])
logger = logging.getLogger(__name__)

class TelegramAuthRequest(BaseModel):
    initData: str

class TelegramAuthResponse(BaseModel):
    token: str
    account_id: str

@router.post("/telegram", response_model=TelegramAuthResponse)
async def authenticate_telegram_user(
    request: TelegramAuthRequest,
    db: AsyncSession = Depends(get_db)
):
    settings: Telegram = await telegram_settings()
    
    if not settings.token:
        raise HTTPException(status_code=503, detail="Telegram bot not configured")
        
    try:
        data: WebAppInitData = safe_parse_webapp_init_data(
            token=settings.token, 
            init_data=request.initData
        )
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid token")

    telegram_id = data.user.id
    
    # Find active integration
    integration = (await db.execute(
        select(OCIntegration).where(OCIntegration.is_active == True).limit(1)
    )).scalar_one_or_none()
    
    if not integration:
        raise HTTPException(status_code=503, detail="No active OC integration")
        
    from app.utils.crypto import decrypt_secret
    
    url = f"{integration.base_url.rstrip('/')}/v1/integration/account/by-telegram/{telegram_id}"
    
    try:
        decrypted_token = await decrypt_secret(integration.api_token_encrypted)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, 
                headers={"X-Integration-Token": decrypted_token},
                timeout=aiohttp.ClientTimeout(total=10.0)
            ) as resp:
                status_code = resp.status
                account_data = await resp.json()
    except Exception as e:
        logger.error(f"OC API timeout/error: {e}")
        raise HTTPException(status_code=503, detail="OC API unavailable")
        
    if status_code == 404:
        raise HTTPException(status_code=404, detail="Unknown Telegram account")
    if status_code != 200:
        raise HTTPException(status_code=502, detail="OC API error")
        
    account_id = str(account_data["account_id"])
    if account_data.get("status") != "active":
        raise HTTPException(status_code=403, detail="Inactive OC account")
        
    token = await create_customer_token(account_id)
    return TelegramAuthResponse(token=token, account_id=account_id)
