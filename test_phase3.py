import asyncio
import jwt
from app.utils.jwt import create_customer_token, get_customer_payload
from app.utils.crypto import encrypt_secret, decrypt_secret

async def test_crypto():
    token = "test-integration-token"
    enc = await encrypt_secret(token)
    assert enc != token
    dec = await decrypt_secret(enc)
    assert dec == token
    print("Crypto tests passed")

async def test_jwt():
    account_id = "55"
    token = await create_customer_token(account_id)
    
    # Valid token
    payload = await get_customer_payload(token)
    assert payload["account_id"] == account_id
    
    # Tampered token (change account_id)
    import base64
    import json
    header, payload_b64, signature = token.split(".")
    tampered_payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode())
    tampered_payload["sub"] = "99"
    new_payload_b64 = base64.urlsafe_b64encode(json.dumps(tampered_payload).encode()).decode().rstrip("=")
    tampered_token = f"{header}.{new_payload_b64}.{signature}"
    
    payload = await get_customer_payload(tampered_token)
    assert payload is None
    print("JWT tests passed")

async def main():
    await test_crypto()
    await test_jwt()

if __name__ == "__main__":
    asyncio.run(main())
