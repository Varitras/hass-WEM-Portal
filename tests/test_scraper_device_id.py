"""Stable scraper device id: scraped sensors must keep a constant device id
(and thus their history) across mode switches. The id is decided once
(preferring a real API-discovered device, else a placeholder) and then
reused/persisted.
"""

from custom_components.wemportal.wemportalapi import (
    WemPortalApi,
    SCRAPER_FALLBACK_DEVICE_ID,
)


def _api(**kwargs):
    return WemPortalApi("user@example.org", "secret", **kwargs)


def test_pure_web_install_uses_placeholder():
    """No real device ever known (no module cache) -> placeholder id."""
    api = _api()
    assert api.modules is None
    assert api.resolve_scraper_device_id() == SCRAPER_FALLBACK_DEVICE_ID
    assert api.scraper_device_id == SCRAPER_FALLBACK_DEVICE_ID


def test_prefers_real_api_device_id():
    """With a real API-discovered device, scraped sensors share that id."""
    api = _api(cached_modules={"1234": {(0, 1): {"Index": 0, "Type": 1, "Name": "HP"}}})
    assert api.resolve_scraper_device_id() == "1234"
    assert api.scraper_device_id == "1234"


def test_persisted_value_is_locked_and_wins_over_real_device():
    """A previously-decided id (loaded from storage) must NOT be overridden
    when a real device id is now known - this keeps a pure-web install pinned
    to the placeholder even after it later switches to `both`."""
    api = _api(
        scraper_device_id=SCRAPER_FALLBACK_DEVICE_ID,
        cached_modules={"1234": {(0, 1): {"Index": 0, "Type": 1, "Name": "HP"}}},
    )
    assert api.resolve_scraper_device_id() == SCRAPER_FALLBACK_DEVICE_ID


def test_decision_is_stable_across_calls():
    """Once decided, repeated calls return the same id even if a real device
    appears afterwards."""
    api = _api()
    first = api.resolve_scraper_device_id()
    api.modules = {"9999": {}}
    assert api.resolve_scraper_device_id() == first == SCRAPER_FALLBACK_DEVICE_ID


def test_merge_stores_scraped_data_under_resolved_id():
    """_merge_webscraping_data lands under the resolved device id, which is
    then reused (locked) on the next scrape."""
    api = _api(cached_modules={"1234": {(0, 1): {"Index": 0, "Type": 1, "Name": "HP"}}})
    dev = api.resolve_scraper_device_id()
    api._merge_webscraping_data(dev, {"hp-temp": {"value": 21.0, "friendlyName": "HP Temp"}})
    assert "1234" in api.data
    assert api.data["1234"]["hp-temp"]["value"] == 21.0
    assert api.resolve_scraper_device_id() == "1234"
