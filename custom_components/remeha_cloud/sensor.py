"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RemehaConfigEntry
from .const import PRODUCER_GAS_BOILER, PRODUCER_HEAT_PUMP
from .coordinator import EnergyDay
from .entity import (
    RemehaApplianceEntity,
    RemehaClimateZoneEntity,
    RemehaHotWaterZoneEntity,
    iter_zones,
)


@dataclass(frozen=True, kw_only=True)
class RemehaSensorDescription(SensorEntityDescription):
    """value_fn gets the appliance/zone dict."""

    value_fn: Callable[[dict[str, Any]], Any]
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True


@dataclass(frozen=True, kw_only=True)
class RemehaEnergyDescription(SensorEntityDescription):
    """value_fn gets (EnergyDay, appliance); exists_fn the same at setup."""

    value_fn: Callable[[EnergyDay, dict[str, Any]], float | None]
    exists_fn: Callable[[EnergyDay | None, dict[str, Any]], bool] = lambda e, a: True
    daily_total: bool = True  # state_class TOTAL with last_reset = start of that day


def _producer(energy: EnergyDay | None, producer: str, key: str) -> float | None:
    if energy is None or (row := energy.producers.get(producer)) is None:
        return None
    value = row.get(key)
    return None if value is None else float(value)


def _has_producer(producer: str) -> Callable[[EnergyDay | None, dict[str, Any]], bool]:
    return lambda e, a: e is not None and producer in e.producers


def _cop(energy: EnergyDay, _: dict[str, Any]) -> float | None:
    used = _producer(energy, PRODUCER_HEAT_PUMP, "energyConsumptionTotal")
    made = _producer(energy, PRODUCER_HEAT_PUMP, "energyProductionTotal")
    return round(made / used, 2) if used and made is not None else None


def _gas_m3(energy: EnergyDay, appliance: dict[str, Any]) -> float | None:
    kwh = _producer(energy, PRODUCER_GAS_BOILER, "energyConsumptionTotal")
    cv = appliance.get("gasCalorificValue")
    return round(kwh / cv, 3) if kwh is not None and cv else None


ENERGY_COMMON = {
    "device_class": SensorDeviceClass.ENERGY,
    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    "state_class": SensorStateClass.TOTAL,
    "suggested_display_precision": 1,
}
_has_cooling = lambda e, a: bool(a.get("capabilityCooling"))  # noqa: E731

ENERGY_SENSORS: tuple[RemehaEnergyDescription, ...] = (
    *(
        RemehaEnergyDescription(
            key=f"{key}_yesterday",
            translation_key=f"{key}_yesterday",
            value_fn=lambda e, a, k=api_key: e.totals.get(k),
            exists_fn=_has_cooling if "cooling" in key else (lambda e, a: True),
            **ENERGY_COMMON,
        )
        for key, api_key in (
            ("heating_energy_consumed", "heatingEnergyConsumed"),
            ("heating_energy_delivered", "heatingEnergyDelivered"),
            ("hot_water_energy_consumed", "hotWaterEnergyConsumed"),
            ("hot_water_energy_delivered", "hotWaterEnergyDelivered"),
            ("cooling_energy_consumed", "coolingEnergyConsumed"),
            ("cooling_energy_delivered", "coolingEnergyDelivered"),
        )
    ),
    RemehaEnergyDescription(
        key="heat_pump_electricity_yesterday",
        translation_key="heat_pump_electricity_yesterday",
        value_fn=lambda e, a: _producer(e, PRODUCER_HEAT_PUMP, "energyConsumptionTotal"),
        exists_fn=_has_producer(PRODUCER_HEAT_PUMP),
        **ENERGY_COMMON,
    ),
    RemehaEnergyDescription(
        key="heat_pump_heat_yesterday",
        translation_key="heat_pump_heat_yesterday",
        value_fn=lambda e, a: _producer(e, PRODUCER_HEAT_PUMP, "energyProductionTotal"),
        exists_fn=_has_producer(PRODUCER_HEAT_PUMP),
        **ENERGY_COMMON,
    ),
    RemehaEnergyDescription(
        key="heat_pump_cop_yesterday",
        translation_key="heat_pump_cop_yesterday",
        value_fn=_cop,
        exists_fn=_has_producer(PRODUCER_HEAT_PUMP),
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        daily_total=False,
    ),
    RemehaEnergyDescription(
        key="heat_pump_seasonal_efficiency",
        translation_key="heat_pump_seasonal_efficiency",
        value_fn=lambda e, a: _producer(e, PRODUCER_HEAT_PUMP, "seasonalEfficiency"),
        exists_fn=_has_producer(PRODUCER_HEAT_PUMP),
        state_class=SensorStateClass.MEASUREMENT,
        daily_total=False,
    ),
    RemehaEnergyDescription(
        key="boiler_gas_energy_yesterday",
        translation_key="boiler_gas_energy_yesterday",
        value_fn=lambda e, a: _producer(e, PRODUCER_GAS_BOILER, "energyConsumptionTotal"),
        exists_fn=_has_producer(PRODUCER_GAS_BOILER),
        **ENERGY_COMMON,
    ),
    RemehaEnergyDescription(
        key="boiler_gas_volume_yesterday",
        translation_key="boiler_gas_volume_yesterday",
        value_fn=_gas_m3,
        exists_fn=lambda e, a: _has_producer(PRODUCER_GAS_BOILER)(e, a)
        and bool(a.get("gasCalorificValue")),
        device_class=SensorDeviceClass.GAS,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
    ),
)


