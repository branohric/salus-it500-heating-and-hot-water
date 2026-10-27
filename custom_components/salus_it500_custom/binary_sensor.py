"""Binary sensors: heating relay, hot water relay, connectivity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SalusConfigEntry
from .api import SalusState
from .entity import SalusEntity


@dataclass(frozen=True, kw_only=True)
class SalusBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[SalusState], bool]


BINARY_SENSORS: tuple[SalusBinaryDescription, ...] = (
    SalusBinaryDescription(
        key="heating_relay",
        device_class=BinarySensorDeviceClass.HEAT,
        value=lambda s: s.heating_relay,
    ),
    SalusBinaryDescription(
        key="hot_water_relay",
        device_class=BinarySensorDeviceClass.RUNNING,
        value=lambda s: s.hot_water_on,
    ),
    SalusBinaryDescription(
        key="battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.battery_low,
    ),
    SalusBinaryDescription(
        key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SalusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(SalusBinarySensor(entry.runtime_data, d) for d in BINARY_SENSORS)


class SalusBinarySensor(SalusEntity, BinarySensorEntity):
    entity_description: SalusBinaryDescription

    def __init__(self, coordinator, description: SalusBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.value(self.coordinator.data)
