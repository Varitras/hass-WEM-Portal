"""
Number platform for wemportal component
"""

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import get_wemportal_unique_id
from homeassistant.helpers.device_registry import DeviceInfo
from .const import _LOGGER, DOMAIN
from .utils import (fix_value_and_uom, uom_to_device_class, build_device_info)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Number entry setup."""

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities: list[WemPortalNumber] = []
    for device_id, entity_data in coordinator.data.items():
        for unique_id, values in entity_data.items():
            if isinstance(values, int):
                continue
            # .get() instead of direct indexing: one malformed data point
            # should not crash setup for every number entity on this device.
            if values.get("platform") == "number":
                entities.append(
                    WemPortalNumber(
                        coordinator, config_entry, device_id, unique_id, values
                    )
                )

    async_add_entities(entities)

    # Expert write access (web): add the configured expert numbers.
    # Returns [] unless the option is enabled - lazy import keeps the
    # expert module out of the load path while disabled.
    from .expert_writer import create_expert_number_entities
    expert_entities = create_expert_number_entities(config_entry)
    if expert_entities:
        _async_migrate_expert_unique_ids(hass, config_entry, expert_entities)
        async_add_entities(expert_entities)
        # Expose them to the optional hourly auto-poll (set up in __init__),
        # which reads all configured ids in one shared session and pushes the
        # values back into these entities.
        store = hass.data[DOMAIN][config_entry.entry_id]
        store["expert_entities"] = expert_entities
        start_poll = store.get("start_expert_auto_poll")
        if start_poll is not None:
            start_poll()


def _async_migrate_expert_unique_ids(hass, config_entry, expert_entities) -> None:
    """Migrate expert entities from raw-entityvalue unique_ids to digests.

    Older versions embedded the raw, installation-specific entityvalue in
    the unique_id (persisted in .storage/core.entity_registry); it is now a
    SHA-256 digest (see expert_writer.ev_digest). Updating the registry
    entry in place preserves the entity_id, history and restored state.
    Best-effort: a failure only means the entity is re-created under the
    new unique_id instead of migrated.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity in expert_entities:
        old_unique_id = f"{config_entry.entry_id}:expert:{entity.entityvalue}"
        if old_unique_id == entity.unique_id:
            continue
        entity_id = registry.async_get_entity_id("number", DOMAIN, old_unique_id)
        if entity_id is None:
            continue
        if registry.async_get_entity_id("number", DOMAIN, entity.unique_id) is not None:
            # A digest-format entity already exists; leave both untouched
            # rather than colliding (should not happen in practice).
            _LOGGER.debug("Skipping expert unique_id migration for %s: target exists.", entity_id)
            continue
        try:
            registry.async_update_entity(entity_id, new_unique_id=entity.unique_id)
            _LOGGER.info("Migrated expert entity %s to digest-based unique_id.", entity_id)
        except ValueError as exc:
            _LOGGER.warning("Could not migrate expert entity %s: %s", entity_id, exc)


class WemPortalNumber(CoordinatorEntity, NumberEntity):
    """Representation of a WEM Portal number."""

    def _validated_native_value(self, val):
        """Return a Home Assistant-safe native value.

        Unlike sensor.py (where a value can legitimately be text when no
        device_class is set), a NumberEntity's native_value is ALWAYS
        required to be numeric - there is no valid "text" state for a
        number input. So this always attempts the numeric conversion,
        rather than only doing so when a unit happens to be present this
        cycle (which previously could let a non-numeric string slip
        through uncaught whenever the per-cycle unit was empty).
        """
        if val is None:
            _LOGGER.warning('Invalid number value for "%s": %r -> set to None', self._attr_name, val)
            return None

        if isinstance(val, str):
            val = val.strip()
            if val == "":
                _LOGGER.warning('Invalid number value for "%s": %r -> set to None', self._attr_name, val)
                return None

        try:
            # Return the CONVERTED float: NumberEntity.native_value must be
            # numeric, and a numeric string like "42.5" should not leak
            # through as a str just because it parses.
            return float(val)
        except (TypeError, ValueError):
            _LOGGER.warning('Invalid numeric number value for "%s": %r -> set to None', self._attr_name, val)
            return None

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        # .get() with sensible fallbacks rather than direct indexing: an
        # unexpected/malformed data point should degrade gracefully
        # (skip this one entity's optional metadata) instead of raising a
        # KeyError that would abort setup for every number entity on this
        # device.
        val, uom = fix_value_and_uom(entity_data.get("value"), entity_data.get("unit"))

        self._config_entry = config_entry
        self._device_id = device_id
        self._attr_has_entity_name = True
        self._attr_name = entity_data.get("friendlyName", _unique_id)
        self._attr_unique_id = get_wemportal_unique_id(
            self._config_entry.entry_id, str(self._device_id), str(_unique_id)
        )
        self._last_updated = None
        self._parameter_id = entity_data.get("ParameterID", _unique_id)
        self._data_key = _unique_id
        self._attr_icon = entity_data.get("icon", "mdi:flash")
        self._attr_native_unit_of_measurement = uom
        self._attr_native_value = self._validated_native_value(val)
        self._attr_native_min_value = entity_data.get("min_value", 0.0)
        self._attr_native_max_value = entity_data.get("max_value", 100.0)
        self._attr_native_step = entity_data.get("step", 1)
        self._attr_should_poll = False
        self._module_index = entity_data.get("ModuleIndex")
        self._module_type = entity_data.get("ModuleType")

        _LOGGER.debug(
            'Init number: %s: "%s" [%s]',
            self._attr_name,
            self._attr_native_value,
            self._attr_native_unit_of_measurement
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self.hass.async_add_executor_job(
            self.coordinator.api.change_value,
            self._device_id,
            self._parameter_id,
            self._module_index,
            self._module_type,
            value,
        )
        self._attr_native_value = value  # type: ignore
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        return build_device_info(self._config_entry.entry_id, self._device_id)

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:
            entity_data = self.coordinator.data[self._device_id][self._data_key]
            val, uom = fix_value_and_uom(entity_data.get("value"), entity_data.get("unit"))

            self._attr_native_value = self._validated_native_value(val)

            # set uom if it references a valid non-trivial unit of measurement
            if uom not in (None, ""):
                self._attr_native_unit_of_measurement = uom

            _LOGGER.debug(
                'Update number: %s: "%s" [%s]',
                self._attr_name,
                self._attr_native_value,
                self._attr_native_unit_of_measurement
            )

        except KeyError:
            self._attr_native_value = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)

        self.async_write_ha_state()

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return uom_to_device_class(self._attr_native_unit_of_measurement)

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attr = {}
        if self._last_updated is not None:
            attr["Last Updated"] = self._last_updated
        return attr
