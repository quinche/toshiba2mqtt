# toshiba2mqtt

A standalone **MQTT bridge for Toshiba air conditioners**. It logs into the
Toshiba cloud, publishes each unit's live state to MQTT, and listens for command
topics — so **any MQTT-capable home automation system** (openHAB, Home Assistant,
Node-RED, ioBroker, …) can monitor and control your Toshiba AC units.

It is built on top of the excellent
[`toshiba-ac-community`](https://github.com/vmvelev/Toshiba-AC-control) protocol
library (originally by [KaSroka](https://github.com/KaSroka/Toshiba-AC-control)),
which handles the Toshiba cloud login and the Azure IoT Hub push channel. This
project adds a thin, robust MQTT layer on top.

> ⚠️ Works only with Toshiba AC units that use the official Toshiba app
> (`toshibahomeaccontrols.com` cloud). **North American** units use a different
> system and are **not** supported — see the note at the bottom.

---

## Features

- 🔌 Live state via Toshiba's push channel (no aggressive polling)
- 📤 Publishes full JSON state **and** individual attribute topics (retained)
- 📥 Command topics to control power, mode, temperature, fan, swing, and more
- ♻️ Auto-reconnect with exponential backoff
- 🪧 MQTT Last-Will so consumers know when the bridge goes offline
- 🐳 Runs as a systemd service or a Docker container

---

## Topic layout

Default prefix is `toshiba2mqtt` (configurable). `<device>` is a slug of the AC's
name from the Toshiba app (e.g. *"Living Room"* → `living_room`).

| Topic | Direction | Payload |
|---|---|---|
| `toshiba2mqtt/bridge/state` | out (retained, LWT) | `online` / `offline` |
| `toshiba2mqtt/<device>/available` | out (retained) | `online` / `offline` |
| `toshiba2mqtt/<device>/supported` | out (retained) | JSON: which modes/features this unit supports |
| `toshiba2mqtt/<device>/state` | out (retained) | full JSON state |
| `toshiba2mqtt/<device>/energy_wh` | out (retained) | year-to-date energy in Wh (units that report it) |
| `toshiba2mqtt/<device>/energy` | out (retained) | JSON: `energy_wh` + `since` timestamp |
| `toshiba2mqtt/<device>/<attr>` | out (retained) | single attribute value |
| `toshiba2mqtt/<device>/set/<attr>` | **in** | new value |

### State attributes (published)

`power`, `mode`, `temperature`, `fan`, `swing`, `air_pure_ion`, `merit_a`,
`merit_b`, `power_select`, `indoor_temperature`, `outdoor_temperature`,
`self_cleaning`, `wireless_led`.

### Capability discovery (`supported`)

At startup each unit publishes a retained `toshiba2mqtt/<device>/supported`
topic describing exactly what that unit can do, e.g.:

```json
{
  "name": "Wohnzimmer",
  "modes": ["AUTO", "COOL", "DRY", "FAN", "HEAT"],
  "fan": ["AUTO", "QUIET", "LOW", "MEDIUM", "HIGH"],
  "swing": ["OFF", "SWING_VERTICAL"],
  "power_select": ["POWER_50", "POWER_75", "POWER_100"],
  "merit_a": ["OFF", "HIGH_POWER", "ECO", "COMFORT"],
  "merit_b": ["OFF"],
  "air_pure_ion": ["OFF"],
  "energy_report": false
}
```

Subscribe to it (or read the bridge's startup log) to see the valid command
values for your specific hardware before wiring up openHAB.

### Settable attributes (`set/<attr>`)

| Attribute | Accepted values |
|---|---|
| `power` | `ON`, `OFF` |
| `mode` | `AUTO`, `COOL`, `HEAT`, `DRY`, `FAN` |
| `temperature` | integer °C, e.g. `21` |
| `fan` | `AUTO`, `QUIET`, `LOW`, `MEDIUM_LOW`, `MEDIUM`, `MEDIUM_HIGH`, `HIGH` |
| `swing` | `OFF`, `SWING_VERTICAL`, `SWING_HORIZONTAL`, `SWING_VERTICAL_AND_HORIZONTAL`, `FIXED_1`…`FIXED_5`, `HADA` |
| `air_pure_ion` | `ON`, `OFF` |
| `merit_a` | `OFF`, `HIGH_POWER`, `ECO`, `COMFORT`, `CDU_SILENT_1`, `CDU_SILENT_2`, `HEATING_8C`, `FLOOR`, `SLEEP_CARE` (device dependent) |
| `merit_b` | `OFF`, `FIREPLACE_1`, `FIREPLACE_2` (device dependent) |
| `power_select` | `50`, `75`, `100` |

> **Tip:** Check each unit's `supported` topic (see above) for the exact
> `merit_a` / `merit_b` / `swing` values your hardware accepts. Values the unit
> doesn't support are silently ignored.

> Unsupported values for your specific unit/mode are silently ignored by the
> Toshiba library (it only sends what the device advertises as supported).

### Energy consumption

Units that advertise `energy_report` publish their year-to-date energy use to
`toshiba2mqtt/<device>/energy_wh` (a number in Wh) and a richer JSON version to
`toshiba2mqtt/<device>/energy`. The Toshiba cloud refreshes this roughly every
10 minutes, so treat it as a slow-moving cumulative counter, not a live power
reading.

---

## Installation

### Requirements

- Python 3.10+
- An MQTT broker (e.g. Mosquitto)
- Your Toshiba app account credentials

### Manual / systemd (recommended for a Debian server)

```bash
sudo mkdir -p /opt/toshiba2mqtt
sudo chown "$USER" /opt/toshiba2mqtt
git clone https://github.com/quinche/toshiba2mqtt.git /opt/toshiba2mqtt
cd /opt/toshiba2mqtt

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.yaml.example config.yaml
# edit config.yaml with your Toshiba + MQTT details
$EDITOR config.yaml

# test run
.venv/bin/python toshiba2mqtt.py --config config.yaml
```

Once it works, install the service:

```bash
# create a dedicated user (optional but recommended)
sudo useradd --system --home /opt/toshiba2mqtt --shell /usr/sbin/nologin toshiba2mqtt
sudo chown -R toshiba2mqtt /opt/toshiba2mqtt

sudo cp systemd/toshiba2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now toshiba2mqtt
sudo systemctl status toshiba2mqtt
journalctl -u toshiba2mqtt -f
```

### Docker

```bash
docker build -t toshiba2mqtt .
docker run -d --name toshiba2mqtt \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  --restart unless-stopped \
  toshiba2mqtt
```

Or provide secrets via environment variables instead of a config file:
`TOSHIBA_USERNAME`, `TOSHIBA_PASSWORD`, `MQTT_HOST`, `MQTT_PORT`,
`MQTT_USERNAME`, `MQTT_PASSWORD`.

---

## openHAB integration

Assuming you already have the **MQTT binding** and a broker Thing configured.

### 1. Thing (`.things` file)

```java
Thing mqtt:topic:toshiba_living "Toshiba Living Room" (mqtt:broker:mosquitto) {
    Channels:
        Type switch : power [
            stateTopic="toshiba2mqtt/living_room/power",
            commandTopic="toshiba2mqtt/living_room/set/power",
            on="ON", off="OFF"
        ]
        Type string : mode [
            stateTopic="toshiba2mqtt/living_room/mode",
            commandTopic="toshiba2mqtt/living_room/set/mode"
        ]
        Type number : setpoint [
            stateTopic="toshiba2mqtt/living_room/temperature",
            commandTopic="toshiba2mqtt/living_room/set/temperature"
        ]
        Type string : fan [
            stateTopic="toshiba2mqtt/living_room/fan",
            commandTopic="toshiba2mqtt/living_room/set/fan"
        ]
        Type string : swing [
            stateTopic="toshiba2mqtt/living_room/swing",
            commandTopic="toshiba2mqtt/living_room/set/swing"
        ]
        Type number : indoor_temp [
            stateTopic="toshiba2mqtt/living_room/indoor_temperature"
        ]
        Type number : outdoor_temp [
            stateTopic="toshiba2mqtt/living_room/outdoor_temperature"
        ]
        Type switch : available [
            stateTopic="toshiba2mqtt/living_room/available",
            on="online", off="offline"
        ]
}
```

### 2. Items (`.items` file)

```java
Switch  Toshiba_Living_Power    "Power [%s]"          { channel="mqtt:topic:toshiba_living:power" }
String  Toshiba_Living_Mode     "Mode [%s]"           { channel="mqtt:topic:toshiba_living:mode" }
Number  Toshiba_Living_Setpoint "Setpoint [%.0f °C]"  { channel="mqtt:topic:toshiba_living:setpoint" }
String  Toshiba_Living_Fan      "Fan [%s]"            { channel="mqtt:topic:toshiba_living:fan" }
String  Toshiba_Living_Swing    "Swing [%s]"          { channel="mqtt:topic:toshiba_living:swing" }
Number  Toshiba_Living_Indoor   "Indoor [%.1f °C]"    { channel="mqtt:topic:toshiba_living:indoor_temp" }
Number  Toshiba_Living_Outdoor  "Outdoor [%.1f °C]"   { channel="mqtt:topic:toshiba_living:outdoor_temp" }
Switch  Toshiba_Living_Online   "Bridge online [%s]"  { channel="mqtt:topic:toshiba_living:available" }
```

### 3. Sitemap snippet

```perl
Frame label="Living Room AC" {
    Switch    item=Toshiba_Living_Power
    Selection item=Toshiba_Living_Mode  mappings=[AUTO="Auto", COOL="Cool", HEAT="Heat", DRY="Dry", FAN="Fan"]
    Setpoint  item=Toshiba_Living_Setpoint minValue=17 maxValue=30 step=1
    Selection item=Toshiba_Living_Fan   mappings=[AUTO="Auto", QUIET="Quiet", LOW="Low", MEDIUM="Medium", HIGH="High"]
    Text      item=Toshiba_Living_Indoor
    Text      item=Toshiba_Living_Outdoor
}
```

Duplicate the block for your second unit, swapping `living_room` for that unit's
slug. Not sure of the slug? Watch the bridge log at startup — it prints
`'<Name>' -> topic base toshiba2mqtt/<slug>` for every unit, or just subscribe to
`toshiba2mqtt/#` with `mosquitto_sub` and read the topics.

---

## Debugging

```bash
# watch everything the bridge publishes
mosquitto_sub -h 127.0.0.1 -t 'toshiba2mqtt/#' -v

# send a manual command
mosquitto_pub -h 127.0.0.1 -t 'toshiba2mqtt/living_room/set/power' -m 'ON'
```

Set `log_level: DEBUG` in `config.yaml` for verbose logs.

---

## Credits

- Protocol library: [`vmvelev/Toshiba-AC-control`](https://github.com/vmvelev/Toshiba-AC-control)
  (fork of [`KaSroka/Toshiba-AC-control`](https://github.com/KaSroka/Toshiba-AC-control))
- Home Assistant integration that inspired this bridge:
  [`vmvelev/home-assistant-toshiba_ac`](https://github.com/vmvelev/home-assistant-toshiba_ac)

## License

[Apache License 2.0](LICENSE)

---

### ⚠️ North America

Toshiba sells AC units in the US/Canada under a completely different app and
cloud system. This bridge will **not** work with those. For NA units, look at
[`midea-ac-py`](https://github.com/mill1000/midea-ac-py) instead.
