#!/usr/bin/env python3
"""Quick check of the Salus iT500 cloud API - no dependencies, Python 3.9+.

Usage:
  python3 salus_cli.py status            # login, list devices, show state
  python3 salus_cli.py heat 23           # heating to 23 °C in the thermostat's MANUAL mode (also leaves OFF)
  python3 salus_cli.py off               # heating "off" = 14 °C, manual mode (like the old Node-RED flow)

Writes go one attribute per call, after waking the thermostat (F=60), and the
script then waits up to 30 s for the thermostat to confirm.
  python3 salus_cli.py boost 1           # hot water boost 1 h  (0 = cancel)
  python3 salus_cli.py raw               # dump all attributes

Credentials from env SALUS_EMAIL / SALUS_PASSWORD (asked interactively otherwise).
Optional SALUS_DEVICE_ID if the account has more than one device.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HOST = "https://sal-emea-p01-api.arrayent.com"
APP_AUTHORIZATION = "687886-679716122"


def local(tag: str) -> str:
    return tag.rpartition("}")[2]


def http(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def login(email: str, password: str) -> tuple[str, str]:
    body = json.dumps({"username": email, "password": hashlib.md5(password.encode()).hexdigest()}).encode()
    status, text = http("POST", f"{HOST}/acc/applications/SalusService/sessions", body, {
        "Authorization": APP_AUTHORIZATION, "Content-Type": "application/json", "Accept": "application/json"})
    if status != 200:
        sys.exit(f"Login failed (HTTP {status}): {text[:300]}")
    data = json.loads(text)
    return str(data["userId"]), data["securityToken"]


def zamapi(method: str, user_id: str, token: str, **params) -> tuple[int, str]:
    query = urllib.parse.urlencode({**{k: str(v) for k, v in params.items()}, "secToken": token, "userId": user_id})
    return http("GET", f"{HOST}/zdk/services/zamapi/{method}?{query}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    email = os.environ.get("SALUS_EMAIL") or input("Salus e-mail: ")
    password = os.environ.get("SALUS_PASSWORD") or getpass.getpass("Salus heslo: ")

    user_id, token = login(email, password)
    print(f"Login OK, userId={user_id}, token={token[:6]}…")

    _, text = zamapi("getDeviceList", user_id, token)
    devices = [
        {local(c.tag): (c.text or "").strip() for c in el}
        for el in ET.fromstring(text).iter() if local(el.tag) == "devList"
    ]
    for d in devices:
        print(f"  device: devId={d.get('devId')} name={d.get('devName')}")
    if not devices:
        sys.exit("No device on this account.")
    dev = os.environ.get("SALUS_DEVICE_ID") or devices[0]["devId"]

    def read() -> dict[str, str]:
        _, xml = zamapi("getDeviceAttributesWithValues", user_id, token, devId=dev, deviceTypeId=1)
        attrs = {}
        for el in ET.fromstring(xml).iter():
            if local(el.tag) == "attrList":
                kv = {local(c.tag): (c.text or "").strip() for c in el}
                attrs[kv.get("name", "")] = kv.get("value", "")
        return attrs

    def set_one(name: str, value) -> str | None:
        """One attribute per call (the iT500 reports multiAttributeCapable=false)."""
        status, text = zamapi("setMultiDeviceAttributes2", user_id, token,
                              devId=dev, name1=name, value1=value)
        code = None
        try:
            for el in ET.fromstring(text).iter():
                if local(el.tag) == "errorCode":
                    code = (el.text or "").strip()
        except ET.ParseError:
            pass
        print(f"  set {name}={value} -> HTTP {status}" + (f", error {code}" if code else ""))
        return code

    def write(pairs: list[tuple[str, int]], force: tuple[str, ...] = ()) -> bool:
        """Wake the thermostat (F=60), then write only changed values, one per call."""
        todo = [(n, v) for n, v in pairs if a.get(n) != str(v) or n in force]
        if not todo:
            print("  nothing to change")
            return True
        set_one("F", 60)
        for name, value in todo:
            if set_one(name, value) == "202":  # Request time out - thermostat did not answer
                print("  thermostat did not answer, waking it again and retrying in 5 s")
                set_one("F", 60)
                time.sleep(5)
                if set_one(name, value) == "202":
                    print("  !! thermostat still not answering (asleep / out of range / weak battery)")
                    return False
        return True

    def heat(temp_x100: int) -> None:
        """Manual mode at temp: A93 (manual setpoint) first, mode flags (A89=0 last), then A85."""
        flags = [("A92", 1), ("A88", 0), ("A89", 0)]
        mode_change = any(a.get(n) != str(v) for n, v in flags)
        setpoint = [("A93", temp_x100), ("A85", temp_x100)]
        if not write([setpoint[0], *flags, setpoint[1]], force=("A85",) if mode_change else ()):
            return
        confirm(flags, setpoint)

    def confirm(flags: list[tuple[str, int]], values: list[tuple[str, int]]) -> None:
        """Wait for the thermostat to report; if it overwrote the setpoint after the
        mode change (seen on a real unit: 14 °C instead of 23 °C), send it once more."""
        nonlocal a
        resent = False
        for _ in range(8):
            time.sleep(5)
            a = read()
            if not all(a.get(n) == str(v) for n, v in flags):
                continue  # mode change not reported yet
            if all(a.get(n) == str(v) for n, v in values):
                print("  confirmed by the thermostat")
                return
            if resent:
                break
            print(f"  thermostat reports setpoint A85={a.get('A85')} A93={a.get('A93')} - sending it again")
            if not write(values, force=tuple(n for n, _ in values)):
                return
            resent = True
        print("  not confirmed (cloud copy differs) - check the app in a minute")

    a = read()
    if cmd == "raw":
        for k in sorted(a):
            print(f"  {k} = {a[k]}")
        return
    pairs: list[tuple[str, int]] = []
    if cmd == "heat":
        heat(int(round(float(sys.argv[2] if len(sys.argv) > 2 else 23) * 10)) * 10)
    elif cmd == "off":
        if a.get("A89") == "1":
            print("  heating is already OFF on the thermostat")
        else:
            heat(1400)
    elif cmd == "boost":
        pairs = [("C43", int(sys.argv[2] if len(sys.argv) > 2 else 1))]
    elif cmd != "status":
        sys.exit(__doc__)
    if pairs and write(pairs, force=("C43",)):
        confirm([], pairs)

    mode = "off" if a.get("A89") == "1" else "manual" if a.get("A92") == "1" else "temp_hold" if a.get("A88") == "1" else "auto"
    t = lambda k: f"{int(a[k]) / 100:.1f} °C" if a.get(k, "").lstrip("-").isdigit() else "?"
    print(f"Room {t('A84')} | setpoint {t('A85')} | mode {mode} | boiler {'ON' if a.get('A87') == '1' else 'off'}"
          f" | battery {'OK' if a.get('S03', '0') == '0' else 'LOW'}")
    print(f"Hot water: relay {'ON' if a.get('C45') == '1' else 'off'} | boost {a.get('C43', '?')} h | mode C42={a.get('C42', '?')}")
    print("(cloud copy - a sleeping thermostat may report the change after a while)")


if __name__ == "__main__":
    main()
