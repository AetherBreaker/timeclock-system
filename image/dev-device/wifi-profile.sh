#!/bin/sh
# Write the wifi network a clock image joins, from WIFI_SSID and WIFI_PASSWORD (kept out of git: the repository is
# public), as a NetworkManager profile in the folder $1. It names no interface or MAC, so any wifi device joins it: a USB
# dongle or one built in. Nothing is written when WIFI_SSID is unset. Both images' build.sh run it.
set -eu
[ -n "${WIFI_SSID:-}" ] || { echo "wifi-profile: WIFI_SSID unset, so no wifi network is baked in"; exit 0; }
: "${WIFI_PASSWORD:?WIFI_PASSWORD is required with WIFI_SSID}"
# A keyfile reads a backslash as an escape and ends a value at a newline.
case "$WIFI_SSID$WIFI_PASSWORD" in *\\* | *'
'*)
  echo "wifi-profile: a backslash or newline in WIFI_SSID or WIFI_PASSWORD isn't supported" >&2
  exit 1
  ;;
esac
# One uuid per image is enough: it only has to be unique on its own machine. Read here, not in the heredoc, so set -e
# stops on a failed read.
uuid=$(cat /proc/sys/kernel/random/uuid)
cat > "$1/wifi.nmconnection" <<EOF
[connection]
id=$WIFI_SSID
uuid=$uuid
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=$WIFI_SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PASSWORD

[ipv4]
method=auto

[ipv6]
addr-gen-mode=default
method=auto
EOF
echo "wifi-profile: baked in $WIFI_SSID"
