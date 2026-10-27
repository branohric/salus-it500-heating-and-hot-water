# Salus iT500 – heating + hot water (custom Home Assistant integration)

🇸🇰 [Slovenská verzia](README.sk.md)

A Home Assistant custom integration for the Salus iT500 wired as **thermostat + hot water** (system type `S06 = 2`, CH1 + HW).

## Important: the old web portal is gone

Older setups (Node-RED flows, `salusfy`-style components) sent `POST` requests to **salus-it500.com** (`current_tempZ1`, `hwboosthours`, ...). That portal **stopped working on 22 October 2026**. The mobile app keeps working because it uses a different backend: the **Arrayent API** (`sal-emea-p01-api.arrayent.com`). ladamczyk-it, ZaBug and gvasilakiss all moved to it, and this integration uses it too.

### Old portal commands → Arrayent attributes

| Old portal (`set.php`) | Arrayent attributes (this integration) |
|---|---|
| `hwboosthours_set=1, hwboosthours=1` | `C43 = 1` (hot water boost for 1 h) |
| `hwboosthours_set=1, hwboosthours=0` | `C43 = 0` (cancel boost) |
| `current_tempZ1_set=1, current_tempZ1=23` | thermostat **manual** mode at 23.0 °C: `A93 = 2300`, `A92 = 1`, `A88 = 0`, `A89 = 0`, `A85 = 2300` (only the values that change are written) |
| `current_tempZ1_set=1, current_tempZ1=14` | the same with `1400` |

Temperatures are integers ×100. Writes go through `GET /zdk/services/zamapi/setMultiDeviceAttributes2?devId=…&name1=…&value1=…` **one attribute per call**: the iT500 reports `multiAttributeCapable=false`. If you send several pairs at once (e.g. `A88` + `A85`), the request ends with `requestFault 202 "Request time out"` and nothing is applied, while the app reports the thermostat as not responding / low battery.

Order of the writes: the manual setpoint `A93` first (so manual mode starts at the new value), then the mode flags with `A89=0` last when leaving OFF (verified by ZaBug on real hardware), then the setpoint `A85`.

After a mode change the thermostat may report its own setpoint instead of the one just written (seen on a real unit: schedule value 14 °C instead of 23 °C). The integration and the CLI therefore check the reported state afterwards and send the setpoint once more if needed.

The old portal behaviour (hold the temperature only until the next scheduled change, `A88 = 1`) is still available in the options as *Like the app / old portal*.

## Token handling

Expiring tokens are the classic problem with the Salus API. This integration handles them at several levels:

1. **Login**: `POST /acc/applications/SalusService/sessions` with the email and the **MD5 hash of the password** returns `userId` + `securityToken`. **You don't need to enter the devId**: it is discovered with `getDeviceList`.
2. **Proactive renewal**: after 30 minutes the token is dropped and the next call logs in again (same as ladamczyk; gvasilakiss uses 55 min).
3. **Reactive renewal**: when the server rejects the token (HTTP 401/403 or a `requestFault` with `errorCode 106`), the client logs in **once** more and repeats the request. If the fresh token or the login fails too, Home Assistant starts **re-authentication** (you enter the new password in the UI, no need to remove the integration).
4. The known "32 °C / 32 °C" reading (a broken session) is never shown. The token is dropped instead.

Network errors are retried only a limited number of times (1 s, 3 s). Salus reportedly blocks IP addresses that flood it with requests.

## Entities

| Entity | Description |
|---|---|
| `climate.<name>_heating` | **Virtual thermostat.** *Heat* = target temperature in the thermostat's manual mode (default **23 °C**), *Off* = **14 °C** or the thermostat's real OFF mode (configurable). The temperature you choose in HA is remembered across restarts. |
| `switch.<name>_hot_water_boost` | **Hot water boost.** ON = boost for **1 h** (configurable 1–3 h), OFF = cancel. Turns itself off when the time runs out. |
| `sensor.…_hot_water_boost_remaining` | Hours of boost left |
| `sensor.…_room_temperature`, `…_setpoint` | Temperatures (for graphs) |
| `binary_sensor.…_boiler_heating`, `…_hot_water` | Whether the boiler is heating / hot water is on (relays) |
| `select.…_hot_water_mode` | Schedule / Once / Always on / Always off (`C42`) |
| `binary_sensor.…_thermostat_battery` | Thermostat battery low (`S03`) |
| `binary_sensor.…_online`, `sensor.…_heating_mode` | Diagnostics |

