"""
Sensor platform for wemportal component
"""

from homeassistant.components.sensor import SensorEntity, RestoreSensor
from homeassistant.config_entries import ConfigEntry

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import EntityCategory

from .const import _LOGGER, DOMAIN
from . import get_wemportal_unique_id
from .utils import (fix_value_and_uom, uom_to_device_class, uom_to_state_class)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensor entry setup."""

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities: list[WemPortalSensor] = []
    for device_id, entity_data in coordinator.data.items():
        for unique_id, values in entity_data.items():
            if isinstance(values, int):
                continue
            # Use .get() rather than values["platform"] here: if a single
            # data point is ever missing this key (e.g. an unexpected API
            # response shape), we want to skip just that one entry instead
            # of raising a KeyError that would abort setup for every
            # sensor on this device.
            if values.get("platform") == "sensor":
                entities.append(
                    WemPortalSensor(
                        coordinator, config_entry, device_id, unique_id, values
                    )
                )
    async_add_entities(entities)


class WemPortalSensor(CoordinatorEntity, RestoreSensor):
    """Representation of a WEM Portal Sensor."""

    def _validated_native_value(self, val, uom):
        """Return a Home Assistant-safe native value."""
        effective_uom = uom
        if effective_uom in (None, ""):
            effective_uom = getattr(self, "_attr_native_unit_of_measurement", None)
        # A sensor is "numeric" if it has a real unit OR if it's tagged
        # with a device_class/state_class that requires a numeric state
        # (Home Assistant enforces this - see the entity's own state
        # property). Checking device_class/state_class too, not just
        # uom, closes a gap where fix_value_and_uom() can legitimately
        # return an empty/None uom for a given reading (e.g. a boolean
        # placeholder string with no unit attached) even though the
        # entity itself is declared as a numeric power/energy/etc.
        # sensor - which would otherwise let a non-numeric string like
        # "Off" slip through uncaught and crash entity setup entirely.
        is_numeric_sensor = (
            effective_uom not in (None, "")
            or getattr(self, "_attr_device_class", None) is not None
            or getattr(self, "_attr_state_class", None) is not None
        )

        if val is None:
            _LOGGER.warning('Invalid sensor value for "%s": %r -> set to None', self._attr_name, val)
            return None

        if isinstance(val, str):
            val = val.strip()
            if val == "":
                _LOGGER.warning('Invalid sensor value for "%s": %r -> set to None', self._attr_name, val)
                return None
            if val.startswith("{"):
                return "Programmed"

        if is_numeric_sensor:
            try:
                float(val)
            except (TypeError, ValueError):
                _LOGGER.warning('Invalid numeric sensor value for "%s": %r -> set to None', self._attr_name, val)
                return None

        return val

    def __init__(
        self, coordinator, config_entry: ConfigEntry, device_id, _unique_id, entity_data
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        val, uom = fix_value_and_uom(entity_data["value"], entity_data["unit"])

        self._last_updated = None
        self._config_entry = config_entry
        self._device_id = device_id
        self._attr_has_entity_name = True
        self._attr_name = entity_data.get("friendlyName", _unique_id)
        self._attr_unique_id = get_wemportal_unique_id(
            self._config_entry.entry_id, str(self._device_id), str(_unique_id)
        )
        # .get() with a sensible fallback rather than direct indexing: an
        # unexpected/malformed data point should degrade gracefully (skip
        # this one entity's optional metadata) instead of raising a
        # KeyError that would abort setup for every sensor on this device.
        self._parameter_id = entity_data.get("ParameterID", _unique_id)
        self._data_key = _unique_id
        self._attr_icon = entity_data.get("icon", "mdi:flash")
        self._attr_native_unit_of_measurement = uom
        # Set device_class/state_class BEFORE validating the native value:
        # _validated_native_value() uses them (in addition to uom) to
        # decide whether a numeric value is required, so they must already
        # be in place the first time it runs, not just on later updates.
        self._attr_device_class = entity_data.get("device_class")
        self._attr_state_class = entity_data.get("state_class")
        self._attr_native_value = self._validated_native_value(val, uom)
        self._attr_should_poll = False

        _LOGGER.debug(
            'Init sensor: %s: "%s" [%s]',
            self._attr_name,
            self._attr_native_value,
            self._attr_native_unit_of_measurement
        )

    async def async_added_to_hass(self) -> None:
        """Restore the unit of measurement from the last known state, if needed.

        On a fresh Home Assistant restart, the very first coordinator
        update might briefly report a value without a unit (e.g. "--" from
        the portal). Without this, that would flash the sensor's unit as
        blank/unknown for one cycle. RestoreSensor lets us fall back to
        whatever unit was last recorded, avoiding that.
        """
        await super().async_added_to_hass()
        if self._attr_native_unit_of_measurement in (None, ""):
            last_sensor_data = await self.async_get_last_sensor_data()
            if last_sensor_data is not None and last_sensor_data.native_unit_of_measurement:
                self._attr_native_unit_of_measurement = last_sensor_data.native_unit_of_measurement
                _LOGGER.debug(
                    "Restored unit %s for %s from previous session",
                    self._attr_native_unit_of_measurement,
                    self._attr_name,
                )

    @property
    def device_info(self) -> DeviceInfo:
        """Get device information."""
        info = {
            "identifiers": {
                (DOMAIN, f"{self._config_entry.entry_id}:{str(self._device_id)}")
            },
            "via_device": (DOMAIN, self._config_entry.entry_id),
            "name": str(self._device_id),
            "manufacturer": "Weishaupt",
            "model": "WEM Portal",
        }
        if hasattr(self.coordinator.api, "api_version") and self.coordinator.api.api_version:
            info["sw_version"] = self.coordinator.api.api_version
        return info

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:

            entity_data = self.coordinator.data[self._device_id][self._data_key]
            val, uom = fix_value_and_uom(entity_data["value"], entity_data["unit"])
            self._attr_native_value = self._validated_native_value(val, uom)

            # set uom if it references a valid non-trivial unit of measurement
            if uom not in (None, ""):
                self._attr_native_unit_of_measurement = uom

            _LOGGER.debug(
                'Update sensor: %s: "%s" [%s]', 
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
    def entity_category(self):
        """Return the entity category."""
        if any(x in self._attr_unique_id for x in ["ConnectionStatus", "HasErrors", "ErrorMessages"]):
            return EntityCategory.DIAGNOSTIC
        return None

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        if self._attr_device_class is not None:
            return self._attr_device_class
        return uom_to_device_class(self._attr_native_unit_of_measurement)

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        if self._attr_state_class is not None:
            return self._attr_state_class
        return uom_to_state_class(self._attr_native_unit_of_measurement)

    @property
    def extra_state_attributes(self):
        """Return the state attributes of this device."""
        attr = {}
        if self._last_updated is not None:
            attr["Last Updated"] = self._last_updated
            
        try:
            entity_data = self.coordinator.data[self._device_id][self._data_key]
            if "CircuitTimesDay" in entity_data:
                attr["CircuitTimesDay"] = entity_data["CircuitTimesDay"]
            if "PossibleValues" in entity_data:
                attr["PossibleValues"] = entity_data["PossibleValues"]
            if isinstance(entity_data.get("value"), str) and entity_data["value"].startswith("{"):
                attr["Raw_JSON"] = entity_data["value"]
        except KeyError:
            pass
            
        return attr
