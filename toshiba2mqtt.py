#!/usr/bin/env python3
# Copyright 2026 quinche
# Licensed under the Apache License, Version 2.0 (the "License").
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
"""
toshiba2mqtt - A standalone MQTT bridge for Toshiba AC units.

Connects to the Toshiba cloud (via the toshiba-ac-community protocol library),
publishes each unit's live state to MQTT, and listens for command topics so any
MQTT-capable home automation system (openHAB, Home Assistant, Node-RED, ioBroker,
...) can control the units.

Topic layout (default prefix "toshiba2mqtt"):

  toshiba2mqtt/bridge/state                 -> "online" / "offline" (LWT retained)
  toshiba2mqtt/<device>/available          -> "online" / "offline" (retained)
  toshiba2mqtt/<device>/state              -> full JSON state (retained)
  toshiba2mqtt/<device>/<attr>             -> individual attribute (retained)
  toshiba2mqtt/<device>/set/<attr>         -> command topic (subscribe)

<device> is a slugified version of the AC's name from the Toshiba app.

Supported settable attributes:
  power        -> ON | OFF
  mode         -> AUTO | COOL | HEAT | DRY | FAN
  temperature  -> integer degrees C (e.g. 21)
  fan          -> AUTO | QUIET | LOW | MEDIUM_LOW | MEDIUM | MEDIUM_HIGH | HIGH
  swing        -> OFF | SWING_VERTICAL | SWING_HORIZONTAL | SWING_VERTICAL_AND_HORIZONTAL | FIXED_1..5 | HADA
  air_pure_ion -> ON | OFF
  merit_a      -> OFF | HIGH_POWER | ECO | ... (device dependent)
  power_select -> 50 | 75 | 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import typing as t
import unicodedata

import yaml

try:
    import aiomqtt
except ImportError:  # pragma: no cover
    print("Missing dependency 'aiomqtt'. Run: pip install -r requirements.txt", file=sys.stderr)
    raise

from toshiba_ac.device_manager import ToshibaAcDeviceManager
from toshiba_ac.device import ToshibaAcDevice
from toshiba_ac.device.properties import (
    ToshibaAcStatus,
    ToshibaAcMode,
    ToshibaAcFanMode,
    ToshibaAcSwingMode,
    ToshibaAcAirPureIon,
    ToshibaAcMeritA,
    ToshibaAcPowerSelection,
)

logger = logging.getLogger("toshiba2mqtt")


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def slugify(name: str) -> str:
    """Turn an AC display name into a safe, ASCII-only MQTT/openHAB topic segment."""
    # Transliterate accented characters to ASCII (ü -> u, é -> e, ...).
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = ascii_name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "ac"


def pretty(enum_val: t.Any) -> str:
    """Human readable enum name (e.g. ToshibaAcMode.COOL -> 'COOL')."""
    return getattr(enum_val, "name", str(enum_val))


# --------------------------------------------------------------------------- #
# Command mapping: MQTT string payload -> library setter call                  #
# --------------------------------------------------------------------------- #

POWER_SELECT_MAP = {
    "50": ToshibaAcPowerSelection.POWER_50,
    "75": ToshibaAcPowerSelection.POWER_75,
    "100": ToshibaAcPowerSelection.POWER_100,
}


async def apply_command(device: ToshibaAcDevice, attr: str, payload: str) -> None:
    """Translate an incoming MQTT command into a library setter call."""
    value = payload.strip()
    upper = value.upper()

    try:
        if attr == "power":
            await device.set_ac_status(ToshibaAcStatus[upper])

        elif attr == "mode":
            await device.set_ac_mode(ToshibaAcMode[upper])

        elif attr == "temperature":
            await device.set_ac_temperature(int(round(float(value))))

        elif attr == "fan":
            await device.set_ac_fan_mode(ToshibaAcFanMode[upper])

        elif attr == "swing":
            await device.set_ac_swing_mode(ToshibaAcSwingMode[upper])

        elif attr == "air_pure_ion":
            await device.set_ac_air_pure_ion(ToshibaAcAirPureIon[upper])

        elif attr == "merit_a":
            await device.set_ac_merit_a(ToshibaAcMeritA[upper])

        elif attr == "power_select":
            if value not in POWER_SELECT_MAP:
                raise ValueError(f"power_select must be one of {list(POWER_SELECT_MAP)}")
            await device.set_ac_power_selection(POWER_SELECT_MAP[value])

        else:
            logger.warning("Unknown command attribute '%s' (payload=%r)", attr, payload)
            return

        logger.info("Applied command %s=%s to '%s'", attr, value, device.name)

    except KeyError:
        logger.error("Invalid value %r for command '%s' on '%s'", payload, attr, device.name)
    except ValueError as e:
        logger.error("Bad value for command '%s' on '%s': %s", attr, device.name, e)
    except Exception:
        logger.exception("Failed to apply command %s=%s on '%s'", attr, value, device.name)


# --------------------------------------------------------------------------- #
# State serialisation                                                          #
# --------------------------------------------------------------------------- #

def device_state_dict(device: ToshibaAcDevice) -> dict[str, t.Any]:
    """Build a JSON-serialisable snapshot of a device's current state."""
    return {
        "name": device.name,
        "power": pretty(device.ac_status),
        "mode": pretty(device.ac_mode),
        "temperature": device.ac_temperature,
        "fan": pretty(device.ac_fan_mode),
        "swing": pretty(device.ac_swing_mode),
        "air_pure_ion": pretty(device.ac_air_pure_ion),
        "merit_a": pretty(device.ac_merit_a),
        "power_select": pretty(device.ac_power_selection),
        "indoor_temperature": device.ac_indoor_temperature,
        "outdoor_temperature": device.ac_outdoor_temperature,
        "self_cleaning": pretty(device.ac_self_cleaning),
        "wireless_led": pretty(device.ac_wireless_led),
    }


