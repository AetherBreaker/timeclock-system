#!/bin/sh
set -eu
/usr/lib/systemd/systemd-udevd --daemon
udevadm trigger --action=add
udevadm settle --timeout=10 || true
export XDG_RUNTIME_DIR=/run/kiosk
mkdir -p -m 700 "$XDG_RUNTIME_DIR"
exec seatd-launch -- cage -- firefox-esr --kiosk "${KIOSK_URL}"