The exact `entity_id`s depend on the device name and on your HA language (entity names are translated to English and Slovak). Check them in *Settings → Devices & services*.

### Command confirmation

The battery-powered thermostat sleeps most of the time and only reports changes to the cloud. So the integration:
- wakes the thermostat before a command (`F = 60`, refresh mode; recommended, can be turned off in the options). Hot water attributes (`C4x`) live in the mains-powered receiver, but heating attributes (`A8x`) have to reach the battery thermostat, which often misses them without the wake-up,
- writes only the attributes whose value actually changes,
- if the thermostat still does not answer (error 202), wakes it again and retries once after 5 s; if that fails too, HA shows an error instead of pretending the change was made,
- shows the new value in HA right away (optimistically) and keeps it until the thermostat confirms it, for at most 3 minutes,
- re-reads the state from the cloud after 8 s and 30 s.

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=branohric&repository=salus-it500-heating-and-hot-water&category=integration)

1. Click the button above, or in HACS open **⋮ → Custom repositories**, add `https://github.com/branohric/salus-it500-heating-and-hot-water` with type **Integration**.
2. Find **Salus iT500 (heating + hot water)** in HACS, click **Download** and restart Home Assistant.

### Manually

Copy the `custom_components/salus_it500_custom` folder into `config/custom_components/` of your Home Assistant (e.g. via the Samba share or Studio Code Server add-on) and restart Home Assistant.

### Setup

1. *Settings → Devices & services → Add integration* → **Salus iT500 (heating + hot water)**.
2. Enter the email and password from the Salus iT500 mobile app. If the account has several devices, pick one.
3. Optional: *Configure* → default temperature, what "off" means, boost length, polling interval.

Requires Home Assistant **2024.12** or newer. Tested against HA 2026.2.

> Password with special characters: Salus silently drops them when the password is created. If login fails, try the password without them (per ZaBug).

## Check the API from your computer first

```bash
cd tools
SALUS_EMAIL='you@example.com' python3 salus_cli.py status   # login, devId, state
python3 salus_cli.py raw                                     # all attributes (check C43, C45, S06)
python3 salus_cli.py boost 1                                 # hot water boost 1 h
python3 salus_cli.py boost 0                                 # cancel the hot water boost
python3 salus_cli.py heat 23                                 # heating to 23 °C in the thermostat's manual mode (also leaves OFF)
python3 salus_cli.py off                                     # heating "off" = 14 °C, manual mode
```

The script needs no libraries. It asks for the password interactively unless you set `SALUS_PASSWORD`.

## Examples

Dashboard card:

```yaml
type: vertical-stack
cards:
  - type: thermostat
    entity: climate.home_heating
  - type: entities
    entities:
      - entity: switch.home_hot_water_boost
      - entity: sensor.home_hot_water_boost_remaining
      - entity: binary_sensor.home_boiler_heating
```

Automation (hot water boost when you get home):

```yaml
alias: Hot water on arrival
triggers:
  - trigger: state
    entity_id: person.you
    to: home
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.home_hot_water_boost
```

## Debugging

```yaml
logger:
  logs:
    custom_components.salus_it500_custom: debug
```

Tokens and passwords never end up in the log.

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

The tests run a fake Arrayent server over real HTTP. They cover login, token expiry (3 server response variants), proactive renewal, the "32/32" reading, one attribute per call, a sleeping or unreachable thermostat (error 202 + retry), leaving OFF mode, tolerating a bare HTTP 500, config flow, re-auth, options and all heating and boost commands.

## Credits

- [ladamczyk-it/salus-it500](https://github.com/ladamczyk-it/salus-it500): same wiring (thermostat + hot water), portal → Arrayent mapping
- [ZaBug/salus-it500](https://github.com/ZaBug/salus-it500): refresh mode `F`, error code 106, the 32/32 issue
- [gvasilakiss/salus-it500](https://github.com/gvasilakiss/salus-it500): complete attribute map (`C42`/`C43`/`C45`)

The API is unofficial and Salus may change it at any time.
