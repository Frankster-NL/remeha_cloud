"""Domestic hot water zones."""

from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemehaConfigEntry
from .api import DHW_MODE_COMFORT, DHW_MODE_ECO, DHW_MODE_SCHEDULE
from .entity import RemehaHotWaterZoneEntity, iter_zones

NATIVE = hasattr(WaterHeaterEntity, "native_temperature_unit")

OP_ECO = "eco"
OP_COMFORT = "comfort"
OP_SCHEDULE = "schedule"
OP_TO_API = {OP_ECO: DHW_MODE_ECO, OP_COMFORT: DHW_MODE_COMFORT, OP_SCHEDULE: DHW_MODE_SCHEDULE}


def _operation(dhw_zone_mode: str | None) -> str:
    """Map dhwZoneMode to an operation. API docs only show 'Off' (= eco/anti-frost)."""
    mode = (dhw_zone_mode or "").lower()
    if "comfort" in mode:
        return OP_COMFORT
    if "sched" in mode:
        return OP_SCHEDULE
    return OP_ECO


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemehaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        RemehaWaterHeater(coordinator, aid, zid)
        for aid, zid, _ in iter_zones(coordinator, "hotWaterZones")
    )


class RemehaWaterHeater(RemehaHotWaterZoneEntity, WaterHeaterEntity):
    """A Remeha DHW zone."""

    _attr_name = None
    _attr_translation_key = "dhw"
    if NATIVE:
        _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    else:
        _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_operation_list = [OP_ECO, OP_COMFORT, OP_SCHEDULE]
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
    )

    def __init__(self, coordinator, appliance_id: str, zone_id: str) -> None:
        super().__init__(coordinator, appliance_id, zone_id, "water_heater")

    @property
    def current_operation(self) -> str:
        return _operation(self.zone.get("dhwZoneMode"))

    def _dhw_temperature(self) -> float | None:
        return self.zone.get("dhwTemperature")

    def _target(self) -> float | None:
        return self.zone.get("targetSetpoint")

    if NATIVE:
        native_current_temperature = property(_dhw_temperature)
        native_target_temperature = property(_target)
    else:
        current_temperature = property(_dhw_temperature)
        target_temperature = property(_target)

    @property
    def _ranges(self) -> dict[str, float]:
        return self.zone.get("setPointRanges") or {}

    @property
    def min_temp(self) -> float:
        key = "reducedSetpointMin" if self.current_operation == OP_ECO else "comfortSetpointMin"
        return self._ranges.get(key) or self.zone.get("setPointMin") or 10.0

    @property
    def max_temp(self) -> float:
        key = "reducedSetpointMax" if self.current_operation == OP_ECO else "comfortSetpointMax"
        return self._ranges.get(key) or self.zone.get("setPointMax") or 65.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        z = self.zone
        return {
            "dhw_zone_mode": z.get("dhwZoneMode"),
            "dhw_status": z.get("dhwStatus"),
            "comfort_setpoint": z.get("comfortSetPoint"),
            "reduced_setpoint": z.get("reducedSetpoint"),
            "next_switch_activity": z.get("nextSwitchActivity"),
            "next_switch_time": z.get("nextSwitchTime"),
            "boost_end_time": z.get("boostModeEndTime"),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Eco -> reduced setpoint, otherwise comfort setpoint."""
        temp = float(kwargs[ATTR_TEMPERATURE])
        client = self.coordinator.client
        self.zone["targetSetpoint"] = temp
        if self.current_operation == OP_ECO:
            self.zone["reducedSetpoint"] = temp
            await self._async_command(client.async_dhw_reduced_setpoint(self._zone_id, temp))
        else:
            self.zone["comfortSetPoint"] = temp
            await self._async_command(client.async_dhw_comfort_setpoint(self._zone_id, temp))

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        self.zone["dhwZoneMode"] = {
            OP_ECO: "AntiFrost", OP_COMFORT: "ContinuousComfort", OP_SCHEDULE: "Scheduling"
        }[operation_mode]
        await self._async_command(
            self.coordinator.client.async_dhw_mode(self._zone_id, OP_TO_API[operation_mode])
        )
