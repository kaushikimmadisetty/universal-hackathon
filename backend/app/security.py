import hashlib
import hmac
import time
from urllib.parse import urlencode


def verify_webhook_signature(
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    secret: str,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    if not timestamp_header or not signature_header:
        return False
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        return False
    if abs(int(time.time()) - timestamp) > tolerance_seconds:
        return False

    supplied = signature_header.removeprefix("sha256=")
    signed_body = timestamp_header.encode("ascii") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def build_asset_url(
    base_url: str,
    asset_id: str,
    secret: str,
    *,
    ttl_seconds: int = 3600,
) -> str:
    expires = int(time.time()) + ttl_seconds
    signed_value = f"{asset_id}:{expires}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signed_value, hashlib.sha256).hexdigest()
    query = urlencode({"expires": expires, "signature": signature})
    return f"{base_url}/internal/assets/{asset_id}?{query}"


def verify_asset_signature(
    asset_id: str,
    expires: int,
    signature: str,
    secret: str,
    *,
    max_ttl_seconds: int = 3600,
) -> bool:
    now = int(time.time())
    if expires < now or expires > now + max_ttl_seconds:
        return False
    signed_value = f"{asset_id}:{expires}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed_value, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
