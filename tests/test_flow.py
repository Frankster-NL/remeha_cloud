import custom_components.remeha_cloud.api as api  # noqa
from unittest.mock import patch, AsyncMock
from homeassistant import config_entries

async def test_flow(hass):
    with patch.object(api.RemehaClient, "async_login", AsyncMock(side_effect=api.RemehaAuthError)):
        r = await hass.config_entries.flow.async_init("remeha_cloud", context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"email": "a@b.nl", "password": "bad"})
        assert r["errors"] == {"base": "invalid_auth"}
    async def ok(self): self._token = {"access_token": "t", "refresh_token": "r", "expires_at": 9e9}
    with patch.object(api.RemehaClient, "async_login", ok), \
         patch.object(api.RemehaClient, "async_get_dashboard", AsyncMock(return_value={"appliances": []})), \
         patch("custom_components.remeha_cloud.async_setup_entry", AsyncMock(return_value=True)):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"email": "a@b.nl", "password": "good"})
        assert r["type"] == "create_entry" and r["data"]["token"]["access_token"] == "t"
