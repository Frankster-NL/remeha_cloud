"""Base entities for Remeha Home (cloud)."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RemehaAuthError, RemehaError
from .const import DOMAIN
from .coordinator import RemehaCoordinator


class RemehaEntity(CoordinatorEntity[RemehaCoordinator]):
    """Common base."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RemehaCoordinator, appliance_id: str) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id

    @property
    def appliance(self) -> dict[str, Any]:
        return self.coordinator.data.appliances.get(self._appliance_id, {})


    async def _async_command(self, call: Awaitable[None]) -> None:
        """Run an API command, write optimistic state, schedule a refresh."""
        try:
            await call
        except RemehaAuthError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Remeha authentication failed: {err}") from err
        except RemehaError as err:
            raise HomeAssistantError(f"Remeha API error: {err}") from err
        self.async_write_ha_state()
        self.coordinator.async_refresh_soon()


class RemehaApplianceEntity(RemehaEntity):
    """Entity attached to the appliance (boiler / heat pump) device."""

    def __init__(self, coordinator: RemehaCoordinator, appliance_id: str, key: str) -> None:
        super().__init__(coordinator, appliance_id)
        self._attr_unique_id = f"{appliance_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, appliance_id)})

    @property
    def available(self) -> bool:
        return super().available and self._appliance_id in self.coordinator.data.appliances


class RemehaZoneEntity(RemehaEntity):
    """Entity attached to a climate zone or hot water zone device."""

    _zone_store: str  # "climate_zones" | "hot_water_zones"

    def __init__(
        self, coordinator: RemehaCoordinator, appliance_id: str, zone_id: str, key: str
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{zone_id}_{key}"
        # Devices (incl. the zone -> appliance link) are registered in __init__.py.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, zone_id)})

    @property
    def zone(self) -> dict[str, Any]:
        return getattr(self.coordinator.data, self._zone_store).get(self._zone_id, {})

    @property
    def available(self) -> bool:
        return super().available and self._zone_id in getattr(
            self.coordinator.data, self._zone_store
        )


class RemehaClimateZoneEntity(RemehaZoneEntity):
    _zone_store = "climate_zones"


class RemehaHotWaterZoneEntity(RemehaZoneEntity):
    _zone_store = "hot_water_zones"


def iter_zones(coordinator: RemehaCoordinator, store: str):
    """Yield (appliance_id, zone_id, zone) for 'climateZones' or 'hotWaterZones'."""
    for appliance_id, appliance in coordinator.data.appliances.items():
        id_key = "climateZoneId" if store == "climateZones" else "hotWaterZoneId"
        for zone in appliance.get(store) or []:
            yield appliance_id, zone[id_key], zone
