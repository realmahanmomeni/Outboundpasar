from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from app.services.integration_request_security import build_signature_payload, compute_request_signature


def signed_headers(method: str, path: str, body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    payload = build_signature_payload(timestamp, method, path, body)
    signature = compute_request_signature(secret, payload)
    return {
        "X-PG-Timestamp": timestamp,
        "X-PG-Nonce": nonce,
        "X-PG-Signature": signature,
    }
