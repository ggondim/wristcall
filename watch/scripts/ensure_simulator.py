#!/usr/bin/env python3
"""Prints the UDID of a watchOS simulator with the given name and OS version, creating it if missing.

Usage: python3 scripts/ensure_simulator.py "Apple Watch Series 7 (45mm)" 26.5

To create a device, the name must be a device type name (see `xcrun simctl list devicetypes`).
"""

import json
import subprocess
import sys


def main(name: str, os_version: str) -> str:
    data = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "-j"]))
    runtime = next(
        (
            r
            for r in data["runtimes"]
            if r.get("platform") == "watchOS" and r.get("version") == os_version and r.get("isAvailable")
        ),
        None,
    )
    if runtime is None:
        sys.exit(f"error: the watchOS {os_version} simulator runtime is not installed")
    for device in data["devices"].get(runtime["identifier"], []):
        if device["name"] == name and device.get("isAvailable"):
            return device["udid"]
    device_type = next((t for t in data["devicetypes"] if t["name"] == name), None)
    if device_type is None:
        sys.exit(f"error: no simulator named {name!r} and no device type with that name")
    created = subprocess.check_output(
        ["xcrun", "simctl", "create", name, device_type["identifier"], runtime["identifier"]], text=True
    )
    return created.strip()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(main(sys.argv[1], sys.argv[2]))
