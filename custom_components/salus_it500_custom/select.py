"""Hot water mode select: auto (schedule) / once / on / off."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SalusConfigEntry
from .const import HW_MODES
from .entity import SalusEntity

MODE_TO_VALUE = {name: value for value, name in HW_MODES.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: SalusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([SalusHotWaterMode(entry.runtime_data)])


class SalusHotWaterMode(SalusEntity, SelectEntity):
    _attr_options = list(HW_MODES.values())
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "hot_water_mode")

    @property
    def current_option(self) -> str | None:
        return HW_MODES.get(self.coordinator.data.hot_water_mode)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_hot_water_mode(MODE_TO_VALUE[option])
