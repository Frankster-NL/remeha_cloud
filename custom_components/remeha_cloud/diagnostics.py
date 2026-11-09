"""Diagnostics (Settings > Devices > Remeha > Download diagnostics).

Besides the current data, this runs a read-only exploration of the Remeha API
(GET requests only) with the integration's own login. IDs and the e-mail
address are replaced by placeholders.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import json
import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import RemehaConfigEntry
from .api import RemehaError
from .const import CONF_TOKEN

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_TOKEN, "serialNumber", "houseName"}
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _mask(obj: Any, email: str) -> Any:
    """Replace UUIDs by stable placeholders and drop the e-mail address."""
    text = json.dumps(obj, default=str)
    mapping: dict[str, str] = {}
    text = UUID_RE.sub(
        lambda m: mapping.setdefault(m.group(0).lower(), f"<id{len(mapping) + 1}>"), text
    )
    if email:
        text = text.replace(email, "<email>")
    return json.loads(text)


async def _explore(entry: RemehaConfigEntry) -> dict[str, Any]:
    client = entry.runtime_data.client
    data = entry.runtime_data.data
    out: dict[str, Any] = {}

    async def get(label: str, path: str, params: dict[str, str] | None = None) -> None:
        try:
            out[label] = await client._async_request("GET", path, params=params)  # noqa: SLF001
        except RemehaError as err:
            out[label] = {"error": str(err)[:300]}

    today = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%d %H:%M:%S.000Z"
    for i, (aid, appliance) in enumerate(data.appliances.items()):
        for period, days in (("daily", 7), ("monthly", 365), ("yearly", 1100)):
            await get(
                f"appliance{i}.energy.{period}",
                f"/appliances/{aid}/energyconsumption/{period}",
                {
                    "startDate": (today - timedelta(days=days)).strftime(fmt),
                    "endDate": (today + timedelta(days=1)).strftime(fmt),
                },
            )
        # Undocumented guesses: errors are expected for some.
        for suffix in ("", "/parameters", "/statistics", "/errors", "/maintenance"):
            await get(f"appliance{i}.probe{suffix or '/'}", f"/appliances/{aid}{suffix}")
        for j, zone in enumerate(appliance.get("climateZones") or []):
            zid = zone["climateZoneId"]
            for suffix in ("", "/time-programs/heating", "/heating-curve", "/activities"):
                await get(f"appliance{i}.zone{j}{suffix or '/'}", f"/climate-zones/{zid}{suffix}")
        for j, zone in enumerate(appliance.get("hotWaterZones") or []):
            zid = zone["hotWaterZoneId"]
            for suffix in ("", "/time-programs"):
                await get(f"appliance{i}.dhw{j}{suffix or '/'}", f"/hot-water-zones/{zid}{suffix}")
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RemehaConfigEntry
) -> dict[str, Any]:
    email = entry.data.get(CONF_EMAIL, "")
    # Mask in one pass so a placeholder means the same object everywhere.
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        **_mask(
            {
                "data": async_redact_data(asdict(entry.runtime_data.data), TO_REDACT),
                "exploration": await _explore(entry),
            },
            email,
        ),
    }
