from app.services.oc_destination_runtime import destination_config_allowed_by_sync


def test_synced_by_catalog_id():
    assert destination_config_allowed_by_sync(
        oc_source_config_id="inbound-1",
        panel_id=1,
        destination_config_id="fpabc",
        synced={"inbound-1"},
    )


def test_synced_by_destination_fingerprint_when_catalog_id_mismatch():
    assert destination_config_allowed_by_sync(
        oc_source_config_id="stale-catalog",
        panel_id=1,
        destination_config_id="fpabc",
        synced={"fpabc"},
    )


def test_not_synced_when_catalog_id_missing_from_sync():
    assert not destination_config_allowed_by_sync(
        oc_source_config_id="inbound-1",
        panel_id=1,
        destination_config_id="fpabc",
        synced={"other"},
    )


def test_missing_catalog_id_on_host_not_filtered():
    assert destination_config_allowed_by_sync(
        oc_source_config_id="",
        panel_id=1,
        destination_config_id="fpabc",
        synced={"other"},
    )


def test_empty_synced_allows_all():
    assert destination_config_allowed_by_sync(
        oc_source_config_id="inbound-1",
        panel_id=1,
        destination_config_id="fpabc",
        synced=set(),
    )