@dataclass(frozen=True, kw_only=True)
class RemehaZoneConfigDescription(SensorEntityDescription):
    """Read-only heating-curve value."""

    curve_key: str


ZONE_CONFIG_COMMON = {"entity_category": EntityCategory.DIAGNOSTIC}
HEATING_CURVE_SENSORS = (
    RemehaZoneConfigDescription(
        key="heating_curve_slope", translation_key="heating_curve_slope",
        curve_key="slope", suggested_display_precision=1, **ZONE_CONFIG_COMMON,
    ),
    RemehaZoneConfigDescription(
        key="heating_curve_base_setpoint", translation_key="heating_curve_base_setpoint",
        curve_key="baseSetpoint", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, **ZONE_CONFIG_COMMON,
    ),
    RemehaZoneConfigDescription(
        key="max_flow_temperature", translation_key="max_flow_temperature",
        curve_key="maxFlowTemperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, **ZONE_CONFIG_COMMON,
    ),
)


def _timestamp(value: str | None) -> datetime | None:
    if not value or (parsed := dt_util.parse_datetime(value)) is None:
        return None
    return parsed if parsed.year > 2000 else None  # "0001-01-01" = not set


def _outdoor(appliance: dict[str, Any]) -> float | None:
    info = appliance.get("outdoorTemperatureInformation") or {}
    for key in ("applianceOutdoorTemperature", "internetOutdoorTemperature", "cloudOutdoorTemperature"):
        if info.get(key) is not None:
            return info[key]
    return None


TEMP = {
    "device_class": SensorDeviceClass.TEMPERATURE,
    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
    "state_class": SensorStateClass.MEASUREMENT,
}

APPLIANCE_SENSORS = (
    RemehaSensorDescription(
        key="water_pressure",
        translation_key="water_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.get("waterPressure"),
    ),
    RemehaSensorDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        value_fn=_outdoor,
        exists_fn=lambda a: bool(a.get("capabilityOutdoorTemperature", True)),
        **TEMP,
    ),
    RemehaSensorDescription(
        key="error_status",
        translation_key="error_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda a: a.get("errorStatus"),
    ),
    RemehaSensorDescription(
        key="active_thermal_mode",
        translation_key="active_thermal_mode",
        value_fn=lambda a: a.get("activeThermalMode"),
    ),
    RemehaSensorDescription(
        key="operating_mode",
        translation_key="operating_mode",
        value_fn=lambda a: a.get("operatingMode"),
    ),
)

CLIMATE_ZONE_SENSORS = (
    RemehaSensorDescription(key="next_setpoint", translation_key="next_setpoint",
                            value_fn=lambda z: z.get("nextSetpoint"), **TEMP),
    RemehaSensorDescription(key="schedule_setpoint", translation_key="schedule_setpoint",
                            value_fn=lambda z: z.get("currentScheduleSetPoint"), **TEMP),
    RemehaSensorDescription(key="next_switch_time", translation_key="next_switch_time",
                            device_class=SensorDeviceClass.TIMESTAMP,
                            value_fn=lambda z: _timestamp(z.get("nextSwitchTime"))),
    RemehaSensorDescription(key="comfort_demand", translation_key="comfort_demand",
                            value_fn=lambda z: z.get("activeComfortDemand")),
)

