"""Data update coordinator for Salus iT500."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ATTR_HW_BOOST,
    ATTR_HW_MODE,
    ATTR_MANUAL,
    ATTR_MANUAL_SETPOINT,
    ATTR_OFF,
    ATTR_SETPOINT,
    ATTR_TEMP_HOLD,
    SalusAuthError,
    SalusClient,
    SalusError,
    SalusState,
    SalusTimeoutError,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_HOLD_MODE,
    CONF_HW_BOOST_HOURS,
    CONF_OFF_MODE,
    CONF_OFF_TEMPERATURE,
    CONF_SCAN_INTERVAL,
    CONF_WAKE_ON_COMMAND,
    CONFIRM_DELAYS,
    VERIFY_DELAYS,
    DEFAULTS,
    DOMAIN,
    HOLD_MODE_PERMANENT,
    OFF_MODE_THERMOSTAT,
    PENDING_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class SalusCoordinator(DataUpdateCoordinator[SalusState]):
    """Polls the Salus cloud and sends commands."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SalusClient) -> None:
        self.client = client
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        scan = entry.options.get(CONF_SCAN_INTERVAL, DEFAULTS[CONF_SCAN_INTERVAL])
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=int(scan)),
        )
        # attr -> (value, expires_at): optimistic values not yet confirmed by the cloud.
        self._pending: dict[str, tuple[str, float]] = {}
        self._confirm_unsubs: list[Any] = []
        self._verify_task: asyncio.Task | None = None

    def option(self, key: str) -> Any:
        return self.config_entry.options.get(key, DEFAULTS[key])

    # --- polling ---------------------------------------------------------

    async def _async_update_data(self) -> SalusState:
        try:
            state = await self.client.async_get_state(self.device_id)
        except SalusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SalusError as err:
            raise UpdateFailed(str(err)) from err
        return self._overlay(state)

    def _overlay(self, state: SalusState) -> SalusState:
        """Show commanded values until the cloud confirms them (or they time out)."""
        now = time.monotonic()
        raw = dict(state.raw)
        for name, (value, expires) in list(self._pending.items()):
            if raw.get(name) == value or now > expires:
                if raw.get(name) != value:
                    _LOGGER.debug("Salus: %s=%s not confirmed by the thermostat", name, value)
                del self._pending[name]
            else:
                raw[name] = value
        return SalusState(raw)

    # --- commands --------------------------------------------------------

    async def async_command(
        self, writes: list[tuple[str, Any]], *, force: tuple[str, ...] = ()
    ) -> None:
        """Send attributes (one per call, in order), update state optimistically, re-read later.

        Only attributes whose value differs from the current state are written
        (fewer calls to the sleeping thermostat), except those in ``force``.
        """
        current = self.data.raw if self.data is not None else {}
        pending = [(n, v) for n, v in writes if current.get(n) != str(v) or n in force]
        if not pending:
            return
        try:
            await self.client.async_set(
                self.device_id, dict(pending), wake=bool(self.option(CONF_WAKE_ON_COMMAND))
            )
        except SalusAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Salus login failed: {err}") from err
        except SalusTimeoutError as err:
            await self.async_request_refresh()
            raise HomeAssistantError(
                "The thermostat did not respond (asleep or weak battery). Try again in a minute."
            ) from err
        except SalusError as err:
            await self.async_request_refresh()
            raise HomeAssistantError(f"Salus command failed: {err}") from err

        expires = time.monotonic() + PENDING_TIMEOUT
        for name, value in pending:
            self._pending[name] = (str(value), expires)
        if self.data is not None:
            self.async_set_updated_data(self._overlay(self.data))
        self._schedule_confirm()

    @callback
    def _schedule_confirm(self) -> None:
        for unsub in self._confirm_unsubs:
            unsub()
        self._confirm_unsubs = [
            async_call_later(self.hass, delay, self._confirm_refresh) for delay in CONFIRM_DELAYS
        ]

    async def _confirm_refresh(self, _now: Any) -> None:
        await self.async_request_refresh()

    @callback
    def async_cancel_timers(self) -> None:
        for unsub in self._confirm_unsubs:
            unsub()
        self._confirm_unsubs = []
        if self._verify_task is not None:
            self._verify_task.cancel()

    # --- high level operations -------------------------------------------

    async def async_set_heating(self, temperature: float) -> None:
        """Heat to ``temperature``.

        Default (hold mode "permanent"): the thermostat's **manual** mode.
        Order, one attribute per call:
          1. ``A93`` manual setpoint - so manual mode starts at the new value,
          2. mode flags ``A92``/``A88``, and ``A89=0`` last when leaving OFF (ZaBug),
          3. ``A85`` setpoint - re-sent if the mode change made the thermostat
             overwrite it (it falls back to its own manual/schedule value).
        """
        state = self.data
        value = state.to_device_temp(temperature)
        if self.option(CONF_HOLD_MODE) == HOLD_MODE_PERMANENT:
            pre = [(ATTR_MANUAL_SETPOINT, value)]
            flags = [(ATTR_MANUAL, 1), (ATTR_TEMP_HOLD, 0), (ATTR_OFF, 0)]
        elif state.manual and not state.zone_off:
            pre, flags = [(ATTR_MANUAL_SETPOINT, value)], []
        else:
            # Schedule mode: hold this temperature until the next scheduled change.
            pre, flags = [], [(ATTR_MANUAL, 0), (ATTR_TEMP_HOLD, 1), (ATTR_OFF, 0)]
        mode_change = any(state.raw.get(n) != str(v) for n, v in flags)
        await self.async_command(
            [*pre, *flags, (ATTR_SETPOINT, value)],
            force=(ATTR_SETPOINT,) if mode_change else (),
        )
        if mode_change:
            self._start_verify(dict(flags), value, bool(pre))

    @callback
    def _start_verify(self, flags: dict[str, Any], value: int, manual: bool) -> None:
        if self._verify_task is not None and not self._verify_task.done():
            self._verify_task.cancel()
        self._verify_task = self.config_entry.async_create_background_task(
            self.hass, self._async_verify_setpoint(flags, value, manual), "salus_verify_setpoint"
        )

    async def _async_verify_setpoint(self, flags: dict[str, Any], value: int, manual: bool) -> None:
        """After a mode change, make sure the thermostat kept our setpoint.

        Seen on a real iT500: after leaving OFF the mode was applied, but the
        setpoint ended at the thermostat's own value (14 °C) instead of 23 °C.
        """
        resent = False
        for delay in VERIFY_DELAYS:
            await asyncio.sleep(delay)
            try:
                cloud = await self.client.async_get_state(self.device_id)
            except SalusError as err:
                _LOGGER.debug("Setpoint verification read failed: %s", err)
                continue
            if any(cloud.raw.get(n) != str(v) for n, v in flags.items()):
                continue  # mode change not reported yet
            if cloud.raw.get(ATTR_SETPOINT) == str(value):
                break
            if resent:
                _LOGGER.warning(
                    "Salus thermostat keeps setpoint %s instead of %s", cloud.raw.get(ATTR_SETPOINT), value
                )
                break
            _LOGGER.debug("Thermostat overwrote the setpoint after the mode change, sending it again")
            writes = [(ATTR_MANUAL_SETPOINT, value)] if manual else []
            try:
                await self.client.async_set(
                    self.device_id,
                    dict([*writes, (ATTR_SETPOINT, value)]),
                    wake=bool(self.option(CONF_WAKE_ON_COMMAND)),
                )
            except SalusError as err:
                _LOGGER.warning("Re-sending the setpoint failed: %s", err)
                break
            resent = True
        await self.async_request_refresh()

    async def async_heating_off(self) -> None:
        """Turn heating 'off' - low setpoint (default) or real thermostat OFF."""
        if self.data.zone_off:
            return  # already off on the thermostat itself
        if self.option(CONF_OFF_MODE) == OFF_MODE_THERMOSTAT:
            await self.async_command([(ATTR_OFF, 1)])
        else:
            await self.async_set_heating(float(self.option(CONF_OFF_TEMPERATURE)))

    async def async_hot_water_boost(self, hours: int | None = None) -> None:
        """Start hot water boost (hwboosthours=1 on the old portal); 0 cancels."""
        if hours is None:
            hours = int(self.option(CONF_HW_BOOST_HOURS))
        hours = max(0, min(int(hours), 3))
        # Rewrite the hours even when equal, so a running boost restarts from the full length.
        await self.async_command([(ATTR_HW_BOOST, hours)], force=(ATTR_HW_BOOST,) if hours else ())

    async def async_hot_water_mode(self, mode: int) -> None:
        await self.async_command([(ATTR_HW_MODE, mode)])
