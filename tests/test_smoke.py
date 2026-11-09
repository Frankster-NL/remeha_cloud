import json
import custom_components.remeha_cloud.api  # noqa
import copy, time
from unittest.mock import AsyncMock, patch
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.config_entries import ConfigEntryState

DASH = {"appliances": [{
  "applianceId": "app1", "applianceOnline": True, "applianceType": "HeatPump", "houseName": "Thuis",
  "errorStatus": "Running", "activeThermalMode": "Idle", "operatingMode": "AutomaticHeating",
  "outdoorTemperatureInformation": {"cloudOutdoorTemperature": 7.5, "applianceOutdoorTemperature": None},
  "holidaySchedule": {"active": False}, "waterPressure": 1.4, "gasCalorificValue": 8.79, "waterPressureOK": True,
  "capabilityEnergyConsumption": True, "capabilityCooling": True, "capabilityOutdoorTemperature": True,
  "hotWaterZones": [{"hotWaterZoneId": "dhw1", "name": "DHW", "dhwZoneMode": "Off", "dhwStatus": "Idle",
     "dhwTemperature": None, "targetSetpoint": 60.0, "reducedSetpoint": 15.0, "comfortSetPoint": 60.0,
     "setPointMin": 40.0, "setPointMax": 65.0,
     "setPointRanges": {"comfortSetpointMin": 40.0, "comfortSetpointMax": 65.0, "reducedSetpointMin": 10.0, "reducedSetpointMax": 60.0},
     "nextSwitchTime": "2026-11-09T22:00:00Z"}],
  "climateZones": [{"climateZoneId": "cz1", "name": "Woonkamer", "activeComfortDemand": "ProducingHeat",
     "zoneMode": "Scheduling", "firePlaceModeActive": False, "capabilityFirePlaceMode": True,
     "roomTemperature": 19.5, "setPoint": 20.0, "nextSetpoint": 16.0, "nextSwitchTime": "2026-11-09T22:00:00Z",
     "setPointMin": 5.0, "setPointMax": 30.0, "currentScheduleSetPoint": 20.0,
     "activeHeatingClimateTimeProgramNumber": 1, "preHeat": {"enabled": True, "active": False},
     "temporaryOverride": {"endTime": "0001-01-01T00:00:00Z"}}]}]}
from datetime import date, timedelta
_Y = (date.today() - timedelta(days=1)).isoformat()
_T = date.today().isoformat()
_PRODUCERS = {"producers": [
    {"producerType": "HeatPumpAirSource", "energyConsumptionTotal": 10.0, "energyProductionTotal": 46.0, "seasonalEfficiency": 1.9},
    {"producerType": "GasBoiler", "energyConsumptionTotal": 12.0, "energyProductionTotal": None}]}
ENERGY = {"data": [
    {"isDataReported": True, "timeStamp": f"{_Y}T00:00:00+00:00", "heatingEnergyConsumed": 22.0, "hotWaterEnergyConsumed": 0,
     "coolingEnergyConsumed": 0, "heatingEnergyDelivered": 57.0, "hotWaterEnergyDelivered": 0, "coolingEnergyDelivered": 0,
     "producerPerformanceStatistics": _PRODUCERS},
    {"isDataReported": False, "timeStamp": f"{_T}T00:00:00+00:00", "heatingEnergyConsumed": 0}]}
CURVE = {"slope": 1.5, "baseSetpoint": 15.0, "maxFlowTemperature": 60.0}
ACTIVITIES = [{"activityNumber": 2, "type": "Heating", "name": "Thuis", "temperature": 21.0, "setPointMin": 5.0, "setPointMax": 30.0},
              {"activityNumber": 2, "type": "Cooling", "name": "Thuis", "temperature": 25.0}]

