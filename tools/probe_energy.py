#!/usr/bin/env python3
# Copyright 2026 quinche
# Licensed under the Apache License, Version 2.0 (the "License").
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
"""
probe_energy.py - Discover which energy-consumption granularities the Toshiba
cloud exposes for your account (hourly / daily / monthly / yearly).

The community protocol library only ever asks for `Type=EnergyYear` and sums the
result into a single running counter. Toshiba's `GetGroupACEnergyConsumption`
endpoint almost certainly supports finer granularities, but the exact `Type`
strings and time-window format are not publicly documented. This script probes
several candidates against YOUR account and prints the raw responses so we know
exactly what to build into toshiba2mqtt.

It reuses the library's authenticated HTTP client (same login/token flow as the
bridge), so it needs the SAME credentials the bridge uses.

USAGE (on the machine that has the credentials, e.g. smartix):

  # Option 1: read the bridge's own config.yaml (recommended)
  python3 probe_energy.py --config /home/chef/toshiba2mqtt/config.yaml

  # Option 2: pass creds directly
  python3 probe_energy.py --username you@example.com --password 'secret'

Nothing is written anywhere and no device state is changed — this only READS
energy data. Credentials are never printed. Copy the full output back to Clawee.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import uuid

try:
    import yaml
except ImportError:
    yaml = None

# Import the package first to avoid a circular-import error that triggers when
# toshiba_ac.utils.http_api is imported as the very first submodule.
import toshiba_ac.device_manager  # noqa: F401
from toshiba_ac.utils.http_api import ToshibaAcHttpApi


def load_creds(args) -> tuple[str, str]:
    if args.username and args.password:
        return args.username, args.password
    if args.config:
        if yaml is None:
            sys.exit("PyYAML not available; pass --username/--password instead.")
        with open(args.config, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        tc = cfg.get("toshiba", cfg)  # tolerate flat or nested layout
        user = tc.get("username") or cfg.get("username")
        pw = tc.get("password") or cfg.get("password")
        if user and pw:
            return user, pw
        sys.exit(f"Could not find username/password in {args.config}")
    sys.exit("Provide --config or --username/--password.")


def fmt(obj) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return repr(obj)


async def probe(api: ToshibaAcHttpApi, unique_ids: list[str], label: str, post: dict) -> None:
    print("\n" + "=" * 70)
    print(f"PROBE: {label}")
    print("  request:", json.dumps(post, default=str))
    print("-" * 70)
    try:
        res = await api.request_api(api.AC_ENERGY_CONSUMPTION_PATH, post=post)
    except Exception as e:  # noqa: BLE001 - we want to see every failure mode
        print(f"  -> EXCEPTION: {type(e).__name__}: {e}")
        return
    # Trim huge payloads but keep enough to see the shape.
    text = fmt(res)
    if len(text) > 6000:
        text = text[:6000] + f"\n  ... [truncated, total {len(text)} chars]"
    print("  -> RESPONSE:")
    print(text)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="Path to bridge config.yaml")
    ap.add_argument("--username")
    ap.add_argument("--password")
    args = ap.parse_args()

    username, password = load_creds(args)

    device_id = str(uuid.uuid4())
    api = ToshibaAcHttpApi(username=username, password=password, device_id=device_id)

    print("Logging in to Toshiba cloud ...")
    await api.connect()
    print("  login OK")

    print("Fetching device list ...")
    devices = await api.get_devices()
    unique_ids = [d.ac_unique_id for d in devices]
    for d in devices:
        print(f"  - {d.name!r}: unique_id={d.ac_unique_id} id={d.ac_id}")
    if not unique_ids:
        sys.exit("No devices found on this account.")

    now = dt.datetime.now(dt.timezone.utc)
    year = now.year
    month = now.month
    day = now.day
    hour = now.hour

    # --- Candidate probes -------------------------------------------------
    # We vary the `Type` string and the From/To window format. The baseline
    # (EnergyYear) is what the library uses and is known to work; the rest are
    # educated guesses based on typical Toshiba/Azure energy APIs.
    base = {"ACDeviceUniqueIdList": unique_ids, "Timezone": "UTC"}

    probes: list[tuple[str, dict]] = []

    # Baseline (known-good) — proves auth + shows yearly shape.
    probes.append(("EnergyYear (baseline, known-good)", {
        **base, "FromUtcTime": str(year), "ToUtcTime": str(year + 1), "Type": "EnergyYear",
    }))

    # Hourly candidates — several plausible Type strings + window formats.
    ymd = f"{year:04d}{month:02d}{day:02d}"
    ymd_dash = f"{year:04d}-{month:02d}-{day:02d}"
    probes.append(("EnergyHour (YYYYMMDD window)", {
        **base, "FromUtcTime": ymd, "ToUtcTime": ymd, "Type": "EnergyHour",
    }))
    probes.append(("EnergyHour (dashed date window)", {
        **base, "FromUtcTime": ymd_dash, "ToUtcTime": ymd_dash, "Type": "EnergyHour",
    }))
    probes.append(("EnergyHour (full ISO datetime window)", {
        **base,
        "FromUtcTime": f"{ymd_dash}T00:00:00",
        "ToUtcTime": f"{ymd_dash}T23:59:59",
        "Type": "EnergyHour",
    }))
    probes.append(("Hour (short Type)", {
        **base, "FromUtcTime": ymd, "ToUtcTime": ymd, "Type": "Hour",
    }))
    probes.append(("EnergyHourly (alt spelling)", {
        **base, "FromUtcTime": ymd, "ToUtcTime": ymd, "Type": "EnergyHourly",
    }))

    # Daily candidates — for a monthly window (useful as fallback granularity).
    ym = f"{year:04d}{month:02d}"
    probes.append(("EnergyDay (YYYYMM window)", {
        **base, "FromUtcTime": ym, "ToUtcTime": ym, "Type": "EnergyDay",
    }))
    probes.append(("EnergyMonth (year window)", {
        **base, "FromUtcTime": str(year), "ToUtcTime": str(year + 1), "Type": "EnergyMonth",
    }))

    for label, post in probes:
        await probe(api, unique_ids, label, post)
        await asyncio.sleep(1.0)  # be polite to the WAF/rate-limiter

    print("\n" + "=" * 70)
    print("DONE. Copy everything above back to Clawee.")
    print("Look for the probe whose RESPONSE contains a per-hour breakdown")
    print("(a list of ~24 Energy values), that's the one we build on.")
    await api.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
