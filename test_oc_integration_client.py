"""Unit tests for Outbound Center integration client error mapping."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.oc_integration_client import OcIntegrationApiError, http_exception_from_oc_error


def test_maps_integration_api_not_configured_to_503():
    err = OcIntegrationApiError(
        oc_status=500,
        detail="Integration API is not configured on this server.",
    )
    exc = http_exception_from_oc_error(err)
    assert exc.status_code == 503
    assert "INTEGRATION_API_TOKEN" in exc.detail


def test_maps_invalid_verification_code_to_400():
    err = OcIntegrationApiError(oc_status=403, detail="Invalid verification code")
    exc = http_exception_from_oc_error(err)
    assert exc.status_code == 400
    assert exc.detail == "Invalid verification code"


def test_maps_expired_session_to_409():
    err = OcIntegrationApiError(oc_status=410, detail="Connection request expired")
    exc = http_exception_from_oc_error(err)
    assert exc.status_code == 409


def test_maps_too_many_attempts_to_429():
    err = OcIntegrationApiError(oc_status=429, detail="Too many attempts")
    exc = http_exception_from_oc_error(err)
    assert exc.status_code == 429


def test_maps_oc_html_bad_gateway_to_actionable_panel_message():
    err = OcIntegrationApiError(
        oc_status=502,
        detail="<!DOCTYPE html><html><title>502: Bad gateway</title></html>",
    )
    exc = http_exception_from_oc_error(err)
    assert exc.status_code == 503
    assert "remote panel API" in exc.detail


if __name__ == "__main__":
    test_maps_integration_api_not_configured_to_503()
    test_maps_invalid_verification_code_to_400()
    test_maps_expired_session_to_409()
    test_maps_too_many_attempts_to_429()
    test_maps_oc_html_bad_gateway_to_actionable_panel_message()
    print("test_oc_integration_client passed")
