from app.utils.jwt import get_customer_payload
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

# Re-use the existing oauth2_scheme from authentication.py or define one if isolated
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/customer/auth/telegram", auto_error=False)

async def get_current_customer(token: str = Depends(oauth2_scheme)) -> str:
    """
    Dependency to authenticate a customer via JWT and return their account_id.
    Ensures that only tokens with access='customer' are accepted.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate customer credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    payload = await get_customer_payload(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid customer token or not a customer",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    account_id = payload.get("account_id")
    if not account_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid customer token payload",
        )
        
    return account_id

async def verify_customer_owns_resource(account_id: str, resource_account_id: str):
    """
    Helper to verify that the authenticated customer actually owns the requested resource.
    """
    if account_id != resource_account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this resource"
        )
