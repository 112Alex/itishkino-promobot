"""Validate native config parsing without touching a VPN or production secrets."""
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("vpn_entry", Path(__file__).parents[1] / "deploy/vpn/entrypoint.py")
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)

NATIVE = """[Interface]
Address = 10.8.0.2/32
DNS = 1.1.1.1, 8.8.8.8
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Jc = 4
S3 = 7
I1 = <b 0x1234>
[Peer]
PublicKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
PresharedKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.test:12345
PersistentKeepalive = 25
"""


def test_prepare_keeps_native_keys_and_obfuscation():
    c, addresses, dns, host, port = vpn.prepare(NATIVE)
    assert addresses == ["10.8.0.2/32"]
    assert dns == ["1.1.1.1", "8.8.8.8"]
    assert (host, port) == ("vpn.example.test", 12345)
    assert c["Interface"]["table"] == "off"
    assert c["Interface"]["i1"] == "<b 0x1234>"
    assert c["Peer"]["presharedkey"] == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.mark.parametrize("old,new", [
    ("0.0.0.0/0", "10.0.0.0/8"),
    ("10.8.0.2/32", "fd00::2/128"),
    ("1.1.1.1, 8.8.8.8", "dns.example.test"),
    ("vpn.example.test:12345", "[::1]:12345"),
    ("vpn.example.test:12345", "vpn.example.test:0"),
])
def test_prepare_refuses_unsupported_network(old, new):
    with pytest.raises(ValueError):
        vpn.prepare(NATIVE.replace(old, new))


def test_health_does_not_claim_ready_without_marker(monkeypatch):
    monkeypatch.setattr(vpn.Path, "exists", lambda _: False)
    assert vpn.health() == 1


def test_native_ipv6_dns_is_omitted_in_ipv4_container():
    _, _, dns, _, _ = vpn.prepare(NATIVE.replace("1.1.1.1, 8.8.8.8", "1.1.1.1, 2606:4700:4700::1111"))
    assert dns == ["1.1.1.1"]


dns_spec = importlib.util.spec_from_file_location("vpn_dns", Path(__file__).parents[1] / "scripts/prepare-vpn-dns.py")
dns_writer = importlib.util.module_from_spec(dns_spec)
dns_spec.loader.exec_module(dns_writer)


def test_resolver_has_only_native_ipv4_dns_and_no_keys(tmp_path):
    source, target = tmp_path / "awg.conf", tmp_path / "resolv.conf"
    source.write_text(NATIVE.replace("1.1.1.1, 8.8.8.8", "1.1.1.1, 2606:4700:4700::1111"))
    source.chmod(0o600)
    dns_writer.prepare(source, target)
    assert target.read_text() == "nameserver 1.1.1.1\noptions timeout:2 attempts:2\n"
    assert source.read_text().startswith("[Interface]")


def test_dns_writer_refuses_to_overwrite_private_config(tmp_path):
    source = tmp_path / "awg.conf"
    source.write_text(NATIVE)
    source.chmod(0o600)
    with pytest.raises(ValueError):
        dns_writer.prepare(source, source)
    assert source.read_text() == NATIVE
