"""Config flow: log in, pick the thermostat (devId is discovered automatically)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SalusAuthError, SalusClient, SalusDevice, SalusError
from .const import (
    CONF_DEFAULT_TARGET,
    CONF_DEVICE_ID,
    CONF_HOLD_MODE,
    CONF_HW_BOOST_HOURS,
    CONF_OFF_MODE,
    CONF_OFF_TEMPERATURE,
    CONF_SCAN_INTERVAL,
    CONF_WAKE_ON_COMMAND,
    DEFAULTS,
    DOMAIN,
    HOLD_MODE_FOLLOW,
    HOLD_MODE_PERMANENT,
    MAX_TEMP,
    MIN_TEMP,
    OFF_MODE_SETPOINT,
    OFF_MODE_THERMOSTAT,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class SalusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._devices: list[SalusDevice] = []

    async def _async_devices(self, username: str, password: str) -> list[SalusDevice]:
        client = SalusClient(async_get_clientsession(self.hass), username, password)
        await client.async_login()
        return await client.async_get_devices()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                devices = await self._async_devices(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except SalusAuthError:
                errors["base"] = "invalid_auth"
            except SalusError:
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._credentials = user_input
                    self._devices = devices
                    if len(devices) == 1:
                        return await self._async_create(devices[0])
                    return await self.async_step_device()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            device = next(d for d in self._devices if d.device_id == user_input[CONF_DEVICE_ID])
            return await self._async_create(device)
        options = [
            selector.SelectOptionDict(value=d.device_id, label=f"{d.name} ({d.device_id})")
            for d in self._devices
        ]
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )}
            ),
        )

    async def _async_create(self, device: SalusDevice) -> ConfigFlowResult:
        await self.async_set_unique_id(device.device_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=device.name,
            data={**self._credentials, CONF_DEVICE_ID: device.device_id},
        )

    # --- re-authentication (password changed) ------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                devices = await self._async_devices(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            except SalusAuthError:
                errors["base"] = "invalid_auth"
            except SalusError:
                errors["base"] = "cannot_connect"
            else:
                if all(d.device_id != entry.data[CONF_DEVICE_ID] for d in devices):
                    errors["base"] = "no_devices"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )}
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SalusOptionsFlow()


def _temp_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_TEMP, max=MAX_TEMP, step=0.5, unit_of_measurement="°C",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEFAULT_TARGET): _temp_selector(),
        vol.Required(CONF_OFF_MODE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[OFF_MODE_SETPOINT, OFF_MODE_THERMOSTAT], translation_key="off_mode"
            )
        ),
        vol.Required(CONF_OFF_TEMPERATURE): _temp_selector(),
        vol.Required(CONF_HOLD_MODE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[HOLD_MODE_PERMANENT, HOLD_MODE_FOLLOW], translation_key="hold_mode"
            )
        ),
        vol.Required(CONF_HW_BOOST_HOURS): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=3, step=1, unit_of_measurement="h")
        ),
        vol.Required(CONF_SCAN_INTERVAL): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=30, max=600, step=10, unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_WAKE_ON_COMMAND): selector.BooleanSelector(),
    }
)


class SalusOptionsFlow(OptionsFlow):
    """Options: default target, what 'off' means, boost length, polling."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_HW_BOOST_HOURS] = int(user_input[CONF_HW_BOOST_HOURS])
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)
        current = {**DEFAULTS, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, current),
        )
