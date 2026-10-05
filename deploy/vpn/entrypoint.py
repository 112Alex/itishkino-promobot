"""AWG2 + SOCKS in this container only. Never log configuration or subprocess output."""
import configparser
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def prepare(text):
    c = configparser.ConfigParser(interpolation=None, strict=True, inline_comment_prefixes=("#",))
    c.read_string(text)
    c["Interface"]["Table"] = "off"
    c.optionxform = str
    # ConfigParser's first read lowercases keys. Keep them: awg accepts case-insensitive keys.
    i, p = c["Interface"], c["Peer"]
    addresses = [ipaddress.ip_interface(a.strip()) for a in i["address"].split(",")]
    addresses = [str(a) for a in addresses if a.version == 4]
    networks = [ipaddress.ip_network(a.strip(), strict=False) for a in p["allowedips"].split(",")]
    if not addresses or ipaddress.ip_network("0.0.0.0/0") not in networks:
        raise ValueError("IPv4 full tunnel required")
    native_dns = [ipaddress.ip_address(a.strip()) for a in i["dns"].split(",")]
    dns = [str(a) for a in native_dns if a.version == 4]
    if not dns:
        raise ValueError("Native IPv4 DNS required")
    host, port = p["endpoint"].rsplit(":", 1)
    if not host or host.startswith("[") or not 1 <= int(port) <= 65535:
        raise ValueError("IPv4 endpoint required")
    return c, addresses, dns, host, int(port)


def health():
    if not Path("/run/ready").exists():
        return 1
    try:
        host = Path("/run/proxy-ip").read_text().strip()
        with socket.create_connection((host, 1080), timeout=2) as s:
            s.sendall(b"\x05\x01\x00")
            return 0 if s.recv(2) == b"\x05\x00" else 1
    except Exception:
        return 1


def main():
    children = []
    phase = "configuration"
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        os.umask(0o077)
        source = Path("/run/secrets/awg0.conf")
        if source.stat().st_mode & 0o077:
            raise ValueError("Protected file required")
        c, addresses, dns, endpoint_host, endpoint_port = prepare(source.read_text())
        isolated = Path("/run/isolated.conf")
        with isolated.open("w") as f:
            c.write(f)
        spec = importlib.util.spec_from_file_location("check", "/app/check-awg-config.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        checker.check(isolated)
        resolvers = [line.split()[1] for line in Path("/etc/resolv.conf").read_text().splitlines()
                     if line.strip().startswith("nameserver ")]
        if resolvers != dns:
            raise ValueError("Resolver must match native IPv4 DNS")
        endpoint_ip = socket.getaddrinfo(endpoint_host, endpoint_port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
        route = json.loads(run("ip", "-j", "route", "show", "default"))[0]
        interface, gateway = route["dev"], route["gateway"]
        link = json.loads(run("ip", "-j", "address", "show", "dev", interface))[0]
        proxy_ip = next(a["local"] for a in link["addr_info"] if a["family"] == "inet")
        mtu = int(c["Interface"].get("mtu", min(1420, int(link["mtu"]) - 80)))
        if not 576 <= mtu <= int(link["mtu"]):
            raise ValueError("MTU unsupported")
        # The endpoint retains a direct route; every other new external connection
        # must go through AWG. No fallback if the tunnel/interface disappears.
        phase = "firewall"
        run("iptables", "-P", "OUTPUT", "DROP")
        run("iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
        # Return SOCKS responses to clients without allowing established tunnel
        # streams to escape via eth0 if a direct route were later restored.
        run("iptables", "-A", "OUTPUT", "-o", interface, "-p", "tcp", "--sport", "1080", "-m", "conntrack", "--ctstate", "ESTABLISHED", "-j", "ACCEPT")
        run("iptables", "-A", "OUTPUT", "-o", "awg0", "-j", "ACCEPT")
        run("iptables", "-A", "OUTPUT", "-o", interface, "-d", endpoint_ip, "-p", "udp", "--dport", str(endpoint_port), "-j", "ACCEPT")
        run("ip", "route", "replace", endpoint_ip + "/32", "via", gateway, "dev", interface)
        phase = "tunnel"
        awg = subprocess.Popen(["amneziawg-go", "-f", "awg0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(awg)
        for _ in range(100):
            if Path("/var/run/amneziawg/awg0.sock").exists():
                break
            if awg.poll() is not None:
                raise RuntimeError("AWG exited")
            time.sleep(0.1)
        # Only native AWG fields go to setconf; no hooks or config shell execution.
        stripped = Path("/run/stripped.conf")
        c["Peer"]["endpoint"] = f"{endpoint_ip}:{endpoint_port}"
        for key in ("address", "dns", "table", "Table", "mtu"):
            c["Interface"].pop(key, None)
        # Amnezia import templates include disabled I2-I5 as empty strings.
        # The native tools reject empty assignments; omit only disabled fields.
        for key in ("i1", "i2", "i3", "i4", "i5"):
            if not c["Interface"].get(key, "").strip():
                c["Interface"].pop(key, None)
        with stripped.open("w") as f:
            c.write(f)
        phase = "tunnel parameters"
        run("awg", "setconf", "awg0", str(stripped))
        phase = "header verification"
        spec = importlib.util.spec_from_file_location("headers", "/app/fix-awg-headers.py")
        headers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(headers)
        headers.fix()
        print("AWG native H1/H2 verified", flush=True)
        stripped.unlink()
        isolated.unlink()
        phase = "tunnel addresses"
        for address in addresses:
            run("ip", "address", "add", address, "dev", "awg0")
        run("ip", "link", "set", "dev", "awg0", "mtu", str(mtu), "up")
        run("ip", "route", "replace", "default", "dev", "awg0")
        phase = "proxy"
        Path("/run/proxy-ip").write_text(proxy_ip)
        Path("/run/danted.conf").write_text(f"""logoutput: /dev/null
internal: {proxy_ip} port = 1080
external: awg0
user.privileged: root
user.notprivileged: nobody
clientmethod: none
socksmethod: none
client pass {{ from: 0.0.0.0/0 to: 0.0.0.0/0 }}
socks pass {{ from: 0.0.0.0/0 to: 0.0.0.0/0 port = 443 command: connect }}
""")
        run("danted", "-V", "-f", "/run/danted.conf")
        proxy = subprocess.Popen(["danted", "-f", "/run/danted.conf"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(proxy)
        Path("/run/ready").touch()
        print("AWG2 proxy started; verify Telegram with network-check", flush=True)
        phase = "running"
        while True:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("Child exited")
            time.sleep(1)
    except Exception:
        print(f"VPN startup/service failed at phase: {phase}; configuration not displayed", flush=True)
        return 1
    finally:
        Path("/run/ready").unlink(missing_ok=True)
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    sys.exit(health() if sys.argv[1:] == ["health"] else main())
