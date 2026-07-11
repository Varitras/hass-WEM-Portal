"""Shared pytest configuration for the CI suite.

Declares the Home Assistant custom-component test plugin (which provides the
`hass` fixture and installs a matching Home Assistant), and centralises the
`time.sleep` mock so the integration's real server-load pacing sleeps never
run in tests. Importing the modules here also makes collection fail loudly
if the integration cannot be imported against the installed HA version.
"""

import pytest

from custom_components.wemportal import wemportalapi, expert_writer

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Neutralise real time.sleep() in the modules that pace server load.

    The production code deliberately sleeps between portal requests; in tests
    those waits must be instant. Mocking centrally (not per test) keeps later
    tests that hit the same code paths fast too.
    """
    monkeypatch.setattr(wemportalapi.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(expert_writer.time, "sleep", lambda *a, **k: None)
    yield
