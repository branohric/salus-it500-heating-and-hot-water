"""In-process fake of the Arrayent (Salus) cloud API, served over real HTTP."""

from __future__ import annotations

import hashlib
import itertools

from aiohttp import web

NS = "http://www.arrayent.com/zamapi"


class FakeSalus:
    def __init__(self, email="user@example.com", password="secret", device_id="1234567"):
        self.email = email
        self.pw_md5 = hashlib.md5(password.encode()).hexdigest()
        self.device_id = device_id
        self.valid_tokens: set[str] = set()
        self._counter = itertools.count(1)
        self.logins = 0
        self.sets: list[list[tuple[str, str]]] = []
        self.set_status = 200  # 500 = simulate Salus false negative
        self.fault_style = "http500"  # how an invalid token is reported: http500 | http200 | http401
        # Like the real battery thermostat: A*/S* writes time out (202) until F (refresh mode) wakes it.
        self.asleep = True
        self.unreachable = False  # thermostat never answers (flat battery / out of range)
        self.timeouts = 0
        # Seen on the real unit: after a mode change the thermostat reports its own
        # setpoint (A94 = schedule value) instead of the A85 written right after it.
        self.mode_change_overwrites_setpoint = False
        self._mode_changed = False
        self.attrs = {
            "A84": "2150", "A85": "2000", "A87": "0", "A88": "0", "A89": "0",
            "A90": "0", "A91": "0", "A92": "0", "A93": "2000", "A94": "1400",
            "C42": "0", "C43": "0", "C45": "0",
            "S03": "0", "S06": "2", "S07": "0", "S09": "500", "online": "1",
        }

    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    # --- handlers --------------------------------------------------------

    async def login(self, request: web.Request) -> web.Response:
        assert request.headers.get("Authorization") == "687886-679716122"
        body = await request.json()
        self.logins += 1
        if body.get("username") != self.email or body.get("password") != self.pw_md5:
            return web.json_response({"errorCode": 101, "errorMessage": "bad credentials"}, status=401)
        token = f"tok-{next(self._counter)}"
        self.valid_tokens.add(token)
        return web.json_response({"userId": 42, "securityToken": token})

    def _auth_fault(self) -> web.Response:
        xml = (f'<ns2:requestFault xmlns:ns2="{NS}"><errorCode>106</errorCode>'
               "<errorMsg>Invalid security token</errorMsg></ns2:requestFault>")
        status = {"http500": 500, "http200": 200, "http401": 401}[self.fault_style]
        return web.Response(text=xml, status=status, content_type="text/xml")

    def _authorized(self, request: web.Request) -> bool:
        return request.query.get("secToken") in self.valid_tokens and request.query.get("userId") == "42"

    async def zamapi(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._auth_fault()
        method = request.match_info["method"]
        if method == "getDeviceList":
            xml = (f'<ns2:getDeviceListResponse xmlns:ns2="{NS}"><devList><devId>{self.device_id}</devId>'
                   "<devName>Doma</devName><typeId>1</typeId></devList></ns2:getDeviceListResponse>")
        elif method == "getDeviceAttributesWithValues":
            assert request.query["devId"] == self.device_id
            if self._mode_changed and self.mode_change_overwrites_setpoint:
                self.attrs["A85"] = self.attrs["A94"]
                self.mode_change_overwrites_setpoint = False  # only once
            self._mode_changed = False
            items = "".join(
                f"<attrList><name>{k}</name><value>{v}</value><updTime>1</updTime></attrList>"
                for k, v in self.attrs.items()
            )
            xml = f'<ns2:getDeviceAttributesWithValuesResponse xmlns:ns2="{NS}">{items}</ns2:getDeviceAttributesWithValuesResponse>'
        elif method == "setMultiDeviceAttributes2":
            pairs = []
            for i in (1, 2, 3):
                if f"name{i}" in request.query:
                    pairs.append((request.query[f"name{i}"], request.query[f"value{i}"]))
            assert pairs and len(pairs) <= 3
            self.sets.append(pairs)
            needs_thermostat = any(n[0] in "AS" for n, _ in pairs)
            if len(pairs) > 1 or (needs_thermostat and (self.asleep or self.unreachable)):
                # multiAttributeCapable=false, or the thermostat is asleep: nothing is applied.
                self.timeouts += 1
                return web.Response(
                    text=f'<ns1:requestFault xmlns:ns1="{NS}/"><errorCode>202</errorCode>'
                    "<errorMsg>Request time out.</errorMsg></ns1:requestFault>",
                    status=500, content_type="text/xml",
                )
            for name, value in pairs:
                if name == "F":
                    self.asleep = False
                else:
                    if name in ("A88", "A89", "A92") and self.attrs.get(name) != value:
                        self._mode_changed = True
                    self.attrs[name] = value
            if self.set_status == 500:
                return web.Response(text="<soap:Fault/>", status=500)
            xml = f'<ns2:setMultiDeviceAttributes2Response xmlns:ns2="{NS}"><retCode>0</retCode></ns2:setMultiDeviceAttributes2Response>'
        else:
            return web.Response(status=404)
        return web.Response(text=xml, content_type="text/xml")

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/acc/applications/SalusService/sessions", self.login)
        app.router.add_get("/zdk/services/zamapi/{method}", self.zamapi)
        return app

    def all_set_pairs(self) -> list[tuple[str, str]]:
        """Applied-or-attempted pairs without wake-ups and without duplicates from retries."""
        out: list[tuple[str, str]] = []
        for call in self.sets:
            for p in call:
                if p[0] != "F" and (not out or out[-1] != p):
                    out.append(p)
        return out
