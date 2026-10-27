#!/bin/sh
# Remote access (RustDesk) beside the kiosk, started in the background by kiosk.sh. Anything failing here only costs
# remote access: it never stops cage or Firefox, and the kiosk's health check ignores it.
set -u
REMOTE=/remote
PKG="$REMOTE/sweetfiresupportdesk.deb"
if [ ! -f "$PKG" ]; then
  echo "remote: no $PKG; running without remote access"
  exit 0
fi
# The package's postinst only links the binary here (PID 1 isn't systemd), so installing at every start is cheap and
# picks up a replaced package after a plain restart.
if ! dpkg -i "$PKG" > /tmp/remote-install.log 2>&1; then
  echo "remote: installing $PKG failed; running without remote access (see /tmp/remote-install.log)"
  exit 0
fi
# Its settings (ID, key pair) live on the device, so a redeploy keeps the same RustDesk ID.
mkdir -p "$REMOTE/config" /root/.config
chmod 700 "$REMOTE" "$REMOTE/config"
rm -rf /root/.config/sweetfiresupportdesk
ln -s "$REMOTE/config" /root/.config/sweetfiresupportdesk

for _ in $(seq 120); do [ -S "$XDG_RUNTIME_DIR/wayland-0" ] && break; sleep 0.5; done
export WAYLAND_DISPLAY=wayland-0 XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=wlroots
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --nopidfile
pipewire > /tmp/pipewire.log 2>&1 &
sleep 1
wireplumber > /tmp/wireplumber.log 2>&1 &
/usr/libexec/xdg-desktop-portal-wlr > /tmp/xdpw.log 2>&1 &
sleep 1
/usr/libexec/xdg-desktop-portal > /tmp/xdp.log 2>&1 &
sleep 2
# The service starts the screen process itself (as the session's user, see loginctl) and provides the input devices.
while true; do
  sweetfiresupportdesk --service > /tmp/remote-service.log 2>&1
  echo "remote: service exited ($?); restarting in 10 s"
  sleep 10
done
