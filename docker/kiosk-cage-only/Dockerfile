# Backup kiosk without remote access: cage and Firefox only. To use it, point the compose kiosk's
# `dockerfile` here. The Firefox settings are shared with docker/kiosk.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    cage seatd udev firefox-esr libgl1-mesa-dri libegl-mesa0 fonts-dejavu-core ca-certificates curl procps \
  && rm -rf /var/lib/apt/lists/*
COPY docker/kiosk-cage-only/kiosk.sh /kiosk.sh
COPY docker/kiosk/handback.sh /handback.sh
COPY docker/kiosk/policies.json /usr/lib/firefox-esr/distribution/policies.json
COPY docker/kiosk/autoconfig.js /usr/lib/firefox-esr/defaults/pref/autoconfig.js
COPY docker/kiosk/kiosk.cfg /usr/lib/firefox-esr/kiosk.cfg
RUN chmod 755 /kiosk.sh /handback.sh
ENTRYPOINT ["/kiosk.sh"]
