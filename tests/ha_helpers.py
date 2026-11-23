"""Home Assistant's test helpers, from whichever package this environment has.

From Home Assistant 2026.11 they come in `pytest-homeassistant`, which Home
Assistant ships itself. Before that the community package
`pytest-homeassistant-custom-component` carried them, and it ended with
2026.10: nothing newer will come from it, and the new package has nothing
older. The suite runs against both ends of the supported range, so it needs
both until the declared minimum reaches 2026.11.
"""

try:
    from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
except ImportError:
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
    )

__all__ = ["MockConfigEntry", "async_fire_time_changed"]
