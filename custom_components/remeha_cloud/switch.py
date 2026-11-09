"""Fireplace mode switch per climate zone."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemehaConfigEntry
from .entity import RemehaClimateZoneEntity, iter_zones


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemehaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        RemehaFireplaceSwitch(coordinator, aid, zid)
        for aid, zid, zone in iter_zones(coordinator, "climateZones")
        if zone.get("capabilityFirePlaceMode")
    )


class RemehaFireplaceSwitch(RemehaClimateZoneEntity, SwitchEntity):
    """Fireplace mode: zone ignores its own room sensor while a stove heats the room."""

    _attr_translation_key = "fireplace_mode"

    def __init__(self, coordinator, appliance_id: str, zone_id: str) -> None:
        super().__init__(coordinator, appliance_id, zone_id, "fireplace_mode")

    @property
    def is_on(self) -> bool | None:
        return self.zone.get("firePlaceModeActive")

    async def _set(self, active: bool) -> None:
        self.zone["firePlaceModeActive"] = active
        await self._async_command(
            self.coordinator.client.async_zone_fireplace(self._zone_id, active)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
