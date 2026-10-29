FROM debian:trixie-slim
# cage + Firefox are the kiosk. The rest is remote access (remote.sh): the screen-sharing stack RustDesk's Wayland path
# uses (session bus, PipeWire, the wlroots portal) and the dependencies of the client package, which isn't in the image:
# it stays out of git (its password is baked in) and is installed at start from the device's persisted folder.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cage seatd udev firefox-esr libgl1-mesa-dri libegl-mesa0 fonts-dejavu-core ca-certificates \
    procps dbus pipewire wireplumber xdg-desktop-portal xdg-desktop-portal-wlr \
    libgtk-3-0t64 libxcb-randr0 libxdo3 libxfixes3 libxcb-shape0 libxcb-xfixes0 libasound2t64 libsystemd0 curl \
    libva2 libva-drm2 libva-x11-2 libgstreamer-plugins-base1.0-0 gstreamer1.0-pipewire \
  && rm -rf /var/lib/apt/lists/*
COPY docker/kiosk/kiosk.sh /kiosk.sh
COPY docker/kiosk/remote.sh /remote.sh
COPY docker/kiosk/handback.sh /handback.sh
# Over systemd's own (pulled in above), which finds no session without logind; see the file.
COPY docker/kiosk/loginctl /usr/bin/loginctl
COPY docker/kiosk/xdpw.conf /root/.config/xdg-desktop-portal-wlr/config
COPY docker/kiosk/policies.json /usr/lib/firefox-esr/distribution/policies.json
COPY docker/kiosk/autoconfig.js /usr/lib/firefox-esr/defaults/pref/autoconfig.js
COPY docker/kiosk/kiosk.cfg /usr/lib/firefox-esr/kiosk.cfg
RUN chmod 755 /kiosk.sh /remote.sh /handback.sh /usr/bin/loginctl
ENTRYPOINT ["/kiosk.sh"]