async def test_setup_and_commands(hass):
    entry = MockConfigEntry(domain="remeha_cloud", unique_id="a@b.nl",
        data={"email": "a@b.nl", "password": "x", "token": {"access_token": "t", "refresh_token": "r", "expires_at": time.time()+3600}})
    entry.add_to_hass(hass)
    req = AsyncMock()
    async def fake(method, path, **kw):
        req(method, path, **kw)
        if path == "/homes/dashboard": return copy.deepcopy(DASH)
        if "energyconsumption" in path: return ENERGY
        if "technicaldetails" in path: return {"applianceName": "Elga Ace", "internetConnectedGateways": [{"softwareVersion": "1.2"}]}
        if path.endswith("/heating-curve"): return CURVE
        if path.endswith("/activities"): return ACTIVITIES
        return None
    with patch("custom_components.remeha_cloud.api.RemehaClient._async_request", side_effect=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        states = {s.entity_id: s.state for s in hass.states.async_all()}
        for k, v in sorted(states.items()): print(k, "=", v)
        assert states["sensor.thuis_heatpump_heat_pump_electricity_yesterday"] == "10.0"
        assert states["sensor.thuis_heatpump_heat_pump_cop_yesterday"] == "4.6"
        assert states["sensor.thuis_heatpump_boiler_gas_yesterday"] == str(round(12.0 / 8.79, 3))
        assert states["sensor.thuis_heatpump_heating_energy_delivered_yesterday"] == "57.0"
        assert hass.states.get("sensor.thuis_heatpump_heat_pump_heat_produced_yesterday").attributes["last_reset"].startswith(_Y)
        assert states["sensor.woonkamer_heating_curve_slope"] == "1.5"
        assert states["sensor.woonkamer_activity_thuis"] == "21.0"
        assert not any("today" in e for e in states)
        c = hass.states.get("climate.woonkamer")
        assert c.state == "auto" and c.attributes["hvac_action"] == "heating" and c.attributes["preset_mode"] == "program_1"
        await hass.services.async_call("climate", "set_temperature", {"entity_id": "climate.woonkamer", "temperature": 21}, blocking=True)
        req.assert_any_call("POST", "/climate-zones/cz1/modes/temporary-override", json_body={"roomTemperatureSetPoint": 21.0})
        await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": "climate.woonkamer", "hvac_mode": "off"}, blocking=True)
        req.assert_any_call("POST", "/climate-zones/cz1/modes/anti-frost")
        await hass.services.async_call("water_heater", "set_operation_mode", {"entity_id": "water_heater.dhw", "operation_mode": "comfort"}, blocking=True)
        req.assert_any_call("POST", "/hot-water-zones/dhw1/modes/continuous-comfort")
        await hass.services.async_call("water_heater", "set_temperature", {"entity_id": "water_heater.dhw", "temperature": 55}, blocking=True)
        req.assert_any_call("POST", "/hot-water-zones/dhw1/comfort-setpoint", json_body={"comfortSetpoint": 55.0})
        await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.woonkamer_fireplace_mode"}, blocking=True)
        req.assert_any_call("POST", "/climate-zones/cz1/modes/fireplacemode", json_body={"fireplaceModeActive": True})
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_zone_devices_linked_to_appliance(hass):
    from homeassistant.helpers import device_registry as dr
    entry = MockConfigEntry(domain="remeha_cloud", unique_id="c@d.nl",
        data={"email": "c@d.nl", "password": "x", "token": {"access_token": "t", "refresh_token": "r", "expires_at": time.time()+3600}})
    entry.add_to_hass(hass)
    async def fake(method, path, **kw):
        if path == "/homes/dashboard": return copy.deepcopy(DASH)
        if "energyconsumption" in path: return ENERGY
        return {}
    with patch("custom_components.remeha_cloud.api.RemehaClient._async_request", side_effect=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    reg = dr.async_get(hass)
    appliance = reg.async_get_device(identifiers={("remeha_cloud", "app1")})
    zone = reg.async_get_device(identifiers={("remeha_cloud", "cz1")})
    assert zone.via_device_id == appliance.id
    assert hass.states.get("climate.woonkamer").attributes["current_temperature"] == 19.5
    assert hass.states.get("water_heater.dhw").attributes["temperature"] == 60.0


async def test_diagnostics_exploration(hass):
    from custom_components.remeha_cloud.diagnostics import async_get_config_entry_diagnostics
    entry = MockConfigEntry(domain="remeha_cloud", unique_id="e@f.nl",
        data={"email": "e@f.nl", "password": "x", "token": {"access_token": "t", "refresh_token": "r", "expires_at": time.time()+3600}})
    entry.add_to_hass(hass)
    async def fake(method, path, **kw):
        assert method == "GET"
        if path == "/homes/dashboard": return copy.deepcopy(DASH)
        if "energyconsumption" in path: return ENERGY
        if path.endswith("/statistics"):
            raise custom_components.remeha_cloud.api.RemehaConnectionError("HTTP 404")
        return {"path": path}
    with patch("custom_components.remeha_cloud.api.RemehaClient._async_request", side_effect=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        diag = await async_get_config_entry_diagnostics(hass, entry)
    exp = diag["exploration"]
    assert "error" in exp["appliance0.probe/statistics"]
    assert "appliance0.zone0/heating-curve" in exp and "password" not in json.dumps(diag["entry"]) or diag["entry"]["password"] == "**REDACTED**"
