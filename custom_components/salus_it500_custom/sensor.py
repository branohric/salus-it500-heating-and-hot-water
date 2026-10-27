"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SalusConfigEntry
from .api import SalusState
from .entity import SalusEntity


@dataclass(frozen=True, kw_only=True)
class SalusSensorDescription(SensorEntityDescription):
    value: Callable[[SalusState], float | int | str | None]


SENSORS: tuple[SalusSensorDescription, ...] = (
    SalusSensorDescription(
        key="room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value=lambda s: s.room_temperature,
    ),
    SalusSensorDescription(
        key="setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value=lambda s: s.setpoint,
    ),
    SalusSensorDescription(
        key="hot_water_boost_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        value=lambda s: s.hot_water_boost_hours,
    ),
    SalusSensorDescription(
        key="heating_mode",
        device_class=SensorDeviceClass.ENUM,
        options=["auto", "temp_hold", "manual", "off"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.heating_mode,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SalusConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(SalusSensor(entry.runtime_data, d) for d in SENSORS)


class SalusSensor(SalusEntity, SensorEntity):
    entity_description: SalusSensorDescription

    def __init__(self, coordinator, description: SalusSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        return self.entity_description.value(self.coordinator.data)
