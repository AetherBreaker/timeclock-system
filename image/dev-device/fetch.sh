#!/bin/sh
# Download the current Debian 13 amd64 netinst ISO into /work and verify it against Debian's
# signed SHA512SUMS (checked with the CD signing key from the debian-keyring package).
set -eu
BASE=https://cdimage.debian.org/debian-cd/current/amd64/iso-cd
cd /work
wget -q -O SHA512SUMS "$BASE/SHA512SUMS"
wget -q -O SHA512SUMS.sign "$BASE/SHA512SUMS.sign"
gpgv --keyring /usr/share/keyrings/debian-role-keys.gpg SHA512SUMS.sign SHA512SUMS
ISO=$(grep -o 'debian-13[^ ]*-amd64-netinst\.iso' SHA512SUMS | head -n1)
[ -n "$ISO" ] || { echo "no Debian 13 netinst ISO listed" >&2; exit 1; }
[ -f "$ISO" ] || wget -q -O "$ISO" "$BASE/$ISO"
grep " $ISO\$" SHA512SUMS | sha512sum -c -
echo "$ISO" > iso-name
