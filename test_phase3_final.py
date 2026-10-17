import asyncio
import base64
import json
import hmac
import hashlib
from datetime import datetime, UTC
from pydantic import BaseModel
from fastapi import HTTPException
import aiohttp

# Imports from app
from app.utils.jwt import create_customer_token, get_customer_payload
from app.utils.crypto import encrypt_secret, decrypt_secret
from app.routers.customer_deps import get_current_customer, verify_customer_owns_resource
from aiogram.utils.web_app import safe_parse_webapp_init_data, WebAppInitData

def sign_telegram_data(bot_token: str, init_data_dict: dict) -> str:
    # Sort keys
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(init_data_dict.items()) if k != "hash")
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return expected_hash

async def test_telegram_auth_cryptography():
    print("Testing Telegram WebApp Cryptography...")
    bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    
    # Valid auth data
    user_data = '{"id":123456789,"first_name":"Test","last_name":"","username":"testuser"}'
    init_data_dict = {
        "auth_date": "1672531200",
        "query_id": "AAHdF6IQAAAAAN0XohD-xxxxx",
        "user": user_data
    }
    init_data_dict["hash"] = sign_telegram_data(bot_token, init_data_dict)
    
    valid_init_data_string = "&".join(f"{k}={v}" for k, v in init_data_dict.items())
    
    # 1. Valid auth
    try:
        parsed = safe_parse_webapp_init_data(token=bot_token, init_data=valid_init_data_string)
        assert parsed.user.id == 123456789
        print("  [x] Valid authentication passed.")
    except Exception as e:
        print(f"  [ ] Valid authentication failed! {e}")
        raise
        
    # 2. Tampered auth (user.id)
    tampered_user_data = '{"id":999999999,"first_name":"Test","last_name":"","username":"testuser"}'
    init_data_dict_tampered = dict(init_data_dict)
    init_data_dict_tampered["user"] = tampered_user_data
    tampered_string = "&".join(f"{k}={v}" for k, v in init_data_dict_tampered.items())
    
    try:
        safe_parse_webapp_init_data(token=bot_token, init_data=tampered_string)
        assert False, "Tampered user.id should have failed!"
    except ValueError:
        print("  [x] Tampered authentication rejected.")

    # 3. Invalid hash
    init_data_dict_invalid_hash = dict(init_data_dict)
    init_data_dict_invalid_hash["hash"] = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    invalid_hash_string = "&".join(f"{k}={v}" for k, v in init_data_dict_invalid_hash.items())
    try:
        safe_parse_webapp_init_data(token=bot_token, init_data=invalid_hash_string)
        assert False, "Invalid hash should have failed!"
    except ValueError:
        print("  [x] Invalid hash rejected.")

async def test_cross_account_authorization():
    print("\nTesting Cross-Account Authorization...")
    
    account_a = "ACC-A"
    account_b = "ACC-B"
    
    token_a = await create_customer_token(account_a)
    token_b = await create_customer_token(account_b)
    
    # JWT A -> Account A (Allowed)
    current_a = await get_current_customer(token_a)
    try:
        await verify_customer_owns_resource(current_a, account_a)
        print("  [x] JWT A -> Account A: ALLOWED")
    except HTTPException:
        assert False
        
    # JWT A -> Account B (Denied)
    try:
        await verify_customer_owns_resource(current_a, account_b)
        assert False, "JWT A -> Account B should be denied!"
    except HTTPException as e:
        assert e.status_code == 403
        print("  [x] JWT A -> Account B: DENIED")
        
    # JWT B -> Account B (Allowed)
    current_b = await get_current_customer(token_b)
    try:
        await verify_customer_owns_resource(current_b, account_b)
        print("  [x] JWT B -> Account B: ALLOWED")
    except HTTPException:
        assert False
        
    # JWT B -> Account A (Denied)
    try:
        await verify_customer_owns_resource(current_b, account_a)
        assert False, "JWT B -> Account A should be denied!"
    except HTTPException as e:
        assert e.status_code == 403
        print("  [x] JWT B -> Account A: DENIED")

async def test_crypto_encryption():
    print("\nTesting Token Encryption...")
    plain_token = "secret-token-1234"
    cipher_text = await encrypt_secret(plain_token)
    assert cipher_text != plain_token
    assert plain_token not in cipher_text
    
    decrypted = await decrypt_secret(cipher_text)
    assert decrypted == plain_token
    print("  [x] Token encryption/decryption verified.")

async def test_jwt_compatibility():
    print("\nTesting JWT Compatibility...")
    token = await create_customer_token("55")
    
    # Verify customer token has distinct claim
    payload = await get_customer_payload(token)
    assert payload["account_id"] == "55"
    print("  [x] Customer JWT distinct claim verified.")
    
    # Tampering test
    header, payload_b64, signature = token.split(".")
    tampered_payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode())
    tampered_payload["sub"] = "99"
    new_payload_b64 = base64.urlsafe_b64encode(json.dumps(tampered_payload).encode()).decode().rstrip("=")
    tampered_token = f"{header}.{new_payload_b64}.{signature}"
    
    assert await get_customer_payload(tampered_token) is None
    print("  [x] Tampered JWT signature verification failed correctly.")

async def main():
    await test_telegram_auth_cryptography()
    await test_cross_account_authorization()
    await test_crypto_encryption()
    await test_jwt_compatibility()
    print("\nALL PHASE 3 VERIFICATION TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(main())
