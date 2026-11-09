"""Config flow: email/password login with reauth."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RemehaAuthError, RemehaClient, RemehaError, RemehaLoginFlowError
from .const import CONF_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCHEMA = vol.Schema({vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str})


class RemehaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _async_validate(self, email: str, password: str) -> tuple[dict | None, dict[str, str]]:
        client = RemehaClient(async_get_clientsession(self.hass), email, password)
        try:
            await client.async_login()
            await client.async_get_dashboard()
        except RemehaAuthError as err:
            _LOGGER.warning("Remeha login rejected: %s", err)
            return None, {"base": "invalid_auth"}
        except RemehaLoginFlowError as err:
            _LOGGER.error("Remeha login flow failed after credentials were accepted: %s", err)
            return None, {"base": "login_flow_failed"}
        except RemehaError as err:
            _LOGGER.warning("Remeha login failed: %s", err)
            return None, {"base": "cannot_connect"}
        return client.token, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()
            token, errors = await self._async_validate(email, user_input[CONF_PASSWORD])
            if not errors:
                return self.async_create_entry(
                    title=email,
                    data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD], CONF_TOKEN: token},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token, errors = await self._async_validate(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD], CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )
