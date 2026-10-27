"""Home Assistant level tests: config flow, entities and commands end-to-end over HTTP."""

from __future__ import annotations

import functools
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestServer
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.salus_it500_custom import api
from custom_components.salus_it500_custom.const import (
    CONF_DEVICE_ID,
    CONF_HOLD_MODE,
    HOLD_MODE_FOLLOW,
    CONF_OFF_MODE,
    CONF_WAKE_ON_COMMAND,
    DOMAIN,
    OFF_MODE_THERMOSTAT,
)

from .fake_salus import FakeSalus

DEV = "1234567"


@pytest.fixture
async def fake():
    fake = FakeSalus(device_id=DEV)
    server = TestServer(fake.app())
    await server.start_server()
    host = str(server.make_url("")).rstrip("/")
    client_cls = functools.partial(api.SalusClient, host=host)
    with (
        patch("custom_components.salus_it500_custom.SalusClient", client_cls),
        patch("custom_components.salus_it500_custom.config_flow.SalusClient", client_cls),
        patch("custom_components.salus_it500_custom.const.CONFIRM_DELAYS", ()),
        patch("custom_components.salus_it500_custom.coordinator.CONFIRM_DELAYS", ()),
        patch("custom_components.salus_it500_custom.api.TIMEOUT_RETRY_DELAY", 0),
        patch("custom_components.salus_it500_custom.coordinator.VERIFY_DELAYS", (0, 0, 0, 0)),
    ):
        yield fake
    await server.close()


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Doma",
        unique_id=DEV,
        data={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret", CONF_DEVICE_ID: DEV},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _eid(hass, platform, key):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DEV}_{key}")


async def test_config_flow_discovers_device(hass: HomeAssistant, fake) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Doma"
    assert result["data"][CONF_DEVICE_ID] == DEV
    await hass.async_block_till_done()


async def test_entities_created(hass: HomeAssistant, fake) -> None:
    await _setup(hass)
    climate = hass.states.get(_eid(hass, "climate", "heating"))
    assert climate.state == "heat"
    assert climate.attributes["current_temperature"] == 21.5
    assert climate.attributes["temperature"] == 20.0
    assert climate.attributes["on_target"] == 23.0
    assert hass.states.get(_eid(hass, "switch", "hot_water_boost")).state == "off"
    assert hass.states.get(_eid(hass, "select", "hot_water_mode")).state == "auto"
    assert hass.states.get(_eid(hass, "sensor", "room_temperature")).state == "21.5"
    print(sorted(s.entity_id for s in hass.states.async_all()))


async def test_heating_on_off_manual_mode(hass: HomeAssistant, fake) -> None:
    """Default: the thermostat's manual mode, A93 first, flags, then A85."""
    await _setup(hass)
    eid = _eid(hass, "climate", "heating")

    await hass.services.async_call("climate", "turn_off", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake.sets == [[("F", "60")], [("A93", "1400")], [("A92", "1")], [("A85", "1400")]]
    assert hass.states.get(eid).state == "off"
    assert hass.states.get(eid).attributes["salus_mode"] == "manual"

    await hass.services.async_call("climate", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake.sets[-3:] == [[("F", "60")], [("A93", "2300")], [("A85", "2300")]]
    state = hass.states.get(eid)
    assert state.state == "heat" and state.attributes["temperature"] == 23.0

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": eid, "temperature": 21.5}, blocking=True
    )
    assert fake.all_set_pairs()[-2:] == [("A93", "2150"), ("A85", "2150")]
    assert hass.states.get(eid).attributes["on_target"] == 21.5
    assert fake.timeouts == 0


async def test_turn_on_from_off_when_thermostat_overwrites_setpoint(hass: HomeAssistant, fake) -> None:
    """The real case: OFF + manual flag, setpoint 20.8. After the mode change the
    thermostat reported 14 °C (its own value) instead of 23 °C -> setpoint is re-sent."""
    fake.attrs.update({"A89": "1", "A92": "1", "A85": "2080", "A93": "2080"})
    fake.mode_change_overwrites_setpoint = True
    await _setup(hass)
    eid = _eid(hass, "climate", "heating")
    assert hass.states.get(eid).state == "off"

    await hass.services.async_call("climate", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake.sets == [[("F", "60")], [("A93", "2300")], [("A89", "0")], [("A85", "2300")]]
    await hass.async_block_till_done(wait_background_tasks=True)
    # verification saw A85=1400 and sent the setpoint again
    assert fake.sets[4:] == [[("F", "60")], [("A93", "2300")], [("A85", "2300")]]
    assert fake.attrs["A85"] == "2300" and fake.attrs["A92"] == "1" and fake.attrs["A89"] == "0"
    state = hass.states.get(eid)
    assert state.state == "heat" and state.attributes["temperature"] == 23.0
    assert state.attributes["salus_mode"] == "manual"


async def test_follow_mode_like_old_portal(hass: HomeAssistant, fake) -> None:
    await _setup(hass, {CONF_HOLD_MODE: HOLD_MODE_FOLLOW})
    eid = _eid(hass, "climate", "heating")
    await hass.services.async_call("climate", "turn_on", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake.all_set_pairs() == [("A88", "1"), ("A85", "2300")]
    assert hass.states.get(eid).attributes["salus_mode"] == "temp_hold"


async def test_off_when_already_off_sends_nothing(hass: HomeAssistant, fake) -> None:
    fake.attrs["A89"] = "1"
    await _setup(hass)
    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": _eid(hass, "climate", "heating")}, blocking=True
    )
    assert fake.sets == []


async def test_unreachable_thermostat_reports_error(hass: HomeAssistant, fake) -> None:
    fake.unreachable = True
    await _setup(hass)
    eid = _eid(hass, "climate", "heating")
    with pytest.raises(HomeAssistantError, match="did not respond"):
        await hass.services.async_call("climate", "turn_on", {"entity_id": eid}, blocking=True)
    assert hass.states.get(eid).attributes["temperature"] == 20.0  # nothing pretended


async def test_without_wake_option_a_timeout_is_retried(hass: HomeAssistant, fake) -> None:
    fake.attrs["A92"] = "1"
    await _setup(hass, {CONF_WAKE_ON_COMMAND: False})
    eid = _eid(hass, "climate", "heating")
    await hass.services.async_call("climate", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake.sets == [[("A93", "2300")], [("F", "60")], [("A93", "2300")], [("A85", "2300")]]
    assert fake.attrs["A85"] == fake.attrs["A93"] == "2300"


async def test_thermostat_off_mode(hass: HomeAssistant, fake) -> None:
    await _setup(hass, {CONF_OFF_MODE: OFF_MODE_THERMOSTAT})
    eid = _eid(hass, "climate", "heating")
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "off"}, blocking=True)
    assert fake.all_set_pairs()[-1] == ("A89", "1")
    assert hass.states.get(eid).state == "off"
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "heat"}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake.all_set_pairs()[-4:] == [("A93", "2300"), ("A92", "1"), ("A89", "0"), ("A85", "2300")]