HOT_WATER_SENSORS = (
    RemehaSensorDescription(key="dhw_temperature", translation_key="dhw_temperature",
                            value_fn=lambda z: z.get("dhwTemperature"),
                            exists_fn=lambda z: z.get("dhwTemperature") is not None, **TEMP),
    RemehaSensorDescription(key="dhw_status", translation_key="dhw_status",
                            value_fn=lambda z: z.get("dhwStatus")),
    RemehaSensorDescription(key="dhw_next_switch_time", translation_key="next_switch_time",
                            device_class=SensorDeviceClass.TIMESTAMP,
                            value_fn=lambda z: _timestamp(z.get("nextSwitchTime"))),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemehaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for aid, appliance in coordinator.data.appliances.items():
        entities += [
            RemehaApplianceSensor(coordinator, aid, d)
            for d in APPLIANCE_SENSORS if d.exists_fn(appliance)
        ]
        if appliance.get("capabilityEnergyConsumption"):
            energy = coordinator.data.energy.get(aid)
            entities += [
                RemehaEnergySensor(coordinator, aid, d)
                for d in ENERGY_SENSORS if d.exists_fn(energy, appliance)
            ]
    for aid, zid, zone in iter_zones(coordinator, "climateZones"):
        entities += [
            RemehaClimateZoneSensor(coordinator, aid, zid, d)
            for d in CLIMATE_ZONE_SENSORS if d.exists_fn(zone)
        ]
    for aid, zid, zone in iter_zones(coordinator, "climateZones"):
        config = coordinator.data.zone_config.get(zid)
        if config is None:
            continue
        entities += [
            RemehaHeatingCurveSensor(coordinator, aid, zid, d)
            for d in HEATING_CURVE_SENSORS if d.curve_key in config.heating_curve
        ]
        entities += [
            RemehaActivitySensor(coordinator, aid, zid, a["activityNumber"], a.get("name"))
            for a in config.activities
            if a.get("type") == "Heating" and a.get("activityNumber") is not None
        ]
    for aid, zid, zone in iter_zones(coordinator, "hotWaterZones"):
        entities += [
            RemehaHotWaterZoneSensor(coordinator, aid, zid, d)
            for d in HOT_WATER_SENSORS if d.exists_fn(zone)
        ]
    async_add_entities(entities)


class RemehaApplianceSensor(RemehaApplianceEntity, SensorEntity):
    entity_description: RemehaSensorDescription

    def __init__(self, coordinator, appliance_id, description) -> None:
        super().__init__(coordinator, appliance_id, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.appliance)


class RemehaEnergySensor(RemehaApplianceEntity, SensorEntity):
    """Energy of the last completed day (normally yesterday)."""

    entity_description: RemehaEnergyDescription

    def __init__(self, coordinator, appliance_id, description) -> None:
        super().__init__(coordinator, appliance_id, description.key)
        self.entity_description = description

    @property
    def _energy(self) -> EnergyDay | None:
        return self.coordinator.data.energy.get(self._appliance_id)

    @property
    def native_value(self) -> float | None:
        energy = self._energy
        return None if energy is None else self.entity_description.value_fn(energy, self.appliance)

    @property
    def last_reset(self) -> datetime | None:
        energy = self._energy
        if energy is None or not self.entity_description.daily_total:
            return None
        return datetime.combine(energy.day, time.min, tzinfo=dt_util.get_default_time_zone())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        energy = self._energy
        return None if energy is None else {"day": energy.day.isoformat()}


class RemehaHeatingCurveSensor(RemehaClimateZoneEntity, SensorEntity):
    """Heating-curve value of a climate zone (read-only)."""

    entity_description: RemehaZoneConfigDescription

    def __init__(self, coordinator, appliance_id, zone_id, description) -> None:
        super().__init__(coordinator, appliance_id, zone_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        config = self.coordinator.data.zone_config.get(self._zone_id)
        return None if config is None else config.heating_curve.get(self.entity_description.curve_key)


class RemehaActivitySensor(RemehaClimateZoneEntity, SensorEntity):
    """Temperature of a named schedule activity (e.g. 'Thuis'), read-only."""

    _attr_translation_key = "activity_temperature"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, appliance_id, zone_id, number: int, name: str | None) -> None:
        super().__init__(coordinator, appliance_id, zone_id, f"activity_heating_{number}")
        self._number = number
        self._attr_translation_placeholders = {"name": name or str(number)}

    @property
    def _activity(self) -> dict[str, Any]:
        config = self.coordinator.data.zone_config.get(self._zone_id)
        return next(
            (
                a for a in (config.activities if config else [])
                if a.get("type") == "Heating" and a.get("activityNumber") == self._number
            ),
            {},
        )

    @property
    def native_value(self) -> float | None:
        return self._activity.get("temperature")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self._activity
        return {"activity_number": self._number, "min": a.get("setPointMin"), "max": a.get("setPointMax")}


class RemehaClimateZoneSensor(RemehaClimateZoneEntity, SensorEntity):
    entity_description: RemehaSensorDescription

    def __init__(self, coordinator, appliance_id, zone_id, description) -> None:
        super().__init__(coordinator, appliance_id, zone_id, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.zone)


class RemehaHotWaterZoneSensor(RemehaHotWaterZoneEntity, SensorEntity):
    entity_description: RemehaSensorDescription

    def __init__(self, coordinator, appliance_id, zone_id, description) -> None:
        super().__init__(coordinator, appliance_id, zone_id, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.zone)
