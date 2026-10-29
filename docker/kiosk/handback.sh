#!/bin/sh
# Run by cage as its client, which cage 0.2.0 starts only once its display is up (standby-screen spec §3.3): hand the
# screen back to the terminal that showed before the kiosk checked in, if a display still holds it; wait for the app;
# then run Firefox, which loads the clock behind the other screen until the host's watcher shows it.
# Usage: handback.sh <own terminal> <previous terminal> <command...>
# Perl, not chvt: perl-base is in every Debian image, and the switch mustn't wait for the other display (no
# VT_WAITACTIVE).
set -eu
own=$1 prev=$2
shift 2
claimed() { # in graphics mode (KDGETMODE == KD_GRAPHICS)
  perl -e 'open(T, "<", "/dev/tty$ARGV[0]") or exit 1; my $m = pack("i", 0); ioctl(T, 0x4B3B, $m) or exit 1;
    exit(unpack("i", $m) == 1 ? 0 : 1)' "$1"
}
if [ "$prev" != "$own" ] && claimed "$prev"; then
  perl -e 'open(T, "<", "/dev/tty0") or die "tty0: $!"; ioctl(T, 0x5606, $ARGV[0] + 0) or die "VT_ACTIVATE: $!"' "$prev"
fi
# Wait for the app, or Firefox's first load shows an error page: after a reboot Docker starts both containers at once,
# ignoring depends_on. At most 60 s, then open anyway; kiosk.cfg reloads an error page once the app serves.
origin=$(printf '%s\n' "$KIOSK_URL" | sed -E 's|^([a-z]+://[^/?]+).*|\1|')
i=0
until curl -fso /dev/null --max-time 2 "$origin/favicon.ico"; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || break
  sleep 1
done
exec "$@"
