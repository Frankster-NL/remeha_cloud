"""Binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemehaConfigEntry
from .entity import RemehaApplianceEntity, RemehaClimateZoneEntity, iter_zones


@dataclass(frozen=True, kw_only=True)
class RemehaBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True


APPLIANCE_BINARY = (
    RemehaBinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda a: a.get("applianceOnline"),
    ),
    RemehaBinaryDescription(
        key="water_pressure_problem",
        translation_key="water_pressure_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda a: None if a.get("waterPressureOK") is None else not a["waterPressureOK"],
    ),
    RemehaBinaryDescription(
        key="holiday_active",
        translation_key="holiday_active",
        value_fn=lambda a: (a.get("holidaySchedule") or {}).get("active"),
    ),
)

ZONE_BINARY = (
    RemehaBinaryDescription(
        key="preheat_active",
        translation_key="preheat_active",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda z: (z.get("preHeat") or {}).get("active"),
        exists_fn=lambda z: bool((z.get("preHeat") or {}).get("enabled")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemehaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        RemehaApplianceBinary(coordinator, aid, d)
        for aid, appliance in coordinator.data.appliances.items()
        for d in APPLIANCE_BINARY if d.exists_fn(appliance)
    ]
    entities += [
        RemehaZoneBinary(coordinator, aid, zid, d)
        for aid, zid, zone in iter_zones(coordinator, "climateZones")
        for d in ZONE_BINARY if d.exists_fn(zone)
    ]
    async_add_entities(entities)


class RemehaApplianceBinary(RemehaApplianceEntity, BinarySensorEntity):
    entity_description: RemehaBinaryDescription

    def __init__(self, coordinator, appliance_id, description) -> None:
        super().__init__(coordinator, appliance_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.appliance)


class RemehaZoneBinary(RemehaClimateZoneEntity, BinarySensorEntity):
    entity_description: RemehaBinaryDescription

    def __init__(self, coordinator, appliance_id, zone_id, description) -> None:
        super().__init__(coordinator, appliance_id, zone_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.zone)
