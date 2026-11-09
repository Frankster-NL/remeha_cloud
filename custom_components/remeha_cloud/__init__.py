"""Remeha Home (cloud) integration."""

from __future__ import annotations

import inspect
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RemehaClient
from .const import CONF_TOKEN, DOMAIN
from .coordinator import RemehaCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]

type RemehaConfigEntry = ConfigEntry[RemehaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RemehaConfigEntry) -> bool:
    """Set up Remeha Home from a config entry."""

    @callback
    def _store_token(token: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: token}
        )

    client = RemehaClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        token=entry.data.get(CONF_TOKEN),
        on_token_update=_store_token,
    )
    coordinator = RemehaCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    _async_register_devices(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RemehaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


# Newer HA takes via_device_id; via_device (identifier tuple) stops working in 2027.8.
_VIA_DEVICE_ID = "via_device_id" in inspect.signature(
    dr.DeviceRegistry.async_get_or_create
).parameters


@callback
def _async_register_devices(
    hass: HomeAssistant, entry: RemehaConfigEntry, coordinator: RemehaCoordinator
) -> None:
    """Create appliance devices first, then zone devices linked to them."""
    registry = dr.async_get(hass)
    data = coordinator.data
    for appliance_id, appliance in data.appliances.items():
        tech = data.technical.get(appliance_id, {})
        gateway = (tech.get("internetConnectedGateways") or [{}])[0]
        appliance_device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, appliance_id)},
            manufacturer="Remeha",
            name=f"{appliance.get('houseName', 'Remeha')} {appliance.get('applianceType', '')}".strip(),
            model=tech.get("applianceName") or appliance.get("applianceType"),
            hw_version=gateway.get("hardwareVersion"),
            sw_version=gateway.get("softwareVersion"),
        )
        via = (
            {"via_device_id": appliance_device.id}
            if _VIA_DEVICE_ID
            else {"via_device": (DOMAIN, appliance_id)}
        )
        zones = [
            (z["climateZoneId"], z.get("name"), "Climate zone")
            for z in appliance.get("climateZones") or []
        ] + [
            (z["hotWaterZoneId"], z.get("name"), "Hot water zone")
            for z in appliance.get("hotWaterZones") or []
        ]
        for zone_id, name, model in zones:
            registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, zone_id)},
                manufacturer="Remeha",
                name=name or zone_id,
                model=model,
                **via,
            )
