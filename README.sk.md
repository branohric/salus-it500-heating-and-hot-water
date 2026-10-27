# Salus iT500 – kúrenie + teplá voda (vlastná HA integrácia)

🇬🇧 [English version](README.md)

Custom integrácia pre Home Assistant pre Salus iT500 v zapojení **termostat + ohrev teplej vody** (systémový typ `S06 = 2`, CH1 + HW).

## Dôležité: starý portál nefunguje

Pôvodný Node-RED flow posielal `POST` na **salus-it500.com** (`current_tempZ1`, `hwboosthours`, ...). Tento portál **prestal fungovať 22. 10. 2026**. Mobilná aplikácia funguje ďalej, lebo používa iný backend: **Arrayent API** (`sal-emea-p01-api.arrayent.com`). Na ten prešli aj ladamczyk-it, ZaBug aj gvasilakiss, a rovnako ho používa aj táto integrácia.

### Preklad Node-RED príkazov

| Node-RED (starý portál) | Arrayent atribúty (táto integrácia) |
|---|---|
| `hwboosthours_set=1, hwboosthours=1` | `C43 = 1` (boost teplej vody 1 h) |
| `hwboosthours_set=1, hwboosthours=0` | `C43 = 0` (zrušenie boostu) |
| `current_tempZ1_set=1, current_tempZ1=23` | **manuálny** režim termostatu na 23,0 °C: `A93 = 2300`, `A92 = 1`, `A88 = 0`, `A89 = 0`, `A85 = 2300` (zapisujú sa len hodnoty, ktoré sa menia) |
| `current_tempZ1_set=1, current_tempZ1=14` | to isté s `1400` |

Teploty sú celé čísla ×100. Zápis ide cez `GET /zdk/services/zamapi/setMultiDeviceAttributes2?devId=…&name1=…&value1=…` **vždy jeden atribút na jedno volanie**: iT500 hlási `multiAttributeCapable=false`. Pri viacerých pároch naraz (napr. `A88` + `A85`) príde `requestFault 202 "Request time out"` a nič sa nezapíše. Aplikácia potom hlási, že termostat neodpovedá / slabá batéria.

Poradie zápisov: najprv manuálna teplota `A93` (aby manuálny režim naštartoval už s novou hodnotou), potom príznaky režimu s `A89=0` ako posledným pri zapínaní z OFF (overené ZaBugom na reálnom zariadení) a nakoniec teplota `A85`.

Po zmene režimu môže termostat nahlásiť vlastnú teplotu namiesto práve zapísanej (videné na reálnom zariadení: 14 °C z programu namiesto 23 °C). Integrácia aj CLI preto stav po zápise skontrolujú a teplotu v prípade potreby pošlú ešte raz.

Správanie starého portálu (podržať teplotu len do ďalšej zmeny v programe, `A88 = 1`) sa dá zapnúť v nastaveniach ako *Ako aplikácia / starý portál*.

## Token – ako je to vyriešené

Toto bol hlavný problém v Node-RED. Integrácia ho rieši na troch úrovniach:

1. **Login** – `POST /acc/applications/SalusService/sessions` s e-mailom a **MD5 hashom hesla** vráti `userId` + `securityToken`. **devId netreba zadávať**: zistí sa sám cez `getDeviceList`.
2. **Proaktívne obnovenie** – token sa po 30 minútach automaticky zahodí a pri ďalšom volaní sa urobí nový login (rovnako ako ladamczyk; gvasilakiss používa 55 min).
3. **Reaktívne obnovenie** – keď server token odmietne (HTTP 401/403 alebo `requestFault` s `errorCode 106`), klient sa **raz** znova prihlási a požiadavku zopakuje. Ak zlyhá aj čerstvý token alebo login, HA spustí **re-autentifikáciu** (zadáš nové heslo v UI, nemusíš integráciu mazať).
4. Známa chyba „32 °C / 32 °C“ (pokazená session) sa nezobrazí. Namiesto toho sa token zahodí.

Sieťové chyby sa opakujú len obmedzene (1 s, 3 s). Salus vraj blokuje IP adresy, ktoré ho zahltia požiadavkami.

## Čo vznikne v HA

| Entita | Popis |
|---|---|
| `climate.<názov>_kurenie` | **Virtuálny termostat.** *Kúrenie* = cieľová teplota v manuálnom režime termostatu (predvolene **23 °C**), *Vypnuté* = **14 °C** (ako v Node-RED) alebo skutočný režim OFF termostatu (dá sa prepnúť v nastaveniach). Teplota zvolená v HA sa zapamätá aj po reštarte. |
| `switch.<názov>_ohrev_vody_boost` | **Boost teplej vody.** ON = boost na **1 h** (dá sa nastaviť 1–3 h), OFF = zrušenie. Po uplynutí sa sám vypne. |
| `sensor.…_zostavajuci_boost_vody` | Koľko hodín boostu ešte zostáva |
| `sensor.…_teplota_v_miestnosti`, `…_nastavena_teplota` | Teploty (na grafy) |
| `binary_sensor.…_kotol_kurenie`, `…_ohrev_vody` | Či kotol práve kúri / či je zapnutý ohrev vody (relé) |
| `select.…_rezim_teplej_vody` | Program / Raz / Stále zapnuté / Stále vypnuté (`C42`) |
| `binary_sensor.…_bateria_termostatu` | Slabá batéria termostatu (`S03`) |
| `binary_sensor.…_online`, `sensor.…_rezim_kurenia` | Diagnostika |

