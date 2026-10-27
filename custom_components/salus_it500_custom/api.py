"""Async client for the Salus iT500 cloud (Arrayent backend used by the mobile app).

The old web portal salus-it500.com (set.php with current_tempZ1 / hwboosthours)
stopped working on 2026-10-22. The mobile app backend kept working, so this
client talks to it directly.

Token handling (the main pain point with the old Node-RED flow):

* login returns ``userId`` + ``securityToken``;
* the token is renewed **proactively** after ``TOKEN_TTL`` seconds;
* it is also renewed **reactively** - when the API rejects it (HTTP 401/403 or
  a ``requestFault`` with error code 106) the client logs in again and repeats
  the request exactly once;
* a "32 °C / 32 °C" reading (a known symptom of a broken session) drops the
  token as well.

No Home Assistant imports here, so the module can be tested on its own.
Protocol references: ladamczyk-it/salus-it500, ZaBug/salus-it500,
gvasilakiss/salus-it500, RichyA/pyit500.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "https://sal-emea-p01-api.arrayent.com"
LOGIN_PATH = "/acc/applications/SalusService/sessions"
ZAMAPI_PATH = "/zdk/services/zamapi"

# Identifies the Salus tenant on Arrayent; same value the iT500 app sends. Not a secret.
APP_AUTHORIZATION = "687886-679716122"

# Renew the token proactively well before it could expire.
TOKEN_TTL = 30 * 60
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Back-off for network-level errors only (Salus is known to block IPs that hammer it).
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)
# Wait before retrying a write the thermostat did not answer (error 202).
TIMEOUT_RETRY_DELAY = 5.0

ERROR_INVALID_TOKEN = 106
ERROR_DEVICE_TIMEOUT = 202  # "Request time out" - the gateway could not reach the thermostat

# --- Attribute names -------------------------------------------------------
# Zone prefix A = heating zone 1, C = hot water, S = system.
ATTR_ROOM_TEMP = "A84"  # x100
ATTR_SETPOINT = "A85"  # x100
ATTR_HEAT_RELAY = "A87"  # 1 = boiler is called
ATTR_TEMP_HOLD = "A88"  # 1 = hold until next scheduled change
ATTR_OFF = "A89"  # 1 = zone switched off
ATTR_FROST_ACTIVE = "A90"
ATTR_HEAT_BOOST = "A91"  # boost hours remaining
ATTR_MANUAL = "A92"  # 1 = permanent hold (schedule ignored)
ATTR_MANUAL_SETPOINT = "A93"
ATTR_HW_MODE = "C42"  # 0 auto, 1 once, 2 on, 3 off
ATTR_HW_BOOST = "C43"  # hot water boost hours remaining (write 1-3, 0 = cancel)
ATTR_HW_RELAY = "C45"  # 1 = hot water on
ATTR_SYSTEM_TYPE = "S06"  # 0 CH1, 1 CH1+CH2, 2 CH1+HW
ATTR_TEMP_UNIT = "S07"  # 0 C, 1 F
ATTR_FROST_TEMP = "S09"
ATTR_BATTERY = "S03"  # "0" = OK, anything else = low
ATTR_REFRESH = "F"  # "turn on refresh mode for X seconds" - wakes the thermostat
ATTR_ONLINE = "online"

_SECRET_RE = re.compile(r"(secToken|password)=[^&\s'\"]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Remove tokens / passwords from text that may end up in a log."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)


class SalusError(Exception):
    """Base error."""


class SalusAuthError(SalusError):
    """Credentials rejected."""


class SalusConnectionError(SalusError):
    """Network problem or unexpected server answer."""


class SalusApiError(SalusError):
    """The API answered with an error."""


class SalusTimeoutError(SalusApiError):
    """Error 202: the gateway did not get an answer from the (sleeping) thermostat."""


@dataclass(slots=True)
class SalusDevice:
    """Device from the account's device list."""

    device_id: str
    name: str


