#!/bin/sh
set -eu
image=itishkino-promobot-vpn:awg2-1-amd64
archive=itishkino-promobot-vpn-awg2-1-linux-amd64.tar.gz
mkdir -p outputs
docker build --platform linux/amd64 -f deploy/vpn/Dockerfile -t "$image" .
docker save -o "outputs/$archive.tar" "$image"
gzip -1 -c "outputs/$archive.tar" > "outputs/$archive.tmp"
mv "outputs/$archive.tmp" "outputs/$archive"
rm "outputs/$archive.tar"
(cd outputs && sha256sum "$archive" > VPN-SHA256SUMS)
printf 'Ready: outputs/%s\n' "$archive"