# --------------------------------------------------------------------------- #
# Bridge                                                                       #
# --------------------------------------------------------------------------- #

class Toshiba2Mqtt:
    def __init__(self, config: dict[str, t.Any]) -> None:
        self.cfg = config
        self.prefix: str = config["mqtt"].get("prefix", "toshiba2mqtt").rstrip("/")
        self.device_manager: ToshibaAcDeviceManager | None = None
        self.mqtt: aiomqtt.Client | None = None
        self.devices_by_slug: dict[str, ToshibaAcDevice] = {}
        self._publish_queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._stop = asyncio.Event()

    # -- topic helpers ----------------------------------------------------- #
    def bridge_topic(self, leaf: str) -> str:
        return f"{self.prefix}/bridge/{leaf}"

    def dev_topic(self, slug: str, leaf: str) -> str:
        return f"{self.prefix}/{slug}/{leaf}"

    # -- publishing -------------------------------------------------------- #
    def enqueue(self, topic: str, payload: str, retain: bool = True) -> None:
        self._publish_queue.put_nowait((topic, payload, retain))

    def publish_device_state(self, device: ToshibaAcDevice) -> None:
        slug = slugify(device.name)
        state = device_state_dict(device)
        self.enqueue(self.dev_topic(slug, "state"), json.dumps(state), retain=True)
        for key, val in state.items():
            if val is None:
                continue
            self.enqueue(self.dev_topic(slug, key), str(val), retain=True)

    # -- library callbacks ------------------------------------------------- #
    async def _on_state_changed(self, device: ToshibaAcDevice) -> None:
        logger.debug("State changed for '%s'", device.name)
        self.publish_device_state(device)

    # -- MQTT command loop ------------------------------------------------- #
    async def _handle_incoming(self, message: "aiomqtt.Message") -> None:
        topic = str(message.topic)
        try:
            payload = message.payload.decode() if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
        except Exception:
            logger.error("Could not decode payload on %s", topic)
            return

        # Expect: <prefix>/<slug>/set/<attr>
        parts = topic.split("/")
        if len(parts) < 4 or parts[-2] != "set":
            return
        slug = parts[-3]
        attr = parts[-1]

        device = self.devices_by_slug.get(slug)
        if not device:
            logger.warning("Command for unknown device slug '%s' (topic=%s)", slug, topic)
            return

        await apply_command(device, attr, payload)

    # -- main lifecycle ---------------------------------------------------- #
    async def run(self) -> None:
        tcfg = self.cfg["toshiba"]
        mcfg = self.cfg["mqtt"]

        will = aiomqtt.Will(
            topic=self.bridge_topic("state"), payload="offline", qos=1, retain=True
        )

        mqtt_kwargs: dict[str, t.Any] = {
            "hostname": mcfg["host"],
            "port": int(mcfg.get("port", 1883)),
            "will": will,
        }
        if mcfg.get("username"):
            mqtt_kwargs["username"] = mcfg["username"]
            mqtt_kwargs["password"] = mcfg.get("password", "")
        if mcfg.get("client_id"):
            mqtt_kwargs["identifier"] = mcfg["client_id"]

        logger.info("Connecting to MQTT broker %s:%s ...", mqtt_kwargs["hostname"], mqtt_kwargs["port"])

        async with aiomqtt.Client(**mqtt_kwargs) as mqtt:
            self.mqtt = mqtt
            await mqtt.publish(self.bridge_topic("state"), "online", qos=1, retain=True)

            # Connect to Toshiba cloud
            logger.info("Connecting to Toshiba cloud ...")
            self.device_manager = ToshibaAcDeviceManager(
                username=tcfg["username"],
                password=tcfg["password"],
                device_id=tcfg.get("device_id"),
                sas_token=tcfg.get("sas_token"),
            )
            await self.device_manager.connect()
            devices = await self.device_manager.get_devices()
            logger.info("Found %d Toshiba AC device(s)", len(devices))

            for device in devices:
                slug = slugify(device.name)
                self.devices_by_slug[slug] = device
                device.on_state_changed_callback.add(self._on_state_changed)
                logger.info("  - '%s' -> topic base %s/%s", device.name, self.prefix, slug)
                self.enqueue(self.dev_topic(slug, "available"), "online", retain=True)
                self.publish_device_state(device)

            # Subscribe to all command topics
            await mqtt.subscribe(f"{self.prefix}/+/set/+", qos=1)

            # Run publisher + consumer concurrently
            await asyncio.gather(
                self._publisher_loop(),
                self._consumer_loop(),
                self._wait_stop(),
            )

    async def _publisher_loop(self) -> None:
        assert self.mqtt is not None
        while not self._stop.is_set():
            try:
                topic, payload, retain = await asyncio.wait_for(self._publish_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self.mqtt.publish(topic, payload, qos=1, retain=retain)
            except Exception:
                logger.exception("Failed to publish to %s", topic)

    async def _consumer_loop(self) -> None:
        assert self.mqtt is not None
        async for message in self.mqtt.messages:
            if self._stop.is_set():
                break
            await self._handle_incoming(message)

    async def _wait_stop(self) -> None:
        await self._stop.wait()

    async def shutdown(self) -> None:
        logger.info("Shutting down ...")
        self._stop.set()
        if self.mqtt is not None:
            try:
                await self.mqtt.publish(self.bridge_topic("state"), "offline", qos=1, retain=True)
            except Exception:
                pass
        if self.device_manager is not None:
            try:
                await self.device_manager.shutdown()
            except Exception:
                logger.exception("Error during device manager shutdown")


# --------------------------------------------------------------------------- #
# Config + entrypoint                                                          #
# --------------------------------------------------------------------------- #

def load_config(path: str) -> dict[str, t.Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Allow environment variable overrides for secrets (useful for Docker).
    cfg.setdefault("toshiba", {})
    cfg.setdefault("mqtt", {})
    cfg["toshiba"]["username"] = os.environ.get("TOSHIBA_USERNAME", cfg["toshiba"].get("username"))
    cfg["toshiba"]["password"] = os.environ.get("TOSHIBA_PASSWORD", cfg["toshiba"].get("password"))
    cfg["mqtt"]["host"] = os.environ.get("MQTT_HOST", cfg["mqtt"].get("host"))
    if os.environ.get("MQTT_PORT"):
        cfg["mqtt"]["port"] = int(os.environ["MQTT_PORT"])
    cfg["mqtt"]["username"] = os.environ.get("MQTT_USERNAME", cfg["mqtt"].get("username"))
    cfg["mqtt"]["password"] = os.environ.get("MQTT_PASSWORD", cfg["mqtt"].get("password"))

    if not cfg["toshiba"].get("username") or not cfg["toshiba"].get("password"):
        raise SystemExit("Config error: toshiba.username and toshiba.password are required.")
    if not cfg["mqtt"].get("host"):
        raise SystemExit("Config error: mqtt.host is required.")

    return cfg


async def main_async(config_path: str) -> None:
    cfg = load_config(config_path)

    level = getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    bridge = Toshiba2Mqtt(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bridge.shutdown()))
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    backoff = 5
    while not bridge._stop.is_set():
        try:
            await bridge.run()
        except Exception:
            logger.exception("Bridge crashed; reconnecting in %ss", backoff)
            try:
                await bridge.device_manager.shutdown()  # type: ignore[union-attr]
            except Exception:
                pass
            if bridge._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Toshiba AC -> MQTT bridge")
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("TOSHIBA2MQTT_CONFIG", "config.yaml"),
        help="Path to config.yaml (default: config.yaml or $TOSHIBA2MQTT_CONFIG)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