async def test_hot_water_boost_switch(hass: HomeAssistant, fake) -> None:
    await _setup(hass)
    eid = _eid(hass, "switch", "hot_water_boost")
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake.sets[-2:] == [[("F", "60")], [("C43", "1")]]
    assert hass.states.get(eid).state == "on"
    # pressing ON again restarts the boost (written even though C43 is already 1)
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake.sets[-1] == [("C43", "1")]
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid}, blocking=True)
    assert fake.sets[-1] == [("C43", "0")]
    assert hass.states.get(eid).state == "off"


async def test_battery_sensor(hass: HomeAssistant, fake) -> None:
    fake.attrs["S03"] = "1"
    await _setup(hass)
    assert hass.states.get(_eid(hass, "binary_sensor", "battery")).state == "on"


async def test_optimistic_state_until_thermostat_reports(hass: HomeAssistant, fake) -> None:
    """Sleeping thermostat: cloud still shows the old value - UI must not flip back."""
    fake.set_status = 500
    entry = await _setup(hass, {CONF_WAKE_ON_COMMAND: False})
    eid = _eid(hass, "switch", "hot_water_boost")
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    fake.attrs["C43"] = "0"  # cloud has not got the report yet
    await entry.runtime_data.async_refresh()
    assert hass.states.get(eid).state == "on"


async def test_token_expiry_during_polling(hass: HomeAssistant, fake) -> None:
    entry = await _setup(hass)
    fake.expire_tokens()
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.last_update_success
    assert fake.logins == 2


async def test_password_change_starts_reauth(hass: HomeAssistant, fake) -> None:
    entry = await _setup(hass)
    fake.pw_md5 = "different"
    fake.expire_tokens()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_options_flow(hass: HomeAssistant, fake) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "default_target": 22.5, "off_mode": "setpoint", "off_temperature": 15,
            "hold_mode": "permanent", "hw_boost_hours": 2, "scan_interval": 120,
            "wake_on_command": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["hw_boost_hours"] == 2
    eid = _eid(hass, "switch", "hot_water_boost")
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert fake.all_set_pairs()[-1] == ("C43", "2")
    ceid = _eid(hass, "climate", "heating")
    await hass.services.async_call("climate", "turn_on", {"entity_id": ceid}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake.all_set_pairs()[-3:] == [("A93", "2250"), ("A92", "1"), ("A85", "2250")]


async def test_slovak_entity_ids(hass: HomeAssistant, fake) -> None:
    hass.config.language = "sk"
    await _setup(hass)
    ids = {s.entity_id for s in hass.states.async_all()}
    for eid in ("climate.doma_kurenie", "switch.doma_ohrev_vody_boost",
                "sensor.doma_zostavajuci_boost_vody", "binary_sensor.doma_kotol_kurenie"):
        assert eid in ids, sorted(ids)
