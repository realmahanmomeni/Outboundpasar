"""Native subscription-userinfo behavior (OC path uses the same header builder)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.settings import Subscription as SubSettings

_SUB_SETTINGS = SubSettings(rules=[])
from app.models.user import UsersResponseWithInbounds
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.db.models import UserStatus


def _user(**kwargs) -> UsersResponseWithInbounds:
    base = {
        "id": 1,
        "username": "u",
        "status": UserStatus.active,
        "used_traffic": 0,
        "data_limit": None,
        "expire": None,
        "inbounds": [],
        "proxy_settings": {},
        "admin_id": 1,
        "created_at": datetime.now(UTC),
    }
    base.update(kwargs)
    return UsersResponseWithInbounds(**base)


def test_subscription_userinfo_unlimited_no_total():
    sub = SubscriptionOperation(operator_type=OperatorType.API)
    user = _user(used_traffic=5000, data_limit=None)
    headers = sub.create_response_headers(user, "https://example.test/sub/x", _SUB_SETTINGS)
    info = headers["subscription-userinfo"]
    assert "download=5000" in info
    assert "total=0" in info


def test_subscription_userinfo_with_quota_and_expiry():
    sub = SubscriptionOperation(operator_type=OperatorType.API)
    exp = datetime.now(UTC) + timedelta(days=7)
    user = _user(used_traffic=100, data_limit=1000, expire=exp)
    headers = sub.create_response_headers(user, "https://example.test/sub/x", _SUB_SETTINGS)
    info = headers["subscription-userinfo"]
    assert "download=100" in info
    assert "total=1000" in info
    assert f"expire={int(exp.timestamp())}" in info


def test_subscription_userinfo_zero_remaining_quota():
    sub = SubscriptionOperation(operator_type=OperatorType.API)
    user = _user(used_traffic=1000, data_limit=1000)
    headers = sub.create_response_headers(user, "https://example.test/sub/x", _SUB_SETTINGS)
    assert "download=1000" in headers["subscription-userinfo"]
    assert "total=1000" in headers["subscription-userinfo"]
