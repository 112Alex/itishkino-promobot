#!/usr/bin/env python3
"""Run inside VPN container via python stdin. No keys, tokens, endpoint or URLs printed."""
import json
from pathlib import Path
import socket
import ssl
import subprocess
import time


def awg_status():
    def query(field):
        result = subprocess.run(["awg", "show", "awg0", field], capture_output=True, check=True)
        return [line.split()[1:] for line in result.stdout.decode().splitlines()]
    try:
        stamps = [int(row[0]) for row in query("latest-handshakes")]
        transfers = query("transfer")
        newest = max(stamps, default=0)
        return {"peers": len(stamps), "handshake_age_seconds": int(time.time() - newest) if newest else None,
                "received_bytes": sum(int(row[0]) for row in transfers),
                "sent_bytes": sum(int(row[1]) for row in transfers)}
    except Exception:
        return {"status": "awg_status_unavailable"}


def read_exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise ConnectionError("SOCKS closed")
        data += part
    return data


report = {"before": awg_status()}
stage = "proxy_connection"
try:
    proxy = Path("/run/proxy-ip").read_text().strip()
    with socket.create_connection((proxy, 1080), timeout=8) as sock:
        stage = "socks_greeting"
        sock.sendall(b"\x05\x01\x00")
        if read_exact(sock, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS greeting rejected")
        stage = "socks_connect"
        host = b"api.telegram.org"
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
        header = read_exact(sock, 4)
        report["socks_reply_code"] = header[1]
        if header[1] != 0:
            raise ConnectionError("SOCKS connect rejected")
        lengths = {1: 4, 4: 16}
        length = read_exact(sock, 1)[0] if header[3] == 3 else lengths[header[3]]
        read_exact(sock, length + 2)
        stage = "tls"
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host.decode()) as tls:
            stage = "https"
            tls.sendall(b"GET / HTTP/1.1\r\nHost: api.telegram.org\r\nConnection: close\r\n\r\n")
            status = tls.recv(128).split(b"\r\n", 1)[0].split()
            report["telegram_https_status"] = int(status[1])
            report["connection"] = "ok"
except Exception as exc:
    report.update(connection="failed", failed_stage=stage, error_type=type(exc).__name__)
report["after"] = awg_status()
print(json.dumps(report, ensure_ascii=False))
