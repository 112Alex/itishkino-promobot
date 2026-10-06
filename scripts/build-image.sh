#!/bin/sh
# Run from the repository root. Exports a Linux amd64 image for Intel Mac Mini.
set -eu
image_name=itishkino-promobot:0.3.0-amd64
archive_name=itishkino-promobot-0.3.0-linux-amd64.tar.gz
mkdir -p outputs
docker build --platform linux/amd64 -t "$image_name" .
# A failed docker save must not leave an apparently valid gzip archive.
docker save -o "outputs/$archive_name.tar" "$image_name"
gzip -1 -c "outputs/$archive_name.tar" > "outputs/$archive_name.tmp"
mv "outputs/$archive_name.tmp" "outputs/$archive_name"
rm "outputs/$archive_name.tar"
(cd outputs && sha256sum "$archive_name" > SHA256SUMS)
docker image inspect "$image_name" --format '{{.Id}} {{.Os}}/{{.Architecture}} {{.Size}} bytes'
printf 'Ready: outputs/%s\n' "$archive_name"
