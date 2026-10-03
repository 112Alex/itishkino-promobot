#!/usr/bin/env python3
"""Read a protected native AWG2 file; never print values, keys, links or parser errors."""
import argparse
import base64
import configparser
import ipaddress
import json
import os
import stat
from pathlib import Path


def check(path):
    path = Path(path)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError
    c = configparser.ConfigParser(interpolation=None, strict=True, inline_comment_prefixes=("#",))
    with path.open() as stream:
        c.read_file(stream)
    if set(c.sections()) != {"Interface", "Peer"}:
        raise ValueError
    i, p = c["Interface"], c["Peer"]
    allowed_i = {"privatekey", "address", "dns", "mtu", "table", "listenport", "jc", "jmin", "jmax",
                 "s1", "s2", "s3", "s4", "h1", "h2", "h3", "h4", "i1", "i2", "i3", "i4", "i5"}
    allowed_p = {"publickey", "presharedkey", "allowedips", "endpoint", "persistentkeepalive"}
    if set(i) - allowed_i or set(p) - allowed_p or i.get("table", "").lower() != "off":
        raise ValueError
    for section, key in ((i, "privatekey"), (p, "publickey")):
        if len(base64.b64decode(section[key], validate=True)) != 32:
            raise ValueError
    if p.get("presharedkey") and len(base64.b64decode(p["presharedkey"], validate=True)) != 32:
        raise ValueError
    for address in i["address"].split(","):
        ipaddress.ip_interface(address.strip())
    for cidr in p["allowedips"].split(","):
        ipaddress.ip_network(cidr.strip(), strict=False)
    if not p.get("endpoint"):
        raise ValueError
    for key in ("jc", "jmin", "jmax", "s1", "s2", "s3", "s4"):
        if key in i and not 0 <= int(i[key]) <= 65535:
            raise ValueError
    if int(i.get("jmin", 0)) > int(i.get("jmax", 0)):
        raise ValueError
    for key in ("h1", "h2", "h3", "h4"):
        if key in i:
            ends = i[key].split("-")
            if len(ends) > 2 or any(not 0 <= int(x) <= 4294967295 for x in ends) or int(ends[0]) > int(ends[-1]):
                raise ValueError
    return {"format": "native AWG2 candidate", "secrets": "not displayed",
            "awg_parameters_present": [k.upper() for k in i if k.startswith(("s", "h", "j", "i"))],
            "network_verified": False,
            "next": "Проверить парсер выбранных awg-tools, handshake, DNS и HTTPS; параметры не изменены"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="Путь к native .conf с правами 600; не vpn:// ссылка")
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.path), ensure_ascii=False, indent=2))
    except Exception:
        parser.exit(2, "Конфигурация не проверена: права, формат или AWG2-параметры требуют проверки локально. Содержимое не выводится.\n")
