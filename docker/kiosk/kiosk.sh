#!/bin/sh
set -eu
/usr/lib/systemd/systemd-udevd --daemon
udevadm trigger --action=add
udevadm settle --timeout=10 || true
# root's standard runtime dir: RustDesk's service assumes /run/user/<uid> for the session it serves (remote.sh).
export XDG_RUNTIME_DIR=/run/user/0
# A restarted container keeps /run: seatd refuses to start over its old socket, and cage over stale Wayland ones.
rm -rf /run/seatd.sock "$XDG_RUNTIME_DIR"
mkdir -p -m 700 "$XDG_RUNTIME_DIR"
# A fresh profile every start: a kept one remembers the unclean stop and offers to restore the old session.
PROFILE=/tmp/kiosk-profile
rm -rf "$PROFILE"
mkdir -p "$PROFILE"
/remote.sh &
exec seatd-launch -- cage -- firefox-esr --profile "$PROFILE" --kiosk "${KIOSK_URL}"
