"""Constants for Remeha Home (cloud)."""

from datetime import timedelta

DOMAIN = "remeha_cloud"
CONF_TOKEN = "token"

SCAN_INTERVAL = timedelta(seconds=60)
ENERGY_INTERVAL = timedelta(minutes=60)  # daily data is published once a day
ZONE_CONFIG_INTERVAL = timedelta(minutes=60)  # heating curve / activities
REFRESH_AFTER_COMMAND = 5  # seconds; cloud needs a moment to reflect changes

PRODUCER_HEAT_PUMP = "HeatPumpAirSource"
PRODUCER_GAS_BOILER = "GasBoiler"

ENERGY_KEYS = (
    "heatingEnergyConsumed",
    "hotWaterEnergyConsumed",
    "coolingEnergyConsumed",
    "heatingEnergyDelivered",
    "hotWaterEnergyDelivered",
    "coolingEnergyDelivered",
)
