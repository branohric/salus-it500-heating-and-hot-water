"""Client tests against the fake cloud: login, token rotation, reads, writes."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.salus_it500_custom import api
from custom_components.salus_it500_custom.api import (
    SalusAuthError,
    SalusClient,
    SalusConnectionError,
    SalusTimeoutError,
)

from .fake_salus import FakeSalus


@pytest.fixture
async def fake():
    fake = FakeSalus()
    server = TestServer(fake.app())
    await server.start_server()
    fake.host = str(server.make_url(""))
    yield fake
    await server.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def client(session, fake, **kw):
    c = SalusClient(session, fake.email, "secret", host=fake.host, **kw)
    c.timeout_retry_delay = 0
    return c


async def test_login_devices_and_state(session, fake):
    c = client(session, fake)
    devices = await c.async_get_devices()
    assert [(d.device_id, d.name) for d in devices] == [("1234567", "Doma")]
    state = await c.async_get_state("1234567")
    assert state.room_temperature == 21.5
    assert state.setpoint == 20.0
    assert state.has_hot_water and state.online and not state.battery_low
    assert fake.logins == 1  # token reused


async def test_bad_credentials(session, fake):
    c = SalusClient(session, fake.email, "wrong", host=fake.host)
    with pytest.raises(SalusAuthError):
        await c.async_get_devices()


@pytest.mark.parametrize("style", ["http500", "http200", "http401"])
async def test_expired_token_is_renewed(session, fake, style):
    """The Node-RED problem: the token dies server side -> re-login and retry once."""
    fake.fault_style = style
    c = client(session, fake)
    await c.async_get_state("1234567")
    fake.expire_tokens()
    state = await c.async_get_state("1234567")
    assert state.setpoint == 20.0
    assert fake.logins == 2


async def test_token_renewed_proactively(session, fake):
    c = client(session, fake, token_ttl=0)
    await c.async_get_state("1234567")
    await c.async_get_state("1234567")
    assert fake.logins == 2


async def test_one_attribute_per_call_with_wake(session, fake):
    """multiAttributeCapable=false: every pair goes in its own call, F (wake) first."""
    c = client(session, fake)
    await c.async_set("1234567", {"A88": 1, "A85": 2300}, wake=True)
    await c.async_set("1234567", {"C43": 1})
    assert fake.sets == [[("F", "60")], [("A88", "1")], [("A85", "2300")], [("C43", "1")]]
    assert fake.timeouts == 0
    state = await c.async_get_state("1234567")
    assert state.setpoint == 23.0 and state.hot_water_boost_hours == 1


async def test_sleeping_thermostat_is_woken_and_retried(session, fake):
    """The real failure: 202 'Request time out' -> wake (F) and retry once."""
    c = client(session, fake)
    await c.async_set("1234567", {"A85": 2300})
    assert fake.sets == [[("A85", "2300")], [("F", "60")], [("A85", "2300")]]
    assert fake.attrs["A85"] == "2300"


async def test_unreachable_thermostat_raises_timeout(session, fake):
    fake.unreachable = True
    c = client(session, fake)
    with pytest.raises(SalusTimeoutError):
        await c.async_set("1234567", {"A85": 2300}, wake=True)
    assert fake.attrs["A85"] == "2000"


async def test_bare_500_is_tolerated(session, fake):
    fake.asleep = False
    c = client(session, fake)

    async def bare_500(*_a, **_k):
        return 500, "<soap:Fault/>"

    c._call = bare_500  # type: ignore[method-assign]
    await c.async_set("1234567", {"A85": 2300})


async def test_broken_session_reading_forces_relogin(session, fake):
    fake.attrs["A84"] = fake.attrs["A85"] = "3200"
    c = client(session, fake)
    with pytest.raises(SalusConnectionError):
        await c.async_get_state("1234567")
    fake.attrs["A84"], fake.attrs["A85"] = "2100", "2000"
    await c.async_get_state("1234567")
    assert fake.logins == 2


async def test_unreachable_host(session):
    c = SalusClient(session, "a", "b", host="http://127.0.0.1:1")
    api.RETRY_DELAYS = (0, 0)
    with pytest.raises(SalusConnectionError):
        await c.async_login()
