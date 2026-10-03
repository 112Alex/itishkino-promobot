#!/bin/sh
# Read-only checks. No network changes, no secrets.
set -eu
uname -m
uname -r
cat /etc/os-release
free -h
df -h / /var
if [ -c /dev/net/tun ]; then echo 'TUN exists'; else echo 'TUN missing'; fi
python3 --version
systemctl --version | head -1
systemctl is-active ssh || true
systemctl is-active NetworkManager || true
systemctl is-active systemd-networkd || true
command -v awg || true
command -v amneziawg-go || true