@dataclass(slots=True)
class SalusState:
    """Normalised snapshot of the thermostat."""

    raw: dict[str, str] = field(default_factory=dict)

    def _int(self, name: str, default: int | None = None) -> int | None:
        value = self.raw.get(name, "")
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @property
    def fahrenheit(self) -> bool:
        return self.raw.get(ATTR_TEMP_UNIT) == "1"

    def _temp(self, name: str) -> float | None:
        value = self._int(name)
        if value is None:
            return None
        degrees = value / 100
        if self.fahrenheit:
            degrees = (degrees - 32) / 1.8
        return round(degrees, 1)

    @property
    def room_temperature(self) -> float | None:
        return self._temp(ATTR_ROOM_TEMP)

    @property
    def setpoint(self) -> float | None:
        return self._temp(ATTR_SETPOINT)

    @property
    def frost_temperature(self) -> float | None:
        return self._temp(ATTR_FROST_TEMP)

    @property
    def heating_relay(self) -> bool:
        return self._int(ATTR_HEAT_RELAY, 0) == 1

    @property
    def zone_off(self) -> bool:
        return self._int(ATTR_OFF, 0) == 1

    @property
    def manual(self) -> bool:
        return self._int(ATTR_MANUAL, 0) == 1

    @property
    def temp_hold(self) -> bool:
        return self._int(ATTR_TEMP_HOLD, 0) == 1

    @property
    def heating_mode(self) -> str:
        """auto / temp_hold / manual / off."""
        if self.zone_off:
            return "off"
        if self.manual:
            return "manual"
        if self.temp_hold:
            return "temp_hold"
        return "auto"

    @property
    def heating_boost_hours(self) -> int:
        return self._int(ATTR_HEAT_BOOST, 0) or 0

    @property
    def has_hot_water(self) -> bool:
        return ATTR_HW_RELAY in self.raw or self.raw.get(ATTR_SYSTEM_TYPE) == "2"

    @property
    def hot_water_on(self) -> bool:
        return self._int(ATTR_HW_RELAY, 0) == 1

    @property
    def hot_water_boost_hours(self) -> int:
        return self._int(ATTR_HW_BOOST, 0) or 0

    @property
    def hot_water_mode(self) -> int | None:
        return self._int(ATTR_HW_MODE)

    @property
    def battery_low(self) -> bool:
        return self.raw.get(ATTR_BATTERY, "0") not in ("0", "")

    @property
    def online(self) -> bool:
        # 'online' is the gateway; a thermostat without RF reports 0 °C everywhere.
        if self.raw.get(ATTR_ONLINE, "1") in ("0", "false"):
            return False
        return self._int(ATTR_ROOM_TEMP, 0) != 0

    def to_device_temp(self, celsius: float) -> int:
        """Setpoint in the API unit (x100), on the thermostat's 0.1 degree grid."""
        degrees = celsius * 1.8 + 32 if self.fahrenheit else celsius
        return int(round(degrees * 10)) * 10


