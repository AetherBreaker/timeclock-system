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
# Check in on terminal 1 (standby-screen spec §3.3): a display binds to whichever terminal shows when it starts, so
# switch there first, but only from 1 or the standby's 7; a console in use (2-6) is waited out, never switched away from.
while :; do
  prev=$(sed 's/^tty//' /sys/class/tty/tty0/active)
  case "$prev" in 1 | 7) break ;; esac
  sleep 1
done
perl -e 'open(T, "<", "/dev/tty0") or die "tty0: $!"; ioctl(T, 0x5606, 1) or die "VT_ACTIVATE: $!";
  ioctl(T, 0x5607, 1) or die "VT_WAITACTIVE: $!"'
# Remote access waits for cage's Wayland socket, up within a second of this.
/remote.sh &
# The number pad types digits whatever Num Lock says: cage starts every keyboard with Num Lock off, including one that
# drops off USB and comes back (main-office's did, 2026-10-29).
export XKB_DEFAULT_OPTIONS=numpad:mac
# -s: Ctrl+Alt+F<n> switches to the host's text consoles, for a shell at the clock while debugging. cage starts
# handback.sh once its display is up: it hands the screen back, waits for the app, then runs Firefox.
seatd-launch -- cage -s -- /handback.sh 1 "$prev" firefox-esr --profile "$PROFILE" --kiosk "${KIOSK_URL}" &
launch=$!
# Not exec'd: as the container's PID 1, seatd-launch would ignore docker stop's SIGTERM (it has no handler), so every
# stop waited 10 s for a kill, which also left terminal 1 marked in use. Closing cage instead lets seatd-launch stop
# seatd, which hands terminal 1 back in text mode, within a second.
trap 'pkill -TERM -x cage' TERM INT
while kill -0 "$launch" 2>/dev/null; do
  wait "$launch" || true # a trapped signal ends the wait early; wait again for the exit
done
