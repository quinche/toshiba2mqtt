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


async def probe(api: ToshibaAcHttpApi, label: str, post: dict) -> None:
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

    def pick(obj, *names):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v:
                    return v
        return None

    unique_ids = []
    for d in devices:
        uid = pick(d, "ac_unique_id", "unique_id", "device_unique_id", "ac_id", "id")
        name = pick(d, "name", "ac_name", "label") or "?"
        if uid:
            unique_ids.append(uid)
        # Show every public field so we can adapt if the shape differs again.
        fields = {k: v for k, v in vars(d).items()} if hasattr(d, "__dict__") else {}
        if not fields:
            fields = {a: getattr(d, a) for a in dir(d) if not a.startswith("_") and not callable(getattr(d, a))}
        print(f"  - name={name!r} unique_id={uid}")
        print(f"      all fields: {fields}")
    if not unique_ids:
        sys.exit("No device unique ids found on this account.")

    now = dt.datetime.now(dt.timezone.utc)
    year = now.year
    month = now.month
    day = now.day
    hour = now.hour

    # --- Candidate probes -------------------------------------------------
    # KEY INSIGHT from the first run: `Type=EnergyYear` with window
    # From="2026" / To="2027" returns TWELVE monthly buckets (Time "01".. "12").
    # So the pattern seems to be: the window is expressed at ONE granularity
    # coarser than the buckets you get back, and the string format matches that
    # granularity. Following that logic:
    #   - monthly buckets  <- window in YEARS   ("2026"/"2027")   [CONFIRMED]
    #   - daily buckets     <- window in MONTHS  ("202607"/"202608")
    #   - hourly buckets    <- window in DAYS    ("20260729"/"20260730")
    # We probe that hypothesis with several Type spellings + window formats,
    # plus a few timezone / param-name variants.
    base = {"ACDeviceUniqueIdList": unique_ids, "Timezone": "UTC"}
    base_local = {"ACDeviceUniqueIdList": unique_ids, "Timezone": "Europe/Zurich"}

    _tomorrow = now + dt.timedelta(days=1)
    ymd = f"{year:04d}{month:02d}{day:02d}"
    ymd_next = f"{_tomorrow.year:04d}{_tomorrow.month:02d}{_tomorrow.day:02d}"
    ym = f"{year:04d}{month:02d}"
    ym_next = f"{year:04d}{month + 1:02d}" if month < 12 else f"{year + 1:04d}01"
    ymd_dash = f"{year:04d}-{month:02d}-{day:02d}"

    probes: list[tuple[str, dict]] = []

    # Baseline (known-good) — proves auth + shows the monthly shape again.
    probes.append(("EnergyYear (baseline, known-good -> monthly buckets)", {
        **base, "FromUtcTime": str(year), "ToUtcTime": str(year + 1), "Type": "EnergyYear",
    }))

    # --- DAILY buckets: window in MONTHS -------------------------------
    for typ in ("EnergyMonth", "EnergyDay", "Month", "Day"):
        probes.append((f"{typ} (month window {ym}->{ym_next}) [expect daily buckets]", {
            **base, "FromUtcTime": ym, "ToUtcTime": ym_next, "Type": typ,
        }))

    # --- HOURLY buckets: window in DAYS --------------------------------
    for typ in ("EnergyDay", "EnergyHour", "Day", "Hour"):
        probes.append((f"{typ} (day window {ymd}->{ymd_next}) [expect hourly buckets]", {
            **base, "FromUtcTime": ymd, "ToUtcTime": ymd_next, "Type": typ,
        }))
    # Same-day window (From==To), which is what the monthly call effectively does
    for typ in ("EnergyDay", "EnergyHour"):
        probes.append((f"{typ} (same-day window {ymd}) [expect hourly buckets]", {
            **base, "FromUtcTime": ymd, "ToUtcTime": ymd, "Type": typ,
        }))

    # --- Format / param variants for the day window --------------------
    probes.append(("EnergyHour (dashed date window)", {
        **base, "FromUtcTime": ymd_dash, "ToUtcTime": ymd_dash, "Type": "EnergyHour",
    }))
    probes.append(("EnergyHour (full ISO datetime window, Z)", {
        **base,
        "FromUtcTime": f"{ymd_dash}T00:00:00Z",
        "ToUtcTime": f"{ymd_dash}T23:59:59Z",
        "Type": "EnergyHour",
    }))
    probes.append(("EnergyDay day-window in LOCAL tz (Europe/Zurich)", {
        **base_local, "FromUtcTime": ymd, "ToUtcTime": ymd_next, "Type": "EnergyDay",
    }))

    # --- Wildcard alternative Type spellings (same-day window) ----------
    for typ in ("EnergyHourly", "Hourly", "EnergyDaily", "Daily", "EnergyWeek"):
        probes.append((f"{typ} (day window {ymd}->{ymd_next})", {
            **base, "FromUtcTime": ymd, "ToUtcTime": ymd_next, "Type": typ,
        }))

    for label, post in probes:
        await probe(api, label, post)
        await asyncio.sleep(1.0)  # be polite to the WAF/rate-limiter

    print("\n" + "=" * 70)
    print("DONE. Copy everything above back to Clawee.")
    print("WHAT TO LOOK FOR:")
    print("  * hourly win: EnergyConsumption with ~24 entries, Time '00'..'23'")
    print("  * daily win:  EnergyConsumption with ~28-31 entries, Time '01'..'31'")
    print("  * null EnergyConsumption = that Type/window combo is NOT supported.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
