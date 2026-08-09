"""
Number platform for wemportal component
"""

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_EXPERT_WRITE, DOMAIN
from .entity import WemPortalEntity
from .utils import fix_value_and_unit, unit_to_device_class

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Number entry setup."""

    coordinator = config_entry.runtime_data.coordinator
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
    #
    # The import sits INSIDE the option check, not merely inside the
    # function. expert_writer pulls curl_cffi at module level - 114 ms cold,
    # measured - and this ran on every entry setup regardless of the option,
    # on the event loop, while three comments claimed the opposite.
    #
    # This gate alone was not enough: config_flow imported expert_writer at
    # module level, and Home Assistant loads config_flow during a normal
    # entry setup, so curl_cffi arrived anyway. That is why the pure option
    # helpers now live in expert_options.py - see the structural guard in
    # tests/test_security.py.
    expert_entities = []
    if config_entry.options.get(CONF_EXPERT_WRITE, False):
        from .expert_writer import create_expert_number_entities

        expert_entities = create_expert_number_entities(config_entry)
        if expert_entities:
            _async_migrate_expert_unique_ids(hass, config_entry, expert_entities)
            async_add_entities(expert_entities)
            # Expose them to the optional hourly auto-poll (set up in
            # __init__), which reads all configured ids in one shared session
            # and pushes the values back into these entities.
            config_entry.runtime_data.expert.attach_entities(expert_entities)

    # AFTER the migration above: the migration renames a configured slot's
    # old raw-id registry entry onto its digest id, and running the cleanup
    # first would delete exactly the entry the migration exists to preserve.
    _async_drop_ghost_expert_entities(
        hass, config_entry, {entity.unique_id for entity in expert_entities}
    )


def _async_migrate_expert_unique_ids(hass, config_entry, expert_entities) -> None:
    """Migrate expert entities from raw-entityvalue unique_ids to digests.

    Older versions embedded the raw, installation-specific entityvalue in
    the unique_id (persisted in .storage/core.entity_registry); it is now a
    SHA-256 digest (see expert_writer.entityvalue_digest). Updating the registry
    entry in place preserves the entity_id, history and restored state.
    Best-effort: a failure only means the entity is re-created under the
    new unique_id instead of migrated.
    """
    registry = entity_registry.async_get(hass)
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
            _LOGGER.debug(
                "Skipping expert unique_id migration for %s: target exists.", entity_id
            )
            continue
        try:
            registry.async_update_entity(entity_id, new_unique_id=entity.unique_id)
            _LOGGER.info(
                "Migrated expert entity %s to digest-based unique_id.", entity_id
            )
        except ValueError as exc:
            _LOGGER.warning("Could not migrate expert entity %s: %s", entity_id, exc)


def _async_drop_ghost_expert_entities(hass, config_entry, expected_unique_ids) -> None:
    """Drop registry entries of expert slots this entry no longer offers.

    The unique_id of a cleared slot (or of every slot, once the expert option
    is off) is never registered again, so its entry sat in the dashboard as a
    permanently unavailable number - one more per cleared slot. Only entries
    under this entry's own expert unique_id prefix are candidates; everything
    else this integration registers stays untouched.

    Deleting rather than keeping is safe for history: the digest unique_id is
    stable, so re-configuring the slot re-creates the entity under its old
    entity_id, which is what the recorder keys history by.
    """
    registry = entity_registry.async_get(hass)
    expert_prefix = f"{config_entry.entry_id}:expert:"
    for registry_entry in entity_registry.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        is_ghost = (
            registry_entry.platform == DOMAIN
            and registry_entry.unique_id.startswith(expert_prefix)
            and registry_entry.unique_id not in expected_unique_ids
        )
        if is_ghost:
            _LOGGER.info(
                "Removing expert entity %s: its slot is no longer configured.",
                registry_entry.entity_id,
            )
            registry.async_remove(registry_entry.entity_id)


class WemPortalNumber(WemPortalEntity, NumberEntity):
    """Representation of a WEM Portal number."""

    def _validated_native_value(self, value):
        """Return a Home Assistant-safe native value.

        Unlike sensor.py (where a value can legitimately be text when no
        device_class is set), a NumberEntity's native_value is ALWAYS
        required to be numeric - there is no valid "text" state for a
        number input. So this always attempts the numeric conversion,
        rather than only doing so when a unit happens to be present this
        cycle (which previously could let a non-numeric string slip
        through uncaught whenever the per-cycle unit was empty).
        """
        # No reading this cycle is expected, not invalid. The portal regularly
        # answers nothing for a parameter, and _clear_unanswered deliberately
        # blanks one the portal left out so a stale reading is not published as
        # current - so warning about it reported the integration's own
        # bookkeeping as a fault. Same rule sensor.py states for its own
        # readings. A value that is present but unusable still warns below.
        if value is None:
            _LOGGER.debug('No value for "%s" this cycle -> unknown', self._attr_name)
            return None

        if isinstance(value, str):
            value = value.strip()
            if value == "":
                _LOGGER.warning(
                    'Invalid number value for "%s": %r -> set to None',
                    self._attr_name,
                    value,
                )
                return None

        try:
            # Return the CONVERTED float: NumberEntity.native_value must be
            # numeric, and a numeric string like "42.5" should not leak
            # through as a str just because it parses.
            return float(value)
        except (TypeError, ValueError):
            _LOGGER.warning(
                'Invalid numeric number value for "%s": %r -> set to None',
                self._attr_name,
                value,
            )
            return None

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, device_id, _unique_id, entity_data)

        # .get() with sensible fallbacks rather than direct indexing: an
        # unexpected/malformed data point should degrade gracefully
        # (skip this one entity's optional metadata) instead of raising a
        # KeyError that would abort setup for every number entity on this
        # device.
        value, unit = fix_value_and_unit(
            entity_data.get("value"), entity_data.get("unit")
        )

        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = self._validated_native_value(value)
        self._attr_native_min_value = entity_data.get("min_value", 0.0)
        self._attr_native_max_value = entity_data.get("max_value", 100.0)
        self._attr_native_step = entity_data.get("step", 1)

        _LOGGER.debug(
            'Init number: %s: "%s" [%s]',
            self._attr_name,
            self._attr_native_value,
            self._attr_native_unit_of_measurement,
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self.async_write_parameter(value)
        self._attr_native_value = value  # type: ignore
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:
            entity_data = self.coordinator.data[self._device_id][self._data_key]
            value, unit = fix_value_and_unit(
                entity_data.get("value"), entity_data.get("unit")
            )

            # Metadata BEFORE the value. Rediscovery replaces the parameter
            # descriptions once a day and the mapper delivers fresh bounds
            # with every cycle - published only at construction, a value the
            # device newly accepts was refused by Home Assistant's own range
            # check before this integration was ever asked.
            if "min_value" in entity_data:
                self._attr_native_min_value = entity_data["min_value"]
            if "max_value" in entity_data:
                self._attr_native_max_value = entity_data["max_value"]
            if "step" in entity_data:
                self._attr_native_step = entity_data["step"]

            self._attr_native_value = self._validated_native_value(value)

            # set unit if it references a valid non-trivial unit of measurement
            if unit not in (None, ""):
                self._attr_native_unit_of_measurement = unit

            _LOGGER.debug(
                'Update number: %s: "%s" [%s]',
                self._attr_name,
                self._attr_native_value,
                self._attr_native_unit_of_measurement,
            )

        except KeyError:
            self._attr_native_value = None
            _LOGGER.warning("Can't find %s", self._attr_unique_id)
            _LOGGER.debug("Sensor data %s", self.coordinator.data)

        self.async_write_ha_state()

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return unit_to_device_class(self._attr_native_unit_of_measurement)

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attributes = {}
        if self._last_updated is not None:
            attributes["Last Updated"] = self._last_updated
        return attributes
