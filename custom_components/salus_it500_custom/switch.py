"""Hot water boost as a switch: ON = boost for N hours (default 1), OFF = cancel."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SalusConfigEntry
from .const import CONF_HW_BOOST_HOURS
from .entity import SalusEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: SalusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([SalusHotWaterBoost(entry.runtime_data)])


class SalusHotWaterBoost(SalusEntity, SwitchEntity):
    """Hot water boost (old portal: hwboosthours=1 / 0)."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "hot_water_boost")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.hot_water_boost_hours > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data
        return {
            "remaining_hours": state.hot_water_boost_hours,
            "boost_hours_on_start": int(self.coordinator.option(CONF_HW_BOOST_HOURS)),
            "hot_water_relay": state.hot_water_on,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_hot_water_boost()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_hot_water_boost(0)
