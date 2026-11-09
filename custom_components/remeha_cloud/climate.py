"""Climate zones (eTwist / room thermostat zones)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemehaConfigEntry
from .entity import RemehaClimateZoneEntity, iter_zones

MODE_SCHEDULE = "Scheduling"
MODE_OVERRIDE = "TemporaryOverride"
MODE_MANUAL = "Manual"
MODE_OFF = "FrostProtection"

REMEHA_TO_HVAC = {
    MODE_SCHEDULE: HVACMode.AUTO,
    MODE_OVERRIDE: HVACMode.AUTO,
    MODE_MANUAL: HVACMode.HEAT,
    MODE_OFF: HVACMode.OFF,
}
DEMAND_TO_ACTION = {
    "ProducingHeat": HVACAction.HEATING,
    "RequestingHeat": HVACAction.HEATING,
    "ProducingCold": HVACAction.COOLING,
    "RequestingCold": HVACAction.COOLING,
    "Idle": HVACAction.IDLE,
}
# HA 2026.11 introduced native_* temperature members; older versions only know the old names.
NATIVE = hasattr(ClimateEntity, "native_temperature_unit")

PROGRAMS = {1: "program_1", 2: "program_2", 3: "program_3"}
PROGRAM_IDS = {v: k for k, v in PROGRAMS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemehaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        RemehaClimate(coordinator, aid, zid)
        for aid, zid, _ in iter_zones(coordinator, "climateZones")
    )


class RemehaClimate(RemehaClimateZoneEntity, ClimateEntity):
    """A Remeha climate zone."""

    _attr_name = None
    _attr_translation_key = "zone"
    if NATIVE:
        _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    else:
        _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = list(PROGRAMS.values())
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator, appliance_id: str, zone_id: str) -> None:
        super().__init__(coordinator, appliance_id, zone_id, "climate")

    # ---- state
    def _room_temperature(self) -> float | None:
        return self.zone.get("roomTemperature")

    def _setpoint(self) -> float | None:
        return None if self.hvac_mode == HVACMode.OFF else self.zone.get("setPoint")

    if NATIVE:
        native_current_temperature = property(_room_temperature)
        native_target_temperature = property(_setpoint)
    else:
        current_temperature = property(_room_temperature)
        target_temperature = property(_setpoint)

    @property
    def min_temp(self) -> float:
        return self.zone.get("setPointMin") or 5.0

    @property
    def max_temp(self) -> float:
        return self.zone.get("setPointMax") or 30.0

    @property
    def hvac_mode(self) -> HVACMode | None:
        return REMEHA_TO_HVAC.get(self.zone.get("zoneMode"))

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return DEMAND_TO_ACTION.get(self.zone.get("activeComfortDemand"))

    @property
    def preset_mode(self) -> str | None:
        if self.zone.get("zoneMode") not in (MODE_SCHEDULE, MODE_OVERRIDE):
            return None
        return PROGRAMS.get(self.zone.get("activeHeatingClimateTimeProgramNumber"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        attrs = {
            "zone_mode": zone.get("zoneMode"),
            "next_setpoint": zone.get("nextSetpoint"),
            "next_switch_time": zone.get("nextSwitchTime"),
            "schedule_setpoint": zone.get("currentScheduleSetPoint"),
            "fireplace_mode": zone.get("firePlaceModeActive"),
        }
        if zone.get("zoneMode") == MODE_OVERRIDE:
            attrs["override_end_time"] = (zone.get("temporaryOverride") or {}).get("endTime")
        return attrs

    def _program(self) -> int:
        return self.zone.get("activeHeatingClimateTimeProgramNumber") or 1

    # ---- commands
    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None and ATTR_TEMPERATURE not in kwargs:
            await self.async_set_hvac_mode(hvac_mode)
            return
        temp = float(kwargs[ATTR_TEMPERATURE])
        target_mode = kwargs.get(ATTR_HVAC_MODE, self.hvac_mode)
        client = self.coordinator.client
        if target_mode == HVACMode.AUTO:
            self.zone.update(setPoint=temp, zoneMode=MODE_OVERRIDE)
            await self._async_command(client.async_zone_temporary_override(self._zone_id, temp))
        else:  # HEAT or OFF -> manual setpoint (also turns the zone on)
            self.zone.update(setPoint=temp, zoneMode=MODE_MANUAL)
            await self._async_command(client.async_zone_manual(self._zone_id, temp))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        client = self.coordinator.client
        if hvac_mode == HVACMode.AUTO:
            self.zone["zoneMode"] = MODE_SCHEDULE
            await self._async_command(client.async_zone_schedule(self._zone_id, self._program()))
        elif hvac_mode == HVACMode.HEAT:
            temp = self.zone.get("setPoint") or self.zone.get("currentScheduleSetPoint") or 20.0
            self.zone["zoneMode"] = MODE_MANUAL
            await self._async_command(client.async_zone_manual(self._zone_id, temp))
        elif hvac_mode == HVACMode.OFF:
            self.zone["zoneMode"] = MODE_OFF
            await self._async_command(client.async_zone_off(self._zone_id))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        program = PROGRAM_IDS[preset_mode]
        client = self.coordinator.client
        was_scheduling = self.zone.get("zoneMode") == MODE_SCHEDULE
        self.zone.update(activeHeatingClimateTimeProgramNumber=program, zoneMode=MODE_SCHEDULE)
        await self._async_command(client.async_zone_activate_program(self._zone_id, program))
        if not was_scheduling:
            await self._async_command(client.async_zone_schedule(self._zone_id, program))

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
