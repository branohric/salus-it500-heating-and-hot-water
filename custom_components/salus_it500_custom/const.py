"""Constants for the Salus iT500 (custom) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "salus_it500_custom"
MANUFACTURER: Final = "Salus"
MODEL: Final = "iT500"

CONF_DEVICE_ID: Final = "device_id"

# Options
CONF_DEFAULT_TARGET: Final = "default_target"
CONF_OFF_TEMPERATURE: Final = "off_temperature"
CONF_OFF_MODE: Final = "off_mode"
CONF_HOLD_MODE: Final = "hold_mode"
CONF_HW_BOOST_HOURS: Final = "hw_boost_hours"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_WAKE_ON_COMMAND: Final = "wake_on_command"

# "Off" = low setpoint (14 °C, like the old Node-RED flow) or the thermostat's real OFF mode.
OFF_MODE_SETPOINT: Final = "setpoint"
OFF_MODE_THERMOSTAT: Final = "thermostat"
# Setpoint change = the thermostat's manual mode (default, schedule ignored), or
# like the old portal (keep manual if manual, else hold until next scheduled change).
HOLD_MODE_FOLLOW: Final = "follow"
HOLD_MODE_PERMANENT: Final = "permanent"

DEFAULTS: Final = {
    CONF_DEFAULT_TARGET: 23.0,
    CONF_OFF_TEMPERATURE: 14.0,
    CONF_OFF_MODE: OFF_MODE_SETPOINT,
    CONF_HOLD_MODE: HOLD_MODE_PERMANENT,
    CONF_HW_BOOST_HOURS: 1,
    CONF_SCAN_INTERVAL: 60,
    CONF_WAKE_ON_COMMAND: True,
}

MIN_TEMP: Final = 5.0
MAX_TEMP: Final = 34.5
TEMP_STEP: Final = 0.5

# How long an optimistic value is shown while the (sleeping) thermostat has
# not reported the change to the cloud yet.
PENDING_TIMEOUT: Final = 180
# Re-read the cloud this many seconds after a command.
CONFIRM_DELAYS: Final = (8, 30)
# After a mode change, check (and if needed re-send) the setpoint after these pauses.
VERIFY_DELAYS: Final = (5, 10, 15, 30)

HW_MODES: Final = {0: "auto", 1: "once", 2: "on", 3: "off"}
