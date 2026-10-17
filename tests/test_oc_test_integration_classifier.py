from app.services.oc_test_integration import is_test_oc_integration_base_url


def test_test_integration_urls():
    assert is_test_oc_integration_base_url("https://oc.example")
    assert is_test_oc_integration_base_url("http://test-multi-panel")
    assert not is_test_oc_integration_base_url("https://outboundcenter.shop/api")
