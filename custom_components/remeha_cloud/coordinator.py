"""Data coordinator for Remeha Home (cloud)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import RemehaAuthError, RemehaClient, RemehaError
from .const import (
    DOMAIN,
    ENERGY_INTERVAL,
    ENERGY_KEYS,
    REFRESH_AFTER_COMMAND,
    SCAN_INTERVAL,
    ZONE_CONFIG_INTERVAL,
)

if TYPE_CHECKING:
    from . import RemehaConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class RemehaData:
    """Indexed snapshot of the dashboard."""

    appliances: dict[str, dict[str, Any]] = field(default_factory=dict)
    climate_zones: dict[str, dict[str, Any]] = field(default_factory=dict)
    hot_water_zones: dict[str, dict[str, Any]] = field(default_factory=dict)
    technical: dict[str, dict[str, Any]] = field(default_factory=dict)
    energy: dict[str, EnergyDay] = field(default_factory=dict)
    zone_config: dict[str, ZoneConfig] = field(default_factory=dict)


@dataclass
class EnergyDay:
    """Energy of one completed day (Remeha publishes a day's data the next day)."""

    day: date
    totals: dict[str, float]
    producers: dict[str, dict[str, Any]]  # producerType -> statistics row


@dataclass
class ZoneConfig:
    """Read-only zone configuration."""

    heating_curve: dict[str, Any] = field(default_factory=dict)
    activities: list[dict[str, Any]] = field(default_factory=list)


class RemehaCoordinator(DataUpdateCoordinator[RemehaData]):
    """Polls /homes/dashboard; energy and technical details less often."""

    config_entry: RemehaConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: RemehaConfigEntry, client: RemehaClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self._technical: dict[str, dict[str, Any]] = {}
        self._energy: dict[str, EnergyDay] = {}
        self._energy_updated: dict[str, datetime] = {}
        self._zone_config: dict[str, ZoneConfig] = {}
        self._zone_config_updated: datetime | None = None
        self._cancel_delayed_refresh = None

    async def _async_update_data(self) -> RemehaData:
        try:
            dashboard = await self.client.async_get_dashboard()
        except RemehaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RemehaError as err:
            raise UpdateFailed(str(err)) from err

        data = RemehaData(
            technical=self._technical, energy=self._energy, zone_config=self._zone_config
        )
        for appliance in dashboard.get("appliances") or []:
            aid = appliance["applianceId"]
            data.appliances[aid] = appliance
            for zone in appliance.get("climateZones") or []:
                data.climate_zones[zone["climateZoneId"]] = zone
            for zone in appliance.get("hotWaterZones") or []:
                data.hot_water_zones[zone["hotWaterZoneId"]] = zone

            if aid not in self._technical:
                try:
                    self._technical[aid] = await self.client.async_get_technical_details(aid)
                except RemehaError as err:
                    _LOGGER.debug("No technical details for %s: %s", aid, err)
                    self._technical[aid] = {}

            if appliance.get("capabilityEnergyConsumption"):
                await self._async_update_energy(aid)

        await self._async_update_zone_config(data)
        return data

    async def _async_update_zone_config(self, data: RemehaData) -> None:
        now = dt_util.now()
        if self._zone_config_updated and now - self._zone_config_updated < ZONE_CONFIG_INTERVAL:
            return
        for zone_id in data.climate_zones:
            config = self._zone_config.setdefault(zone_id, ZoneConfig())
            try:
                config.heating_curve = await self.client.async_get_heating_curve(zone_id)
            except RemehaAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except RemehaError as err:
                _LOGGER.debug("No heating curve for zone %s: %s", zone_id, err)
            try:
                config.activities = await self.client.async_get_activities(zone_id)
            except RemehaAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except RemehaError as err:
                _LOGGER.debug("No activities for zone %s: %s", zone_id, err)
        self._zone_config_updated = now

    async def _async_update_energy(self, appliance_id: str) -> None:
        """Fetch yesterday's (complete) daily energy, at most once an hour."""
        now = dt_util.now()
        yesterday = now.date() - timedelta(days=1)
        last = self._energy_updated.get(appliance_id)
        current = self._energy.get(appliance_id)
        if last is not None:
            have_yesterday = current is not None and current.day == yesterday
            # Up to date: once an hour. Waiting for yesterday's data: every 15 min.
            if now - last < (ENERGY_INTERVAL if have_yesterday else ENERGY_INTERVAL / 4):
                return

        # Two days, so there is always a complete day even before yesterday is published.
        start = datetime.combine(yesterday - timedelta(days=1), datetime.min.time())
        end = datetime.combine(yesterday, datetime.min.time()) + timedelta(
            hours=23, minutes=59, seconds=59
        )
        self._energy_updated[appliance_id] = now  # also throttles retries after errors
        try:
            result = await self.client.async_get_energy_consumption(appliance_id, start, end)
        except RemehaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RemehaError as err:
            _LOGGER.warning("Energy data for %s unavailable: %s", appliance_id, err)
            return

        reported = [
            r for r in result.get("data") or []
            if r.get("isDataReported", True)
            and len(str(r.get("timeStamp") or "")) >= 10
            and str(r["timeStamp"])[:10] <= yesterday.isoformat()
        ]
        if not reported:
            _LOGGER.debug("No reported energy days for %s yet", appliance_id)
            return
        row = max(reported, key=lambda r: str(r.get("timeStamp")))
        day = date.fromisoformat(str(row["timeStamp"])[:10])
        if current is not None and current.day == day:
            return  # nothing new

        producers = {
            p["producerType"]: p
            for p in (row.get("producerPerformanceStatistics") or {}).get("producers") or []
            if p.get("producerType")
        }
        self._energy[appliance_id] = EnergyDay(
            day=day,
            totals={key: float(row.get(key) or 0) for key in ENERGY_KEYS},
            producers=producers,
        )

    @callback
    def async_refresh_soon(self) -> None:
        """Refresh a few seconds after a command (cloud lags behind)."""
        if self._cancel_delayed_refresh:
            self._cancel_delayed_refresh()

        async def _refresh(_now: datetime) -> None:
            self._cancel_delayed_refresh = None
            await self.async_request_refresh()

        self._cancel_delayed_refresh = async_call_later(
            self.hass, REFRESH_AFTER_COMMAND, HassJob(_refresh, cancel_on_shutdown=True)
        )
