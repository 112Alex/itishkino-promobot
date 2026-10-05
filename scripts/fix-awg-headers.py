#!/usr/bin/env python3
"""Verify/repair AWG H1/H2 through local UAPI; never print configuration values."""
import configparser
import json
from pathlib import Path
import re
import socket


def request(command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect("/run/amneziawg/awg0.sock")
        sock.sendall(command.encode())
        data = b""
        while b"\n\n" not in data:
            part = sock.recv(65536)
            if not part or len(data) > 262144:
                raise ValueError("Invalid UAPI response")
            data += part
    return dict(line.split("=", 1) for line in data.decode().strip().splitlines() if "=" in line)


def fix():
    path = Path("/run/secrets/awg0.conf")
    if path.stat().st_mode & 0o077:
        raise ValueError("Protected file required")
    c = configparser.ConfigParser(interpolation=None, strict=True)
    c.read(path)
    expected = {key: c["Interface"][key].strip() for key in ("h1", "h2")}
    for value in expected.values():
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", value):
            raise ValueError("Invalid header format")
        ends = [int(x) for x in value.split("-")]
        if any(x > 4294967295 for x in ends) or ends[0] > ends[-1]:
            raise ValueError("Invalid header range")
    before = request("get=1\n\n")
    updated = any(before.get(key) != value for key, value in expected.items())
    if updated:
        result = request("set=1\n" + "".join(key + "=" + value + "\n" for key, value in expected.items()) + "\n")
        if result.get("errno") != "0":
            raise ValueError("UAPI rejected headers")
    after = request("get=1\n\n")
    matches = {key + "_matches": after.get(key) == value for key, value in expected.items()}
    if not all(matches.values()):
        raise ValueError("Header verification failed")
    return {**matches, "updated": updated}


if __name__ == "__main__":
    try:
        print(json.dumps(fix()))
    except Exception:
        print('{"header_fix": "failed", "secrets": "not displayed"}')
        raise SystemExit(2)
