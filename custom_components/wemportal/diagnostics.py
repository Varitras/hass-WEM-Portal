"""Diagnostics support for the WEM Portal integration.

The download is written to be attached to a public issue, so everything
installation-specific is stripped before it leaves the house: credentials
and configured expert ids by key, device ids by aliasing - they are dict
KEYS, which the redaction helper cannot reach.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_EXPERT_SLOT_ID_TEMPLATE, EXPERT_SLOT_COUNT
from .models import WemPortalConfigEntry

# Keys whose values identify the installation or the account. "cookie" is the
# scraped session (stored as a data row); "DeviceID" appears inside API rows.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "cookie",
    "DeviceID",
    *(CONF_EXPERT_SLOT_ID_TEMPLATE % slot for slot in range(1, EXPERT_SLOT_COUNT + 1)),
}


def _device_aliases(device_ids: Iterable[Any]) -> dict[Any, str]:
    """Positional alias per device id, stable within one report.

    Sorted so two downloads from the same installation name the same device
    the same way - what matters for debugging is telling devices apart, not
    knowing their ids.
    """
    ordered = sorted(device_ids, key=str)
    return {
        device_id: f"device_{position}"
        for position, device_id in enumerate(ordered, start=1)
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WemPortalConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, redacted for publication."""
    data = getattr(entry, "runtime_data", None)
    coordinator = getattr(data, "coordinator", None)
    api = getattr(data, "api", None)

    # The coordinator snapshot rather than api.data: it is what the entities
    # read, so the report shows the same world the dashboard does.
    current = getattr(coordinator, "data", None) or {}
    modules = (getattr(api, "modules", None) or {}) if api is not None else {}
    aliases = _device_aliases(current.keys() | modules.keys())

    readings = {
        aliases[device_id]: async_redact_data(rows, TO_REDACT)
        for device_id, rows in current.items()
    }
    module_counts = {}
    if modules:
        # Counts only: the full module descriptions are bulky and repeat the
        # readings; how many modules and parameters were discovered is what a
        # report needs to say.
        module_counts = {
            aliases[device_id]: {
                "modules": len(device_modules),
                "parameters": sum(
                    len(module.get("parameters", {}))
                    for module in device_modules.values()
                ),
            }
            for device_id, device_modules in modules.items()
        }

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator": {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "num_failed": getattr(coordinator, "num_failed", None),
            "num_auth_failed": getattr(coordinator, "num_auth_failed", None),
            "update_interval": str(getattr(coordinator, "update_interval", None)),
        },
        "devices": readings,
        "module_counts": module_counts,
    }
