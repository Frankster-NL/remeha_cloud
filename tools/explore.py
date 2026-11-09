"""Read-only Remeha Home API explorer.

Usage (from the repo root):
    pip install aiohttp
    python tools/explore.py

Only GET requests. Writes remeha_dump.json with UUIDs and your email masked.
"""

import asyncio
from datetime import datetime, timedelta
import getpass
import importlib.util
import json
from pathlib import Path
import re

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "remeha_api", ROOT / "custom_components/remeha_cloud/api.py"
)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def mask(obj, email: str):
    """Replace UUIDs with stable placeholders and drop the email."""
    text = json.dumps(obj, indent=2, default=str)
    mapping: dict[str, str] = {}

    def repl(m):
        return mapping.setdefault(m.group(0).lower(), f"<id{len(mapping) + 1}>")

    text = UUID_RE.sub(repl, text).replace(email, "<email>")
    return json.loads(text)


async def main() -> None:
    email = input("Remeha e-mail: ").strip()
    password = getpass.getpass("Password: ")
    out: dict = {}

    async with aiohttp.ClientSession() as session:
        client = api.RemehaClient(session, email, password)

        async def get(label: str, path: str, params=None):
            try:
                out[label] = await client._async_request("GET", path, params=params)
                print(f"OK    {label}")
            except api.RemehaError as err:
                out[label] = {"error": str(err)[:300]}
                print(f"FAIL  {label}: {str(err)[:120]}")

        await client.async_login()
        print("Login OK")

        await get("dashboard", "/homes/dashboard")
        appliances = (out.get("dashboard") or {}).get("appliances") or []

        now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        fmt = "%Y-%m-%d %H:%M:%S.000Z"
        for i, a in enumerate(appliances):
            aid = a["applianceId"]
            await get(f"appliance{i}.technicaldetails", f"/appliances/{aid}/technicaldetails")
            for period, days in (("daily", 7), ("monthly", 365), ("yearly", 1100)):
                await get(
                    f"appliance{i}.energy.{period}",
                    f"/appliances/{aid}/energyconsumption/{period}",
                    {"startDate": (now - timedelta(days=days)).strftime(fmt),
                     "endDate": (now + timedelta(days=1)).strftime(fmt)},
                )
            # Probes: undocumented guesses, a 404 is expected for some.
            for suffix in ("", "/parameters", "/statistics", "/errors", "/maintenance"):
                await get(f"appliance{i}.probe{suffix or '/'}", f"/appliances/{aid}{suffix}")

            for j, z in enumerate(a.get("climateZones") or []):
                zid = z["climateZoneId"]
                await get(f"appliance{i}.zone{j}.probe/", f"/climate-zones/{zid}")
                await get(f"appliance{i}.zone{j}.time-programs",
                          f"/climate-zones/{zid}/time-programs/heating")
            for j, z in enumerate(a.get("hotWaterZones") or []):
                zid = z["hotWaterZoneId"]
                await get(f"appliance{i}.dhw{j}.probe/", f"/hot-water-zones/{zid}")
                await get(f"appliance{i}.dhw{j}.time-programs",
                          f"/hot-water-zones/{zid}/time-programs")

    dump = Path("remeha_dump.json")
    dump.write_text(json.dumps(mask(out, email), indent=2))
    print(f"\nWritten {dump.resolve()} - review it, then share it.")


asyncio.run(main())