Presné `entity_id` závisia od názvu zariadenia a jazyka HA. Skontroluj ich v *Nastavenia → Zariadenia a služby*.

### Potvrdenie príkazov

Batériový termostat väčšinu času spí a do cloudu hlási len zmeny. Preto integrácia:
- pred príkazom termostat „zobudí“ (`F = 60`, refresh mode; odporúčané, dá sa vypnúť v nastaveniach). Atribúty teplej vody (`C4x`) sú v prijímači na sieti, ale atribúty kúrenia (`A8x`) musia doletieť do batériového termostatu, ktorý ich bez zobudenia často nezachytí,
- zapisuje len atribúty, ktorých hodnota sa naozaj mení,
- ak termostat aj tak neodpovie (chyba 202), zobudí ho znova a po 5 s to raz zopakuje; ak to zlyhá aj vtedy, HA ukáže chybu namiesto predstierania, že sa zmena urobila,
- novú hodnotu ukáže v HA hneď (optimisticky) a drží ju, kým ju termostat nepotvrdí, najviac 3 minúty,
- po 8 s a po 30 s si stav z cloudu načíta znova.

## Inštalácia

### Cez HACS (odporúčané)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=branohric&repository=salus-it500-heating-and-hot-water&category=integration)

1. Klikni na tlačidlo vyššie, alebo v HACS otvor **⋮ → Custom repositories** a pridaj `https://github.com/branohric/salus-it500-heating-and-hot-water` s typom **Integration**.
2. V HACS nájdi **Salus iT500 (heating + hot water)**, klikni **Download** a reštartuj Home Assistant.

### Ručne

Skopíruj priečinok `custom_components/salus_it500_custom` do `config/custom_components/` v Home Assistante (napr. cez doplnok Samba share alebo Studio Code Server) a reštartuj Home Assistant.

### Nastavenie

1. *Nastavenia → Zariadenia a služby → Pridať integráciu* → **Salus iT500 (heating + hot water)**.
2. Zadaj e-mail a heslo z mobilnej aplikácie Salus iT500. Ak máš na účte viac zariadení, vyberieš si jedno.
3. Voliteľne: *Konfigurovať* → predvolená teplota, čo znamená „vypnuté“, dĺžka boostu, interval čítania.

Vyžaduje Home Assistant **2024.12** alebo novší. Testované proti HA 2026.2.

> Heslo so špeciálnymi znakmi: Salus ich pri vytváraní hesla ticho zahodí. Ak login zlyhá, skús heslo bez nich (podľa ZaBug).

## Najprv over API z počítača

```bash
cd tools
SALUS_EMAIL='tvoj@email.sk' python3 salus_cli.py status     # login, devId, stav
python3 salus_cli.py raw                                     # všetky atribúty (skontroluj C43, C45, S06)
python3 salus_cli.py boost 1                                 # boost vody 1 h
python3 salus_cli.py boost 0                                 # zruší boost teplej vody
python3 salus_cli.py heat 23                                 # kúrenie na 23 °C v manuálnom režime termostatu (aj z OFF)
python3 salus_cli.py off                                     # kúrenie „vypnuté“ = 14 °C, manuálny režim
```

Skript nepotrebuje žiadne knižnice. Heslo si vypýta interaktívne, ak ho nedáš do `SALUS_PASSWORD`.

## Príklady

Karta na dashboard:

```yaml
type: vertical-stack
cards:
  - type: thermostat
    entity: climate.doma_kurenie
  - type: entities
    entities:
      - entity: switch.doma_ohrev_vody_boost
      - entity: sensor.doma_zostavajuci_boost_vody
      - entity: binary_sensor.doma_kotol_kurenie
```

Automatizácia (boost vody keď prídeš domov):

```yaml
alias: Teplá voda po príchode
triggers:
  - trigger: state
    entity_id: person.you
    to: home
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.doma_ohrev_vody_boost
```

## Ladenie

```yaml
logger:
  logs:
    custom_components.salus_it500_custom: debug
```

Tokeny a heslá sa do logu nedostanú.

## Testy

```bash
pip install pytest-homeassistant-custom-component
pytest
```

Testy spúšťajú falošný Arrayent server cez skutočné HTTP. Pokrývajú login, expiráciu tokenu (3 varianty odpovede servera), proaktívnu obnovu, „32/32“, jeden atribút na volanie, spiaci alebo nedostupný termostat (chyba 202 + opakovanie), zapnutie z režimu OFF, toleranciu holej HTTP 500, config flow, re-auth, options a všetky príkazy kúrenia a boostu.

## Zdroje

- [ladamczyk-it/salus-it500](https://github.com/ladamczyk-it/salus-it500): rovnaké zapojenie (termostat + voda), mapovanie portál → Arrayent
- [ZaBug/salus-it500](https://github.com/ZaBug/salus-it500): refresh mode `F`, kód chyby 106, problém 32/32
- [gvasilakiss/salus-it500](https://github.com/gvasilakiss/salus-it500): kompletná mapa atribútov (`C42`/`C43`/`C45`)

API je neoficiálne a Salus ho môže kedykoľvek zmeniť.
