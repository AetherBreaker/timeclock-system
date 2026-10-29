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
# Wait for the app, or Firefox's first load shows an error page: after a reboot Docker starts both containers at once,
# ignoring depends_on. At most 60 s, then open anyway; kiosk.cfg reloads an error page once the app serves. remote.sh
# waits for cage's screen, so it starts after this.
ORIGIN=$(printf '%s\n' "$KIOSK_URL" | sed -E 's|^([a-z]+://[^/?]+).*|\1|')
i=0
until curl -fso /dev/null --max-time 2 "$ORIGIN/favicon.ico"; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || break
  sleep 1
done
/remote.sh &
# The number pad types digits whatever Num Lock says: cage starts every keyboard with Num Lock off, including one that
# drops off USB and comes back (main-office's did, 2026-10-29).
export XKB_DEFAULT_OPTIONS=numpad:mac
# -s: Ctrl+Alt+F<n> switches to the host's text consoles, for a shell at the clock while debugging.
exec seatd-launch -- cage -s -- firefox-esr --profile "$PROFILE" --kiosk "${KIOSK_URL}"
