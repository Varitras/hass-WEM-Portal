""" WemPortal integration coordinator """
from __future__ import annotations
from time import monotonic
import copy

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.storage import Store
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from .exceptions import ForbiddenError, ServerError, WemPortalError, AuthError
from .const import _LOGGER, DEFAULT_CONF_SCAN_INTERVAL_API_VALUE, DEFAULT_TIMEOUT, DOMAIN
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from .wemportalapi import WemPortalApi
from .utils import serialize_modules

# Version of the on-disk format used to persist discovered device/module/
# parameter metadata (see get_modules_store()). Bump this if the structure
# of the cached data ever changes in a backwards-incompatible way.
MODULES_STORAGE_VERSION = 1

# A safety cap on how long the coordinator will ever wait between retries
# after repeated failures (see the backoff logic in _async_update_data).
MAX_BACKOFF_SECONDS = 6 * 3600  # 6 hours


def get_modules_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Return the Store used to persist discovered module/parameter metadata.

    Used both by __init__.py (to load the cache before creating the
    WemPortalApi instance) and by the coordinator itself (to save it after
    a successful update), so both sides always agree on the same file.
    """
    return Store(hass, MODULES_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_modules")


class WemPortalDataUpdateCoordinator(DataUpdateCoordinator):
    """DataUpdateCoordinator for wemportal component"""

    def __init__(
        self,
        hass: HomeAssistant,
        api: WemPortalApi,
        config_entry: ConfigEntry,
        update_interval,
    ) -> None:
        """Initialize DataUpdateCoordinator for the wemportal component"""
        super().__init__(
            hass,
            _LOGGER,
            name="WemPortal update",
            update_interval=update_interval,
        )
        self.api = api
        self.hass = hass
        self.config_entry = config_entry
        self.last_try = None
        self.num_failed = 0
        self._modules_store = get_modules_store(hass, config_entry.entry_id)


    async def _async_save_modules_cache(self) -> None:
        """Persist discovered device/module/parameter metadata to disk.

        This is what lets a future Home Assistant restart skip the slow,
        rate-limited per-module parameter discovery in
        WemPortalApi.get_parameters() (see get_devices()/fetch_data() in
        wemportalapi.py). This is purely a "nice to have" cache: any
        failure to save it is logged and otherwise ignored, since losing
        it only costs a slower next startup, never incorrect data.
        """
        if not self.api.modules:
            return
        try:
            await self._modules_store.async_save(serialize_modules(self.api.modules))
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not persist WEM Portal module cache: %s", exc)

    async def _async_update_data(self):
        """Fetch data from the wemportal api"""
        if self.num_failed > 2:
            # Wait longer than the plain scan interval before retrying,
            # and wait progressively longer the more consecutive failures
            # we've seen (capped at MAX_BACKOFF_SECONDS). This is a purely
            # additive safety margin on top of the existing, already
            # rate-limit-aware pacing inside wemportalapi.py itself (which
            # is intentionally left untouched here) - it only ever waits
            # *at least* as long as before, never less, to avoid adding any
            # extra risk of triggering a server-side block.
            required_wait = min(
                DEFAULT_CONF_SCAN_INTERVAL_API_VALUE * (self.num_failed - 2),
                MAX_BACKOFF_SECONDS,
            )
            if self.last_try is not None and monotonic() - self.last_try < required_wait:
                raise UpdateFailed("Waiting for more time to pass before retrying")

        device_registry = dr.async_get(self.hass)
        enabled_devices = []
        for device_id in self.api.data.keys():
            device_entry = device_registry.async_get_device(identifiers={(DOMAIN, str(device_id))})
            if device_entry is not None and device_entry.disabled_by is not None:
                _LOGGER.debug("Skipping disabled device %s", device_id)
                continue
            enabled_devices.append(device_id)

        async with async_timeout.timeout(DEFAULT_TIMEOUT):
            try:
                x = await self.hass.async_add_executor_job(self.api.fetch_data, enabled_devices)
                self.num_failed = 0
                await self._async_save_modules_cache()
                return x
            except AuthError as exc:
                self.num_failed += 1
                _LOGGER.error("Authentication error, raising ConfigEntryAuthFailed: %s", exc)
                raise ConfigEntryAuthFailed("WEM Portal authentication failed. Check your credentials.") from exc
            except (WemPortalError, ForbiddenError) as exc:
                self.num_failed += 1
                if self.num_failed >= 2:
                    _LOGGER.info("API errors persistent. Re-instantiating WemPortalApi to recover from potentially corrupted session/state.")
                    old_session = getattr(self.api, "session", None)
                    self.api = WemPortalApi(
                        self.config_entry.data.get(CONF_USERNAME),
                        self.config_entry.data.get(CONF_PASSWORD),
                        config=self.config_entry.options,
                        existing_data=self.api.data,
                        # Keep any already-discovered module/parameter metadata
                        # so re-instantiating the API (to recover from a
                        # corrupted session) doesn't also throw away
                        # everything get_parameters() already learned and
                        # force a full, slow rediscovery.
                        cached_modules=self.api.modules,
                        # Preserve an active 403 cooldown across the swap -
                        # a fresh instance would otherwise reset it and
                        # resume hitting a server that just rate-limited us.
                        blocked_until=getattr(self.api, "_blocked_until", 0.0),
                    )
                    # Point hass.data at the new instance so other consumers
                    # (e.g. the expert writer's shared cooldown check) use
                    # the current api, not the discarded one.
                    entry_store = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
                    if entry_store is not None:
                        entry_store["api"] = self.api
                    # Best-effort cleanup of the old HTTP session so it
                    # doesn't linger with an open connection after being
                    # discarded.
                    if old_session is not None:
                        try:
                            old_session.close()
                        except Exception:  # pylint: disable=broad-except
                            pass
                raise UpdateFailed(f"Error fetching data from wemportal: {exc}") from exc
            except Exception as exc:  # pylint: disable=broad-except
                # Catch-all safety net: covers cases that don't come from
                # fetch_data() itself (which already wraps its own
                # unexpected errors as WemPortalError) - most notably
                # asyncio.TimeoutError raised by the async_timeout.timeout()
                # context above when a very large installation's discovery
                # genuinely takes longer than DEFAULT_TIMEOUT. Without this,
                # such an error would propagate out of the coordinator
                # unwrapped, without the same retry/backoff bookkeeping as
                # every other failure mode gets.
                self.num_failed += 1
                _LOGGER.warning("Unexpected error updating WEM Portal data: %s", exc)
                raise UpdateFailed(f"Unexpected error fetching data from wemportal: {exc}") from exc
            finally:
                self.last_try = monotonic()
