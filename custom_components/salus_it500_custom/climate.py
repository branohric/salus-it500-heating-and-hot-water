"""Heating as a climate entity: HEAT = target (default 23 °C), OFF = 14 °C or thermostat off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SalusConfigEntry
from .const import (
    CONF_DEFAULT_TARGET,
    CONF_OFF_MODE,
    CONF_OFF_TEMPERATURE,
    MAX_TEMP,
    MIN_TEMP,
    OFF_MODE_SETPOINT,
    TEMP_STEP,
)
from .coordinator import SalusCoordinator
from .entity import SalusEntity

ATTR_ON_TARGET = "on_target"
ATTR_DEFAULT_TARGET = "default_target"


async def async_setup_entry(
    hass: HomeAssistant, entry: SalusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([SalusClimate(entry.runtime_data)])


class SalusClimate(SalusEntity, ClimateEntity, RestoreEntity):
    """Virtual thermostat over Salus zone 1."""

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: SalusCoordinator) -> None:
        super().__init__(coordinator, "heating")
        self._on_target: float = self._default_target

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        # Keep the last temperature chosen in HA across restarts, unless the
        # default in the options has been changed since (then the new default wins).
        try:
            restored = float(last.attributes.get(ATTR_ON_TARGET))
            saved_default = float(last.attributes.get(ATTR_DEFAULT_TARGET))
        except (TypeError, ValueError):
            return
        if saved_default == self._default_target and restored > self._off_temperature:
            self._on_target = restored

    @property
    def _default_target(self) -> float:
        return float(self.coordinator.option(CONF_DEFAULT_TARGET))

    @property
    def _off_temperature(self) -> float:
        return float(self.coordinator.option(CONF_OFF_TEMPERATURE))

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.online

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.room_temperature

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.data.setpoint

    @property
    def hvac_mode(self) -> HVACMode:
        state = self.coordinator.data
        if state.zone_off:
            return HVACMode.OFF
        if (
            self.coordinator.option(CONF_OFF_MODE) == OFF_MODE_SETPOINT
            and state.setpoint is not None
            and state.setpoint <= self._off_temperature + 0.05
        ):
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        if self.coordinator.data.heating_relay:
            return HVACAction.HEATING
        return HVACAction.OFF if self.hvac_mode == HVACMode.OFF else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data
        return {
            ATTR_ON_TARGET: self._on_target,
            ATTR_DEFAULT_TARGET: self._default_target,
            "salus_mode": state.heating_mode,
            "boost_hours": state.heating_boost_hours,
            "frost_temperature": state.frost_temperature,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get("hvac_mode")
        if hvac_mode == HVACMode.OFF:
            await self.async_set_hvac_mode(HVACMode.OFF)
            return
        if temperature is None:
            if hvac_mode == HVACMode.HEAT:
                await self.async_set_hvac_mode(HVACMode.HEAT)
            return
        temperature = float(temperature)
        if temperature > self._off_temperature:
            self._on_target = temperature
        await self.coordinator.async_set_heating(temperature)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_heating_off()
        else:
            await self.coordinator.async_set_heating(self._on_target)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
