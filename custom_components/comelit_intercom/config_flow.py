"""Account setup, reauthentication and video preferences."""

import probatio as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ComelitAuthError, ComelitClient, ComelitError
from .const import CONF_RESOLUTION, DEFAULT_RESOLUTION, DOMAIN


class ComelitConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Validate credentials using discovery only; never activate a relay."""

    VERSION = 1

    async def _validate(self, data):
        client = ComelitClient(
            async_get_clientsession(self.hass), data[CONF_EMAIL], data[CONF_PASSWORD]
        )
        try:
            endpoints = await client.discover()
        except ComelitAuthError:
            return {"base": "invalid_auth"}
        except ComelitError:
            return {"base": "cannot_connect"}
        if not any(endpoint.camera or endpoint.power for endpoint in endpoints):
            return {"base": "no_devices"}
        return {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input = {
                **user_input,
                CONF_EMAIL: user_input[CONF_EMAIL].strip().lower(),
            }
            await self.async_set_unique_id(user_input[CONF_EMAIL])
            self._abort_if_unique_id_configured()
            if not (errors := await self._validate(user_input)):
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            if not (errors := await self._validate(data)):
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ComelitOptionsFlow()


class ComelitOptionsFlow(config_entries.OptionsFlow):
    """Change the resolution requested from the monitor."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RESOLUTION,
                        default=self.config_entry.options.get(
                            CONF_RESOLUTION, DEFAULT_RESOLUTION
                        ),
                    ): vol.In(["HD", "SD"]),
                }
            ),
        )
