#!/usr/bin/env python3
"""Write container-only resolver settings from native AWG DNS; never print values."""
import configparser
import ipaddress
import os
from pathlib import Path
import sys


def prepare(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError("Source config must not be overwritten")
    if source.stat().st_mode & 0o077:
        raise ValueError("Protected config required")
    c = configparser.ConfigParser(interpolation=None, strict=True, inline_comment_prefixes=("#",))
    c.read(source)
    dns = [ipaddress.ip_address(a.strip()) for a in c["Interface"]["DNS"].split(",")]
    dns = [str(a) for a in dns if a.version == 4]
    if not dns:
        raise ValueError("Native IPv4 DNS required")
    text = "".join("nameserver " + a + "\n" for a in dns) + "options timeout:2 attempts:2\n"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    target.chmod(0o644)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("Expected source and target paths")
        prepare(*sys.argv[1:])
        print("Container DNS prepared; values not displayed")
    except Exception:
        print("DNS preparation failed; verify native config and file permissions", file=sys.stderr)
        sys.exit(2)