def hash_password(password: str) -> str:
    """The API expects the MD5 hex digest of the password."""
    return hashlib.md5(password.encode("utf-8")).hexdigest()  # noqa: S324 - protocol


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _child(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as err:
        raise SalusConnectionError(f"Malformed XML from Salus: {text[:200]!r}") from err


def fault_code(text: str) -> int | None:
    """Error code of a requestFault document, if the text is one."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if _local(root.tag) != "requestFault" and not any(
        _local(el.tag) == "errorCode" for el in root.iter()
    ):
        return None
    for el in root.iter():
        if _local(el.tag) == "errorCode" and (el.text or "").strip().lstrip("-").isdigit():
            return int((el.text or "").strip())
    return -1


def parse_attributes(text: str) -> dict[str, str]:
    root = parse_xml(text)
    attrs: dict[str, str] = {}
    for element in root.iter():
        if _local(element.tag) != "attrList":
            continue
        name = _child(element, "name")
        if name:
            attrs[name] = _child(element, "value")
    return attrs


def parse_devices(text: str) -> list[SalusDevice]:
    root = parse_xml(text)
    devices = []
    for element in root.iter():
        if _local(element.tag) != "devList":
            continue
        dev_id = _child(element, "devId")
        if dev_id:
            devices.append(SalusDevice(dev_id, _child(element, "devName") or f"iT500 {dev_id}"))
    return devices


class SalusClient:
    """One authenticated session against the Salus cloud."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        host: str = DEFAULT_HOST,
        token_ttl: float = TOKEN_TTL,
    ) -> None:
        self._session = session
        self._username = username
        self._password_hash = hash_password(password)
        self._host = host.rstrip("/")
        self._token_ttl = token_ttl
        self._token: str | None = None
        self._user_id: str | None = None
        self._token_at = 0.0
        self._lock = asyncio.Lock()
        self.timeout_retry_delay = TIMEOUT_RETRY_DELAY

    # --- session ---------------------------------------------------------

    @property
    def token_age(self) -> float | None:
        """Seconds since the current token was obtained (diagnostics)."""
        return None if self._token is None else time.monotonic() - self._token_at

    def invalidate_token(self) -> None:
        """Forget the token; the next call logs in again."""
        self._token = None

    async def async_login(self) -> None:
        """Exchange credentials for a fresh security token."""
        url = f"{self._host}{LOGIN_PATH}"
        headers = {
            "Authorization": APP_AUTHORIZATION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = {"username": self._username, "password": self._password_hash}
        status, text = await self._raw("post", url, json=payload, headers=headers)
        try:
            data: Any = json.loads(text)
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if status in (401, 403) or (status < 500 and "errorCode" in data):
            raise SalusAuthError(data.get("errorMessage") or f"Login rejected (HTTP {status})")
        if status != 200 or "securityToken" not in data:
            raise SalusConnectionError(f"Unexpected login response (HTTP {status})")
        self._token = str(data["securityToken"])
        self._user_id = str(data.get("userId", ""))
        self._token_at = time.monotonic()
        _LOGGER.debug("Salus login OK (user %s)", self._user_id)

    def _token_valid(self) -> bool:
        return self._token is not None and time.monotonic() - self._token_at < self._token_ttl

    async def _raw(self, method: str, url: str, **kwargs: Any) -> tuple[int, str]:
        """HTTP call with bounded retries on network errors only."""
        last: Exception | None = None
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._session.request(
                    method, url, timeout=REQUEST_TIMEOUT, **kwargs
                ) as resp:
                    return resp.status, await resp.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                last = err
                _LOGGER.debug("Salus network error (attempt %s): %s", attempt + 1, redact(str(err)))
        raise SalusConnectionError(f"Cannot reach Salus cloud: {redact(str(last))}") from last

    async def _call(self, endpoint: str, params: dict[str, Any]) -> tuple[int, str]:
        """Authenticated zamapi call. Re-logs in once when the token is rejected.

        Caller holds ``self._lock``.
        """
        url = f"{self._host}{ZAMAPI_PATH}/{endpoint}"
        for attempt in range(2):
            if not self._token_valid():
                await self.async_login()
            query = {k: str(v) for k, v in params.items()}
            query["secToken"] = self._token or ""
            query["userId"] = self._user_id or ""
            # GET with query string: zamapi rejects POST without a body (SOAP 500).
            status, text = await self._raw("get", url, params=query)
            code = fault_code(text) if status != 200 or "requestFault" in text else None
            if status in (401, 403) or code == ERROR_INVALID_TOKEN:
                if attempt == 0:
                    _LOGGER.debug("Salus token rejected on %s, logging in again", endpoint)
                    self._token = None
                    continue
                raise SalusAuthError("Salus rejected a freshly obtained token")
            return status, text
        raise SalusAuthError("Salus rejected a freshly obtained token")  # pragma: no cover

    # --- reads -----------------------------------------------------------

    async def async_get_devices(self) -> list[SalusDevice]:
        async with self._lock:
            status, text = await self._call("getDeviceList", {})
        if status != 200:
            raise SalusConnectionError(f"getDeviceList returned HTTP {status}")
        return parse_devices(text)

    async def async_get_state(self, device_id: str) -> SalusState:
        async with self._lock:
            status, text = await self._call(
                "getDeviceAttributesWithValues", {"devId": device_id, "deviceTypeId": 1}
            )
            if status != 200:
                raise SalusConnectionError(
                    f"getDeviceAttributesWithValues returned HTTP {status}"
                )
            attrs = parse_attributes(text)
            if not attrs:
                raise SalusApiError(f"No attributes returned for device {device_id}")
            if attrs.get(ATTR_ROOM_TEMP) == "3200" and attrs.get(ATTR_SETPOINT) == "3200":
                # Known broken-session symptom: drop the token, don't show the value.
                self._token = None
                raise SalusConnectionError("Broken Salus session (32/32 °C), will log in again")
        return SalusState(attrs)

    # --- writes ----------------------------------------------------------

    async def _set_one(self, device_id: str, name: str, value: Any) -> None:
        """One setMultiDeviceAttributes2 call with one pair. Caller holds the lock."""
        _LOGGER.debug("Salus set %s=%s", name, value)
        status, text = await self._call(
            "setMultiDeviceAttributes2", {"devId": device_id, "name1": name, "value1": value}
        )
        code = fault_code(text)
        if code == ERROR_DEVICE_TIMEOUT:
            raise SalusTimeoutError(f"Thermostat did not respond to {name}={value}")
        if code is not None:
            raise SalusApiError(f"Salus refused {name}={value} (error {code})")
        if status == 500:
            # 500 without a requestFault body: known false negative, the write
            # is usually applied. The coordinator re-reads the state to confirm.
            _LOGGER.debug("setMultiDeviceAttributes2 answered a bare 500 (usually fine)")
            return
        if status != 200:
            raise SalusConnectionError(f"setMultiDeviceAttributes2 returned HTTP {status}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return
        ret = ""
        for el in root.iter():
            if _local(el.tag) == "retCode":
                ret = (el.text or "").strip()
        if ret not in ("", "0"):
            raise SalusApiError(f"Salus refused {name}={value} (retCode {ret})")

    async def async_set(self, device_id: str, pairs: dict[str, Any], *, wake: bool = False) -> None:
        """Write attributes **one per call**, in the given order.

        The iT500 reports ``multiAttributeCapable=false``: several pairs in one
        call end with error 202 "Request time out" and nothing is applied.
        With ``wake`` the thermostat is first put in refresh mode (F), which the
        battery thermostat needs to receive A* writes quickly. A timeout (202)
        is retried once after waking it again.
        """
        async with self._lock:
            if wake:
                await self._wake_locked(device_id)
            for name, value in pairs.items():
                try:
                    await self._set_one(device_id, name, value)
                except SalusTimeoutError:
                    _LOGGER.debug("Thermostat timed out on %s, waking it and retrying once", name)
                    await self._wake_locked(device_id)
                    await asyncio.sleep(self.timeout_retry_delay)
                    await self._set_one(device_id, name, value)

    async def _wake_locked(self, device_id: str, seconds: int = 60) -> None:
        try:
            await self._set_one(device_id, ATTR_REFRESH, seconds)
        except SalusError as err:
            _LOGGER.debug("Wake request failed (ignored): %s", err)

    async def async_wake(self, device_id: str, seconds: int = 60) -> None:
        """Best effort: ask the thermostat to report now (refresh mode)."""
        async with self._lock:
            await self._wake_locked(device_id, seconds)
